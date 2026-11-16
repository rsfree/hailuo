#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具。

## 一套假上游，覆盖**全链路**

`FakeHailuo` 用 `httpx.MockTransport` 把 hailuo 的**每一个**端点都实现成内存桩
（请求凭据、OSS 直传、上传回调、建任务、批量查询、能力表）⇒

🔴 **一个字节都不出网。** 建任务是计费动作，这条红线由夹具保证，
而不是靠"测试里记得别调真接口"。

## 缺依赖就响亮失败，不静默跳过

跳过会让人把"没跑"当成"跑过了"。pytest 里没有 skip 分支。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from itertools import count
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.service import Service
from app.store import TaskStore, VideoTaskStore
from app.video_service import VideoService
from app.upstream.hailuo import upload as up_mod
from app.upstream.hailuo.client import HailuoClient, ST_CREATING, ST_SUCCESS

# ---------------------------------------------------------------------------
# 假上游
# ---------------------------------------------------------------------------

def _make_png(width: int = 1, height: int = 1, *, noise: bool = False) -> bytes:
    """造一张**真** PNG（能过魔数嗅探，也能被 Pillow 打开）。

    `noise=True` 时像素是伪随机的 ⇒ **压不动**，用来测"体积超限要压缩"那条路径
    （纯色图会被 zlib 压到几十字节，测不出压缩行为）。

    ⚠️ 尺寸保持克制（≤ 数百像素）：这是个**真**的图像编码，
    造 5000×2500 会把内存和 CPU 顶到被沙箱杀掉（实测 SIGTERM）。
    """
    import struct
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    rows = []
    if noise:
        # 真伪随机（固定种子 ⇒ 可复现）：PNG 完全压不动，
        # 这样 JPEG 才有优势，才能测出"重编码确实把体积压下来了"。
        import random

        rng = random.Random(20260921)
        body = rng.randbytes(width * height * 3)
        rows = [b"\x00" + body[y * width * 3:(y + 1) * width * 3] for y in range(height)]
    else:
        rows = [b"\x00" + b"\xff\x00\x00" * width for _ in range(height)]
    raw = b"".join(rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


#: 1×1 红色 PNG（真字节，能过魔数嗅探与 Pillow）
#: 🧪 合成 JWT（透传鉴权用）：结构合法 + `exp` 远未来 ⇒ 过本地校验。
#: ⚠️ 它**不是**真 token：测试全程零出网，任何真实调用都不可能发生。
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOjQxMDI0NDQ4MDAsInVzZXIiOnsiaWQiOiIxMDAwMDAwMDAwMDAwMDAwMDEiLCJuYW1lIjoi"
    "dGVzdC11c2VyIiwiZGV2aWNlSUQiOiIxMDAwMDAwMDAwMDAwMDAwMDAifX0."
    "ZmFrZS1zaWduYXR1cmUtZm9yLXRlc3Rz"
)
#: 第二个凭据（断言"按凭据路由"用：两条任务应带各自的 token 出站）
FAKE_JWT_B = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOjQxMDI0NDQ4MDAsInVzZXIiOnsiaWQiOiIxMDAwMDAwMDAwMDAwMDAwMDIiLCJuYW1lIjoi"
    "dGVzdC11c2VyLWIiLCJkZXZpY2VJRCI6IjEwMDAwMDAwMDAwMDAwMDAwMSJ9fQ."
    "ZmFrZS1zaWduYXR1cmUtYi1mb3ItdGVzdHM"
)

PNG_1PX = _make_png()


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={
        "statusInfo": {"code": 0, "httpCode": 0, "message": "成功",
                       "serviceTime": 1789923900, "requestID": "req-fake",
                       "debugInfo": "成功", "serverAlert": 0},
        "data": data,
    })


def _err(code: int, message: str, http: int = 400) -> httpx.Response:
    return httpx.Response(http, json={
        "statusInfo": {"code": code, "httpCode": 0, "message": message,
                       "serviceTime": 1789923900, "requestID": "req-fake",
                       "debugInfo": str(code), "serverAlert": 0}})


