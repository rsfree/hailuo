#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理面板：号池的增删改查 + 零成本巡检 + 概览。

## 挂载判据

`ADMIN_TOKEN` 为空 ⇒ **不挂任何 /panel 路由**（404）。
不做"默认口令"或"开放面板"——那等于给号池留一扇没锁的门。

## 会话

登录换一枚 HMAC 签名 Cookie（`hl_panel=<exp>.<sig>`），HttpOnly + SameSite=Lax，
TTL 12h。签名密钥就是 `ADMIN_TOKEN` 本身，因此**改口令即全端下线**。

## 为什么巡检是免费的

`my/processing` 只回答"这个账号有没有在跑的任务"，不建任务、不计费
（`HailuoClient.probe_token`）。号池巡检绝不能因为"顺手验证一下"就把积分花掉。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from . import __version__ as _VERSION
from . import models as models_mod
from .accounts import (STATUS_ACTIVE, STATUS_DISABLED, AccountCipher,
                       AccountStore, make_prober)
from .credentials import parse_jwt
from .errors import AdapterError

COOKIE = "hl_panel"
#: 别名读-改-写的串行化。单 worker 但 FastAPI 把同步端点丢线程池执行，
#: 两个面板标签页同时保存会互相覆盖 —— 锁的代价远低于"配置莫名少一条"。
_ALIAS_LOCK = threading.Lock()
SESSION_TTL = 12 * 3600
_ASSETS = Path(__file__).resolve().parent / "panel"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class LoginBody(BaseModel):
    token: str = ""


class AccountBody(BaseModel):
    token: str
    label: str = ""
    note: str = ""
    weight: int = Field(default=1, ge=1, le=100)


class ImportBody(BaseModel):
    text: str


class StatusBody(BaseModel):
    status: str


