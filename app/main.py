#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 装配：路由 + 统一错误信封 + lifespan（协调器）+ 埋点接线。

## 对外契约（冻结，见 `docs/INTERFACE.md`）

```
POST   /async/v1/images/generations         受理，只回一个 task_id（202）
GET    /async/v1/images/generations/{id}    非终态 202 / 终态 200 {data, created, usage}
GET    /async/v1/images/generations         本 Key 的任务列表
DELETE /async/v1/images/generations/{id}    删除**已终态**的任务
GET    /async/v1/models                     能力清单（OpenAI 形态 + 上游模型注册表）
```

运维端点（**不属于对外契约**）：`GET /healthz`（零依赖，容器探活用）、
`GET /readyz`、`GET /stats`、`GET /capabilities`。

`/healthz` **既不上报 span、也不留日志** —— 两条通道都从 `observability.PROBE_PATHS` 派生。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Iterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from . import models
from .config import Settings
from .coordinator import Coordinator
from .errors import (
    AdapterError,
    AuthError,
    InvalidParameterError,
    SyncBusy,
    SyncUnavailable,
)
from .observability import OBS, excluded_urls, is_probe_path, setup_logging, should_log_path
from .seedance_api import install_video_routes
from .service import Service, view
from .video_service import VideoService


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class GenerationRequest(BaseModel):
    """`POST /async/v1/images/generations` 的请求体。

    刻意 `extra="allow"`：未知/已知但不支持的字段交给 `Service.build_plan` 统一裁决
    —— 它分得清"**上游没有**"（进 `degradations`）与"**你写错了**"（400）。
    在 schema 层报错就拿不到这个区分。

    🔴 **字段类型刻意宽松**（`Any` / 可空），把严格性全部交给 `Service`：
    schema 层的报错是 Pydantic 的 422，格式固定且**不可执行** ——
    调用方传 `"image": "https://…"`（很常见的写法）会拿到一条机器味儿的
    `Input should be a valid list`，而不是我们那条"请写 `"image": ["…"]`"的提示。
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = Field(
        default=None,
        description="能力名（hailuo-i2i / hailuo-t2i）或上游模型 key（如 nano_banana21_flash）")
    prompt: str | None = Field(default=None, description="提示词")
    image: Any = Field(
        default=None,
        description="输入图 **URL 数组**；文生图传 [] 或省略（图生图重点能力）")
    size: str | None = Field(default=None, description='如 "2048x2048"（本服务换算成档位+比例）')
    n: int | None = Field(default=None, description="出图张数，默认 1")
    resolution: str | None = Field(default=None, description="原生档位（如 1K/2K/4K），按模型校验")
    aspect_ratio: str | None = Field(default=None, description="原生比例（如 1:1/16:9/Auto）")
    quality: str | None = Field(default=None, description="仅 gpt-image-* 支持（low/medium/high…）")
    reference_mode: str | None = Field(
        default=None, description="上游 referenceMode；默认不发送（与抓包一致）")
    seed: int | None = None
    negative_prompt: str | None = None


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def _service(request: Request) -> Service:
    return request.app.state.service


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return raw.strip() or None


def require_key(request: Request) -> str:
    """校验调用方凭据，返回**凭证指纹**（不是明文）。

    ## 唯一的入口鉴权方式：**hailuo JWT 透传**（2026-09-21 起）

    `Authorization: Bearer <你的 hailuo 登录 token>` —— 该 token **同时就是上游凭证**：
    任务全程（建任务/轮询/OSS 上传）都用它 ⇒ **费用记在 token 所有者账上**。
    服务端不持有任何账号凭据，因此**无需也不支持**白名单 key。

    本地只做"结构 + `exp`"校验（**不验签** —— 没有签名密钥）：
    伪造/过期 token 会在上游拿到 401，本地这层是为了早失败、少一次往返。
    """
    service: Service = request.app.state.service
    key = _bearer(request)
    if not key:
        raise AuthError(
            "缺少 Authorization: Bearer <token> —— 请传你的 hailuo 登录 token"
            "（浏览器 F12 里任意请求的 `token` 头 / JWT 形态的那串）。")
    return service.register_credential(key)


def require_key_optional(request: Request) -> str | None:
    """**可选**的调用方 Key —— 只给"按 id 即凭据"的读接口用（GET 单条任务）。

    · **完全没带** ⇒ 返回 `None`，**放行**（`task_id` 是 128 位随机值，
      且只在受理时返回给带 Key 的调用方 ⇒ **id 本身就是凭据**）；
    · **带了但无效** ⇒ **照旧 401**（不能因为"反正放行"就把错的 Key 蒙过去 ——
      那会让调用方的配置错误被静默吞掉，是最难查的一类问题）。
    """
    settings: Settings = request.app.state.settings
    if _bearer(request) is None:
        _ = settings
        return None
    return require_key(request)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None,
               service: Service | None = None) -> FastAPI:
    """装配 FastAPI 应用。

    `service` **可注入**：测试传一个全链路桩化的 `Service`（零真实上游），
    而不是让 `create_app` 自己再建一个会去摸网络的实例。
    生产路径不传，行为不变。
    """
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        app.state.coordinator.start()
        app.state.video_coordinator.start()
        try:
            yield
        finally:
            app.state.video_coordinator.stop()
            app.state.coordinator.stop()
            app.state.video_service.close()
            app.state.service.close()
            # 短命进程必须显式 flush，否则退出时最后一批 span 直接丢
            OBS.flush()
            logger.info("hailuo-service 已停止")

    from . import __version__  # noqa: PLC0415

    app = FastAPI(
        title="hailuo-service",
        version=__version__,
        description="hailuo（hailuoai.video）图片生成的**异步**出口。"
                    "图生图（hailuo-i2i）为重点能力。",
        lifespan=lifespan,
    )

    service = service or Service(settings)
    app.state.settings = settings
    app.state.service = service
    app.state.coordinator = Coordinator(service, settings)
    #: 同步端点的并发护栏：进程内信号量，槽满 429 秒回（见 SyncBusy docstring）。
    #: 挂在 app.state（而非模块级）⇒ 多 app 实例（测试）互不串扰。
    app.state.sync_slots = threading.BoundedSemaphore(settings.sync_max_concurrency)

    #: **视频**链路（火山 Seedance 协议出口）。与图片**同一进程、另一张表**。
    #: 测试里 `service` 是注入的桩 ⇒ 这里跟着用同一套依赖，避免它去摸真实网络。
    video_settings = settings.replace(
        task_timeout=settings.video_task_timeout)
    video_service = VideoService(video_settings, fetch_capabilities=False,
                                 http_transport=getattr(service, "_http_transport", None))
    app.state.video_service = video_service
    app.state.video_coordinator = Coordinator(
        video_service, video_settings,
        task_timeout=settings.video_task_timeout,
        poll_interval=settings.video_poll_interval,
        #: 🔴 两条管线各一把锁 —— 共用 `coordinator` 会让先启动的把另一个永久饿死
        leader_key="coordinator-video")

    if not service.store.ping():
        # 任务库是**事实源**：连不上就别装作能服务。刻意不做"连不上就退回内存"的降级。
        raise RuntimeError(
            f"任务库连不上：{service.store.dsn}。请检查 TASK_DB 与网络连通性。")

    _wire_observability(app, settings)
    _install_error_handlers(app)
    _install_request_logging(app)
    _install_routes(app)
    install_video_routes(app)

    for w in settings.startup_warnings:
        logger.warning(w)
    logger.info("hailuo-service 装配完成 | "
                + json.dumps(service.status(), ensure_ascii=False, default=str))
    return app


def _wire_observability(app: FastAPI, settings: Settings) -> None:
    """logfire + loguru 接线。**失败绝不影响服务启动。**"""
    try:
        OBS.init(settings)
        setup_logging(settings.log_level, obs=OBS)
    except Exception as e:  # noqa: BLE001
        print(f"[observability] 装配失败，已忽略：{type(e).__name__}: {e}")
        setup_logging(settings.log_level, obs=None)
        return

    if not OBS.sdk_configured:
        return
    try:
        import logfire  # noqa: PLC0415

        from .observability import PROBE_PATHS  # noqa: PLC0415

        # `excluded_urls` 由**路径表机械生成**（别手写：它是正则且 logfire 用
        # `re.search` 子串匹配，写 "/" 会命中每一个 URL ⇒ 全站追踪静默关闭）
        url_regex = excluded_urls(PROBE_PATHS)
        logfire.instrument_fastapi(app, excluded_urls=url_regex, capture_headers=False)
        logger.debug(f"探活路径已从 span 中摘除：{url_regex}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"instrument_fastapi 失败，已忽略：{type(e).__name__}: {e}")


def _install_error_handlers(app: FastAPI) -> None:
    """所有 `AdapterError` → 统一错误信封。**HTTP 状态码来自错误类本身。**"""

    @app.exception_handler(AdapterError)
    async def _adapter_error(_r: Request, exc: AdapterError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            # `Retry-After` 是事实：上游说多久就多久。**没说就不给这个头**
            # —— 编一个数字等于伪造它。
            headers["Retry-After"] = str(int(max(1, round(exc.retry_after))))
        return JSONResponse(status_code=exc.status_code, content=exc.to_error(),
                            headers=headers)

    @app.exception_handler(InvalidParameterError)
    async def _invalid(_r: Request, exc: InvalidParameterError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_error())


def _install_request_logging(app: FastAPI) -> None:
    """每个请求一行摘要。**探活路径不打**（与 span 侧同源判据）。"""

    @app.middleware("http")
    async def _log_request(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        path = request.url.path
        quiet = not should_log_path(path) or is_probe_path(path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            if not quiet:
                logger.bind(request_id=rid, http_path=path).exception(
                    f"{request.method} {path} -> 未处理异常")
            raise
        response.headers["X-Request-Id"] = rid
        if not quiet:
            ms = round((time.perf_counter() - started) * 1000, 1)
            logger.bind(request_id=rid, http_path=path,
                        http_status=response.status_code, duration_ms=ms).info(
                f"{request.method} {path} -> {response.status_code} ({ms}ms)")
        return response


def _install_routes(app: FastAPI) -> None:
    # ------------------------------------------------------------- 受理
    @app.post("/async/v1/images/generations", status_code=202)
    async def create_generation(
        request: Request,
        body: GenerationRequest,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """受理一次生成，**只回一个 task_id**。

        请求内**零上游往返**：建任务（计费动作）交给后台协调器，受节奏闸门约束。
        输入图拉取与上传同样在后台 —— 拉取失败会体现为任务 `failure`（附原因），
        而不是让受理请求随上游网络抖动。
        """
        svc: Service = request.app.state.service
        rec = svc.create(body.model_dump(), credential=credential)
        # 叫醒协调器：不然这条任务要等到下一个 tick 才被发现（默认最多白等 1s）。
        request.app.state.coordinator.wake()
        return JSONResponse(
            status_code=202, content={"task_id": rec["task_id"]},
            headers={"Location": f"/async/v1/images/generations/{rec['task_id']}"})

    # ------------------------------------------- 同步生成（创建+轮询合并）
    @app.post("/v1/images/generations")
    def create_generation_sync(
        request: Request,
        body: GenerationRequest,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """**同步**出图：创建 + 轮询合并进一个请求，预算内直接给最终结果。

        ⚠️ 刻意用 `def` 而**不是** `async def`：本函数会阻塞等待至多
        `SYNC_MAX_WAIT` 秒（默认 300）—— 在 `async def` 里这样等会卡死整个
        事件循环（同 worker 的其它请求全部排队）。FastAPI 会把同步端点丢进
        线程池执行，阻塞因而只影响这一个请求。**改动签名前先想清这一点。**

        语义（与异步族共用同一判定，`view()` 仍是唯一出口）：
          · 预算内到终态 → 直接回终态体（成功 200；失败 200 + `status: failure`）；
          · 预算耗尽仍在跑 → **202 + `task_id`** + `Location` 指向异步查询端点
            —— 调用方无缝转异步轮询；任务不会丢、也不会被取消；
          · 没有推进者（协调器线程没在跑）→ **503**（`sync_unavailable`）快速
            失败，而不是让调用方白等 —— 那种部署形态下任务根本不会被推进。

        内部链路与异步受理完全一致：落库 → 叫醒协调器 → 等库里的状态变化。
        等待期间**不发任何上游请求**（推进是协调器线程的职责，这里只读库）。
        """
        settings: Settings = request.app.state.settings
        coordinator = request.app.state.coordinator
        if not coordinator.running:
            raise SyncUnavailable(
                "后台协调器未在运行（COORDINATOR_ENABLED=0？）—— 没有任务推进者，"
                "同步等待无法履行。请改用异步端点 POST /async/v1/images/generations，"
                "或开启协调器后重试。")
        slots: threading.BoundedSemaphore = request.app.state.sync_slots
        if not slots.acquire(blocking=False):
            #: 🔴 性能护栏：槽满 **429 秒回**（带 Retry-After），绝不排队等线程 ——
            #: 同步等待阻塞的是线程池线程，最坏占用被钉死在 SYNC_MAX_CONCURRENCY。
            raise SyncBusy(
                f"同步等待并发已达上限（{settings.sync_max_concurrency}）。"
                "请稍后重试（带 Retry-After），或改用异步端点 "
                "POST /async/v1/images/generations + GET 轮询。",
                retry_after=1.0)
        try:
            svc: Service = request.app.state.service
            rec = svc.create(body.model_dump(), credential=credential)
            coordinator.wake()
            rec = svc.wait_terminal(rec["task_id"], credential,
                                    max_wait=settings.sync_max_wait)
        finally:
            slots.release()
        status_code, payload = view(rec)
        headers: dict[str, str] = {}
        if status_code == 202:
            #: 预算耗尽 ⇒ 把异步轮询的入口直接递给调用方（无缝降级）
            headers["Location"] = f"/async/v1/images/generations/{rec['task_id']}"
        return JSONResponse(status_code=status_code, content=payload, headers=headers)

    # ------------------------------------------------------------- 查询单条
    @app.get("/async/v1/images/generations/{task_id}")
    async def get_generation(
        request: Request,
        task_id: str,
        credential: str | None = Depends(require_key_optional),
    ) -> JSONResponse:
        """查任务。**不需要 Authorization：`task_id` 本身就是凭据。**

        · 非终态 → **202** + `{task_id, status}`；
        · 成功 → **200** + `{data: [{url}], created, usage}`；
        · 失败 → **200** + `{task_id, status: "failure", error}`；
        · 不存在 / 不属于本 Key → **404**（**本地拦，不发上游请求**）。
        """
        svc: Service = request.app.state.service
        rec = svc.get_for_credential(task_id, credential)
        status_code, payload = view(rec)
        return JSONResponse(status_code=status_code, content=payload)

    # ------------------------------------------------------------- 列表 / 删除
    @app.get("/async/v1/images/generations")
    async def list_generations(
        request: Request,
        limit: int = 50,
        credential: str = Depends(require_key),
    ) -> dict:
        """本 Key 名下的任务列表。**只列自己的。**"""
        return request.app.state.service.list_for_credential(
            credential, limit=max(1, min(limit, 200)))

    @app.delete("/async/v1/images/generations/{task_id}")
    async def delete_generation(
        request: Request,
        task_id: str,
        credential: str = Depends(require_key),
    ) -> dict:
        """删除任务。**未终态的任务响亮失败（400）** —— hailuo 没有取消端点，
        本地删掉只会让"还在跑并可能计费"变成看不见的事。"""
        return request.app.state.service.delete_for_credential(task_id, credential)

    # ------------------------------------------------------------- 能力
    @app.get("/async/v1/models")
    async def list_models() -> dict:
        """本服务对外宣告的能力清单。

        = **上游模型注册表**（运行期实读到 11 个图片模型，读不到退回冻结快照）
          + **本服务的能力名**（hailuo-i2i / hailuo-t2i / hailuo-image）。
        """
        return {"object": "list", "data": models.catalog()}

    # ------------------------------------------------------------- 运维
    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针。**零依赖、不触上游、不消耗额度。**"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针：**依赖项不通就报 503**，让编排层不要往这里导流量。

        查真正决定"能不能接活"的事：任务库可连即可。

        ⚠️ **不再检查"服务端 token"** —— 鉴权是透传（凭据随请求来），
        服务端本来就不持有账号凭据；"某个调用方 token 是否有效"是**上游**说了算，
        不该由一个探针替它下结论。
        它比 `/healthz` 贵（会 ping 一次库），所以**不要**拿它当容器 HEALTHCHECK。
        """
        svc: Service = request.app.state.service
        if not svc.store.ping():
            return JSONResponse(status_code=503, content={
                "status": "not_ready", "reason": "任务库不可连", "dsn": svc.store.dsn})
        return JSONResponse(status_code=200, content={
            "status": "ready", "auth": "jwt-passthrough"})

    @app.get("/stats")
    async def stats(request: Request) -> dict:
        """运行状态（闸门 / 任务计数 / 能力表来源 / 观测 / 存储 / 协调器）。"""
        svc: Service = request.app.state.service
        return {**svc.status(),
                "coordinator": request.app.state.coordinator.stats_view()}

    @app.get("/capabilities")
    async def capabilities_route(request: Request) -> dict:
        """能力表详情：每个模型的上限、档位、单价，以及**这份数据有多旧**。"""
        return {
            "source": models.status()["source"],
            "snapshot_date": models.SNAPSHOT_DATE,
            "models": [m.to_public() for m in models.all_models()],
            "deliberate_absences": models.DELIBERATE_ABSENCES,
            "degradations": request.app.state.service.capability_degradations,
        }


app_factory = create_app


if __name__ == "__main__":  # pragma: no cover
    settings = Settings.from_env()
    uvicorn.run("app.main:create_app", factory=True,
                host=settings.host, port=settings.port,
                log_level=settings.log_level.lower())