class FakeHailuo:
    """内存版 hailuo。

    只模拟**本服务真正用到**的端点 —— 多模拟一个字段，测试就多一分
    "测了假东西"的风险。
    """

    def __init__(self) -> None:
        #: 建任务收到的请求体（测试断言翻译层正确性）
        self.create_calls: list[dict[str, Any]] = []
        #: **视频**建任务收到的请求体（`fileList[].frameType` 断言靠它）
        self.video_create_calls: list[dict[str, Any]] = []
        #: 上传回调收到的请求体
        self.callback_calls: list[dict[str, Any]] = []
        #: 批量查询次数（断言"一轮只打一次上游"）
        self.batch_calls: int = 0
        self.processing_calls: int = 0
        #: v4 点名直查收到的请求体（断言图片/视频两种形态的 body）
        self.v4_calls: list[dict[str, Any]] = []
        self.policy_calls: int = 0
        #: batch_id -> 下一次查询要回的状态
        self.feed_status: dict[str, int] = {}
        #: batch_id -> 产物 URL
        self.feed_url: dict[str, str] = {}
        #: batch_id -> 建任务时的真实元信息（对账要按 prompt/model/时间匹配）
        self.feed_meta: dict[str, dict[str, Any]] = {}
        #: batch_id -> v4 点名直查要回的 asset（与 `my/batch` 窗口**互相独立**）
        self.v4_batches: dict[str, dict[str, Any]] = {}
        #: 🔴 batch_id -> **视频** feed 规格（`my/batch` + `feedTypes=[0]` 形态）。
        #: 形态取自 2026-09-23 的真实视频批次实测：
        #: `metaInfo.videoMetaInfo.mediaInfo.{url,downloadURL,width,height}`。
        self.video_feeds: dict[str, dict[str, Any]] = {}
        #: 输入图 URL -> 字节
        self.images: dict[str, bytes] = {}
        #: 🔴 每次请求携带的 `token` 头（透传路由断言：两条任务须带各自的 token）
        self.tokens_seen: list[str] = []
        #: 强制某个端点返回错误信封
        self.force_error: dict[str, httpx.Response] = {}
        #: 🔴 itertools.count 线程安全 —— 并行 ingest 时 callback/create 并发到达
        self._seq = count(1)
        self._dl_lock = threading.Lock()
        #: 输入图下载并发观测（并行 ingest 测试用）
        self.download_active: int = 0
        self.download_max_concurrency: int = 0

    # ------------------------------------------------------------------ 造数

    def add_image(self, url: str, data: bytes | None = None) -> bytes:
        payload = data if data is not None else PNG_1PX
        self.images[url] = payload
        return payload

    def set_feed(self, batch_id: str, *, status: int = ST_SUCCESS,
                 url: str = "https://cdn.hailuoai.video/fake/out.png?x=1") -> None:
        self.feed_status[batch_id] = status
        self.feed_url[batch_id] = url

    def set_v4(self, batch_id: str, *, status: int = ST_SUCCESS,
               url: str = "https://cdn.hailuoai.video/fake/v4-out.png",
               percent: int | None = None, message: str = "",
               create_time: int = 1789922888112) -> None:
        """登记 v4 点名直查要回的 asset。形态对齐参考实现读过的键：
        `status / downloadURL(字符串直链) / percent / message / createTime`。"""
        self.v4_batches[batch_id] = {
            "batchID": batch_id, "status": status, "downloadURL": url,
            "percent": percent, "message": message, "createTime": create_time,
        }

    def set_video_feed(self, batch_id: str, *, status: int = ST_CREATING,
                       url: str = "", width: int = 1364, height: int = 768,
                       duration_ms: int = 5920) -> None:
        """登记一条**视频** feed（`my/batch` 视频形态，逐字段对齐真实抓包）。"""
        self.video_feeds[batch_id] = {
            "status": status, "url": url, "width": width, "height": height,
            "duration_ms": duration_ms,
        }

    def hide_video_from_batch(self, batch_id: str) -> None:
        """把视频 batch 从 `my/batch` 抽走（测 v4 兜底路径）。"""
        self.video_feeds.pop(batch_id, None)

    def hide_from_batch(self, batch_id: str) -> None:
        """把 batch 从 `my/batch` 窗口抽走（模拟"被账号历史挤出最近 N 条"）。"""
        self.feed_status.pop(batch_id, None)

    def _video_batches(self) -> list[dict[str, Any]]:
        """`my/batch` 的**视频**形态（2026-09-23 真实抓包：产物在 `videoMetaInfo`）。"""
        out: list[dict[str, Any]] = []
        for batch_id, spec in self.video_feeds.items():
            url = spec["url"]
            out.append({
                "batchID": batch_id,
                "batchCreateTime": 1789922888112,
                "feedType": 0,
                "feeds": [{
                    "feedType": 0,
                    "commonInfo": {"id": f"vfeed-{batch_id}", "batchID": batch_id,
                                   "createTime": 1789922888112, "status": spec["status"]},
                    "feedMessage": {"message": ""},
                    "modelParameter": {"videoParameter": {
                        "modelID": "23210", "desc": "push in"}},
                    "metaInfo": {"videoMetaInfo": {
                        "hasVoice": False, "duration": 6,
                        "durationMs": spec["duration_ms"],
                        "mediaInfo": {
                            "url": url,
                            "downloadURL": {"watermarkURL": url,
                                            "withoutWatermarkURL": url,
                                            "fileName": "Hailuo_Video_fake",
                                            "fileID": "99"},
                            "width": spec["width"], "height": spec["height"]}}},
                }],
            })
        return out

    # ------------------------------------------------------------------ 路由

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.tokens_seen.append(request.headers.get("token", ""))
        path = request.url.path

        if path in self.force_error:
            return self.force_error[path]

        # ---- 输入图（给 media.download_image 用）
        #: 支持三种登记方式：完整 URL / 仅 path / 仅主机名
        full = str(request.url)
        for key in (full, path, request.url.host):
            if key in self.images:
                with self._dl_lock:
                    self.download_active += 1
                    self.download_max_concurrency = max(self.download_max_concurrency,
                                                        self.download_active)
                try:
                    time.sleep(0.05)  # 让并行 ingest 的并发可观测（50ms << 测试预算）
                    return httpx.Response(200, content=self.images[key],
                                          headers={"Content-Type": "image/png"})
                finally:
                    with self._dl_lock:
                        self.download_active -= 1

        # ---- 能力表（公开、免鉴权）
        if path == "/public/api/config/web/common_config":
            return httpx.Response(200, json={"data": {"create_image_models": {
                "isI2IUnsupportedList": [],
                "models": [{
                    "modelKey": "Nano Banana 2", "desc": "d", "filterTags": ["edit", "4k"],
                    "modelList": [{"id": "nano_banana21_flash", "type": "t2i",
                                   "mode": "image-reference", "maxSupportImageCount": 14,
                                   "maxPromptLength": 7500, "aid": "nano_banana21_flash",
                                   "disablePromptOptimization": True}],
                }],
            }}})
        if path == "/public/v2/api/multimodal/video/model/info":
            return httpx.Response(200, json={"data": {"videoModels": [], "audioModels": [],
                "imageModels": [{
                    "modelID": "nano_banana21_flash",
                    "parameter": {
                        "resolutions": [{"value": "1K", "defaultSelect": True},
                                        {"value": "2K", "defaultSelect": True},
                                        {"value": "4K", "defaultSelect": True}],
                        "aspectRatios": [{"value": "Auto", "defaultSelect": True},
                                         {"value": "1:1", "defaultSelect": False},
                                         {"value": "16:9", "defaultSelect": False},
                                         {"value": "9:16", "defaultSelect": False},
                                         {"value": "4:3", "defaultSelect": False},
                                         {"value": "3:4", "defaultSelect": False}],
                    },
                    "costs": [{"resolutions": ["1K"], "realCost": 4, "qualities": []},
                              {"resolutions": ["2K"], "realCost": 5, "qualities": []},
                              {"resolutions": ["4K"], "realCost": 8, "qualities": []}],
                    "defaultCost": 0,
                }]}})

        # ---- 上传三段式
        if path == "/v1/api/files/request_policy":
            self.policy_calls += 1
            return _ok({
                "accessKeyId": "STS.FAKE", "accessKeySecret": "secret",
                "securityToken": "tok", "expiration": "2030-01-01T00:00:00Z",
                "dir": "moss/prod/2026-09-21-00/user/multi_chat_file",
                "endpoint": "oss-us-east-1.aliyuncs.com", "bucketName": "hailuo-video",
                "serverTime": "2026-09-20T17:03:24Z",
            })
        if request.url.host.startswith("hailuo-video."):
            return httpx.Response(200)
        if path == "/v1/api/files/policy_callback":
            body = json.loads(request.content or b"{}")
            self.callback_calls.append(body)
            seq = next(self._seq)
            return _ok({
                "fileID": f"5581220000000000{seq:02d}",
                "url": "https://cdn.hailuoai.video/moss/prod/2026-09-21-00/"
                       f"user/multi_chat_file/fake-{seq}.png",
            })

        # ---- 建任务
        if path == "/v2/api/multimodal/generate/image":
            body = json.loads(request.content or b"{}")
            self.create_calls.append(body)
            batch_id = f"55812{next(self._seq):015d}"
            record_id = f"55813{next(self._seq):015d}"
            self.set_feed(batch_id)
            #: 🔴 对账测试依赖真实元信息：假上游若永远回 "a cat"，
            #: 就测不出"按 prompt 精确匹配"这条判据
            param = (body.get("parameter") or {}) if isinstance(body, dict) else {}
            self.feed_meta[batch_id] = {
                "model": str(param.get("modelID") or ""),
                "desc": str(param.get("desc") or ""),
                "create_time": int(time.time() * 1000),
            }
            # 🔴 真实形态（2026-09-21 实测）：`data.id` 是 **feed 记录 id**，
            # `data.task.batchID` 才是轮询用的批次 id —— 两个值必须不同，
            # 否则测试会放过"拿错 id 导致永远查不到任务"这类缺陷。
            return _ok({"id": record_id,
                        "task": {"batchID": batch_id, "videoIDs": [record_id]},
                        "isFirstGenerate": True})

        # ---- 建**视频**任务
        #: 与图片端点同构（同样有 data.id / data.task.batchID 两个 id 的陷阱），
        #: 差别只在响应里的 `videoIDs` 与"默认先回一个非终态"。
        if path == "/v2/api/multimodal/generate/video":
            body = json.loads(request.content or b"{}")
            self.video_create_calls.append(body)
            batch_id = f"66812{next(self._seq):015d}"
            record_id = f"66813{next(self._seq):015d}"
            #: 默认给出**进行中**的 feed（视频是分钟级的，测试自己再改终态）。
            #: 两条路都登记：主路径走 `my/batch`，兜底走 v4（按**记录 id**）。
            self.set_video_feed(batch_id, status=ST_CREATING, url="")
            self.v4_batches[record_id] = {
                "batchID": record_id, "status": ST_CREATING, "downloadURL": "",
                "percent": 11, "message": "", "createTime": 1789922888112}
            return _ok({"id": record_id,
                        "task": {"batchID": batch_id, "videoIDs": [record_id]},
                        "isFirstGenerate": True})

        # ---- 批量查询
        if path == "/api/feed/creation/my/batch":
            self.batch_calls += 1
            _body = json.loads(request.content or b"{}")
            if 0 in (_body.get("feedTypes") or []):
                return _ok({"batchFeeds": self._video_batches(), "processing": False,
                            "hasPre": False, "hasNext": False,
                            "total": len(self.video_feeds)})
            feeds = []
            for batch_id, status in self.feed_status.items():
                meta = self.feed_meta.get(batch_id) or {}
                ct = int(meta.get("create_time") or 1789922888112)
                feeds.append({
                    "batchID": batch_id,
                    "batchCreateTime": ct,
                    "feedType": 1,
                    "feeds": [{
                        "feedType": 1,
                        "commonInfo": {"id": f"feed-{batch_id}", "batchID": batch_id,
                                       "createTime": ct, "status": status,
                                       "humanCheckStatus": 0, "postStatus": 0},
                        "permissionInfo": {"canRetry": False},
                        "feedCoverInfo": {"coverURL": "https://cdn/cover.png",
                                          "width": 4096, "height": 4096},
                        "contentInfo": {"title": ""},
                        "feedMessage": {"message": "", "messageDetail": ""},
                        "feedTags": [],
                        "modelParameter": {"imageParameter": {
                            "modelID": str(meta.get("model") or "nano_banana21_flash"),
                            "desc": str(meta.get("desc") or "a cat"),
                            "fileList": [], "aspectRatio": "Auto", "resolution": "4K"}},
                        "metaInfo": {"imageMetaInfo": {"mediaInfo": {
                            "url": self.feed_url.get(batch_id, ""),
                            "downloadURL": {
                                "watermarkURL": self.feed_url.get(batch_id, ""),
                                "withoutWatermarkURL": self.feed_url.get(batch_id, ""),
                                "fileName": "Hailuo_Image_a cat", "fileID": "1"},
                            "width": 4096, "height": 4096}}},
                    }],
                })
            return _ok({"batchFeeds": feeds, "processing": False, "hasPre": False,
                        "hasNext": False, "total": len(feeds)})

        # ---- v4 按 id 点名直查（图片 batchType=1 / 视频 batchType=0 共用端点）
        if path == "/v4/api/multimodal/video/processing":
            body = json.loads(request.content or b"{}")
            self.v4_calls.append(body)
            batch_videos = []
            for entry in body.get("batchInfoList") or []:
                asset = self.v4_batches.get(str(entry.get("batchID")))
                if asset is None:
                    continue  # 未知 id 在响应里缺席（上游对未知 id 的真实形态未取证）
                batch_videos.append({"batchID": entry.get("batchID"),
                                     "assets": [dict(asset)]})
            return _ok({"batchVideos": batch_videos})

        if path == "/api/feed/creation/my/processing":
            self.processing_calls += 1
            return _ok({"processing": False, "cycleTime": 0, "hasMore": False,
                        "tipType": 0, "batchFeeds": [], "onProcessingImageNum": 0,
                        "onProcessingVideoNum": 0, "onProcessingToolNum": 0,
                        "onProcessingAudioNum": 0})

        return httpx.Response(404, text=f"no fake route for {path}")

    # ------------------------------------------------------------------ 出口

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake() -> FakeHailuo:
    return FakeHailuo()