class AliasBody(BaseModel):
    """一条「客户端请求名 → 上游模型」映射。"""

    alias: str
    target: str


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def _sign(secret: str, exp: int) -> str:
    return hmac.new(secret.encode("utf-8"), str(exp).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _is_https(request: Request) -> bool:
    """是否走的 HTTPS：直连看 scheme，网关终结看 `X-Forwarded-Proto`。

    只认这一处：Cookie 的 `Secure` 属性由它决定，散落各处必然漏。
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    return request.url.scheme == "https" or proto.strip().lower() == "https"


def _issue(secret: str) -> tuple[str, int]:
    exp = int(time.time()) + SESSION_TTL
    return f"{exp}.{_sign(secret, exp)}", SESSION_TTL


def _verify(secret: str, value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp_s, sig = value.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp <= time.time():
        return False
    return hmac.compare_digest(sig, _sign(secret, exp))


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def install_panel(app: FastAPI, *, settings: Any, service: Any,
                  accounts: AccountStore, cipher: AccountCipher | None) -> bool:
    """挂载面板。返回是否真的挂上了（`ADMIN_TOKEN` 为空时 False）。"""
    secret = (settings.admin_token or "").strip()
    if not secret:
        return False

    prober = getattr(app.state, "account_prober", None) or make_prober(
        settings, getattr(service, "http_transport", None))

    def guard(request: Request) -> None:
        if not _verify(secret, request.cookies.get(COOKIE)):
            raise HTTPException(status_code=401, detail={
                "error": {"message": "未登录或会话已过期", "code": "panel_unauthorized"}})

    def asset(name: str, media: str) -> Response:
        path = _ASSETS / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"缺少静态资源 {name}")
        return Response(path.read_text(encoding="utf-8"), media_type=media)

    # ------------------------------------------------------------------ 页面

    @app.get("/panel", include_in_schema=False)
    def panel_index() -> Response:
        return asset("index.html", "text/html; charset=utf-8")

    @app.get("/panel/app.js", include_in_schema=False)
    def panel_js() -> Response:
        return asset("app.js", "application/javascript; charset=utf-8")

    @app.get("/panel/style.css", include_in_schema=False)
    def panel_css() -> Response:
        return asset("style.css", "text/css; charset=utf-8")

    # ------------------------------------------------------------------ 会话

    @app.post("/panel/api/login", include_in_schema=False)
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not hmac.compare_digest(body.token or "", secret):
            raise HTTPException(status_code=401, detail={
                "error": {"message": "口令不正确", "code": "panel_bad_password"}})
        value, ttl = _issue(secret)
        response.set_cookie(COOKIE, value, max_age=ttl, httponly=True,
                            samesite="lax", secure=_is_https(request))
        return {"ok": True, "expires_in": ttl}

    @app.post("/panel/api/logout", include_in_schema=False)
    def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/panel/api/me", include_in_schema=False)
    def me(request: Request) -> dict[str, Any]:
        guard(request)
        return {"ok": True, "pool_mode": bool(settings.service_keys),
                "version": _VERSION,
                "panel": {"ttl": SESSION_TTL}}

    # ------------------------------------------------------------------ 概览

    @app.get("/panel/api/overview", include_in_schema=False)
    def overview(request: Request) -> dict[str, Any]:
        guard(request)
        tasks: dict[str, Any] = {}
        try:
            tasks = service.store.counts()   # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"任务计数不可用（忽略）：{e}")
        queue = active = 0
        today_requests = 0
        try:
            store = service.store   # type: ignore[attr-defined]
            queue = store.count_queued()
            active = store.count_active()
            today_requests = int(store.usage_series(days=1).get("total", 0))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"观测数据不可用（忽略）：{e}")
        return {
            "accounts": accounts.counts(),
            "pool_mode": bool(settings.service_keys),
            "encryption": cipher is not None,
            "service_keys": len(settings.service_keys or ()),
            "tasks": tasks,
            "queue": queue,
            "active": active,
            "today_requests": today_requests,
            "concurrency_limit": getattr(settings, "hl_concurrency", None),
            "version": _VERSION,
        }

    # ------------------------------------------------------------------ 观测

    @app.get("/panel/api/tasks", include_in_schema=False)
    def list_tasks(request: Request, limit: int = 50, offset: int = 0,
                   status: str = "", model: str = "", q: str = "",
                   days: int = 0) -> dict[str, Any]:
        """任务列表（分页 + 状态/模型/关键词/时间筛选）。`days=0` = 不限时间。"""
        guard(request)
        store = service.store   # type: ignore[attr-defined]
        since = time.time() - days * 86400 if days > 0 else None
        page = store.recent_tasks(limit=limit, offset=offset, status=status,
                                  model=model, q=q, since=since)
        return {**page, "facets": store.faceted_counts(since=since)}

    @app.get("/panel/api/tasks/{task_id}", include_in_schema=False)
    def task_detail(task_id: str, request: Request) -> dict[str, Any]:
        """单任务全量（请求 / 计划 / 结果 / 错误 / 降级）。

        这是面板里**唯一**会吐出请求体的接口 —— 它不是列表，误刷不会批量泄露。
        """
        guard(request)
        rec = service.store.get_full(task_id)   # type: ignore[attr-defined]
        if rec is None:
            raise HTTPException(status_code=404, detail={
                "error": {"message": "任务不存在", "code": "task_not_found"}})
        return {"task": rec}

    @app.get("/panel/api/models", include_in_schema=False)
    def list_models(request: Request) -> dict[str, Any]:
        """能力表：每个上游模型的档位/上限/单价，以及**刻意不做**的能力。

        与公网 `/async/v1/models` 的区别：这里带单价、快照来源与缺席清单，
        是给运维看的；那边是给调用方看的。
        """
        guard(request)
        st = models_mod.status()
        return {
            **st,
            "models": [m.to_public() for m in models_mod.all_models()],
            "deliberate_absences": models_mod.DELIBERATE_ABSENCES,
            "degradations": getattr(service, "capability_degradations", []),
        }

    # -------------------------------------------------------------- 模型别名

    def _alias_snapshot() -> dict[str, Any]:
        """别名全量视图：运行期 + 内置，逐条标来源与是否被遮蔽。"""
        builtin = dict(models_mod.MODEL_ALIASES)
        runtime = models_mod.runtime_aliases()
        items = []
        for alias in sorted(set(builtin) | set(runtime)):
            if alias in runtime:
                items.append({"alias": alias, "target": runtime[alias],
                              "source": "runtime",
                              "shadowed": alias in builtin})
            else:
                items.append({"alias": alias, "target": builtin[alias],
                              "source": "builtin", "shadowed": False})
        return {
            "items": items,
            "builtin": builtin,
            "runtime": runtime,
            "targets": [m.model_id for m in models_mod.all_models()],
            "reserved": sorted(
                [c.name for c in models_mod.CAPABILITIES]
                + [a for c in models_mod.CAPABILITIES for a in c.aliases]),
            "meta_key": models_mod.ALIASES_META_KEY,
        }

    @app.get("/panel/api/aliases", include_in_schema=False)
    def list_aliases(request: Request) -> dict[str, Any]:
        """模型别名：`{客户端请求名: 上游 modelID}`。"""
        guard(request)
        return _alias_snapshot()

    @app.post("/panel/api/aliases", include_in_schema=False)
    def put_alias(body: AliasBody, request: Request) -> dict[str, Any]:
        """新增或改写一条别名。校验在**写入前**完成，失败不留半条配置。"""
        guard(request)
        alias = (body.alias or "").strip()
        target = (body.target or "").strip()

        if not alias:
            raise HTTPException(400, detail={"error": {
                "message": "客户端请求名不能为空。", "code": "invalid_alias"}})
        if len(alias) > 64:
            raise HTTPException(400, detail={"error": {
                "message": f"客户端请求名最长 64 字符，实得 {len(alias)}。",
                "code": "invalid_alias"}})
        if any(ch.isspace() for ch in alias) or any(ord(ch) < 32 for ch in alias):
            raise HTTPException(400, detail={"error": {
                "message": f"客户端请求名不能含空白或控制字符：{alias!r}。",
                "code": "invalid_alias"}})

        #: 🔴 能力名/能力别名**不能**当别名 —— `resolve_capability` 先于模型解析执行，
        #: 配了也永远不会命中，属于"静默无效配置"，必须在写入时挡掉。
        reserved = {c.name.lower() for c in models_mod.CAPABILITIES}
        reserved |= {a.lower() for c in models_mod.CAPABILITIES for a in c.aliases}
        if alias.lower() in reserved:
            raise HTTPException(400, detail={"error": {
                "message": f"{alias!r} 是能力名/能力别名（决定图生图还是文生图路由），"
                           f"不能用作模型别名 —— 配了也不会生效。",
                "code": "alias_reserved"}})

        #: 同值判断放在"目标是否存在"**之前**：`same → same` 的问题是"没有信息量"，
        #: 与目标在不在能力表无关；先报 unknown_target 会把人引向错误的修法。
        if target == alias:
            raise HTTPException(400, detail={"error": {
                "message": f"目标与请求名相同（{alias!r}），这条映射没有意义。",
                "code": "alias_identity"}})

        known = {m.model_id for m in models_mod.all_models()}
        if target not in known:
            raise HTTPException(400, detail={"error": {
                "message": f"目标模型 {target!r} 不在当前能力表里。"
                           f"可用：{', '.join(sorted(known))}。",
                "code": "unknown_target"}})

        store = service.store   # type: ignore[attr-defined]
        with _ALIAS_LOCK:
            current = models_mod.runtime_aliases()
            current[alias] = target
            store.set_meta(models_mod.ALIASES_META_KEY,
                           json.dumps(current, ensure_ascii=False, sort_keys=True))
            models_mod.install_runtime_aliases(current)
        logger.info(f"面板设置别名 {alias!r} → {target}")
        return {"ok": True, **_alias_snapshot()}

    @app.delete("/panel/api/aliases/{alias}", include_in_schema=False)
    def drop_alias(alias: str, request: Request) -> dict[str, Any]:
        """删除一条**运行期**别名（内置条目不可删，只会回落到内置值）。"""
        guard(request)
        store = service.store   # type: ignore[attr-defined]
        with _ALIAS_LOCK:
            current = models_mod.runtime_aliases()
            if alias not in current:
                if alias in models_mod.MODEL_ALIASES:
                    raise HTTPException(400, detail={"error": {
                        "message": f"{alias!r} 是**内置**别名（写死在 app/models.py），"
                                   f"面板不能删。要改语义请给它配一条同名别名覆盖。",
                        "code": "alias_builtin"}})
                raise HTTPException(404, detail={"error": {
                    "message": f"没有运行期别名 {alias!r}。", "code": "alias_not_found"}})
            del current[alias]
            store.set_meta(models_mod.ALIASES_META_KEY,
                           json.dumps(current, ensure_ascii=False, sort_keys=True))
            models_mod.install_runtime_aliases(current)
        logger.info(f"面板删除别名 {alias!r}")
        return {"ok": True, **_alias_snapshot()}

    @app.get("/panel/api/usage", include_in_schema=False)
    def usage(request: Request, days: int = 7) -> dict[str, Any]:
        """用量与质量：受理序列 / 任务序列 / 耗时分布 / 失败原因 Top N。"""
        guard(request)
        store = service.store   # type: ignore[attr-defined]
        days = max(1, min(int(days), 90))
        since = time.time() - days * 86400
        return {
            "usage": store.usage_series(days=days),
            "daily": store.daily_series(days=days),
            "latency": store.latency_summary(since=since),
            "failed_reasons": store.failure_reasons(since=since),
        }

    @app.get("/panel/api/system", include_in_schema=False)
    def system_status(request: Request) -> dict[str, Any]:
        """运行状态：闸门、并发、协调器租约、存储、能力表快照。

        复用 `/stats` 的同一份数据源，区别只是**这里要登录**（`/stats` 公网挡在入口层）。
        """
        guard(request)
        svc = service
        coord: dict[str, Any] = {}
        try:
            coord = request.app.state.coordinator.stats_view()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"协调器状态不可用（忽略）：{e}")
        queue = active = 0
        try:
            queue = svc.store.count_queued()
            active = svc.store.count_active()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"任务计数不可用（忽略）：{e}")
        return {
            "version": _VERSION,
            "status": svc.status(),
            "coordinator": coord,
            "queue": queue,
            "active": active,
        }

    # ------------------------------------------------------------------ 账号

    @app.get("/panel/api/accounts", include_in_schema=False)
    def list_accounts(request: Request) -> dict[str, Any]:
        guard(request)
        #: `use_count` 是**取号次数**（每次鉴权都 +1，含读列表）；真正"出了几张图"
        #: 要按任务表算。两者混在一个数字里会让人以为"没调用却消耗了额度"。
        generated = service.store.succeeded_counts_by_credential()
        rows = accounts.list()
        for r in rows:
            r["generations"] = generated.get(r["fp"], 0)
        return {"accounts": rows}

    @app.post("/panel/api/reconcile", include_in_schema=False)
    def reconcile_now(request: Request) -> dict[str, Any]:
        """手动触发一次上游对账（运维用）。

        自动对账由协调器按 `RECONCILE_INTERVAL` 跑；这个按钮用于"刚怀疑丢单、
        不想等下一轮"。它只读上游 + 必要时回写本地，**不会新建任何上游任务**。
        """
        guard(request)
        return {"stats": service.reconcile()}

    @app.post("/panel/api/accounts", include_in_schema=False)
    def create_account(body: AccountBody, request: Request) -> dict[str, Any]:
        guard(request)
        if cipher is None:
            raise HTTPException(status_code=503, detail={
                "error": {"message": "未配置 ACCOUNT_ENC_KEY，无法加密存储账号",
                          "code": "account_encryption_unavailable"}})
        try:
            claims = parse_jwt(body.token)
        except AdapterError as e:
            raise HTTPException(status_code=400, detail={
                "error": {"message": str(e), "code": "invalid_jwt"}}) from e
        rec = accounts.add(token=body.token, claims=claims, cipher=cipher,
                           label=body.label, note=body.note, weight=body.weight)
        logger.info(f"面板新增/更新账号 {rec['id']}（{rec['label']}）")
        return {"account": rec}

    @app.post("/panel/api/accounts/import", include_in_schema=False)
    def import_accounts(body: ImportBody, request: Request) -> dict[str, Any]:
        guard(request)
        if cipher is None:
            raise HTTPException(status_code=503, detail={
                "error": {"message": "未配置 ACCOUNT_ENC_KEY，无法加密存储账号",
                          "code": "account_encryption_unavailable"}})
        results: list[dict[str, Any]] = []
        for raw in (body.text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            label, token = ("", line) if len(parts) == 1 else (parts[0], parts[-1])
            try:
                claims = parse_jwt(token)
                rec = accounts.add(token=token, claims=claims, cipher=cipher,
                                   label=label)
                results.append({"ok": True, "id": rec["id"],
                                "label": rec["label"], "subject": rec["subject"]})
            except AdapterError as e:
                results.append({"ok": False, "line": line[:32], "error": str(e)})
        ok = sum(1 for r in results if r["ok"])
        logger.info(f"面板批量导入：成功 {ok} / 共 {len(results)} 行")
        return {"results": results, "ok_count": ok, "total": len(results)}

    @app.post("/panel/api/accounts/{account_id}/status", include_in_schema=False)
    def set_status(account_id: str, body: StatusBody, request: Request) -> dict[str, Any]:
        guard(request)
        if body.status not in (STATUS_ACTIVE, STATUS_DISABLED):
            raise HTTPException(status_code=400, detail={
                "error": {"message": "status 只接受 active / disabled（dead 由巡检判定）",
                          "code": "invalid_status"}})
        rec = accounts.set_status(account_id, body.status)
        if rec is None:
            raise HTTPException(status_code=404, detail={
                "error": {"message": "账号不存在", "code": "account_not_found"}})
        return {"account": rec}

    @app.delete("/panel/api/accounts/{account_id}", include_in_schema=False)
    def remove_account(account_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        if not accounts.remove(account_id):
            raise HTTPException(status_code=404, detail={
                "error": {"message": "账号不存在", "code": "account_not_found"}})
        return {"ok": True}

    @app.post("/panel/api/accounts/{account_id}/check", include_in_schema=False)
    def check_account(account_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        rec = accounts.get(account_id)
        if rec is None:
            raise HTTPException(status_code=404, detail={
                "error": {"message": "账号不存在", "code": "account_not_found"}})
        if cipher is None:
            raise HTTPException(status_code=503, detail={
                "error": {"message": "未配置 ACCOUNT_ENC_KEY", "code": "no_cipher"}})
        token = accounts.token_of(account_id, cipher)
        if not token:
            raise HTTPException(status_code=503, detail={
                "error": {"message": "密文无法解密", "code": "no_cipher"}})
        ok, fatal, detail = prober(token)
        updated = accounts.record_check(account_id, ok=ok, detail=detail, fatal=fatal)
        return {"ok": ok, "detail": detail, "account": updated}

    @app.post("/panel/api/accounts/check_all", include_in_schema=False)
    def check_all(request: Request) -> dict[str, Any]:
        guard(request)
        if cipher is None:
            raise HTTPException(status_code=503, detail={
                "error": {"message": "未配置 ACCOUNT_ENC_KEY", "code": "no_cipher"}})
        results = []
        for rec in accounts.list()[:50]:
            token = accounts.token_of(rec["id"], cipher)
            if not token:
                results.append({"id": rec["id"], "ok": False, "detail": "密文无法解密"})
                continue
            ok, fatal, detail = prober(token)
            accounts.record_check(rec["id"], ok=ok, detail=detail, fatal=fatal)
            results.append({"id": rec["id"], "ok": ok, "detail": detail})
        return {"results": results,
                "ok_count": sum(1 for r in results if r["ok"]),
                "total": len(results)}

    logger.info(f"管理面板已挂载：/panel（巡检={'零成本'}）")
    return True


def panel_disabled_response() -> JSONResponse:
    """未挂载时的统一 404 形状（`create_app` 里对 /panel 前缀兜底用）。"""
    return JSONResponse(status_code=404, content={
        "error": {"message": "管理面板未启用（ADMIN_TOKEN 未配置）",
                  "code": "panel_disabled"}})


__all__ = ["COOKIE", "SESSION_TTL", "install_panel", "panel_disabled_response"]
