#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二期：服务 Key 从号池取号，透传模式不回归。

判据来源：`.fixloop/criteria.md` R7–R9。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.service import Service
from app.store import TaskStore
from tests.conftest import FAKE_JWT, FAKE_JWT_B

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
SERVICE_KEY = "sk-hailuo-test"
BODY = {"model": "nano_banana21_flash", "prompt": "a red cube on white",
        "n": 1, "resolution": "1K"}


def _http_service(settings: Settings, store: TaskStore, fake) -> Service:
    """**HTTP 模式**的 Service：不注入 client/uploader ⇒ 走凭据池（第二期的真实形态）。"""
    return Service(settings, store=store, fetch_capabilities=False,
                   http_transport=fake.transport())


@pytest.fixture()
def pool_settings(settings: Settings) -> Settings:
    st = settings.replace(service_keys=(SERVICE_KEY,),
                          admin_token="adm",
                          account_enc_key=KEY)
    st.validate()
    return st


@pytest.fixture()
def pool(pool_settings: Settings, store: TaskStore, fake):
    svc = _http_service(pool_settings, store, fake)
    app = create_app(pool_settings, service=svc)
    return TestClient(app), svc, app


def _add_account(app, token: str = FAKE_JWT) -> dict:
    from app.accounts import AccountCipher

    cipher = AccountCipher.from_key_text(KEY)
    claims = __import__("app.credentials", fromlist=["parse_jwt"]).parse_jwt(token)
    return app.state.accounts.add(token=token, claims=claims,
                                  label="pool", cipher=cipher)


def test_service_key_picks_pool_account(pool, fake) -> None:
    client, svc, app = pool
    rec = _add_account(app)

    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]

    out = svc.submit(task_id)
    assert out["submitted"] is True

    # 出站用的是**账号 token**，不是服务 Key
    assert fake.tokens_seen[-1] == FAKE_JWT
    full = svc.store.get_full(task_id)
    assert full["credential_id"] == svc.credential_of(FAKE_JWT)
    assert full["credential_id"] != svc.credential_of(SERVICE_KEY)
    # 选号留痕
    acc = app.state.accounts.get(rec["id"])
    assert acc["use_count"] == 1 and acc["last_used_at"] > 0


def test_jwt_passthrough_still_works(pool, fake) -> None:
    client, svc, _app = pool
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {FAKE_JWT}"})
    assert r.status_code == 202, r.text
    svc.submit(r.json()["task_id"])
    assert fake.tokens_seen[-1] == FAKE_JWT


def test_unknown_key_401_and_empty_pool_503(pool, fake) -> None:
    client, _svc, _app = pool
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": "Bearer sk-not-configured"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"

    #: 号池为空 ⇒ **503**（服务端此刻拿不出账号），不是 401
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "credential_unavailable"


def test_pool_skips_expired_account(pool, fake) -> None:
    client, svc, app = pool
    from app.accounts import AccountCipher
    from app.credentials import parse_jwt

    cipher = AccountCipher.from_key_text(KEY)
    dead = app.state.accounts.add(token=FAKE_JWT,
                                  claims=parse_jwt(FAKE_JWT), cipher=cipher)
    app.state.accounts.set_status(dead["id"], "disabled")
    app.state.accounts.add(token=FAKE_JWT_B, claims=parse_jwt(FAKE_JWT_B),
                           cipher=cipher, label="b")

    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert r.status_code == 202
    svc.submit(r.json()["task_id"])
    assert fake.tokens_seen[-1] == FAKE_JWT_B, "停用的账号不许被使用"


def test_upstream_401_marks_account_dead(pool, fake) -> None:
    """上游 401 ⇒ 账号立即判死，下一次选号不会再选它（失败要回灌号池）。"""
    import httpx

    from app.errors import AuthError

    client, svc, app = pool
    rec = _add_account(app)
    fake.force_error["/v2/api/multimodal/generate/image"] = httpx.Response(
        401, json={"statusInfo": {"code": 401, "message": "token revoked"}})

    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert r.status_code == 202
    with pytest.raises(AuthError):
        svc.submit(r.json()["task_id"])

    acc = app.state.accounts.get(rec["id"])
    assert acc["status"] == "dead"
    assert app.state.accounts.pick() is None