@pytest.fixture(autouse=True)
def _restore_runtime_models() -> Any:
    """能力表是**进程级全局状态** ⇒ 每个用例后必须还原。

    不还原的话，某个用例"读了一次假上游"会污染后面所有用例
    （它们会看到一个只有 1 个模型的能力表），而且失败点看起来毫不相关。
    """
    from app import models

    from app.upstream.hailuo import video_models as vmodels

    saved = models.runtime_models()
    saved_video = vmodels.runtime_video_models()
    yield
    models.install_runtime_models(saved)
    vmodels.install_runtime_video_models(saved_video)


@pytest.fixture()
def settings(tmp_path: Any) -> Settings:
    """测试用配置：SQLite 落临时目录、**不读真环境变量**。"""
    st = Settings(
        hailuo_token="fake-token-for-tests",
        task_db=f"sqlite+pysqlite:///{tmp_path}/test.db",
        coordinator_enabled=False,   # 测试直接调 tick()，不起线程
        #: 轮询间隔/租约必须是正数（`validate()` 会拦 0）—— 取极小值让测试瞬时完成
        hailuo_poll_interval=0.01,
        coordinator_lease=0.02,
        #: 起轮宽限期归零 —— 否则刚提交的任务会被"宽限"挡掉，tick 行为不确定。
        #: 宽限期本身的语义由 `test_poll_grace_defers_just_submitted_tasks` 单独验证。
        poll_grace=0.0,
        hl_min_interval=0.0,
        hl_per_minute=0,
    )
    st.validate()
    return st


