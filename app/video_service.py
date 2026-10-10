#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频编排层：受理 / 翻译 / 提交 / 轮询 / **Seedance 响应构造**。

## 跟图片服务的关系

`VideoService` **继承** `Service` —— 协调器、节奏闸门、凭据池、并行 ingest、
`pre_create` 重试这套东西全部复用，只替换掉"形状"不同的三处：

| 覆写点 | 为什么不同 |
|---|---|
| `build_video_plan` | 输入是 Seedance 的 `content[]`（多模态数组），不是 `{prompt, image}` |
| `submit` | 上游端点不同（`/v2/api/multimodal/generate/video`），`fileList[]` 要带 `frameType` |
| `_poll_group` | 视频**不走** `my/batch`：那里的 video feed 产物路径未取证 ⇒ 只用 v4 点名直查 |

## "内部自行判断"落在这里

调用方按 Seedance 说话：`content[]` 里有几个 `image_url`、它们的 `role` 是
`first_frame` 还是 `last_frame` —— 由**输入**决定形态，本服务据此落到三个上游
modelID 槽位之一（`t2v` / `first` / `pair`）。这不是"猜"，是
`video_models.VIDEO_FAMILIES` 里一张显式登记的表。

## 每一项被改动的参数都进 `degradations`

视频单价差最高 6 倍（`23218` 15 积分 vs `veo3.1` 180 积分），
"悄咪咪替人换个档位"是最贵的一种 bug。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .errors import AdapterError, InvalidParameterError, TaskNotFoundError
from .observability import OBS
from .service import Service
from .store import ST_FAILURE, ST_SUCCEEDED, VideoTaskStore, new_video_task_id
from .upstream.hailuo.client import Feed, HailuoClient
from .upstream.hailuo.video_models import (
    MODE_FIRST_FRAME,
    MODE_FIRST_LAST,
    MODE_T2V,
    SNAPSHOT_DATE,
    UpstreamVideoModel,
    get_video_model,
    resolve_route,
    snap_duration,
    snap_ratio,
    snap_resolution,
)

# ---------------------------------------------------------------------------
# Seedance `content[]` 的角色 → 上游 `fileList[].frameType`
# ---------------------------------------------------------------------------

ROLE_FIRST_FRAME = "first_frame"
ROLE_LAST_FRAME = "last_frame"

#: 上游 `frameType`（0=首帧，1=尾帧）。**抓包给定的常量**，不是约定。
FRAME_TYPE_FIRST = 0
FRAME_TYPE_LAST = 1

#: Seedance 分辨率写法 → 上游裸数字。上游从不收 `720p` 这种带单位的字符串。
RESOLUTION_ALIASES: dict[str, str] = {
    "480p": "480", "720p": "720", "768p": "768", "1080p": "1080", "1440p": "1440",
    "2k": "1440", "4k": "3840", "2160p": "3840",
}

#: Seedance 的 `adaptive` ⇒ 上游 `Auto`（跟随输入素材的比例）。
RATIO_ALIASES: dict[str, str] = {"adaptive": "Auto"}


# ---------------------------------------------------------------------------
# 认得但上游没有 ⇒ 进 degradations（**不报错**）
# ---------------------------------------------------------------------------

VIDEO_KNOWN_UNSUPPORTED: dict[str, str] = {
    "watermark": "上游视频链路没有水印开关（产物只有一种形态）",
    "seed": "上游视频端点未声明 seed 字段（传了不会有效果）",
    "camera_fixed": "2.x 有 enableCamera 概念，但对应的运镜参数形态未取证",
    "generate_audio": "部分族（Veo 3.1 / Hailuo 3.0）自身会出音轨，但音频开关未取证",
    "return_last_frame": "上游不回尾帧图；需要尾帧请自行从产物视频抽帧",
    "callback_url": "本服务没有 webhook 通道（请用 GET 轮询）",
    "service_tier": "本服务只有在线推理一档",
    "execution_expires_after": "任务过期由本服务看门狗（VIDEO_TASK_TIMEOUT）决定",
    "priority": "上游未声明优先级字段",
}
#: ⚠️ `frames` **不在**上面那张表里 —— 它是**真被处理**的
#: （按 24fps 换算成 duration 转发），只是换算这件事会进 `degradations`。


