#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 契约（`docs/INTERFACE.md` 的可执行版本）。

**改形状 = 改这里**。任何一条红了都意味着调用方会挂。
"""
from __future__ import annotations

from app import models
from app.store import ST_QUEUED

from .conftest import PNG_1PX

IMG = "https://cdn.hailuoai.video/ref/a.png"
from .conftest import FAKE_JWT

#: 合法凭据（合成 JWT：结构合法 + exp 远未来 ⇒ 过本地校验）
KEY = {"Authorization": f"Bearer {FAKE_JWT}"}
#: 非法凭据（不是 JWT 形态 —— 透传模式下**只认 JWT**）
BAD = {"Authorization": "Bearer not-a-jwt"}


def _accept(client, body: dict) -> str:
    r = client.post("/async/v1/images/generations", json=body, headers=KEY)
    assert r.status_code == 202, r.text
    payload = r.json()
    # 🔴 受理**只回一个 id** —— 多一个键都是契约变更
    assert set(payload) == {"task_id"}, payload
    assert r.headers["Location"] == f"/async/v1/images/generations/{payload['task_id']}"
    return payload["task_id"]


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


def test_create_returns_202_and_only_task_id(client) -> None:
    task_id = _accept(client, {"model": "hailuo-i2i", "prompt": "a cat", "image": [IMG]})
    assert task_id.startswith("hailuo_")


def test_create_requires_key(client) -> None:
    r = client.post("/async/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_create_rejects_wrong_key(client) -> None:
    r = client.post("/async/v1/images/generations", json={"prompt": "x"}, headers=BAD)
    assert r.status_code == 401


def test_create_validation_error_envelope(client) -> None:
    """参数错 → 400 + 统一错误信封，且 `param` 指出是哪个字段。"""
    r = client.post("/async/v1/images/generations",
                    json={"model": "hailuo-i2i", "prompt": "x", "image": []}, headers=KEY)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "invalid_parameter"
    assert err["type"] == "invalid_request_error"
    assert err["param"] == "image"


def test_create_image_as_string_gets_actionable_message(client) -> None:
    r = client.post("/async/v1/images/generations",
                    json={"prompt": "x", "image": IMG}, headers=KEY)
    assert r.status_code == 400
    assert '["' in r.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_get_non_terminal_returns_202(client) -> None:
    task_id = _accept(client, {"prompt": "x"})
    r = client.get(f"/async/v1/images/generations/{task_id}", headers=KEY)
    assert r.status_code == 202
    assert r.json()["status"] == ST_QUEUED
    assert r.json()["task_id"] == task_id


def test_get_success_returns_data_created_usage(client, service, fake) -> None:
    fake.add_image(IMG, PNG_1PX)
    task_id = _accept(client, {"prompt": "a cat", "image": [IMG]})
    service.submit(task_id)
    service.poll_many(service.store.in_flight())

    r = client.get(f"/async/v1/images/generations/{task_id}", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    # 契约三件套必须齐；降级说明**不进响应**（2026-10-10 起只进日志与任务库）
    assert set(body) == {"data", "created", "usage"}
    assert body["data"][0]["url"].startswith("https://")
    assert isinstance(body["created"], int) and body["created"] > 0
    assert body["usage"]["images"] == 1


def test_get_without_key_is_allowed(client) -> None:
    """**id 即凭据**：不带 Key 也能查（方便把结果链接直接给别人看）。"""
    task_id = _accept(client, {"prompt": "x"})
    r = client.get(f"/async/v1/images/generations/{task_id}")
    assert r.status_code == 202


def test_get_with_wrong_key_still_401(client) -> None:
    """带了**错的** Key 依旧 401 —— 否则调用方的配置错误会被静默吞掉。"""
    task_id = _accept(client, {"prompt": "x"})
    r = client.get(f"/async/v1/images/generations/{task_id}", headers=BAD)
    assert r.status_code == 401


def test_get_unknown_is_404_without_upstream_call(client, fake) -> None:
    """404 必须**本地拦**，不发上游请求。"""
    before = fake.batch_calls
    r = client.get("/async/v1/images/generations/hailuo_doesnotexist", headers=KEY)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"
    assert fake.batch_calls == before


def test_get_other_keys_task_is_404(client, service) -> None:
    """不属于本 Key ⇒ 404（**刻意不区分**"不存在"，否则等于确认 id 存在）。"""
    from app.service import Service  # noqa: F401

    other = service.credential_of("sk-other-key")
    rec = service.create({"prompt": "x"}, credential=other)
    r = client.get(f"/async/v1/images/generations/{rec['task_id']}", headers=KEY)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 列表 / 删除
# ---------------------------------------------------------------------------


def test_list_only_returns_own_tasks(client, service) -> None:
    mine = _accept(client, {"prompt": "mine"})
    other = service.credential_of("sk-other-key")
    service.create({"prompt": "theirs"}, credential=other)

    r = client.get("/async/v1/images/generations", headers=KEY)
    assert r.status_code == 200
    ids = [t["task_id"] for t in r.json()["data"]]
    assert mine in ids and len(ids) == 1, "只该列自己的任务"


def test_list_requires_key(client) -> None:
    assert client.get("/async/v1/images/generations").status_code == 401


def test_delete_non_terminal_is_400(client) -> None:
    task_id = _accept(client, {"prompt": "x"})
    r = client.delete(f"/async/v1/images/generations/{task_id}", headers=KEY)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "task_not_deletable"


def test_delete_terminal_ok(client, service, fake) -> None:
    fake.add_image(IMG, PNG_1PX)
    task_id = _accept(client, {"prompt": "x", "image": [IMG]})
    service.submit(task_id)
    service.poll_many(service.store.in_flight())
    r = client.delete(f"/async/v1/images/generations/{task_id}", headers=KEY)
    assert r.status_code == 200
    assert r.json() == {"task_id": task_id, "status": "DELETED"}


# ---------------------------------------------------------------------------
# 模型清单
# ---------------------------------------------------------------------------


def test_models_lists_upstream_registry_and_capabilities(client) -> None:
    r = client.get("/async/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    # 能力名必须在
    for cap in ("hailuo-i2i", "hailuo-t2i", "hailuo-image"):
        assert cap in ids
    # 上游模型（假上游只给了一个）也必须在
    assert "nano_banana21_flash" in ids
    model = next(m for m in body["data"] if m["id"] == "nano_banana21_flash")
    assert model["max_images"] == 14
    assert "1K" in model["resolutions"]


def test_models_has_no_auth_requirement(client) -> None:
    assert client.get("/async/v1/models").status_code == 200


# ---------------------------------------------------------------------------
# 运维端点
# ---------------------------------------------------------------------------


def test_healthz_is_dependency_free(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_ok_when_configured(client) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_readyz_no_longer_gates_on_server_token(settings, store) -> None:
    """透传模式下 `/readyz` **不再**检查"服务端 token"。

    凭据随请求来（服务端本就不持有账号凭据）；"某个调用方 token 是否有效"
    是上游说了算 —— 探针只该回答"这个进程能不能接活"（库可连）。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.service import Service

    svc = Service(settings, store=store, fetch_capabilities=False)
    app = create_app(settings, service=svc)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200
    assert r.json()["auth"] == "jwt-passthrough"

