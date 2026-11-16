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


def test_pre_create_failure_rotates_to_other_account(pool, fake, monkeypatch) -> None:
    """建任务**前**失败（未计费）⇒ 换一个号；下一次提交必须用新号的 token。"""
    from app.errors import UpstreamError
    from app.upstream.hailuo.client import HailuoClient

    client, svc, app = pool
    _add_account(app, FAKE_JWT)
    _add_account(app, FAKE_JWT_B)

    def boom(*_a, **_kw):
        err = UpstreamError("connection reset before create")
        err.pre_create = True
        raise err

    monkeypatch.setattr(HailuoClient, "create_image", boom)
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    task_id = r.json()["task_id"]
    first = svc.store.get_full(task_id)["credential_id"]

    with pytest.raises(UpstreamError):
        svc.submit(task_id)

    after = svc.store.get_full(task_id)["credential_id"]
    assert after != first, "pre_create 失败必须换号"
    assert svc.store.get_full(task_id)["owner_id"].startswith("svc-"), "归属不许被换掉"

    monkeypatch.undo()
    out = svc.submit(task_id)
    assert out["submitted"] is True
    expected = svc.credential_of(FAKE_JWT_B) if after != svc.credential_of(FAKE_JWT) \
        else svc.credential_of(FAKE_JWT)
    assert fake.tokens_seen[-1] == (FAKE_JWT_B if expected == svc.credential_of(FAKE_JWT_B)
                                    else FAKE_JWT)


def test_passthrough_never_rotates(settings, store, service, fake, monkeypatch) -> None:
    """透传模式换号 = 花别人的钱，必须**不换**。"""
    from app.errors import UpstreamError
    from app.main import create_app
    from app.upstream.hailuo.client import HailuoClient

    client = TestClient(create_app(settings, service=service))
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {FAKE_JWT}"})
    task_id = r.json()["task_id"]
    before = service.store.get_full(task_id)["credential_id"]

    def boom(*_a, **_kw):
        err = UpstreamError("reset")
        err.pre_create = True
        raise err

    monkeypatch.setattr(HailuoClient, "create_image", boom)
    with pytest.raises(UpstreamError):
        service.submit(task_id)
    assert service.store.get_full(task_id)["credential_id"] == before


def test_readyz_and_stats_report_real_auth_mode(pool, fake) -> None:
    """池化开启后，观测面必须如实说明"两条路都在"，而不是硬写透传。"""
    client, _svc, _app = pool
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["auth"] == "jwt-passthrough+service-key"
    assert client.get("/stats").json()["auth"] == "jwt-passthrough+service-key"


def test_credential_rehydrates_from_pool_after_restart(pool, fake) -> None:
    """进程重启后，池化任务必须能从**加密账号表**恢复凭据，而不是直接判失败。

    2026-11-17 生产事故：发版重启把内存池清空，在途任务被判 credential_unavailable。
    """
    client, svc, app = pool
    _add_account(app)
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    task_id = r.json()["task_id"]
    cred = svc.store.get_full(task_id)["credential_id"]

    #: 模拟重启：内存池清空
    assert svc.credentials.forget(cred) is True
    assert svc.credentials.get(cred) is None

    out = svc.submit(task_id)          # 不抛 CredentialUnavailable
    assert out["submitted"] is True
    assert fake.tokens_seen[-1] == FAKE_JWT
    assert svc.credentials.get(cred) is not None, "恢复后应重新入池"


def test_passthrough_recovers_from_encrypted_token(settings, store, fake) -> None:
    """透传 token **加密落库**（默认开）⇒ 重启后在途任务仍能恢复。

    ⚠️ 必须用 **HTTP 模式**的 Service：`service` 夹具注入了 client（嵌入模式），
    那条路根本不查凭据池，测不到这里的行为。
    """
    from app.main import create_app

    st = settings.replace(account_enc_key=KEY, store_caller_tokens=True)
    svc = _http_service(st, store, fake)
    client = TestClient(create_app(st, service=svc))
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {FAKE_JWT}"})
    task_id = r.json()["task_id"]
    cred = svc.store.get_full(task_id)["credential_id"]

    svc.credentials.forget(cred)          # 模拟重启
    out = svc.submit(task_id)
    assert out["submitted"] is True
    assert fake.tokens_seen[-1] == FAKE_JWT


def test_passthrough_fails_when_storage_disabled(settings, store, fake) -> None:
    """关掉落库 ⇒ 行为回到旧样子：明确失败，绝不臆造凭据。"""
    from app.errors import CredentialUnavailable
    from app.main import create_app

    st = settings.replace(account_enc_key=KEY, store_caller_tokens=False)
    svc = _http_service(st, store, fake)
    client = TestClient(create_app(st, service=svc))
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {FAKE_JWT}"})
    task_id = r.json()["task_id"]
    svc.credentials.forget(svc.store.get_full(task_id)["credential_id"])
    with pytest.raises(CredentialUnavailable):
        svc.submit(task_id)


def test_caller_token_is_stored_as_ciphertext(settings, store, fake) -> None:
    """库里只许有密文；明文 JWT 一个字节都不许出现。"""
    from app.main import create_app

    st = settings.replace(account_enc_key=KEY, store_caller_tokens=True)
    svc = _http_service(st, store, fake)
    client = TestClient(create_app(st, service=svc))
    client.post("/async/v1/images/generations", json=BODY,
                headers={"Authorization": f"Bearer {FAKE_JWT}"})
    raw = "\n".join(
        f"{r[0]}|{r[1]}|{r[2]}"
        for r in store.engine.connect().exec_driver_sql(
            "select fp, token_enc, exp from caller_tokens").fetchall())
    assert raw, "应当写入 caller_tokens"
    assert FAKE_JWT not in raw, "明文 token 不许落库"
    assert "v1." in raw, "应存信封密文"