@dataclass
class VideoPlan:
    """一次视频生成在**上游侧**的完整形态。"""

    capability: str
    upstream_model: str
    desc: str
    first_frame: str = ""
    last_frame: str = ""
    mode: str = MODE_T2V
    duration: int = 6
    resolution: str | None = None
    aspect_ratio: str | None = None
    use_origin_prompt: bool = True
    degradations: list[str] = field(default_factory=list)
    forecast_credits: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "upstream_model": self.upstream_model,
            "desc": self.desc,
            "first_frame": self.first_frame,
            "last_frame": self.last_frame,
            "mode": self.mode,
            "duration": self.duration,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "use_origin_prompt": self.use_origin_prompt,
            "degradations": list(self.degradations),
            "forecast_credits": self.forecast_credits,
        }

    def frame_jobs(self) -> list[tuple[str, int]]:
        """`[(url, frameType), …]` —— **顺序固定**：先首帧后尾帧。"""
        jobs: list[tuple[str, int]] = []
        if self.first_frame:
            jobs.append((self.first_frame, FRAME_TYPE_FIRST))
        if self.last_frame:
            jobs.append((self.last_frame, FRAME_TYPE_LAST))
        return jobs


# ---------------------------------------------------------------------------
# `content[]` 解析 —— **纯函数**
# ---------------------------------------------------------------------------


def parse_content(raw: Any) -> tuple[str, dict[str, str], list[str]]:
    """Seedance 的 `content[]` → `(prompt, {role: url}, 说明)`。

    规则（每条都对应 Seedance 契约里的一种写法）：

    · `type=text` ⇒ 提示词。多条文本按出现顺序用换行拼起来（**留痕**：
      Seedance 原生只允许一条文本，拼接是本服务的口径）；
    · `type=image_url` ⇒ 框架图。`role` 缺省时按**首帧**理解 —— 这正是 Seedance
      图生视频的默认语义（`content` 里那张图就是起始画面）；
    · `role` 只认 `first_frame` / `last_frame`；其它值（含 `reference_image` 这类
      全能参考写法）**明确报错**而不是被当成首帧吞掉 —— 多参考图的语义在上游未取证，
      静默改成首帧等于改变了"用户想要什么"。
    """
    notes: list[str] = []
    if not isinstance(raw, list) or not raw:
        raise InvalidParameterError(
            'content 必须是**非空数组**，且至少含一个 {"type":"text", …} 项。',
            param="content")

    texts: list[str] = []
    frames: dict[str, str] = {}
    image_count = 0
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InvalidParameterError(
                f"content[{idx}] 必须是对象，实得 {type(item).__name__}。",
                param="content")
        kind = str(item.get("type") or "").strip()

        if kind == "text":
            text = str(item.get("text") or "")
            if text:
                texts.append(text)
            continue

        if kind == "image_url":
            image_count += 1
            box = item.get("image_url")
            url = ""
            if isinstance(box, dict):
                url = str(box.get("url") or "")
            elif isinstance(box, str):   #: 容错：有人直接把 URL 铺在 image_url 上
                url = box
            if not url:
                raise InvalidParameterError(
                    f"content[{idx}] 是 image_url 但没有 `image_url.url`。",
                    param="content")
            role = str(item.get("role") or "").strip() or ROLE_FIRST_FRAME
            if role not in (ROLE_FIRST_FRAME, ROLE_LAST_FRAME):
                raise InvalidParameterError(
                    f"content[{idx}].role={role!r} 不被本服务支持。"
                    f"只认 {ROLE_FIRST_FRAME!r} / {ROLE_LAST_FRAME!r}。"
                    f"⚠️ 多参考图（全能参考）的语义在上游未取证 ⇒ 不静默当成首帧处理。",
                    param="content")
            if role in frames:
                raise InvalidParameterError(
                    f"content[] 里出现了两个 {role!r} —— 每个角色最多一张。",
                    param="content")
            frames[role] = url
            continue

        raise InvalidParameterError(
            f"content[{idx}].type={kind or '（空）'!r} 不支持。"
            f"本服务只处理 'text' 与 'image_url'。", param="content")

    if len(texts) > 1:
        notes.append(f"content[] 里有 {len(texts)} 条 text ⇒ 按出现顺序用换行拼接"
                     f"（Seedance 原生只允许一条；这是本服务的合并口径）。")
    if image_count and not texts:
        notes.append("本次没有提供任何 text ⇒ prompt 为空；上游多数视频模型声明了 "
                     "mustHavePrompt，可能被拒。")

    return "\n".join(texts), frames, notes


