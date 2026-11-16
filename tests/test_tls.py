#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板 TLS 与 Cookie 安全属性（U3）。

判据：
1. 证书/私钥必须成对出现，缺一个就**启动失败**（不许半开）；
2. HTTPS 下会话 Cookie 必须带 `Secure`；
3. HTTP 直连（网关未终结 TLS）下仍可用，但启动期必须**告警**。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.main import create_app

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
ADMIN = "adm-tls"


def test_tls_pair_must_be_complete() -> None:
    with pytest.raises(ConfigError):
        Settings(tls_certfile="/tmp/a.pem").validate()
    with pytest.raises(ConfigError):
        Settings(tls_keyfile="/tmp/a.key").validate()
    Settings(tls_certfile="/tmp/a.pem", tls_keyfile="/tmp/a.key").validate()


def test_tls_enabled_property() -> None:
    assert not Settings().tls_enabled
    assert Settings(tls_certfile="c", tls_keyfile="k").tls_enabled


def test_panel_cookie_is_secure_over_https(settings, service) -> None:
    st = settings.replace(admin_token=ADMIN, account_enc_key=KEY)
    app = create_app(st, service=service)
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/panel/api/login", json={"token": ADMIN})
    assert r.status_code == 200, r.text
    cookie = r.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie


def test_panel_cookie_not_secure_over_plain_http(settings, service) -> None:
    st = settings.replace(admin_token=ADMIN, account_enc_key=KEY)
    app = create_app(st, service=service)
    client = TestClient(app, base_url="http://testserver")
    r = client.post("/panel/api/login", json={"token": ADMIN})
    assert r.status_code == 200
    assert "secure" not in r.headers["set-cookie"].lower()


def test_forwarded_proto_marks_cookie_secure(settings, service) -> None:
    """网关终结 TLS 的部署：靠 `X-Forwarded-Proto` 判定，不要求应用自己起 TLS。"""
    st = settings.replace(admin_token=ADMIN, account_enc_key=KEY)
    app = create_app(st, service=service)
    client = TestClient(app, base_url="http://testserver")
    r = client.post("/panel/api/login", json={"token": ADMIN},
                    headers={"X-Forwarded-Proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_warn_when_panel_has_no_tls_and_no_proxy(settings) -> None:
    st = Settings(admin_token=ADMIN, account_enc_key=KEY,
                  task_db="sqlite+pysqlite:///:memory:")
    st.validate()
    joined = " ".join(st.startup_warnings)
    assert "TLS" in joined or "https" in joined.lower()
