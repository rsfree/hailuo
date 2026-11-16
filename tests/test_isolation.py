#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多服务 Key 的任务隔离（U2）。

判据：同一个号池账号、两个服务 Key ⇒ **互不可见**（列表/单条/删除）。
透传模式不受影响；历史行（owner_id 为空）必须仍归原凭据所有。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.service import Service
from app.store import TaskStore
from tests.conftest import FAKE_JWT

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
A, B = "sk-tenant-a", "sk-tenant-b"
BODY = {"model": "nano_banana21_flash", "prompt": "a red cube", "n": 1,
        "resolution": "1K"}


@pytest.fixture()
def iso(settings: Settings, store: TaskStore, fake):
    st = settings.replace(service_keys=(A, B), admin_token="adm",
                          account_enc_key=KEY)
    st.validate()
    svc = Service(st, store=store, fetch_capabilities=False,
                  http_transport=fake.transport())
    app = create_app(st, service=svc)
    from app.accounts import AccountCipher
    from app.credentials import parse_jwt

    app.state.accounts.add(token=FAKE_JWT, claims=parse_jwt(FAKE_JWT),
                           cipher=AccountCipher.from_key_text(KEY), label="pool")
    return TestClient(app), app


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_two_service_keys_are_isolated(iso) -> None:
    client, _app = iso
    r = client.post("/async/v1/images/generations", json=BODY, headers=_h(A))
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]

    # 列表：A 看得到，B 看不到
    assert len(client.get("/async/v1/images/generations",
                          headers=_h(A)).json()["data"]) == 1
    assert client.get("/async/v1/images/generations",
                      headers=_h(B)).json()["data"] == []

    # 单条：B 拿 A 的 id ⇒ 404（不区分"不存在"与"不属于你"）
    assert client.get(f"/async/v1/images/generations/{task_id}",
                      headers=_h(B)).status_code == 404
    assert client.get(f"/async/v1/images/generations/{task_id}",
                      headers=_h(A)).status_code == 202
    # 删除：未终态先拒（400/409），但**归属判断必须更早** ⇒ B 拿到 404
    assert client.delete(f"/async/v1/images/generations/{task_id}",
                         headers=_h(B)).status_code == 404


def test_owner_is_caller_not_account(iso) -> None:
    """任务归属必须是**调用方**，不是号池账号 —— 否则隔离等于没做。"""
    client, app = iso
    task_id = client.post("/async/v1/images/generations", json=BODY,
                          headers=_h(A)).json()["task_id"]
    svc: Service = app.state.service
    full = svc.store.get_full(task_id)
    assert full["credential_id"] != ""
    assert full["owner_id"] != ""
    assert full["owner_id"] != full["credential_id"], "池化模式两个值必须不同"


def test_passthrough_owner_equals_credential(settings, store, service) -> None:
    """透传模式：调用方自带 JWT，owner 与 credential 同值（行为与旧版一致）。"""
    from app.main import create_app

    app = create_app(settings, service=service)
    client = TestClient(app)
    r = client.post("/async/v1/images/generations", json=BODY,
                    headers={"Authorization": f"Bearer {FAKE_JWT}"})
    assert r.status_code == 202
    full = service.store.get_full(r.json()["task_id"])
    assert full["owner_id"] == full["credential_id"]


def test_legacy_rows_without_owner_still_visible(settings, store) -> None:
    """历史行（owner_id 为空，列是后加的）必须仍归原凭据 —— 否则升级即丢任务。"""
    cred = "legacy-credential"
    rec = store.create_task(credential_id=cred, capability="hailuo-t2i",
                            model="m", upstream_model="m", status="queued",
                            request_json="{}", plan_json="{}",
                            degradations_json="[]")
    #: 模拟升级前的行：owner 列是空的
    with store.session() as s:
        from sqlalchemy import text
        s.execute(text("UPDATE tasks SET owner_id='' WHERE task_id=:t"),
                  {"t": rec["task_id"]})
    rows = store.list_for_owner(cred, limit=10)
    assert [r["task_id"] for r in rows] == [rec["task_id"]]
