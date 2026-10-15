#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排层：受理 / 计划 / 提交 / 轮询 / **响应构造**。

## 受理不碰上游

`create()` **只做三件事**：校验 → 翻译成"计划" → 落库。请求内**零上游往返**。

理由：建任务是**计费动作**，必须受节奏闸门约束、要能重试、要能防重复提交 ——
这些都是后台协调器的职责，不是 HTTP 请求的职责。
输入图下载/上传同理，放后台 ⇒ 网络抖动体现为任务 `failure`（附原因），
而不是让受理请求跟着上游一起抖。

## 翻译层（`build_plan`）—— 本项目最该被测试覆盖的地方

**纯函数**，不碰网络、不碰库。它只做一件事：
把 OpenAI 形态的请求翻成 hailuo `parameter` 结构，并把**每一处"请求了 A、实际做了 B"**
写进 `degradations`。

### 图生图（重点）的四条适配规则

1. **`fileList` 顺序 = 请求顺序**（上游按顺序理解垫图语义，实测抓包里 `fileList` 就是有序数组）；
2. **张数上限按"所选模型自己声明的" `maxSupportImageCount` 校验**
   （`gpt-image-1.5` 只有 3，`nano_banana*` 是 14，`gpt-image-2*` 是 16）——
   **超了就 400，绝不"收下 N 张只用第 1 张"**；
3. **`referenceMode` 默认不发** —— 抓包的 i2i 请求里 `imageParameter` **没有**这个键，
   只有显式指定时才带上（`extend` 是"扩图"，勿默认）；
4. **`useOriginPrompt` 默认 `True`** —— 即"不要改写我的 prompt"。
   反过来（`False`）等于让上游偷偷重写调用方的提示词，那是**没被要求的降级**。

## `view()` 是唯一出口

所有响应形状只在这里产出。改形状 = 改这里 + 改 `docs/INTERFACE.md` + 改 `tests/test_api.py`。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from . import media, models, superres
from .config import Settings
from .credentials import CredentialPool, parse_jwt
from .errors import (
    AdapterError,
    CapabilityUnavailable,
    InvalidParameterError,
    RiskControlChallenge,
    TaskNotDeletable,
    TaskNotFoundError,
    CredentialUnavailable,
)
from .gate import Gate, build as build_gate
from .observability import OBS
from .store import (
    ST_FAILURE,
    ST_IN_PROGRESS,
    ST_QUEUED,
    ST_SUCCEEDED,
    TERMINAL,
    TaskStore,
    new_task_id,
)
from .upstream.hailuo import capabilities as caps
from .upstream.hailuo import upload as up
from .upstream.hailuo.client import (
    FEED_TYPE_IMAGE,
    Feed,
    HailuoClient,
    status_name,
)

# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------

_RATIO_RE = re.compile(r"^(\d{1,3}):(\d{1,3})$")
_SIZE_RE = re.compile(r"^(\d{2,5})\s*[x×*]\s*(\d{2,5})$")

#: 同步接口（`POST /v1/images/generations`）等待终态时的**库轮询间隔**（秒）。
#: 只读本地库、不打上游，取小让同步响应紧跟终态出现（实测终态写库后至多半拍延迟）。
SYNC_WAIT_POLL_INTERVAL = 0.5

#: 长边 → resolution 档。**这是本服务自己的口径**（上游只认 1K/2K/4K 这类枚举），
#: 所以每次映射都进 `degradations`。
_RES_BY_MAX_SIDE: tuple[tuple[int, str], ...] = ((1536, "1K"), (2560, "2K"))

#: resolution 档位**从便宜到贵**的排序（未知档位排最后）。
#: 🔴 用户口径（2026-10-15）：调用方未指定 resolution 时**写死最便宜档**，
#: 不再跟随上游 `defaultSelect`（flare 的 defaultSelect 是 2K ⇒ 默认贵 2.5 倍）。
_RES_ORDER: dict[str, int] = {"Low": 0, "1K": 1, "Medium": 2, "2K": 3, "High": 4, "4K": 5}


def _ratio_value(text: str) -> float | None:
    m = _RATIO_RE.match(text.strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w / h) if h else None


def _resolution_from_size(width: int, height: int) -> str:
    max_side = max(width, height)
    for bound, label in _RES_BY_MAX_SIDE:
        if max_side <= bound:
            return label
    return "4K"


