#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务 Key 的轻量配额（每日请求数）。

判据：
1. `label:key:quota` 才能带配额；纯 key 仍然可用（无限额，老配置不破）；
2. 超限 ⇒ **429 `quota_exceeded`**，且**不落库**（不受理、不计费）；
3. 配额按 Key 隔离：A 超限不影响 B；
4. 跨天自动归零（按本地日期分区）。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.main import create_app
from app.poolauth import parse_service_keys
from app.service import Service
from tests.conftest import FAKE_JWT

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
BODY = {"model": "nano_banana21_flash", "prompt": "x", "n": 1, "resolution": "1K"}


def test_parse_specs() -> None:
    specs = parse_service_keys(("tenant-a:sk-a:2", "sk-plain", "t:sk-b:0", "sk-hailuo"))
    assert specs["sk-a"] == ("tenant-a", 2)
    #: 规则：`sk-` + 二级域名 ⇒ 标签自动推导（`sk-plain` → `plain`），且不限量
    assert specs["sk-plain"] == ("plain", 0)
    assert specs["sk-hailuo"] == ("hailuo", 0)
    assert specs["sk-b"] == ("t", 0), "0 = 不限"


def test_bad_quota_rejected() -> None:
    st = Settings(service_keys=("a:sk-a:abc",), task_db="sqlite+pysqlite:///:memory:")
    with pytest.raises(ConfigError):
        st.validate()


def test_quota_is_per_key_and_enforced(settings, store, fake) -> None:
    st = settings.replace(service_keys=("tenant-a:sk-a:2", "tenant-b:sk-b"),
                          admin_token="a", account_enc_key=KEY, hl_concurrency=2)
    st.validate()
    svc = Service(st, store=store, fetch_capabilities=False,
                  http_transport=fake.transport())
    app = create_app(st, service=svc)
    from app.accounts import AccountCipher
    from app.credentials import parse_jwt

    app.state.accounts.add(token=FAKE_JWT, claims=parse_jwt(FAKE_JWT),
                           cipher=AccountCipher.from_key_text(KEY))
    c = TestClient(app)

    def hit(key: str):
        return c.post("/async/v1/images/generations", json=BODY,
                      headers={"Authorization": f"Bearer {key}"})

    assert hit("sk-a").status_code == 202
    assert hit("sk-a").status_code == 202
    third = hit("sk-a")
    assert third.status_code == 429, third.text
    assert third.json()["error"]["code"] == "quota_exceeded"
    assert "配额" in third.json()["error"]["message"]
    assert "tenant-a" in third.json()["error"]["message"], "报错要能看出是哪个 Key"

    assert hit("sk-b").status_code == 202, "另一个 Key 不受影响"
    assert len(store.list_for_owner("svc-tenant-a-" + svc.credential_of("sk-a")[:8],
                                    limit=10)) == 2


def test_rejected_request_is_not_persisted(settings, store, fake) -> None:
    st = settings.replace(service_keys=("t:sk-x:1",), account_enc_key=KEY)
    st.validate()
    svc = Service(st, store=store, fetch_capabilities=False,
                  http_transport=fake.transport())
    app = create_app(st, service=svc)
    from app.accounts import AccountCipher
    from app.credentials import parse_jwt

    app.state.accounts.add(token=FAKE_JWT, claims=parse_jwt(FAKE_JWT),
                           cipher=AccountCipher.from_key_text(KEY))
    c = TestClient(app)
    h = {"Authorization": "Bearer sk-x"}
    assert c.post("/async/v1/images/generations", json=BODY, headers=h).status_code == 202
    assert c.post("/async/v1/images/generations", json=BODY, headers=h).status_code == 429
    assert store.count_queued() == 1, "被配额拒绝的请求绝不能落库"


def test_usage_rolls_over_daily(store) -> None:
    assert store.consume_quota("owner", "2026-11-17", limit=1) is True
    assert store.consume_quota("owner", "2026-11-17", limit=1) is False
    assert store.consume_quota("owner", "2026-11-18", limit=1) is True, "跨天必须归零"


def test_env_uses_hailuo_api_keys(monkeypatch) -> None:
    """变量名对齐 `*_API_KEYS` 约定；旧名 `SERVICE_KEYS` 仍兼容。"""
    monkeypatch.delenv("SERVICE_KEYS", raising=False)
    monkeypatch.setenv("HAILUO_API_KEYS", "sk-hailuo")
    st = Settings.from_env()
    assert st.service_keys == ("sk-hailuo",)
    assert parse_service_keys(st.service_keys)["sk-hailuo"] == ("hailuo", 0)

    monkeypatch.delenv("HAILUO_API_KEYS", raising=False)
    monkeypatch.setenv("SERVICE_KEYS", "sk-legacy")
    assert Settings.from_env().service_keys == ("sk-legacy",)
