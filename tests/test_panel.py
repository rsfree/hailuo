#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理平台 API（第一期）。

判据来源：`.fixloop/criteria.md` R1–R4、R6。
"""
from __future__ import annotations

import base64
import json
import time

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


# ---------------------------------------------------------------- 观测面（第二期）

def _seed(store) -> list[str]:
    """造 2 成功 + 1 失败 —— 三条足够覆盖状态筛选、模型分组、失败原因聚类。"""
    now = time.time()
    for i in range(2):
        store.create_task(
            task_id=f"hailuo_ok{i}", credential_id="cred-a", owner_id="svc-a",
            capability="hailuo-i2i", model="nano_banana21_flash",
            status="succeeded", attempts=1,
            request_json='{"prompt": "a cat"}',
            result_json='{"images": [{"url": "http://x/1.png"}]}',
            submitted_at=now - 30, finished_at=now)
    store.create_task(
        task_id="hailuo_bad0", credential_id="cred-a", owner_id="svc-b",
        capability="hailuo-i2i", model="seedream-4.0",
        status="failure", attempts=2,
        request_json='{"prompt": "a dog"}',
        error_json=json.dumps({"message": "upstream 401", "code": "unauthorized"}),
        submitted_at=now - 10, finished_at=now)
    return ["hailuo_ok0", "hailuo_ok1", "hailuo_bad0"]


def test_observability_requires_login(panel: TestClient) -> None:
    """观测面同样受会话保护 —— 它比账号列表更能看出业务全貌。"""
    assert panel.get("/panel/api/tasks").status_code == 401
    assert panel.get("/panel/api/usage").status_code == 401
    assert panel.get("/panel/api/system").status_code == 401


def test_tasks_list_and_filters(auth: TestClient, store) -> None:
    _seed(store)
    r = auth.get("/panel/api/tasks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # 列表行必须带错误摘要与耗时，但不带请求体（请求体只在详情里）
    failed = [x for x in body["items"] if x["task_id"] == "hailuo_bad0"][0]
    assert failed["error_brief"].startswith("upstream 401")
    assert "request" not in failed
    assert failed["duration"] >= 0

    only_failed = auth.get("/panel/api/tasks", params={"status": "failure"}).json()
    assert only_failed["total"] == 1
    assert only_failed["items"][0]["task_id"] == "hailuo_bad0"

    by_model = auth.get("/panel/api/tasks",
                        params={"model": "seedream-4.0"}).json()
    assert by_model["total"] == 1

    by_q = auth.get("/panel/api/tasks", params={"q": "svc-b"}).json()
    assert by_q["total"] == 1

    paged = auth.get("/panel/api/tasks", params={"limit": 2, "offset": 2}).json()
    assert paged["total"] == 3 and len(paged["items"]) == 1

    assert body["facets"]["status"]["succeeded"] == 2
    assert {m["model"] for m in body["facets"]["model"]} == {
        "nano_banana21_flash", "seedream-4.0"}


def test_task_detail_and_404(auth: TestClient, store) -> None:
    _seed(store)
    r = auth.get("/panel/api/tasks/hailuo_ok0")
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["request"]["prompt"] == "a cat"
    assert task["result"]["images"][0]["url"] == "http://x/1.png"
    assert auth.get("/panel/api/tasks/hailuo_missing").status_code == 404


def test_usage_endpoint(auth: TestClient, store) -> None:
    _seed(store)
    r = auth.get("/panel/api/usage", params={"days": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["daily"]) == 7
    assert body["daily"][-1]["total"] == 3
    assert body["latency"]["count"] == 2          # 只有成功任务参与耗时
    assert body["latency"]["p50"] is not None
    assert body["failed_reasons"][0]["reason"].startswith("upstream 401")


def test_system_endpoint(auth: TestClient, store) -> None:
    _seed(store)
    r = auth.get("/panel/api/system")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"]
    assert body["queue"] == 0
    assert body["status"]["store"]["backend"] in {"sqlite", "postgresql"}


def test_overview_reports_queue_and_today(auth: TestClient, store) -> None:
    _seed(store)
    body = auth.get("/panel/api/overview").json()
    assert body["queue"] == 0
    assert body["tasks"]["total"] == 3
    assert "today_requests" in body and "active" in body


def test_models_requires_login(panel: TestClient) -> None:
    assert panel.get("/panel/api/models").status_code == 401


def test_models_shape(auth: TestClient) -> None:
    """模型页数据来自能力表，必须带单价与「刻意缺席」，否则运维看不到边界。"""
    r = auth.get("/panel/api/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_count"] >= 1
    assert body["snapshot_date"]
    first = body["models"][0]
    for key in ("id", "name", "max_images", "resolutions", "aspect_ratios"):
        assert key in first, key
    assert isinstance(body["deliberate_absences"], dict)
    assert body["deliberate_absences"], "缺席清单不该为空（它是边界的一部分）"


# ---------------------------------------------------------------------------
# 模型别名（客户端请求名 → 上游 modelID）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime_aliases():
    """运行期别名是**模块级**状态：每个用例前后清空，避免串味。

    真实现里它由任务库驱动、单 worker 持有；测试里不清理会让
    `canonical_model_id` 的其它断言看到别的用例留下的映射。
    """
    from app import models as models_mod

    models_mod.install_runtime_aliases({})
    yield
    models_mod.install_runtime_aliases({})


def test_aliases_require_login(panel: TestClient) -> None:
    assert panel.get("/panel/api/aliases").status_code == 401
    assert panel.post("/panel/api/aliases",
                      json={"alias": "x", "target": "nano-banana2"}).status_code == 401


def test_aliases_shape(auth: TestClient) -> None:
    """内置别名必须可见 —— 运维要能区分"我配的"和"代码里写死的"。"""
    body = auth.get("/panel/api/aliases").json()
    assert body["items"], "内置别名不该为空"
    builtin = [i for i in body["items"] if i["source"] == "builtin"]
    assert builtin, "至少要有内置别名"
    assert any(i["alias"] == "nano-banana-pro" for i in builtin)
    assert body["targets"], "目标候选不能为空（否则面板没法选）"
    assert "hailuo-i2i" in body["reserved"]


def test_create_alias_takes_effect_immediately(auth: TestClient) -> None:
    """核心判据：面板配一条，**不重启**就改变 `model` 的解析结果。"""
    from app import models as models_mod

    target = models_mod.all_models()[0].model_id
    r = auth.post("/panel/api/aliases", json={"alias": "my-fast-model", "target": target})
    assert r.status_code == 200, r.text
    assert models_mod.canonical_model_id("my-fast-model") == target

    body = r.json()
    mine = [i for i in body["items"] if i["alias"] == "my-fast-model"]
    assert mine and mine[0]["source"] == "runtime"

    # 受理链路也认它：不再报"未知 model"
    cap, notes = models_mod.resolve_capability("my-fast-model", has_image=False)
    assert cap == "hailuo-t2i" and notes == []


def test_alias_persists_across_service_restart(auth: TestClient, settings, store) -> None:
    """重启后仍生效 —— 配置在库里，不是内存。"""
    from app import models as models_mod
    from app.service import Service

    target = models_mod.all_models()[0].model_id
    assert auth.post("/panel/api/aliases",
                     json={"alias": "persisted", "target": target}).status_code == 200

    models_mod.install_runtime_aliases({})            # 模拟进程重启：内存清空
    assert models_mod.canonical_model_id("persisted") == "persisted"

    fresh = Service(settings, store=store, fetch_capabilities=False)
    try:
        assert models_mod.canonical_model_id("persisted") == target
    finally:
        fresh.close()


def test_alias_can_shadow_builtin_then_fall_back(auth: TestClient) -> None:
    """覆盖内置别名后删掉 ⇒ 回落到内置值（"恢复出厂"）。"""
    from app import models as models_mod

    target = models_mod.all_models()[0].model_id
    r = auth.post("/panel/api/aliases", json={"alias": "nano-banana-pro", "target": target})
    assert r.status_code == 200
    assert models_mod.canonical_model_id("nano-banana-pro") == target
    shadowed = [i for i in r.json()["items"] if i["alias"] == "nano-banana-pro"][0]
    assert shadowed["source"] == "runtime" and shadowed["shadowed"] is True

    assert auth.delete("/panel/api/aliases/nano-banana-pro").status_code == 200
    assert models_mod.canonical_model_id("nano-banana-pro") == "nano-banana2"


@pytest.mark.parametrize("alias,target,code", [
    ("", "nano-banana2", "invalid_alias"),
    ("a" * 65, "nano-banana2", "invalid_alias"),
    ("has space", "nano-banana2", "invalid_alias"),
    ("hailuo-i2i", "nano-banana2", "alias_reserved"),
    ("i2i", "nano-banana2", "alias_reserved"),
    ("图生图", "nano-banana2", "alias_reserved"),
    ("ok-name", "not-a-real-model", "unknown_target"),
    ("same", "same", "alias_identity"),
])
def test_alias_rejects_bad_input(auth: TestClient, alias, target, code) -> None:
    """凡是"配了也不会生效"或"指向不存在的模型"，都在**写入前**挡掉。"""
    r = auth.post("/panel/api/aliases", json={"alias": alias, "target": target})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"]["code"] == code


def test_delete_builtin_alias_is_refused(auth: TestClient) -> None:
    """内置别名删不掉（它写死在 models.py），只能覆盖 —— 必须明说。"""
    r = auth.delete("/panel/api/aliases/nano-banana-pro")
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "alias_builtin"


def test_delete_unknown_alias_is_404(auth: TestClient) -> None:
    r = auth.delete("/panel/api/aliases/never-existed")
    assert r.status_code == 404