def test_stats_exposes_gate_and_capabilities(client) -> None:
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    for key in ("gate", "store", "capabilities", "observability", "coordinator"):
        assert key in body, f"/stats 少了 {key}"


def test_capabilities_route_reports_source_and_snapshot(client) -> None:
    r = client.get("/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] in ("runtime", "frozen_snapshot")
    assert body["snapshot_date"] == models.SNAPSHOT_DATE
    assert body["models"], "能力表不能为空"


def test_probe_paths_are_not_logged_but_others_are() -> None:
    from app.observability import is_probe_path, should_log_path

    assert is_probe_path("/healthz") and is_probe_path("/readyz")
    assert not should_log_path("/healthz")
    assert should_log_path("/async/v1/images/generations")


# ---------------------------------------------------------------------------
# 同步接口（/v1/images/generations —— 创建+轮询合并）
# ---------------------------------------------------------------------------


def test_sync_endpoint_503_without_coordinator(client) -> None:
    """没有推进者（协调器线程没起）⇒ **503 快速失败**，不让调用方白等一整个预算。

    测试环境的 client 不跑 lifespan ⇒ 协调器线程天然没起，正好覆盖这条路径。
    """
    r = client.post("/v1/images/generations", json={"prompt": "x"}, headers=KEY)
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "sync_unavailable"
    assert err["type"] == "service_unavailable"


def test_sync_endpoint_returns_final_result(client, service, fake, monkeypatch) -> None:
    """预算内到终态 ⇒ **200 + data**（与异步终态同形状，view() 唯一出口）。

    推进由后台 feeder 线程做（等任务出现在 due_for_submit 就 submit+poll）——
    同步端点本身只读库等待，这正是"推进是协调器职责"的镜像。
    """
    import threading
    import time as _time

    from app.coordinator import Coordinator

    monkeypatch.setattr(Coordinator, "running", property(lambda self: True))
    monkeypatch.setattr(service.settings, "sync_max_wait", 8.0)

    def feeder() -> None:
        deadline = _time.time() + 6
        while _time.time() < deadline:
            rows = service.store.due_for_submit(limit=1)
            if rows:
                service.submit(rows[0]["task_id"])
                service.poll_many(service.store.in_flight())
                return
            _time.sleep(0.05)

    t = threading.Thread(target=feeder, daemon=True)
    t.start()
    r = client.post("/v1/images/generations", json={"prompt": "a cat"}, headers=KEY)
    t.join(timeout=8)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"data", "created", "usage"}
    assert body["data"][0]["url"].startswith("https://")


def test_sync_endpoint_429_when_slots_exhausted(client, service, monkeypatch) -> None:
    """并发护栏：同步等待槽满 ⇒ **429 + Retry-After 秒回**，绝不排队占线程。

    性能红线：同步等待阻塞的是线程池线程（anyio 默认 40），护栏把最坏占用
    钉死在 SYNC_MAX_CONCURRENCY —— 大量慢请求不能殃及其他请求。
    """
    import threading

    from app.coordinator import Coordinator

    monkeypatch.setattr(Coordinator, "running", property(lambda self: True))
    #: 容量 1 且已占 ⇒ 请求必然撞护栏（与 settings 无关，直接换护栏对象）
    client.app.state.sync_slots = threading.BoundedSemaphore(1)
    assert client.app.state.sync_slots.acquire(blocking=False)

    r = client.post("/v1/images/generations", json={"prompt": "x"}, headers=KEY)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "sync_busy"
    assert err["type"] == "rate_limit_error"
    assert r.headers.get("Retry-After") == "1"


def test_sync_endpoint_202_falls_back_to_async(client, service, monkeypatch) -> None:
    """预算耗尽仍在跑 ⇒ **202 + task_id + Location**（无缝转异步轮询，任务不丢）。"""
    from app.coordinator import Coordinator

    monkeypatch.setattr(Coordinator, "running", property(lambda self: True))
    monkeypatch.setattr(service.settings, "sync_max_wait", 0.5)

    r = client.post("/v1/images/generations", json={"prompt": "x"}, headers=KEY)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"task_id", "status"}
    assert body["status"] == ST_QUEUED
    assert r.headers["Location"] == f"/async/v1/images/generations/{body['task_id']}"
