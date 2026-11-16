#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理平台 API（第一期）。

判据来源：`.fixloop/criteria.md` R1–R4、R6。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.upstream.hailuo.client import PATH_PROCESSING
from tests.conftest import FAKE_JWT, FAKE_JWT_B

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
ADMIN = "adm-secret-for-tests"


@pytest.fixture()
def panel_settings(settings):
    return settings.replace(admin_token=ADMIN, account_enc_key=KEY)


@pytest.fixture()
def panel(panel_settings, service) -> TestClient:
    app = create_app(panel_settings, service=service)
    return TestClient(app)


@pytest.fixture()
def auth(panel: TestClient) -> TestClient:
    r = panel.post("/panel/api/login", json={"token": ADMIN})
    assert r.status_code == 200, r.text
    return panel


def test_panel_is_absent_when_admin_token_unset(settings, service) -> None:
    """没配 ADMIN_TOKEN ⇒ 面板**不存在**（404），不做一个没人守的门。"""
    app = create_app(settings, service=service)
    c = TestClient(app)
    assert c.get("/panel").status_code == 404
    assert c.get("/panel/api/accounts").status_code == 404


def test_login_rejects_wrong_token(panel: TestClient) -> None:
    r = panel.post("/panel/api/login", json={"token": "wrong"})
    assert r.status_code == 401
    assert panel.get("/panel/api/me").status_code == 401


def test_login_sets_cookie_and_me_works(panel: TestClient) -> None:
    r = panel.post("/panel/api/login", json={"token": ADMIN})
    assert r.status_code == 200
    assert "hl_panel" in r.cookies or "hl_panel" in panel.cookies
    me = panel.get("/panel/api/me")
    assert me.status_code == 200 and me.json()["ok"] is True
    assert panel.post("/panel/api/logout").status_code == 200
    assert panel.get("/panel/api/me").status_code == 401


def test_panel_page_served(panel: TestClient) -> None:
    r = panel.get("/panel")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "hailuo" in r.text.lower()


def test_create_list_disable_enable_remove(auth: TestClient) -> None:
    r = auth.post("/panel/api/accounts",
                  json={"token": FAKE_JWT, "label": "me better", "note": "主号"})
    assert r.status_code == 200, r.text
    acc = r.json()["account"]
    assert acc["label"] == "me better"
    assert acc["status"] == "active"
    assert acc["subject"]

    listed = auth.get("/panel/api/accounts").json()["accounts"]
    assert [a["id"] for a in listed] == [acc["id"]]

    assert auth.post(f"/panel/api/accounts/{acc['id']}/status",
                     json={"status": "disabled"}).json()["account"]["status"] == "disabled"
    assert auth.post(f"/panel/api/accounts/{acc['id']}/status",
                     json={"status": "active"}).json()["account"]["status"] == "active"
    assert auth.delete(f"/panel/api/accounts/{acc['id']}").status_code == 200
    assert auth.get("/panel/api/accounts").json()["accounts"] == []


def test_account_view_never_contains_raw_token(auth: TestClient) -> None:
    auth.post("/panel/api/accounts", json={"token": FAKE_JWT, "label": "x"})
    body = auth.get("/panel/api/accounts").text
    assert FAKE_JWT not in body
    assert "token_enc" not in body


def test_import_lines_reports_per_line(auth: TestClient) -> None:
    #: 一行一条：`备注<Tab>token` 或裸 token。**纯文字行没有 token ⇒ 必须报错**，
    #: 不能"看起来像备注就收下"（那等于给一个空账号占位）。
    text = f"me-better\t{FAKE_JWT}\n{FAKE_JWT_B}\nnot-a-jwt"
    r = auth.post("/panel/api/accounts/import", json={"text": text})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 3
    assert results[0]["ok"] is True and results[0]["label"] == "me-better"
    assert results[1]["ok"] is True
    assert results[2]["ok"] is False
    assert "JWT" in results[2]["error"]
    assert len(auth.get("/panel/api/accounts").json()["accounts"]) == 2


def test_import_accepts_label_then_token(auth: TestClient) -> None:
    r = auth.post("/panel/api/accounts/import",
                  json={"text": f"主号\t{FAKE_JWT}"})
    assert r.json()["results"][0]["ok"] is True
    assert auth.get("/panel/api/accounts").json()["accounts"][0]["label"] == "主号"


def test_check_updates_status_and_never_leaks_token(auth: TestClient) -> None:
    acc = auth.post("/panel/api/accounts",
                    json={"token": FAKE_JWT, "label": "x"}).json()["account"]
    r = auth.post(f"/panel/api/accounts/{acc['id']}/check")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert FAKE_JWT not in r.text

    got = auth.get("/panel/api/accounts").json()["accounts"][0]
    assert got["last_check_ok"] is True
    assert got["last_checked_at"] > 0


def test_check_marks_dead_on_401(auth: TestClient, fake) -> None:
    import httpx

    fake.force_error[PATH_PROCESSING] = httpx.Response(
        401, json={"statusInfo": {"code": 401, "message": "token expired"}})
    acc = auth.post("/panel/api/accounts",
                    json={"token": FAKE_JWT, "label": "x"}).json()["account"]
    r = auth.post(f"/panel/api/accounts/{acc['id']}/check")
    assert r.json()["ok"] is False
    got = auth.get("/panel/api/accounts").json()["accounts"][0]
    assert got["status"] == "dead"
    assert got["last_error"]


def test_overview_counts(auth: TestClient) -> None:
    auth.post("/panel/api/accounts", json={"token": FAKE_JWT})
    auth.post("/panel/api/accounts", json={"token": FAKE_JWT_B})
    ov = auth.get("/panel/api/overview").json()
    assert ov["accounts"]["total"] == 2
    assert ov["accounts"]["active"] == 2
    assert "pool_mode" in ov