@dataclass
class Plan:
    """一次生成任务在**上游侧**的完整形态。"""

    capability: str
    upstream_model: str
    desc: str
    image_urls: list[str] = field(default_factory=list)
    quantity: int = 1
    aspect_ratio: str | None = None
    resolution: str | None = None
    quality: str | None = None
    reference_mode: str | None = None
    use_origin_prompt: bool = True
    degradations: list[str] = field(default_factory=list)
    #: 预估积分（来自上游计价表，**只是预估**）
    forecast_credits: float | None = None
    #: 调用方**原始请求**的目标档位（成本策略下 gpt-image-* 上游恒按最便宜档
    #: 生成，这个字段只记录"调用方想要什么"，是超分倍数的决策依据）。
    requested_resolution: str | None = None
    #: 产物阶段要做的腾讯 CI 超分倍数（None = 不超分）。2K→×2、4K→×4。
    superres_magnify: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "upstream_model": self.upstream_model,
            "desc": self.desc,
            "image_urls": list(self.image_urls),
            "quantity": self.quantity,
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "quality": self.quality,
            "reference_mode": self.reference_mode,
            "use_origin_prompt": self.use_origin_prompt,
            "degradations": list(self.degradations),
            "forecast_credits": self.forecast_credits,
            "requested_resolution": self.requested_resolution,
            "superres_magnify": self.superres_magnify,
        }

    def prompt_struct(self) -> str:
        """上游 `promptStruct` —— 富文本结构。

        抓包回显的形态（t2i「a cat」）：

        ```json
        {"value":[{"type":"paragraph","children":[{"text":"a cat"}]}],
         "length":5,"plainLength":5,"rawLength":5}
        ```

        ⚠️ 本服务**只造单段落**结构：调用方给的是纯文本，把它拆成多段落
        是我们**没有依据**的加工。三个长度字段都取纯文本长度（与抓包一致）。
        """
        return json.dumps({
            "value": [{"type": "paragraph", "children": [{"text": self.desc}]}],
            "length": len(self.desc),
            "plainLength": len(self.desc),
            "rawLength": len(self.desc),
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 认可的"上游没有"字段 —— 进 degradations，不报错
# ---------------------------------------------------------------------------

KNOWN_UNSUPPORTED: dict[str, str] = {
    "watermark": "上游图片链路没有水印开关（产物本身就带不带水印两版直链）",
    "response_format": "上游恒回 URL（不回 b64_json）",
    "style": "上游没有 style 维度（画面风格请写进 prompt）",
    "stream": "本服务是两段式异步接口，没有流式",
    "user": "上游没有 user 维度",
    "sequential_image_generation": "上游没有连续生成概念",
    "max_images": "上游张数由 quantity 决定（见 n 的语义）",
    "background": "上游没有 background 维度",
    "output_format": "上游产物格式由模型决定",
    "moderation": "上游审核策略不可配",
}


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class Service:
    """编排层。持有 store / client / uploader / gate，是唯一改任务状态的地方。"""

    #: 产物计数的单位（日志/观测用）。视频管线覆写为"条"。
    produced_unit: str = "张图"

    def __init__(
        self,
        settings: Settings,
        *,
        store: TaskStore | None = None,
        client: HailuoClient | None = None,
        uploader: up.Uploader | None = None,
        fetch_capabilities: bool = True,
        http_transport: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store or TaskStore(
            settings.db_target, pool_size=settings.task_db_pool_size,
            max_overflow=settings.task_db_max_overflow,
            pool_recycle=settings.task_db_pool_recycle,
            pre_ping=settings.task_db_pool_pre_ping,
            connect_timeout=settings.task_db_connect_timeout,
        )
        self.gate: Gate = build_gate(settings)
        #: 🔴 **多账号（号池）模式**：并发槽位、提交节奏、风控冷却都按**凭据（账号）**
        #: 隔离 —— 账号 A 命中风控冷却不能停掉账号 B 的队列；`HL_CONCURRENCY` /
        #: `HL_MIN_INTERVAL` / `HL_PER_MINUTE` 的语义因此是"**每账号**"
        #: （总并发 = N 账号 × 参数值）。`self.gate` 保留为**默认闸门**：
        #: 匿名/无凭据场景与观测兜底用。
        self._gates: dict[str, Gate] = {}
        self._gates_lock = threading.Lock()
        self._client = client
        self._uploader = uploader
        #: 🔴 **透传凭据池**（AUTH_MODE=jwt）：指纹 → (token, 惰性客户端)。
        #: 明文只存内存、绝不落库；进程重启即空 ⇒ 在途任务以 CredentialUnavailable 明确失败。
        self.credentials = CredentialPool(transport=http_transport)
        #: 本服务自建 httpx 客户端时统一用它（输入图下载 + 能力表读取）
        #: —— **测试靠它做到零出网**。为 `None` 时走真实网络。
        self._http_transport = http_transport
        self._caps_degradations: list[str] = []
        self._caps_refreshed_at: float = 0.0
        #: 指纹密钥：让**明文 Key 永不落库**
        self._secret = self.store.fingerprint_secret()
        if fetch_capabilities:
            self.refresh_capabilities()

    # ------------------------------------------------------------------ 依赖

    @property
    def client(self) -> HailuoClient:
        """惰性建上游客户端（未配 token 时不建 —— 让 `/healthz` 零依赖）。"""
        if self._client is None:
            self._client = HailuoClient(
                token=self.settings.hailuo_token,
                base_url=self.settings.hailuo_base_url,
                device=self.settings.device_profile(),
            )
        return self._client

    @property
    def uploader(self) -> up.Uploader:
        if self._uploader is None:
            self._uploader = up.Uploader(client=self.client,
                                         cache_ttl=self.settings.upload_cache_ttl)
        return self._uploader

    def close(self) -> None:
        for name, obj in (("uploader", self._uploader), ("client", self._client)):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception as e:  # noqa: BLE001
                # 关闭失败不该影响进程退出，但**必须留痕** ——
                # 否则"连接关不掉"这件事在任何地方都看不见。
                logger.debug(f"{name} 关闭失败（已忽略）：{type(e).__name__}: {e}")
        #: 透传池里的每凭据客户端（连接池）也要关 —— 否则进程退出时连接悬着
        self.credentials.close_all()
        self.store.close()

    # ------------------------------------------------------------------ 凭据

    def clients_for(self, credential: str | None) -> tuple[HailuoClient, up.Uploader]:
        """按**凭据指纹**取 `(HailuoClient, Uploader)`。

        · **嵌入模式**（构造时显式注入 `client`+`uploader`：测试、`scripts/` 与
          宿主进程直接持有 Service 的情形）⇒ 用注入的实例；
        · **HTTP 服务模式** ⇒ 从**透传凭据池**取；池里没有 ⇒ `CredentialUnavailable`
          （进程重启/token 过期/被淘汰 —— **绝不回落到别的账号**，
          否则费用会记到错误的人头上）。
        """
        if self._client is not None and self._uploader is not None:
            return self._client, self._uploader
        pair = self.credentials.clients_for(credential or "", self.settings)
        if pair is None:
            raise CredentialUnavailable(
                "透传凭据已不在内存池（多为：服务重启，或 token 过期/被淘汰）。"
                "明文凭据不落库，重启后无法恢复 —— 请用同一 token 重新提交任务；"
                "若任务已建到上游，可用该账号在 hailuo 侧查看原批次。")
        return pair

    def gate_for(self, credential_id: str | None) -> Gate:
        """取**该账号**的节奏闸门（惰性创建，每个凭据指纹一把）。

        `credential_id` 为空（匿名/嵌入）⇒ 回落默认闸门 `self.gate`。
        闸门是进程内状态：多**副本**仍会把限速整体放大（见 gate.py 模块注释），
        但同进程内的多**账号**彼此隔离 —— 这是 newapi 号池模式的前提。
        """
        if not credential_id:
            return self.gate
        with self._gates_lock:
            gate = self._gates.get(credential_id)
            if gate is None:
                gate = build_gate(self.settings)
                self._gates[credential_id] = gate
            return gate

    def gates_snapshot(self) -> dict[str, dict[str, Any]]:
        """全部**每账号**闸门的状态快照（观测用）。键 = 凭据指纹（不可逆哈希）。"""
        with self._gates_lock:
            return {cred: g.stats() for cred, g in self._gates.items()}

    def register_credential(self, token: str) -> str:
        """校验并登记一个**透传凭据**，返回凭证指纹。

        入口鉴权（`main.require_key`）与运维脚本共用这一处 —— 校验规则只有一份。
        """
        claims = parse_jwt(token)
        fingerprint = self.credential_of(token)
        self.credentials.put(fingerprint, token, claims)
        return fingerprint

    def _fail_task(self, task_id: str, err: AdapterError) -> None:
        """把任务判失败并带上**可读原因**（唯一判失败入口，避免各处自造形状）。"""
        self.store.update_task(
            task_id, status="failure", finished_at=time.time(),
            error={"message": str(err), "type": err.error_type, "code": err.code})

    # ------------------------------------------------------------------ 公开访问器
    # 这两条存在的理由：协调器与 `/capabilities` 路由都要读它们。
    # 让外部去摸 `service._uploader` / `service._caps_degradations` 是跨层访问私有成员
    # —— 静态门禁（SLF001）会拦，而且那种耦合在重构时会静默断掉。

    @property
    def capability_degradations(self) -> list[str]:
        '''能力表的降级说明（读不到时退回冻结快照的原因）。**只读副本。**'''
        return list(self._caps_degradations)

    def purge_upload_cache(self) -> int:
        '''清理过期的上传缓存。返回清掉的条数（没有 uploader 时返回 0）。'''
        if self._uploader is None:
            return 0
        return self._uploader.purge_expired()

    # ------------------------------------------------------------------ 能力表

    def refresh_capabilities(self) -> list[str]:
        """运行期实读上游能力表（**零计费**）。读不到 ⇒ 退回冻结快照 + 留痕。"""
        #: 🔴 **不检查任何凭据**：能力表的两个端点是**公开免鉴权**的，
        #: 透传模式下来源与费用都无关 ⇒ 直接读，失败按降级处理。
        try:
            #: 🔴 **刻意惰性导入**：`/healthz` 必须零依赖（容器每 30s 打它），
            #: 所以 httpx 只在这条真的要用它的路径上才加载。
            import httpx  # noqa: PLC0415

            with httpx.Client(base_url=self.settings.hailuo_base_url, timeout=12.0,
                              transport=self._http_transport) as c:
                found, notes = _sync_fetch_models(c)
            if found:
                models.install_runtime_models(found)
                OBS.event("capabilities.loaded", coverage=caps.coverage(found))
            self._caps_degradations = notes
            self._caps_refreshed_at = time.time()
            return notes
        except Exception as e:  # noqa: BLE001
            note = (f"上游能力表整体读取失败（{type(e).__name__}: {e}）⇒ "
                    f"退回 {models.SNAPSHOT_DATE} 的冻结快照（{len(models.FROZEN_SNAPSHOT)} 个模型）。")
            logger.warning(note)
            self._caps_degradations = [note]
            return [note]

    # ------------------------------------------------------------------ 凭据

    def credential_of(self, key: str | None) -> str:
        """API Key → **不可逆指纹**。明文 Key 绝不落库、绝不进日志。"""
        material = (key or "anonymous").encode("utf-8")
        return hmac.new(self._secret.encode("utf-8"), material,
                        hashlib.sha256).hexdigest()[:32]

    # ------------------------------------------------------------------ 受理

    def create(self, body: dict[str, Any], *, credential: str) -> dict[str, Any]:
        """受理一次生成。**只落库，零上游往返。**"""
        request = dict(body or {})
        plan, capability, upstream_model = self.build_plan(request)

        rec = self.store.create_task(
            task_id=new_task_id(),
            credential_id=credential,
            capability=capability,
            model=str(request.get("model") or ""),
            upstream_model=upstream_model,
            status=ST_QUEUED,
            request_json=json.dumps(request, ensure_ascii=False),
            plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
            degradations_json=json.dumps(plan.degradations, ensure_ascii=False),
        )
        OBS.event("task.accepted", task_id=rec["task_id"], capability=capability,
                  upstream_model=upstream_model, quantity=plan.quantity,
                  n_images=len(plan.image_urls))
        logger.bind(task_id=rec["task_id"], capability=capability).info(
            f"已受理：{capability} model={upstream_model} "
            f"n={plan.quantity} images={len(plan.image_urls)}")
        #: 🔴 降级说明**只进日志与任务库，不出现在 HTTP 响应**（2026-10-10
        #: 接口字段收敛：调用方不消费该字段，排查以这里打出的日志为准）。
        for note in plan.degradations:
            logger.bind(task_id=rec["task_id"]).info(f"降级留痕：{note}")
        return rec

    # ------------------------------------------------------------------ 翻译层

    def build_plan(self, request: dict[str, Any]) -> tuple[Plan, str, str]:
        """把请求翻译成 `Plan`。返回 `(plan, capability, upstream_model)`。

        **纯函数**（只读 `request` 与全局能力表）—— 因此可以在毫秒内被测试穷举，
        而它正是"翻译层"的全部。任何会改变花费的取舍都必须写进 `degradations`。
        """
        degradations: list[str] = []

        # ---- 未知字段：区分"上游没有"与"你写错了"
        known = {"model", "prompt", "image", "size", "n", "seed", "negative_prompt",
                 "resolution", "aspect_ratio", "quality", "reference_mode"}
        for key in request:
            if key in known:
                continue
            if key in KNOWN_UNSUPPORTED:
                degradations.append(
                    f"参数 {key}={request[key]!r} 本服务**不转发**（{KNOWN_UNSUPPORTED[key]}），"
                    f"已忽略；不要按它的语义预期结果。")
                continue
            raise InvalidParameterError(
                f"未知参数 {key!r}。本服务接受的字段：{', '.join(sorted(known))}。",
                param=key)

        # ---- 输入图
        image_urls = media.accept_form(request.get("image"))

        # ---- 能力
        capability, resolve_notes = models.resolve_capability(
            request.get("model"), has_image=bool(image_urls))
        degradations += resolve_notes
        cap = models.capability(capability)
        assert cap is not None  # resolve_capability 只回已知能力

        # ---- 上游模型
        raw_model = str(request.get("model") or "").strip()
        original_model = raw_model
        raw_model = models.canonical_model_id(raw_model)  # 别名 → 规范 modelID（零说明）
        #: 🔴 成本策略定向（2026-10-15）：gpt-image* ⇒ flare。档位词汇一并
        #: 翻译（1.5 的 Low/Medium/High → flare 的 1K/2K/4K），后续按 flare
        #: 的枚举校验与成本策略走。
        if raw_model != original_model and original_model.startswith(
                models.GPT_IMAGE_PREFIX):
            degradations.append(
                f"【gpt-image 成本策略】model={original_model!r} ⇒ 定向到 "
                f"{raw_model}（上游只按 flare 一个形态计费/出图）。")
            vocab = {"Low": "1K", "Medium": "2K", "High": "4K"}
            if str(request.get("resolution") or "") in vocab:
                request["resolution"] = vocab[str(request["resolution"])]
        available = {m.model_id: m for m in models.all_models()}
        if raw_model and raw_model in available:
            # 调用方直接给了上游 modelID（精确匹配，**不做小写化**）
            upstream_model = raw_model
        else:
            # 空 / 占位名 / 能力名 ⇒ 落默认模型，并**说清楚**
            upstream_model = models.DEFAULT_MODEL
            if models.DEFAULT_MODEL not in available:
                raise CapabilityUnavailable(
                    f"默认模型 {models.DEFAULT_MODEL} 不在当前能力表里 —— "
                    f"能力表可能读坏或上游已下线该模型。请显式传 model。")
            degradations.append(
                f"未指定上游模型（model={raw_model or '（空）'!r}，能力 {capability}）"
                f"⇒ 使用默认 {upstream_model}。"
                f"如需指定请传 model=nano_banana21_flash / nano-banana2 / "
                f"seedream-5.0 / gpt-image-2 等已登记模型。")
        model_meta = available.get(upstream_model)
        if model_meta is None:
            raise InvalidParameterError(
                f"上游模型 {upstream_model} 不在能力表里（能力表来源："
                f"{'运行期实读' if models.runtime_models() else '冻结快照'}）。",
                param="model")

        #: 🔴 **参考图模式不匹配 ⇒ 本地明确拒绝**（2026-09-21 实测驱动）。
        #: `common_config` 给每个模型声明了 `mode`：本服务实现的是
        #: `image-reference`（图片参考/编辑，fileList 语义）；
        #: `subject-reference`（主体参考，如 image-01）需要另一套机制 ——
        #: 带参考图打过去会被上游用 `code 2400052` 拒掉。让调用方**在上传之前**
        #: 就拿到可执行的 400，而不是撞一个没有解释的上游码。
        if image_urls and (getattr(model_meta, "mode", "") or "") != "image-reference":
            raise InvalidParameterError(
                f"模型 {upstream_model} 的参考图模式是 "
                f"{(getattr(model_meta, 'mode', None) or '未声明')!r}（主体参考），"
                f"本服务当前只实现 'image-reference'（图片参考/编辑）—— "
                f"带 image 会 100% 被上游拒。请换用 nano_banana21_flash / "
                f"nano-banana2 / gpt-image-2 等声明 image-reference 的模型，"
                f"或去掉 image 走文生图。",
                param="model")

        # ---- prompt
        prompt = request.get("prompt")
        prompt = "" if prompt is None else str(prompt)
        if cap.requires_prompt and not prompt.strip():
            raise InvalidParameterError(
                f"{capability} 需要 prompt（提示词）。", param="prompt")
        if prompt and model_meta.max_prompt_length \
                and len(prompt) > model_meta.max_prompt_length:
            raise InvalidParameterError(
                f"prompt 长度 {len(prompt)} 超过模型 {upstream_model} 声明的上限 "
                f"{model_meta.max_prompt_length}（上游 maxPromptLength）。", param="prompt")

        # ---- 张数（图片端点的 n）
        quantity = self._resolve_quantity(request.get("n"), degradations)

        # ---- 输入图张数：**按所选模型自己声明的上界**
        limit = model_meta.max_support_image_count
        if image_urls:
            if not cap.requires_image:
                degradations.append(
                    f"{capability} 不接受输入图 ⇒ image 被忽略"
                    f"（请改用 hailuo-i2i）。")
                image_urls = []
            elif limit is not None and len(image_urls) > limit:
                raise InvalidParameterError(
                    f"模型 {upstream_model} 声明最多接受 {limit} 张输入图"
                    f"（上游 maxSupportImageCount），请求给了 {len(image_urls)} 张。"
                    f"请减少张数，或换一个上限更高的模型"
                    f"（如 nano_banana21_flash / gpt-image-2 上限 14/16）。",
                    param="image")
            elif len(image_urls) > 4:
                degradations.append(
                    f"本次带 {len(image_urls)} 张垫图（模型上限 {limit}）—— "
                    f"上游对多图的实际语义未逐一取证，顺序按请求顺序传递。")
        elif cap.requires_image:
            raise InvalidParameterError(
                f"{capability} 需要至少 1 张输入图（这就是图生图）。"
                f"文生图请用 hailuo-t2i 或留空 model。", param="image")

        # ---- (resolution, quality)
        #: 🔴 成本策略（2026-10-15）：先记下调用方的**意图档位**（供超分决策）——
        #: gpt-image-* 上游恒按最便宜档生成，请求档位只用于决定超分倍数。
        requested_resolution = request.get("resolution")
        if requested_resolution is None and request.get("size") is not None:
            m = _SIZE_RE.match(str(request["size"]).strip())
            if m:
                requested_resolution = _resolution_from_size(
                    int(m.group(1)), int(m.group(2)))
        resolution, quality, ratio_notes, ratio = self._resolve_size(
            request, model_meta, degradations)
        degradations += ratio_notes

        #: 🔴 【gpt-image 成本策略】gpt-image-2 / 2.5-* 上游**恒 1K+low**、
        #: gpt-image-1.5 恒 Low —— 上游高档是纯加价（4K/low=10 积分 vs
        #: 1K/low=2 积分 + CI 超分一次几分钱）。调用方传更高档 ⇒ 忽略并转
        #: 超分（2K/Medium→×2、4K/High→×4），每一处覆盖都留痕。
        magnify: int | None = None
        if upstream_model.startswith("gpt-image"):
            requested = str(requested_resolution or "").upper()
            magnify = {"2K": 2, "MEDIUM": 2, "4K": 4, "HIGH": 4}.get(requested)
            target_res = "Low" if upstream_model == "gpt-image-1.5" else "1K"
            forced: list[str] = []
            if resolution != target_res:
                forced.append(f"resolution={resolution!r}→{target_res}")
                resolution = target_res
            if target_res == "1K" and (quality or "").lower() != "low":
                forced.append(f"quality={quality!r}→low")
                quality = "low"
            if magnify:
                forced.append(
                    f"目标档 {requested_resolution} ⇒ 产物阶段腾讯 CI 超分 ×{magnify} 补齐")
            if forced:
                degradations.append(
                    f"【gpt-image 成本策略】上游恒按最便宜档生成（{'；'.join(forced)}）。")

        # ---- referenceMode（**默认不发**）
        reference_mode = request.get("reference_mode")
        if reference_mode is not None:
            reference_mode = str(reference_mode)
            if reference_mode not in models.REFERENCE_MODES:
                raise InvalidParameterError(
                    f"reference_mode={reference_mode!r} 不在已知取值 "
                    f"{sorted(models.REFERENCE_MODES)} 内。", param="reference_mode")
        if reference_mode is None and image_urls:
            degradations.append(
                "未指定 reference_mode ⇒ 不发送该字段（与抓包一致；"
                "上游按模型默认的 image-reference 处理参考图）。")

        forecast = model_meta.cost_for(resolution or "", quality) \
            if (resolution or quality) else model_meta.default_cost

        plan = Plan(
            capability=capability,
            upstream_model=upstream_model,
            desc=prompt,
            image_urls=image_urls,
            quantity=quantity,
            aspect_ratio=ratio,
            resolution=resolution,
            quality=quality,
            reference_mode=reference_mode,
            use_origin_prompt=True,
            degradations=degradations,
            forecast_credits=forecast,
            requested_resolution=requested_resolution,
            superres_magnify=magnify,
        )
        return plan, capability, upstream_model

    # ------------------------------------------------------------------ 翻译辅助

    @staticmethod
    def _resolve_quantity(raw: Any, degradations: list[str]) -> int:
        """`n`（出图张数）。**默认 1**，上界 10。

        ⚠️ 不采用上游的 `defaultSelect` 张数 —— 默认必须是**最省**的那个，
        否则调用方会按"1 张"的预期收到多张的账单。
        """
        if raw is None:
            return 1
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidParameterError(f"n 必须是整数，实得 {raw!r}。", param="n")
        n = int(raw)
        if n < 1:
            raise InvalidParameterError(f"n 必须 >= 1，实得 {n}。", param="n")
        if n > 10:
            degradations.append(f"请求 n={n} 超过本服务上限 10 ⇒ 已按 10 处理。")
            return 10
        return n

    @staticmethod
    def _resolve_size(
        request: dict[str, Any], model_meta: models.UpstreamModel,
        degradations: list[str],
    ) -> tuple[str | None, str | None, list[str], str | None]:
        """`size` / `resolution` / `aspect_ratio` → `(resolution, quality, 说明, aspect_ratio)`。

        三种输入形态，按优先级：
          1. **原生的 `resolution` + `aspect_ratio`** —— 直接按模型声明的枚举校验（最忠实）；
          2. **`size: "WxH"`** —— 换算：长边定 resolution 档，宽高比**吸附**到该模型支持的最接近比例；
          3. 都没给 —— 🔴 **写死最便宜档**（2026-10-15 用户口径）：resolution 取
             `_RES_ORDER` 里该模型支持的最小档（**不跟随上游 defaultSelect**，
             flare 的 defaultSelect=2K 会贵 2.5 倍）；quality 维度存在时写死 `low`。

        🔴 **换算与吸附一定留痕**：`size` 不是上游的概念，
        "4096x4096 ⇒ 4K + 1:1" 是本服务的口径，调用方必须知道。
        """
        notes: list[str] = []
        resolution = request.get("resolution")
        ratio = request.get("aspect_ratio")
        quality = request.get("quality")
        size = request.get("size")

        if resolution is not None:
            resolution = str(resolution)
            if model_meta.resolutions and resolution not in model_meta.resolutions:
                raise InvalidParameterError(
                    f"resolution={resolution!r} 不被 {model_meta.model_id} 支持，"
                    f"可选：{list(model_meta.resolutions)}。", param="resolution")
            if not model_meta.resolutions:
                notes.append(
                    f"模型 {model_meta.model_id} 未声明 resolution 档位 ⇒ "
                    f"resolution={resolution!r} 不予转发。")
                resolution = None

        if ratio is not None:
            ratio = str(ratio)
            if model_meta.aspect_ratios and ratio not in model_meta.aspect_ratios:
                raise InvalidParameterError(
                    f"aspect_ratio={ratio!r} 不被 {model_meta.model_id} 支持，"
                    f"可选：{list(model_meta.aspect_ratios)}。", param="aspect_ratio")

        supported_q = {q for row in model_meta.costs for q in (row.get("qualities") or [])}
        if quality is not None:
            quality = str(quality).lower()
            if supported_q and quality not in supported_q:
                raise InvalidParameterError(
                    f"quality={quality!r} 不被 {model_meta.model_id} 支持，"
                    f"可选：{sorted(supported_q)}。", param="quality")
            if not supported_q:
                notes.append(f"模型 {model_meta.model_id} 没有 quality 维度 ⇒ 已忽略 quality。")
                quality = None
        elif supported_q:
            #: 🔴 用户口径（2026-10-15）：quality **写死 low**。gpt-image-* 按
            #: resolution × quality 计价，而不带这个键时上游按哪档收费**未取证**
            #: （前端抓包恒带 quality）—— 默认走最便宜档，把"未知"变成"已知"，
            #: 静默按高价档扣钱是最坏的降级。
            quality = "low"
            notes.append(
                f"未指定 quality ⇒ 写死 low（{model_meta.model_id} 按 resolution×quality "
                f"计价，默认取最便宜档；可选：{sorted(supported_q)}）。")

        if size is not None:
            m = _SIZE_RE.match(str(size).strip())
            if not m:
                raise InvalidParameterError(
                    f'size 必须形如 "2048x2048"，实得 {size!r}。', param="size")
            width, height = int(m.group(1)), int(m.group(2))
            derived_res = _resolution_from_size(width, height)
            if model_meta.resolutions:
                resolution = derived_res if derived_res in model_meta.resolutions else \
                    _nearest(resolution_options=model_meta.resolutions, target=derived_res)
                notes.append(
                    f"size={width}x{height} ⇒ resolution={resolution}"
                    f"（本服务按长边 {max(width, height)} 换算；上游只认 "
                    f"{list(model_meta.resolutions)} 这类档位）。")
            else:
                notes.append(
                    f"模型 {model_meta.model_id} 未声明 resolution 档位 ⇒ "
                    f"size 只用于推比例，档位不转发。")
            if model_meta.aspect_ratios:
                exact = next((r for r in model_meta.aspect_ratios
                              if _ratio_value(r) is not None
                              and abs(_ratio_value(r) - width / height) < 1e-9), None)
                snapped = exact or _snap_ratio(width / height, model_meta.aspect_ratios)
                if snapped:
                    ratio = snapped
                    if exact:
                        # 正好就是该模型支持的档位 —— 别把"精确对应"说成"吸附"，
                        # 那会让人以为自己给的比例被改了。
                        notes.append(
                            f"size={width}x{height} 正好对应 aspect_ratio={snapped}。")
                    else:
                        notes.append(
                            f"size={width}x{height} 的宽高比 {width / height:.4f} ⇒ "
                            f"**吸附**到 aspect_ratio={snapped}"
                            f"（该模型支持的档位：{list(model_meta.aspect_ratios)}）。")
            else:
                notes.append(f"模型 {model_meta.model_id} 未声明比例档位 ⇒ 比例不转发。")

        if resolution is None and model_meta.resolutions:
            #: 🔴 用户口径（2026-10-15）：未指定 resolution **写死最便宜档**
            #: （1K < 2K < 4K；gpt-image-1.5 是 Low < Medium < High），
            #: **不再跟随上游 defaultSelect** —— flare 的 defaultSelect 是 2K，
            #: 跟着走会让"没指定"的调用方为 1K 能做的事付 2.5 倍积分。
            resolution = min(model_meta.resolutions,
                             key=lambda r: (_RES_ORDER.get(r, 99), r))
            notes.append(
                f"未指定 resolution ⇒ 写死最便宜档 {resolution}"
                f"（该模型可选：{list(model_meta.resolutions)}；"
                f"上游 defaultSelect={list(model_meta.default_resolutions)} 已不作为默认）。")
        if ratio is None and model_meta.default_aspect_ratios:
            ratio = model_meta.default_aspect_ratios[0]
            notes.append(
                f"未指定 aspect_ratio ⇒ 用上游默认档 {ratio}"
                f"（defaultSelect={list(model_meta.default_aspect_ratios)}）。")

        return resolution, quality, notes, ratio

    # ------------------------------------------------------------------ 提交

    def _ingest_one(self, url: str, *, uploader: up.Uploader,
                    dry_run: bool = False) -> tuple[Any, list[str], list[dict[str, Any]]]:
        """单张输入图的完整流水线：**取源**（http(s) 下载 或 data: base64 解码）
        → 归一化 → 上传。

        ⚠️ `uploader` 由调用方按**凭据**传入：上传结果是**账号级**资产
        （fileID 只在那个账号里有效）⇒ 上传缓存也必须按凭据隔离 ——
        每个凭据各有自己的 `Uploader`（见 `credentials.clients_for`）。

        张与张相互独立 ⇒ 线程池里并行跑；异常原样上抛
        （任一张失败 ⇒ 整个任务 failure，与串行版语义一致）。
        """
        blob = media.blob_from_source(
            url, max_bytes=self.settings.max_download_bytes,
            transport=self._http_transport)
        blob, notes = media.normalize(
            blob, max_side=self.settings.normalize_max_side,
            max_bytes=self.settings.normalize_max_bytes,
            enabled=self.settings.normalize_uploads)
        uploaded, traces = uploader.upload_bytes(
            content=blob.data, mime=blob.mime, name=blob.name, dry_run=dry_run)
        return uploaded, notes, traces

    def submit(self, task_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """把一条 `queued` 任务建到上游。🔴 **计费动作**（`dry_run=True` 时零消耗）。

        顺序：闸门 → 上传输入图 → 建任务。
        """
        full = self.store.get_full(task_id)
        if not full:
            raise TaskNotFoundError(f"任务 {task_id} 不存在。")
        plan = full["plan"] or {}
        self.store.update_task(task_id, attempts=(full.get("attempts") or 0) + 1)

        #: 🔴 用**本任务所属账号**的闸门（多账号隔离，见 `gate_for`）
        gate = self.gate_for(full.get("credential_id"))
        decision = gate.check()
        if not decision.allowed:
            logger.bind(task_id=task_id).debug(f"闸门拦截：{decision.reason}")
            return {"submitted": False, "reason": decision.reason}

        # ① 输入图 → fileList（**并行 ingest**：取源→归一化→上传，每张一条独立流水线）
        # 张与张相互独立 ⇒ 墙钟 ≈ 最慢一张，而不是求和（三参考实测 ~20s → ~5s）。
        # 🔴 这一段的失败**一定没有**上游任务（可能连建任务请求都没发出）
        # ⇒ 标记 `pre_create`，协调器据此判断"可安全重试"还是"判失败"。
        #: 🔴 本任务的**凭据客户端**：透传模式下就是调用方那个 token 的客户端
        #: （建任务/上传/后续轮询同一账号；费用与资产都记在它头上）。
        client, uploader = self.clients_for(full.get("credential_id"))

        image_urls = list(plan.get("image_urls") or [])
        file_list: list[dict[str, Any]] = []
        try:
            if image_urls:
                workers = max(1, min(self.settings.ingest_parallelism, len(image_urls)))
                if workers == 1:
                    results = [self._ingest_one(u, uploader=uploader,
                                               dry_run=dry_run)
                               for u in image_urls]
                else:
                    with ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix="ingest") as pool:
                        #: `map` 保序 ⇒ fileList 顺序 = 请求顺序（上游按顺序理解垫图语义）
                        results = list(pool.map(
                            lambda u: self._ingest_one(u, uploader=uploader,
                                                       dry_run=dry_run), image_urls))
                for uploaded, norm_notes, traces in results:
                    for note in norm_notes:
                        self.store.add_degradation(task_id, note)
                        logger.bind(task_id=task_id).info(f"归一化留痕：{note}")
                    for t in traces:
                        OBS.upstream(f"upload.{t.get('stage')}", task_id=task_id, **t)
                    file_list.append(uploaded.to_file_list_entry())
        except AdapterError as e:
            #: 只有**瞬时类**失败才值得重试（5xx/连接类）；4xx 是调用方问题，重试无意义。
            if e.status_code >= 500:
                e.pre_create = True
            raise

        # ② 建任务
        batch_id, trace = client.create_image(
            model_id=plan["upstream_model"],
            desc=plan.get("desc") or "",
            file_list=file_list,
            quantity=int(plan.get("quantity") or 1),
            aspect_ratio=plan.get("aspect_ratio"),
            resolution=plan.get("resolution"),
            quality=plan.get("quality"),
            reference_mode=plan.get("reference_mode"),
            use_origin_prompt=bool(plan.get("use_origin_prompt", True)),
            dry_run=dry_run,
        )
        OBS.upstream("create_image", task_id=task_id, **trace)

        if dry_run:
            return {"submitted": False, "dry_run": True, "trace": trace,
                    "file_list": file_list}

        gate.note_submit()
        self.store.update_task(
            task_id, status=ST_IN_PROGRESS, upstream_batch_id=batch_id,
            submitted_at=time.time())
        return {"submitted": True, "upstream_batch_id": batch_id, "trace": trace}

    # ------------------------------------------------------------------ 轮询

    def poll_many(self, tasks: list[dict[str, Any]], *,
                  dry_run: bool = False) -> dict[str, Any]:
        """推进全部在途任务；**按凭据分组**，每组用各自的客户端查。

        🔴 请求数（透传模式下的关键指标）：
        · 单凭据（绝大多数部署）⇒ 与本项目的批量轮询设计一致：
          `my/batch` 不带 id（回"我最近的若干条"）⇒ 一轮 tick 主路径**恒 1 次**，
          窗口外的合并成**一次** v4 点名直查 ⇒ **至多 2 次**，与在途任务数无关；
        · 多凭据 ⇒ 每个"有在途任务的凭据"各 1 次主查询（+至多 1 次兜底）——
          这是**无法避免**的：`my/batch` 的语义是"**我**最近的 N 条"，跨账号查不了。

        凭据不在池里（进程重启/过期）⇒ 该组任务以 `CredentialUnavailable`
        **明确失败**（不静默卡死，也绝不改用别的账号去查）。
        """
        if not tasks:
            return {"polled": 0, "updated": 0}

        # ---- 起轮宽限：刚提交的任务先别问
        # 🔴 这个旋钮曾经**读了但没用**（`POLL_GRACE` 只被写进配置没人消费）。
        # 语义：建任务返回后 ≤ `poll_grace` 秒内不查上游 —— 因为此刻它几乎必然
        # 还没落库，问了也是白问（纯浪费一次请求）。宽限期过后**再问一次是零代价的**，
        # 所以这个值取小（默认 0.5s）而不是取大。
        now = time.time()
        grace = self.settings.poll_grace
        deferred = [t for t in tasks
                    if t.get("submitted_at") and now - t["submitted_at"] < grace]
        if deferred:
            deferred_ids = {t["task_id"] for t in deferred}
            tasks = [t for t in tasks if t["task_id"] not in deferred_ids]
        if not tasks:
            # 全部都在宽限期内 ⇒ 本轮不打上游（**这是一次被省掉的请求**）
            return {"polled": 0, "updated": 0, "deferred": len(deferred)}

        # ---- 按凭据分组（单凭据 ⇒ 只有一组，行为与旧版逐字节一致）
        groups: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            groups.setdefault(task.get("credential_id") or "", []).append(task)

        out: dict[str, Any] = {"polled": 0, "updated": 0}
        for credential, group in groups.items():
            try:
                client, _ = self.clients_for(credential)
            except CredentialUnavailable as e:
                for task in group:
                    self._fail_task(task["task_id"], e)
                out["failed_no_credential"] = out.get("failed_no_credential", 0) + len(group)
                logger.warning(f"凭据不可用 ⇒ {len(group)} 个任务判失败：{e}")
                continue
            try:
                res = self._poll_group(group, client=client, dry_run=dry_run)
            except RiskControlChallenge as e:
                #: 🔴 多账号：轮询风控只冷却**该账号**（进它自己的闸门）——
                #: 旧行为是全局冷却，一个账号命中风控会把所有账号的在途轮询全停掉。
                self.gate_for(credential).enter_cooldown(str(e))
                logger.warning(f"轮询命中风控，账号 {credential[:12]}… 进入冷却：{e}")
                out["risk_control_groups"] = out.get("risk_control_groups", 0) + 1
                continue
            except AdapterError as e:
                #: 单账号的轮询失败只跳过它自己那组（各有各的上游账号），其他账号继续。
                logger.warning(f"轮询失败（跳过该账号组，其余不受影响）：{e}")
                out["poll_error_groups"] = out.get("poll_error_groups", 0) + 1
                continue
            for key, value in res.items():
                if isinstance(value, int):
                    out[key] = out.get(key, 0) + value
                elif key not in out:
                    out[key] = value
        if deferred:
            out["deferred"] = len(deferred)
        return out

    def _poll_group(self, tasks: list[dict[str, Any]], *, client: HailuoClient,
                    dry_run: bool = False) -> dict[str, Any]:
        """**同一凭据**的一组任务：1 次主查询 + 至多 1 次 v4 兜底。"""
        wanted = {t["task_id"]: t for t in tasks}
        batch_ids = {t.get("upstream_batch_id"): t["task_id"]
                     for t in tasks if t.get("upstream_batch_id")}

        # 只查图片类型；多取一些，因为我们用的是"我最近 N 条"的语义
        limit = max(30, min(100, len(wanted) * 4))
        batches, trace = client.fetch_batches(
            limit=limit, feed_types=[FEED_TYPE_IMAGE], dry_run=dry_run)
        OBS.upstream("fetch_batches", **trace)
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

        # ---- v4 按 id 兜底 ----
        # `my/batch` 回的是"我最近 N 条"，任务可能被账号里的历史记录挤出窗口。
        # 窗口没覆盖到的任务**合并成一次** v4 点名直查
        # （`/v4/api/multimodal/video/processing` 收 `batchInfoList`，一次带多个 id）。
        # 最坏情况一轮 2 次上游查询，仍与在途任务数无关；兜底失败**保持非终态**
        # —— "查不到/查挂了"都不是"任务失败"，与主路径对 missing 的语义一致。
        missing = [tid for tid in batch_ids.values() if tid not in seen]
        fallback: dict[str, Any] = {}
        if missing:
            bid_by_task = {tid: bid for bid, tid in batch_ids.items()}
            to_query = [bid_by_task[tid] for tid in missing][:50]
            try:
                v4_batches, v4_trace = client.fetch_by_ids(to_query)
                OBS.upstream("fetch_by_ids", **v4_trace)
                fallback["polled"] = len(to_query)
                for batch_id, feeds in v4_batches:
                    task_id = batch_ids.get(batch_id)
                    if not task_id or task_id in seen:
                        continue
                    seen.add(task_id)
                    if self._apply_feeds(task_id, feeds):
                        fallback["updated"] = fallback.get("updated", 0) + 1
                        updated += 1
            except AdapterError as e:
                #: 上游对"id 还没落库"的回应形态未取证 —— 把它当失败 =
                #: 让人为一个还在排队的任务哭丧。留痕、保持非终态即可。
                fallback["error"] = f"{type(e).__name__}: {e}"
                logger.debug(f"v4 兜底查询失败（保持非终态）：{e}")

        # 主窗口与 v4 点名都没查到的任务：只是"还没进列表"，**保持非终态**（不是失败）。
        still_missing = [tid for tid in batch_ids.values() if tid not in seen]
        if still_missing:
            logger.debug(f"{len(still_missing)} 个任务主窗口与 v4 兜底均未返回，保持非终态等待")
        group_out: dict[str, Any] = {"polled": len(tasks), "updated": updated,
                                     "missing": len(still_missing), "trace": trace}
        if fallback:
            group_out["fallback"] = fallback
        return group_out

    def _success_payload(self, feeds: list[Feed]) -> dict[str, Any]:
        """成功时写进 `result` 的形状（**图片**：`data[{url}]` + 水印两版直链）。

        `VideoService` 覆写它（视频要的是 Seedance 的 asset 语义）。
        """
        return {
            "data": [{"url": f.url} for f in feeds],
            "created": int((feeds[0].create_time or time.time() * 1000) / 1000),
            "images": len(feeds),
            "model_id": feeds[0].model_id,
            "width": feeds[0].width,
            "height": feeds[0].height,
            "file_name": feeds[0].file_name,
            "url_no_watermark": [f.url_no_watermark for f in feeds],
        }

    def _apply_feeds(self, task_id: str, feeds: list[Feed]) -> bool:
        """把上游某个 batch 下的 feeds 应用到本地任务。返回是否推进了终态。

        同时服务两种来源：`my/batch` 的 feeds 与 v4 直查的 assets ——
        v4 形态的空 feed_id 不写库，避免把已有值覆盖成空串。

        🔴 **多张任务的收口条件**（2026-09-21 n=2 实测教训）：上游对
        `quantity=2` 真的回 **2 条 feed**，但两条**不是同时就绪** ——
        第一条就绪时第二条可能还在跑。若"见到成功就终态"，第二张就
        白花钱还拿不到。规则：

        · 成功且带 URL 的条数 ≥ 计划张数（plan.quantity）⇒ 终态成功，聚合全部产物；
        · 已有失败终态：有产物 ⇒ 按已有产物成功（部分成功也是可用交付）；
          零产物 ⇒ 终态失败（取第一条失败原因）；
        · 其余（产物未齐、无人失败）⇒ **保持非终态**继续等（超时看门狗兜底）。
        """
        if not feeds:
            return False

        full = self.store.get_full(task_id) or {}
        expected = max(1, int(((full.get("plan") or {}).get("quantity")) or 1))

        success = [f for f in feeds if f.is_succeeded and f.url]
        failed = [f for f in feeds if f.is_failed]

        if len(success) < expected and not failed:
            # 产物未齐且无人失败 ⇒ 继续等（n=2 第一条就绪时的正确姿势）
            status = max((f.status or -1) for f in feeds)
            self.store.update_task(task_id, upstream_status=status)
            return False

        if success:
            # 一个 batch 可能有多张（quantity>1）—— 取**全部成功的**作为产物。
            primary = max(success, key=lambda f: f.create_time or 0)
            if len(success) < expected:
                logger.bind(task_id=task_id).warning(
                    f"部分成功：计划 {expected} 张，上游交付 {len(success)} 张"
                    f"（另有 {len(failed)} 条失败）——按已有产物交付")
            #: 收口增强（成本策略的超分段）：在**写终态前**完成 ——
            #: 调用方看到 succeeded 时 URL 就是最终形态，不存在"先 1K 后换链接"。
            payload = self._enhance_result(
                task_id, full, success, self._success_payload(success))
            self.store.update_task(
                task_id, status=ST_SUCCEEDED,
                **({"upstream_feed_id": primary.feed_id} if primary.feed_id else {}),
                upstream_status=primary.status,
                finished_at=time.time(),
                #: 🔴 `result` 的形状由**管线**决定（图片 data[{url}] / 视频 asset 直链），
                #: 所以走 `self._success_payload` —— 子类只覆写这一个方法即可。
                result=payload,
            )
            OBS.event("task.succeeded", task_id=task_id, produced=len(success))
            #: 计数单位由**管线**决定（图片"张" / 视频"条"）—— 别让视频任务的日志
            #: 说"1 张图"，那会让人以为产物类型判错了。
            logger.bind(task_id=task_id).info(
                f"任务成功：{len(success)} {self.produced_unit}")
            return True

        # 零产物 + 有失败 ⇒ 失败（取第一条失败原因）
        feed = failed[0]
        code, message = feed.failure() or ("task_failed", "上游生成失败。")
        detail = feed.message or ""
        self.store.update_task(
            task_id, status=ST_FAILURE,
            **({"upstream_feed_id": feed.feed_id} if feed.feed_id else {}),
            upstream_status=feed.status, finished_at=time.time(),
            error={"message": message + (f" 上游消息：{detail}" if detail else ""),
                   "type": "upstream_error", "code": code},
        )
        OBS.event("task.failed", task_id=task_id, upstream_status=feed.status,
                  upstream_status_name=status_name(feed.status))
        logger.bind(task_id=task_id).warning(
            f"任务失败：{status_name(feed.status)} {message}")
        return True

    def _enhance_result(self, task_id: str, full: dict[str, Any],
                        feeds: list[Feed], payload: dict[str, Any]) -> dict[str, Any]:
        """收口增强：成本策略的超分段（gpt-image-* 1K 产物 → CI 放大到请求档位）。

        🔴 **任一张失败 ⇒ 全部回退原图**（半超分半原图的混合交付会把
        "哪些 URL 是放大的"变成调用方的猜谜）。回退一定在 `superres.status`
        里留痕 —— 静默回退等于让人按 4K 的预期拿到 1K 还以为成功了。

        ⚠️ 在协调器收口线程里同步执行：CI 同步超分秒级（bfl 实测），
        n≤10 的最坏情形也在预算内；阻塞只推迟本轮 tick，不影响任务终态正确性。
        """
        plan = full.get("plan") or {}
        magnify = plan.get("superres_magnify")
        if not magnify:
            return payload
        if not self.settings.superres_enabled:
            payload["superres"] = {"status": "disabled",
                                   "note": "SUPERRES_ENABLED=0 ⇒ 交付原图"}
            logger.bind(task_id=task_id).warning(
                f"超分被关闭（SUPERRES_ENABLED=0）⇒ 任务按原图交付")
            return payload

        sr = superres.SuperRes.from_settings(
            self.settings, transport=self._http_transport)
        try:
            if not sr.configured():
                reason = "未配置腾讯 CI 凭据/桶（TENCENT_API_KEY + SUPERRES_BUCKET）"
                payload["superres"] = {"status": "fallback", "reason": reason}
                logger.bind(task_id=task_id).warning(f"超分回退：{reason}")
                return payload
            data_out: list[dict[str, Any]] = []
            dims: tuple[int, int] | None = None
            for i, f in enumerate(feeds):
                blob = media.download_image(
                    f.url, max_bytes=self.settings.max_download_bytes,
                    transport=self._http_transport)
                key = f"superres/{task_id}/{i}_x{int(magnify)}.png"
                res = sr.super_resolve(blob.data, int(magnify), object_key=key)
                data_out.append({"url": sr.presigned_get(
                    key, ttl_s=self.settings.superres_url_ttl)})
                if dims is None:
                    dims = (res.width, res.height)
            payload["data"] = data_out
            if dims:
                payload["width"], payload["height"] = dims
            payload["superres"] = {
                "status": "ok", "magnify": int(magnify),
                "requested": plan.get("requested_resolution"),
                "images": len(data_out),
            }
            logger.bind(task_id=task_id).info(
                f"超分完成：{len(data_out)} 张 ×{magnify}（{dims[0]}×{dims[1]}）")
            return payload
        except Exception as e:  # noqa: BLE001 —— 超分失败绝不拖垮已花钱的任务
            reason = f"{type(e).__name__}: {e}"
            payload["superres"] = {"status": "fallback", "reason": reason[:300]}
            logger.bind(task_id=task_id).warning(
                f"超分失败 ⇒ 全部回退原图（上游产物不受影响）：{reason}")
            return payload
        finally:
            sr.close()

    # ------------------------------------------------------------------ 查询

    def get_for_credential(self, task_id: str,
                           credential: str | None) -> dict[str, Any]:
        """按 id 取任务。**带 Key 时必须属于该 Key**；不带 Key 放行（id 即凭据）。

        ⚠️ 刻意**不区分**"不存在"与"不属于你" —— 区分开等于确认 id 存在。
        """
        full = self.store.get_full(task_id)
        if not full:
            raise TaskNotFoundError(f"任务 {task_id} 不存在，或不属于当前 API Key。")
        if credential is not None and full.get("credential_id") != credential:
            raise TaskNotFoundError(f"任务 {task_id} 不存在，或不属于当前 API Key。")
        return full

    def wait_terminal(self, task_id: str, credential: str, *,
                      max_wait: float) -> dict[str, Any]:
        """在 `max_wait` 预算内等待任务到**终态**；超预算返回当时的记录（**不抛错**）。

        🔴 这是同步接口（`POST /v1/images/generations`）的等待实现 ——
        它**只查本地库**，从不直接驱动上游：任务的推进始终由协调器线程做
        （建任务是计费动作，必须走队列与闸门），等待方只是把"调用方原本
        要做的多次轮询"折叠进一个 HTTP 请求里。

        预算耗尽不是错误、是降级信号：调用方按 `view()` 的 202 语义拿
        `task_id` **无缝转异步轮询**；任务不会丢、也不会被取消（上游没有
        取消端点，它本来就在继续跑）。

        前提：有推进者在跑（`Coordinator.running`）—— HTTP 层在进来之前
        已经断言过；没有推进者时等满预算也是白等，那里会快速 503。
        """
        t0 = time.monotonic()
        deadline = t0 + max_wait
        while True:
            full = self.get_for_credential(task_id, credential)
            remaining = deadline - time.monotonic()
            if full["status"] in TERMINAL or remaining <= 0:
                OBS.event("sync.wait_finished", task_id=task_id,
                          status=full["status"],
                          waited_s=round(time.monotonic() - t0, 3),
                          timed_out=full["status"] not in TERMINAL)
                return full
            time.sleep(min(SYNC_WAIT_POLL_INTERVAL, remaining))

    def list_for_credential(self, credential: str, *, limit: int = 50) -> dict[str, Any]:
        """**只列自己的** —— 只按服务过滤会把别人的任务列给你。"""
        rows = self.store.list_for_credential(credential, limit=limit)
        return {"object": "list", "data": [
            {"task_id": r["task_id"], "status": r["status"], "model": r["model"],
             "capability": r["capability"], "created_at": r["created_at"]}
            for r in rows
        ]}

    def delete_for_credential(self, task_id: str, credential: str) -> dict[str, Any]:
        """删除**已终态**的任务。未终态**响亮失败**。"""
        full = self.get_for_credential(task_id, credential)
        if full["status"] not in TERMINAL:
            raise TaskNotDeletable(
                f"任务 {task_id} 状态是 {full['status']!r}，未到终态 ⇒ 不能删。"
                f"⚠️ hailuo **没有取消端点**：本地删掉只会让'它还在上游跑并可能计费'"
                f"变成看不见的事。请等到终态再删。")
        self.store.delete_task(task_id)
        return {"task_id": task_id, "status": "DELETED"}

    # ------------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        return {
            "service": self.settings.otel_service_name,
            "upstream_configured": self.settings.upstream_configured,
            "auth": "jwt-passthrough",
            "credentials": self.credentials.stats(),
            "concurrency_limit": self.settings.hl_concurrency,
            "gate": self.gate.stats(),
            #: 每账号闸门（多账号号池）；键 = 凭据指纹（不可逆哈希，非秘密）
            "gates": self.gates_snapshot(),
            "store": self.store.stats(),
            "capabilities": {**models.status(),
                             "degradations": self._caps_degradations,
                             "refreshed_at": self._caps_refreshed_at},
            "observability": OBS.status(),
            "upload_cache": self._uploader.cache_stats() if self._uploader else None,
        }


# ---------------------------------------------------------------------------
# 响应构造 —— **唯一出口**
# ---------------------------------------------------------------------------


def view(record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """任务记录 → `(HTTP 状态码, 响应体)`。**唯一出口。**

    · 非终态 → **202** + `{task_id, status}`（调用方据此继续轮询）；
    · 成功 → **200** + `{data: [{url}], created, usage}`；
    · 失败 → **200** + `{task_id, status: "failure", error}`。

    ⚠️ **失败也回 200**：任务本身完成了（只是结果是失败），请求没出错。
    回 4xx 会误触发调用方的重试逻辑。

    降级说明**不出现在响应里**（2026-10-10 接口字段收敛）：留痕只进
    **服务日志**（受理时 `降级留痕：…` / 提交时 `归一化留痕：…`，带 task_id）
    与任务库 `degradations_json` —— 调用方（newapi 号池）不消费该字段。
    """
    task_id = record["task_id"]
    status = record["status"]

    if status not in TERMINAL:
        return 202, {"task_id": task_id, "status": status}

    if status == ST_SUCCEEDED:
        result = record.get("result") or {}
        data = result.get("data") or []
        usage: dict[str, Any] = {"images": result.get("images", len(data))}
        plan = record.get("plan") or {}
        forecast = plan.get("forecast_credits")
        if forecast is not None:
            #: 🔴 明说是**预估**：来自上游计价表，不是账单实扣值。
            usage["forecast_credits"] = forecast
        return 200, {
            "data": [{"url": d["url"]} for d in data],
            "created": int(result.get("created") or record.get("finished_at") or 0),
            "usage": usage,
        }

    error = record.get("error") or {
        "message": "任务失败（上游未给出原因）。", "type": "upstream_error",
        "code": "task_failed",
    }
    return 200, {"task_id": task_id, "status": "failure", "error": error}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _nearest(*, resolution_options: tuple[str, ...], target: str) -> str:
    """在一组档位里取最接近 `target` 的。认不出来就取第一个（**并已留痕**）。"""
    order = ["1K", "2K", "4K"]
    if target not in order:
        return resolution_options[0]
    idx = order.index(target)
    best = min(resolution_options, key=lambda r: abs(order.index(r) - idx)
               if r in order else 99)
    return best


def _snap_ratio(value: float, options: tuple[str, ...]) -> str | None:
    """把数值宽高比吸附到最近的支持档位。`Auto` 不参与吸附（它本身不是数值）。"""
    best: tuple[float, str] | None = None
    for opt in options:
        rv = _ratio_value(opt)
        if rv is None:
            continue
        distance = abs(rv - value)
        if best is None or distance < best[0]:
            best = (distance, opt)
    return best[1] if best else None


def _sync_fetch_models(client: Any) -> tuple[list[models.UpstreamModel], list[str]]:
    """同步版能力表拉取（`capabilities.fetch_models` 是异步的，服务是同步的）。"""
    #: 刻意惰性导入（与 `refresh_capabilities` 同一理由：让模块导入期保持轻）。
    from .upstream.hailuo.capabilities import (  # noqa: PLC0415
        COMMON_CONFIG_PATH,
        MODEL_INFO_PATH,
        merge,
        parse_common_config,
        parse_model_info,
    )

    notes: list[str] = []
    meta: dict[str, Any] = {}
    info: dict[str, Any] = {}
    for label, path, parser, sink in (
        ("create_image_models", COMMON_CONFIG_PATH, parse_common_config, meta),
        ("imageModels", MODEL_INFO_PATH, parse_model_info, info),
    ):
        try:
            resp = client.get(path, timeout=12.0)
            resp.raise_for_status()
            sink.update(parser(resp.json()))
        except Exception as e:  # noqa: BLE001
            notes.append(f"上游能力表 {label} 读取失败（{type(e).__name__}）⇒ 该部分退回冻结快照。")
            logger.warning(f"能力表 {label} 读取失败：{type(e).__name__}: {e}")
    return merge(meta, info), notes


__all__ = ["KNOWN_UNSUPPORTED", "Plan", "Service", "view"]
