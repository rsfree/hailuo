#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""火山方舟 **Seedance 原生协议**端点。

```
POST   /api/v3/contents/generations/tasks        → {"id": "cgt-…"}（**只有 id**）
GET    /api/v3/contents/generations/tasks/{id}   → 原生任务对象（六态 status）
GET    /api/v3/contents/generations/tasks        → {items, total, page_num, page_size}
DELETE /api/v3/contents/generations/tasks/{id}   → 取消（仅 queued）/ 删除
GET    /v1/models                                → OpenAI 形态的模型清单
```

## 路径逐字等于原生

不加前缀、不加路径段 —— 调用方只改 `Base URL` 与 `API Key` 就能切过来。

## 鉴权（这一处与"GET 全免鉴"的要求**有出入**，刻意如此）

- **写**（`POST` / `DELETE`）⇒ 需要 `Authorization: Bearer <hailuo 登录 JWT>`（透传）；
- **`GET` 单条** ⇒ **免鉴权**：`cgt-` 形态的 id 本身就是凭据（随机段 10 位十六进制），
  与图片链路"id 即凭据"同一姿势；
- **`GET` 列表** ⇒ **默认要鉴权**（`VIDEO_LIST_REQUIRE_AUTH=1`）。

为什么列表不跟着免：单条查询要靠"猜中 id"，列表则会把**所有**任务的 id 一股脑
交给任何未带凭据的人 —— 而 id 正是读接口的凭据，等于把凭据派发出去了。
真要开放就显式设 `VIDEO_LIST_REQUIRE_AUTH=0`（带凭据时仍只列自己的）。
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import AuthError
from .video_service import VideoService, video_view

#: 列表端点一次最多返回多少条（原生 `page_size` 上限就是 100）
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return raw.strip() or None


def require_video_key(request: Request) -> str:
    """**写**接口与（默认）列表接口的鉴权：hailuo JWT 透传。"""
    service: VideoService = request.app.state.video_service
    key = _bearer(request)
    if not key:
        raise AuthError(
            "缺少 Authorization: Bearer <token> —— 请传你的 hailuo 登录 token"
            "（浏览器 F12 里任意请求的 `token` 头 / JWT 形态的那串）。")
    return service.register_credential(key)


def optional_video_key(request: Request) -> str | None:
    """**可选**凭据：带了就校验（错了照旧 401），没带返回 `None`。"""
    if _bearer(request) is None:
        return None
    return require_video_key(request)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def install_video_routes(app: FastAPI) -> None:
    """把 Seedance 端点挂到应用上。**与图片路由互不干扰。**"""

    @app.post("/api/v3/contents/generations/tasks")
    async def create_video_task(
        request: Request,
        body: dict[str, Any],
        credential: str = Depends(require_video_key),
    ) -> JSONResponse:
        """受理一次视频生成，**只回一个 id**。

        请求内**零上游往返**：建任务（计费动作）交给后台协调器，受节奏闸门约束；
        框架图的下载与上传也在后台 ⇒ 网络抖动体现为任务 `failed`（附原因），
        而不是让受理请求跟着一起抖。
        """
        svc: VideoService = request.app.state.video_service
        rec = svc.create_video(body, credential=credential)
        request.app.state.video_coordinator.wake()
        return JSONResponse(
            status_code=200, content={"id": rec["task_id"]},
            headers={"Location": f"/api/v3/contents/generations/tasks/{rec['task_id']}"})

    @app.get("/api/v3/contents/generations/tasks/{task_id}")
    async def get_video_task(request: Request, task_id: str) -> JSONResponse:
        """查任务。**免鉴权** —— `cgt-` 形态的 id 本身就是凭据。"""
        svc: VideoService = request.app.state.video_service
        rec = svc.get_for_credential(task_id, None)
        status_code, payload = video_view(rec)
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/api/v3/contents/generations/tasks")
    async def list_video_tasks(
        request: Request,
        page_num: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        credential: str | None = Depends(optional_video_key),
    ) -> dict[str, Any]:
        """任务列表。**默认要凭据**；不带凭据且 `VIDEO_LIST_REQUIRE_AUTH=0` ⇒ 列全部。"""
        svc: VideoService = request.app.state.video_service
        if credential is None and svc.settings.video_list_require_auth:
            raise AuthError(
                "列表端点需要 Authorization: Bearer <hailuo token>。"
                "（它会列出任务 id，而 id 就是读接口的凭据 ⇒ 无凭据开放等于派发凭据；"
                "确要开放请设 VIDEO_LIST_REQUIRE_AUTH=0。）")
        limit = max(1, min(page_size, MAX_PAGE_SIZE))
        rows = svc.store.list_for_credential(credential or "", limit=limit)
        #: 列表里的每一项**与查询同形**（原生语义），只是不带产物细节。
        items = []
        for r in rows:
            _code, payload = video_view({
                "task_id": r["task_id"], "status": r["status"],
                "request": {}, "plan": {},
                "result": {}, "error": {},
                "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
            })
            items.append(payload)
        return {"items": items, "total": len(items),
                "page_num": max(1, page_num), "page_size": limit}

    @app.delete("/api/v3/contents/generations/tasks/{task_id}")
    async def delete_video_task(
        request: Request,
        task_id: str,
        credential: str = Depends(require_video_key),
    ) -> dict[str, Any]:
        """删除**已终态**的任务。未终态**响亮失败（400）**。

        ⚠️ hailuo **没有取消端点**：上游没有"把 queued 的任务撤下来"的接口，
        本地删掉只会让"它还在跑并可能计费"变成看不见的事。
        """
        svc: VideoService = request.app.state.video_service
        return svc.delete_for_credential(task_id, credential)

    @app.get("/v1/models")
    async def list_models_openai(request: Request) -> dict[str, Any]:
        """**OpenAI 兼容**的模型清单（`GET /v1/models`）。**免鉴权**。

        = 图片能力表（`/async/v1/models` 那一套）+ 视频模型与能力名。
        `object/created/owned_by` 是 OpenAI 的字段集；`hailuo` 块是本服务的扩展
        （族、槽位、档位、单价），OpenAI 客户端会忽略它。
        """
        from . import models  # noqa: PLC0415

        from .upstream.hailuo import video_models as vmodels  # noqa: PLC0415

        data: list[dict[str, Any]] = []
        for item in models.catalog():
            data.append({
                "id": item["id"], "object": "model", "created": 0,
                "owned_by": str(item.get("owned_by") or "hailuo"),
                "hailuo": item,
            })
        for item in vmodels.video_catalog():
            data.append({
                "id": item["id"], "object": "model", "created": 0,
                "owned_by": "hailuo", "hailuo": item,
            })
        return {"object": "list", "data": data}


__all__ = ["install_video_routes"]