def detect_mode(frames: dict[str, str]) -> str:
    """框架图 → 输入形态。**"内部自行判断"的全部内容就这几行。**"""
    if ROLE_LAST_FRAME in frames:
        return MODE_FIRST_LAST
    if ROLE_FIRST_FRAME in frames:
        return MODE_FIRST_FRAME
    return MODE_T2V


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class VideoService(Service):
    """视频编排层。与图片服务同一条传送带，只换 shape。"""

    #: 产物计数单位（日志/观测用）
    produced_unit: str = "条视频"

    def __init__(self, settings: Any, **kw: Any) -> None:
        #: 视频走**另一张表** —— 在 `super().__init__` 之前就建好并传进去，
        #: 免得先建一个图片 store 再丢掉（那会白建一次连接池）。
        kw.setdefault("store", VideoTaskStore(
            settings.db_target, pool_size=settings.task_db_pool_size,
            max_overflow=settings.task_db_max_overflow,
            pool_recycle=settings.task_db_pool_recycle,
            pre_ping=settings.task_db_pool_pre_ping,
            connect_timeout=settings.task_db_connect_timeout))
        super().__init__(settings, **kw)
        self._video_caps_degradations: list[str] = []
        self.refresh_video_capabilities()

    # ------------------------------------------------------------------ 能力表

    def refresh_video_capabilities(self) -> list[str]:
        """运行期实读**视频**能力表（零计费）。读不到 ⇒ 退回冻结快照 + 留痕。"""
        from .upstream.hailuo import video_capabilities as vcap  # noqa: PLC0415
        from .upstream.hailuo import video_models as vmodels  # noqa: PLC0415

        try:
            import httpx  # noqa: PLC0415

            notes: list[str] = []
            meta: dict[str, Any] = {}
            info: dict[str, Any] = {}
            with httpx.Client(base_url=self.settings.hailuo_base_url, timeout=12.0,
                              transport=self._http_transport) as c:
                for label, path, parser, sink in (
                    ("create_video_models", vcap.COMMON_CONFIG_PATH,
                     vcap.parse_video_common_config, meta),
                    ("videoModels", vcap.MODEL_INFO_PATH,
                     vcap.parse_video_model_info, info),
                ):
                    try:
                        resp = c.get(path, timeout=12.0)
                        resp.raise_for_status()
                        sink.update(parser(resp.json()))
                    except Exception as e:  # noqa: BLE001
                        notes.append(
                            f"上游视频能力表 {label} 读取失败（{type(e).__name__}）"
                            f"⇒ 该部分退回冻结快照。")
                        logger.warning(f"视频能力表 {label} 读取失败：{e}")
            if meta or info:
                found = vcap.merge_video(meta, info)
                vmodels.install_runtime_video_models(found)
                OBS.event("video_capabilities.loaded",
                          coverage=vcap.coverage_video(found))
            self._video_caps_degradations = notes
            return notes
        except Exception as e:  # noqa: BLE001
            note = (f"上游视频能力表整体读取失败（{type(e).__name__}: {e}）⇒ "
                    f"退回 {SNAPSHOT_DATE} 的冻结快照。")
            logger.warning(note)
            self._video_caps_degradations = [note]
            return [note]

    @property
    def video_capability_degradations(self) -> list[str]:
        """视频能力表的降级说明（读不到时退回冻结快照的原因）。**只读副本。**"""
        return list(self._video_caps_degradations)

    # ------------------------------------------------------------------ 受理

    def create_video(self, body: dict[str, Any], *, credential: str) -> dict[str, Any]:
        """受理一次视频生成。**只落库，零上游往返。**"""
        request = dict(body or {})
        plan, capability, upstream_model = self.build_video_plan(request)
        rec = self.store.create_task(
            task_id=new_video_task_id(),
            credential_id=credential,
            capability=capability,
            model=str(request.get("model") or ""),
            upstream_model=upstream_model,
            request_json=json.dumps(request, ensure_ascii=False),
            plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
            degradations_json=json.dumps(plan.degradations, ensure_ascii=False),
        )
        OBS.event("video_task.accepted", task_id=rec["task_id"],
                  capability=capability, upstream_model=upstream_model,
                  mode=plan.mode, duration=plan.duration)
        logger.bind(task_id=rec["task_id"]).info(
            f"已受理视频：{capability} model={upstream_model} "
            f"mode={plan.mode} duration={plan.duration}s")
        #: 🔴 降级说明只进日志与任务库，不出现在 HTTP 响应（与图片链路同一口径）。
        for note in plan.degradations:
            logger.bind(task_id=rec["task_id"]).info(f"降级留痕：{note}")
        return rec

    # ------------------------------------------------------------------ 翻译层

    def build_video_plan(self, request: dict[str, Any]) -> tuple[VideoPlan, str, str]:
        """Seedance 请求 → `VideoPlan`。**纯函数**（不碰网络、不碰库）。"""
        degradations: list[str] = []

        #: 🔴 **只列"真被处理"的字段**。曾经把 `watermark` 这些也塞进 `known`，
        #: 结果 `VIDEO_KNOWN_UNSUPPORTED` 那条分支永远走不到 —— 参数被静默丢掉、
        #: 一个降级痕都没有。这是"写了分支但分支不可达"的典型。
        known = {"model", "content", "duration", "frames", "resolution", "ratio"}
        for key in request:
            if key in known:
                continue
            if key in VIDEO_KNOWN_UNSUPPORTED:
                degradations.append(
                    f"参数 {key}={request[key]!r} 本服务**不转发**"
                    f"（{VIDEO_KNOWN_UNSUPPORTED[key]}），已忽略；"
                    f"不要按它的语义预期结果。")
                continue
            raise InvalidParameterError(
                f"未知参数 {key!r}。本服务接受的字段：{', '.join(sorted(known))}。",
                param=key)

        # ---- content[] ⇒ prompt + 框架图 + 形态
        prompt, frames, content_notes = parse_content(request.get("content"))
        degradations += content_notes
        mode = detect_mode(frames)

        # ---- 模型路由（**唯一出口**）
        upstream_model, capability, route_notes = resolve_route(request.get("model"), mode)
        degradations += route_notes
        model_meta = _require_model(upstream_model)

        # ---- prompt 长度
        if model_meta.max_prompt and len(prompt) > model_meta.max_prompt:
            raise InvalidParameterError(
                f"prompt 长度 {len(prompt)} 超过模型 {upstream_model} 声明的上限 "
                f"{model_meta.max_prompt}（上游 maxPromptLength）。", param="content")

        # ---- endFrameRequiredStartFrame：给了尾帧就必须同时给首帧
        if mode == MODE_FIRST_LAST and not frames.get(ROLE_FIRST_FRAME) \
                and model_meta.end_frame_requires_start:
            raise InvalidParameterError(
                f"模型 {upstream_model} 声明了 `endFrameRequiredStartFrame`"
                f"（尾帧必须配首帧），但本次只给了 last_frame。"
                f"请补一个 role=first_frame，或改用支持单独尾帧的能力名。",
                param="content")

        # ---- duration / frames（帧率恒定 24fps ⇒ 帧数换算成秒）
        want_duration = request.get("duration")
        raw_frames = request.get("frames")
        if want_duration is None and raw_frames is not None:
            if isinstance(raw_frames, bool) or not isinstance(raw_frames, (int, float)):
                raise InvalidParameterError(
                    f"frames 必须是数字，实得 {raw_frames!r}。", param="frames")
            want_duration = int(round(float(raw_frames) / 24.0))
            degradations.append(
                f"frames={raw_frames} ⇒ duration={want_duration}s"
                f"（上游按秒计费，Seedance 帧率恒定 24fps；这是本服务的换算口径）。")
        if want_duration is not None:
            if isinstance(want_duration, bool) or not isinstance(want_duration, (int, float)):
                raise InvalidParameterError(
                    f"duration 必须是整数秒，实得 {want_duration!r}。", param="duration")
            want_duration = int(want_duration)
            if want_duration == -1:
                want_duration = None
                degradations.append("duration=-1（Seedance 的「模型自选时长」）⇒ "
                                    "本服务改用上游默认档。")
            elif want_duration < 1:
                raise InvalidParameterError(
                    f"duration 必须 >= 1（或 -1 表示让模型自选），实得 {want_duration}。",
                    param="duration")
        duration, dur_notes = snap_duration(model_meta, want_duration)
        degradations += dur_notes

        # ---- resolution
        raw_res = request.get("resolution")
        if raw_res is not None:
            raw_res = str(raw_res).strip().lower()
            mapped = RESOLUTION_ALIASES.get(raw_res, raw_res)
            if mapped != raw_res:
                degradations.append(
                    f"resolution={raw_res!r} ⇒ {mapped}"
                    f"（上游只认裸数字档位，不带 'p'/'k' 后缀）。")
            raw_res = mapped.rstrip("pP")
        resolution, res_notes = snap_resolution(
            model_meta, raw_res,
            #: 🔴 带框架图时只许落在**上游明确声明** supportFrame 的档位
            require_frames=(mode != MODE_T2V))
        degradations += res_notes

        # ---- ratio
        raw_ratio = request.get("ratio")
        if raw_ratio is not None:
            raw_ratio = str(raw_ratio).strip()
            mapped = RATIO_ALIASES.get(raw_ratio.lower(), raw_ratio)
            if mapped != raw_ratio:
                degradations.append(f"ratio={raw_ratio!r} ⇒ {mapped}"
                                    f"（Seedance 的 adaptive 对应上游 Auto）。")
            raw_ratio = mapped
        aspect_ratio, ratio_notes = snap_ratio(model_meta, raw_ratio)
        degradations += ratio_notes

        #: `useOriginPrompt` 恒 True = **不让上游改写 prompt**。
        #: 反过来（让上游替你润色）是没被要求的加工，且会改变计费语义。
        forecast = model_meta.cost_for(resolution or "", duration) \
            if resolution else model_meta.default_cost

        plan = VideoPlan(
            capability=capability, upstream_model=upstream_model, desc=prompt,
            first_frame=frames.get(ROLE_FIRST_FRAME, ""),
            last_frame=frames.get(ROLE_LAST_FRAME, ""),
            mode=mode, duration=duration, resolution=resolution,
            aspect_ratio=aspect_ratio, use_origin_prompt=True,
            degradations=degradations, forecast_credits=forecast,
        )
        return plan, capability, upstream_model

    # ------------------------------------------------------------------ 提交

    def submit(self, task_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """把一条视频任务建到上游。🔴 **计费动作**（`dry_run=True` 时零消耗）。"""
        full = self.store.get_full(task_id)
        if not full:
            raise TaskNotFoundError(f"任务 {task_id} 不存在。")
        plan = full["plan"] or {}
        self.store.update_task(task_id, attempts=(full.get("attempts") or 0) + 1)

        #: 🔴 用**本任务所属账号**的闸门（多账号隔离，见 `Service.gate_for`）
        gate = self.gate_for(full.get("credential_id"))
        decision = gate.check()
        if not decision.allowed:
            logger.bind(task_id=task_id).debug(f"闸门拦截：{decision.reason}")
            return {"submitted": False, "reason": decision.reason}

        client, uploader = self.clients_for(full.get("credential_id"))

        # ① 框架图 → fileList（**最多两张**：首帧 frameType=0 / 尾帧 frameType=1）
        jobs = [(u, ft) for u, ft in
                ((plan.get("first_frame"), FRAME_TYPE_FIRST),
                 (plan.get("last_frame"), FRAME_TYPE_LAST)) if u]
        file_list: list[dict[str, Any]] = []
        try:
            if jobs:
                workers = max(1, min(self.settings.ingest_parallelism, len(jobs)))
                if workers == 1:
                    results = [self._ingest_one(u, uploader=uploader, dry_run=dry_run)
                               for u, _ in jobs]
                else:
                    with ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix="vframe") as pool:
                        results = list(pool.map(
                            lambda job: self._ingest_one(job[0], uploader=uploader,
                                                         dry_run=dry_run), jobs))
                for (_, frame_type), (uploaded, norm_notes, traces) in zip(jobs, results):
                    for note in norm_notes:
                        self.store.add_degradation(task_id, note)
                        logger.bind(task_id=task_id).info(f"归一化留痕：{note}")
                    for t in traces:
                        OBS.upstream(f"upload.{t.get('stage')}", task_id=task_id, **t)
                    file_list.append(uploaded.to_video_frame_entry(frame_type))
        except AdapterError as e:
            #: 与图片链路同一判据：只有**瞬时类**（5xx/连接类）失败才值得重试
            if e.status_code >= 500:
                e.pre_create = True
            raise

        # ② 建视频任务
        batch_id, trace = client.create_video(
            model_id=plan["upstream_model"],
            desc=plan.get("desc") or "",
            file_list=file_list,
            duration=int(plan.get("duration") or 6),
            resolution=plan.get("resolution"),
            aspect_ratio=plan.get("aspect_ratio"),
            use_origin_prompt=bool(plan.get("use_origin_prompt", True)),
            dry_run=dry_run,
        )
        OBS.upstream("create_video", task_id=task_id, **trace)

        if dry_run:
            return {"submitted": False, "dry_run": True, "trace": trace,
                    "file_list": file_list}

        gate.note_submit()
        self.store.update_task(
            task_id, status="in_progress", upstream_batch_id=batch_id,
            #: 🔴 **记录 id 也要存**：它既是 `data.id`，也是 v4 点名直查唯一认的句柄
            #: （实测：拿 `batchID` 查 v4 恒回空）。丢了它 ⇒ 主路径一失败就没了兜底。
            upstream_feed_id=str(trace.get("upstream_record_id") or ""),
            submitted_at=time.time())
        return {"submitted": True, "upstream_batch_id": batch_id, "trace": trace}

    # ------------------------------------------------------------------ 轮询

    def _poll_group(self, tasks: list[dict[str, Any]], *, client: HailuoClient,
                    dry_run: bool = False) -> dict[str, Any]:
        """**同一凭据**的一组视频任务：1 次 `my/batch` 主查询 + 至多 1 次兜底。

        🔴 **形态是实测逼出来的**（2026-09-23，真实视频批次）：

        · **`my/batch`（`feedTypes=[0]`）是主路径** —— 一次请求覆盖全部在途任务，
          产物在 `metaInfo.videoMetaInfo.mediaInfo`（图片侧同位置换容器名）；
        · **`v4` 的 `batchType=0` 对视频查不到**（4 种 `type` 组合全回 `batchVideos: []`，
          实测），所以它**不能**当主路径 —— 我最初正是照参考实现把它当唯一路径，
          结果任务在上游 2 分钟就出片了，我们轮询了 15 分钟什么都没看到、
          最后被看门狗判 `expired`：**钱花了、片出来了、没拿到**；
        · v4 兜底要用 **`data.id`（记录 id）** 当 `batchID`（实测命中；
          用 `data.task.batchID` 查 v4 恒空）。
        """
        wanted = {t["task_id"]: t for t in tasks}
        batch_ids = {t.get("upstream_batch_id"): t["task_id"]
                     for t in tasks if t.get("upstream_batch_id")}
        if not batch_ids:
            return {"polled": 0, "updated": 0}

        limit = max(30, min(100, len(wanted) * 4))
        batches, trace = client.fetch_batches(
            limit=limit, video=True, dry_run=dry_run)
        OBS.upstream("fetch_video_batches", **trace)
        if dry_run:
            return {"polled": 0, "updated": 0, "dry_run": True, "trace": trace}

        updated = 0
        seen: set[str] = set()
        for batch_id, feeds in batches:
            task_id = batch_ids.get(batch_id)
            if not task_id:
                continue
            seen.add(task_id)
            if self._apply_feeds(task_id, feeds):
                updated += 1

        # ---- v4 兜底（按**记录 id**，不是 batch id）----
        missing = [tid for tid in batch_ids.values() if tid not in seen]
        fallback: dict[str, Any] = {}
        if missing:
            record_by_task = {t["task_id"]: (t.get("upstream_feed_id") or "")
                              for t in tasks}
            to_query = [record_by_task[tid] for tid in missing if record_by_task.get(tid)]
            fallback["requested"] = len(to_query)
            if to_query:
                try:
                    v4_batches, v4_trace = client.fetch_by_ids(to_query)
                    OBS.upstream("fetch_video_by_ids", **v4_trace)
                    by_record = {rid: tid for tid, rid in record_by_task.items() if rid}
                    for batch_id, feeds in v4_batches:
                        task_id = by_record.get(batch_id)
                        if not task_id or task_id in seen:
                            continue
                        seen.add(task_id)
                        if self._apply_feeds(task_id, feeds):
                            updated += 1
                except AdapterError as e:
                    fallback["error"] = f"{type(e).__name__}: {e}"
                    logger.debug(f"视频 v4 兜底失败（保持非终态）：{e}")

        #: 没被返回的任务只是"还没进列表/还在跑" ⇒ **保持非终态**（不是失败）
        still_missing = [tid for tid in batch_ids.values() if tid not in seen]
        if still_missing:
            logger.debug(f"{len(still_missing)} 个视频任务本轮未返回，保持非终态等待")
        group_out: dict[str, Any] = {"polled": len(tasks), "updated": updated,
                                     "missing": len(still_missing), "trace": trace}
        if fallback:
            group_out["fallback"] = fallback
        return group_out

    # ------------------------------------------------------------------ 产物

    def _success_payload(self, feeds: list[Feed]) -> dict[str, Any]:
        """成功时的 `result` —— 视频按 **Seedance 的 asset 语义**存原样。

        `Feed.url` 已经是"去水印优先"（与图片链路同一口径）；水印版与时长
        也一并留在 `result` 里（trace/产物排查要用）。
        """
        primary = feeds[0]
        vmeta = ((primary.raw or {}).get("metaInfo") or {}).get("videoMetaInfo") or {}
        return {
            "video_url": primary.url,
            "url_no_watermark": primary.url_no_watermark,
            "assets": [{"url": f.url, "url_no_watermark": f.url_no_watermark,
                        "id": f.feed_id, "file_id": f.file_id,
                        "width": f.width, "height": f.height,
                        "file_name": f.file_name, "model_id": f.model_id,
                        "create_time": f.create_time} for f in feeds],
            "created_ms": primary.create_time,
            "width": primary.width,
            "height": primary.height,
            "duration_ms": vmeta.get("durationMs"),
        }