@pytest.fixture()
def store(settings: Settings) -> TaskStore:
    return TaskStore(settings.db_target)


@pytest.fixture()
def service(settings: Settings, store: TaskStore, fake: FakeHailuo) -> Service:
    """**全链路桩化的 Service**：零真实上游调用。"""
    client = HailuoClient(token="fake-token-for-tests",
                          base_url="https://hailuoai.video",
                          device=settings.device_profile(),
                          transport=fake.transport())
    oss = httpx.Client(transport=fake.transport(), timeout=10.0)
    uploader = up_mod.Uploader(client=client, oss_client=oss, cache_ttl=60.0)
    svc = Service(settings, store=store, client=client, uploader=uploader,
                  fetch_capabilities=False, http_transport=fake.transport())
    # 能力表：喂假上游读到的数据（等价于运行期实读）
    svc.refresh_capabilities()
    #: 透传凭据：API 测试默认发 `Bearer FAKE_JWT`（入口鉴权会再登记一次，幂等）
    svc.register_credential(FAKE_JWT)
    return svc


@pytest.fixture()
def video_service(settings: Settings, fake: FakeHailuo) -> VideoService:
    """**全链路桩化的 VideoService**：零真实上游调用，落在**视频**那张表上。"""
    client = HailuoClient(token="fake-token-for-tests",
                          base_url="https://hailuoai.video",
                          device=settings.device_profile(),
                          transport=fake.transport())
    oss = httpx.Client(transport=fake.transport(), timeout=10.0)
    uploader = up_mod.Uploader(client=client, oss_client=oss, cache_ttl=60.0)
    svc = VideoService(settings, store=VideoTaskStore(settings.db_target),
                       client=client, uploader=uploader,
                       fetch_capabilities=False, http_transport=fake.transport())
    svc.register_credential(FAKE_JWT)
    return svc


@pytest.fixture()
def client(settings: Settings, service: Service) -> Any:
    """FastAPI TestClient（**不跑 lifespan**，避免起协调器线程）。

    `service` 显式注入 ⇒ `create_app` 不会再建一个会去摸真网络的实例。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, service=service)
    return TestClient(app)


@pytest.fixture()
def video_client(settings: Settings, service: Service,
                 video_service: VideoService) -> Any:
    """`TestClient` + 注入的 `video_service`（**不跑 lifespan**，不起协调器线程）。

    注入方式：按常规装配（图片服务用 `service` 夹具），再把
    `app.state.video_service` 换成视频那一份 —— 夹具里的 `fake` 是断言的入口
    （"发出去的 body 长什么样"）。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, service=service)
    app.state.video_service = video_service
    return TestClient(app)


@pytest.fixture()
def live_client(settings: Settings, service: Service) -> Any:
    """带 lifespan 的 TestClient —— 给"协调器真的会被启停"这类用例用。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings.replace(coordinator_enabled=True,
                                     coordinator_tick=0.05), service=service)
    with TestClient(app) as c:
        yield c


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = ["FakeHailuo", "PNG_1PX", "sha256"]
