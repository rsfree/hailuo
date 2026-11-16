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
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from .accounts import (STATUS_ACTIVE, STATUS_DISABLED, AccountCipher,
                       AccountStore, make_prober)
from .credentials import parse_jwt
from .errors import AdapterError

COOKIE = "hl_panel"
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


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def _sign(secret: str, exp: int) -> str:
    return hmac.new(secret.encode("utf-8"), str(exp).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


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
    def login(body: LoginBody, response: Response) -> dict[str, Any]:
        if not hmac.compare_digest(body.token or "", secret):
            raise HTTPException(status_code=401, detail={
                "error": {"message": "口令不正确", "code": "panel_bad_password"}})
        value, ttl = _issue(secret)
        response.set_cookie(COOKIE, value, max_age=ttl, httponly=True,
                            samesite="lax")
        return {"ok": True, "expires_in": ttl}

    @app.post("/panel/api/logout", include_in_schema=False)
    def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/panel/api/me", include_in_schema=False)
    def me(request: Request) -> dict[str, Any]:
        guard(request)
        return {"ok": True, "pool_mode": bool(settings.service_keys),
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
        return {
            "accounts": accounts.counts(),
            "pool_mode": bool(settings.service_keys),
            "encryption": cipher is not None,
            "service_keys": len(settings.service_keys or ()),
            "tasks": tasks,
        }

    # ------------------------------------------------------------------ 账号

    @app.get("/panel/api/accounts", include_in_schema=False)
    def list_accounts(request: Request) -> dict[str, Any]:
        guard(request)
        return {"accounts": accounts.list()}

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