def _require_model(model_id: str) -> UpstreamVideoModel:
    m = get_video_model(model_id)
    if m is None:
        raise InvalidParameterError(
            f"上游模型 {model_id!r} 不在视频能力表里。", param="model")
    return m


# ---------------------------------------------------------------------------
# Seedance 响应构造 —— **唯一出口**
# ---------------------------------------------------------------------------

#: 本地状态 → Seedance 六态
_STATUS_MAP: dict[str, str] = {
    "queued": "queued",
    "in_progress": "running",
    "succeeded": "succeeded",
    "failure": "failed",
}

#: Seedance 任务保留期（秒）：`id` 存 7 天。与本地 `TASK_RETENTION_DAYS` 无关 ——
#: 那是本地清理策略，这个是**对外告知**的原生语义。
SEEDANCE_KEEP_SECONDS = 7 * 86400

#: 上游生成的是 24fps（`hailuo3.0` 的 duration × 24 与之自洽）
FPS = 24


def video_view(record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """视频任务记录 → `(HTTP 状态码, Seedance 原生任务对象)`。**唯一出口。**

    · 非终态 → **200** + `status: queued|running`（原生 GET 不放 202 ——
      调用方靠 `status` 判断是否继续轮询）；
    · `succeeded` → `content.video_url` + `usage`；
    · `failed` / 超时 → `error: {code, message}`。

    ⚠️ **失败也回 200**（与图片链路同一姿势）：任务本身完成了，只是结果是失败；
    回 4xx 会误触发调用方的重试逻辑。

    降级说明**不出现在响应里**（2026-10-10 接口字段收敛）：只进服务日志
    （`降级留痕：…`，带 task_id）与任务库 `degradations_json`。
    """
    task_id = record["task_id"]
    status = record.get("status")
    request = record.get("request") or {}
    plan = record.get("plan") or {}
    result = record.get("result") or {}
    error = record.get("error") or {}

    created_at = int(record.get("created_at") or 0)
    updated_at = int(record.get("updated_at") or record.get("finished_at") or created_at)

    seedance_status = _STATUS_MAP.get(status or "", "queued")
    #: 看门狗超时 ≠ 上游判失败 —— Seedance 里这叫 `expired`，别混成 `failed`
    if status == ST_FAILURE and (error or {}).get("code") == "task_timeout":
        seedance_status = "expired"

    body: dict[str, Any] = {
        "id": task_id,
        "model": request.get("model") or plan.get("capability") or "",
        "status": seedance_status,
        "created_at": created_at,
        "updated_at": updated_at,
        "seed": -1,
        "execution_expires_after": SEEDANCE_KEEP_SECONDS,
        "service_tier": "default",
    }
    if plan.get("duration"):
        body["duration"] = int(plan["duration"])
        body["framespersecond"] = FPS
    ratio = request.get("ratio") or plan.get("aspect_ratio")
    if ratio:
        body["ratio"] = ratio
    if plan.get("resolution"):
        body["resolution"] = f"{plan['resolution']}p"

    if status == ST_SUCCEEDED:
        usage: dict[str, Any] = {"completion_tokens": 0, "total_tokens": 0}
        forecast = plan.get("forecast_credits")
        if forecast is not None:
            #: 🔴 明说是**预估**（来自上游计价表），不是账单实扣积分
            usage["forecast_credits"] = forecast
        body["content"] = {"video_url": result.get("video_url") or ""}
        body["usage"] = usage
    elif status == ST_FAILURE:
        body["content"] = {"video_url": ""}
        body["usage"] = {"completion_tokens": 0, "total_tokens": 0}
        body["error"] = {
            "code": str(error.get("code") or "task_failed"),
            "message": str(error.get("message") or "任务失败（上游未给出原因）。"),
        }
    else:
        #: 非终态：原生形态里 `content` 仍带着一个空的 video_url
        body["content"] = {"video_url": ""}

    return 200, body


__all__ = [
    "FRAME_TYPE_FIRST",
    "FRAME_TYPE_LAST",
    "RESOLUTION_ALIASES",
    "VIDEO_KNOWN_UNSUPPORTED",
    "VideoPlan",
    "VideoService",
    "detect_mode",
    "parse_content",
    "video_view",
]
