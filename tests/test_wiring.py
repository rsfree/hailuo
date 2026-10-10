#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接线门禁：可观测性纪律 / 配置旋钮 / 闸门 / 存储 / 协调器 / 能力注册表。

## 这类用例抓的是"编译通过、导入正常、但功能其实不存在"的缺陷

静态检查只能查"有没有人读"，查不出"读了有没有用" —— 那是**假配置**。
所以这里既查静态（源码里出现了这个名字），也查动态（真的跑一遍看行为变化）。
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import time

import pytest

from app import models
from app.config import ConfigError, Settings
from app.errors import RiskControlChallenge
from app.gate import Gate
from app.observability import PROBE_PATHS, Observability, excluded_urls, is_probe_path
from app.store import ST_FAILURE, ST_IN_PROGRESS, ST_QUEUED, TaskStore, new_task_id

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------------------
# 可观测性
# ---------------------------------------------------------------------------


def test_excluded_urls_is_anchored_not_substring() -> None:
    """🔴 `excluded_urls` **必须锚定**。

    logfire 用 `re.search`（**子串**匹配）⇒ 写 `"/"` 会命中每一个 URL，
    全站追踪**静默关闭**，而且没有任何报错。这是最阴的一类故障。
    """
    regex = excluded_urls(PROBE_PATHS)
    assert regex == r"^/healthz$|^/readyz$"
    compiled = re.compile(regex)
    assert compiled.search("/healthz")
    assert compiled.search("/readyz")
    assert not compiled.search("/")
    assert not compiled.search("/async/v1/images/generations")
    assert not compiled.search("/healthz/extra")


def test_probe_paths_derive_both_channels() -> None:
    """span 与日志**从同一张表派生** —— 只摘 span 会留下一半噪音。"""
    for p in PROBE_PATHS:
        assert is_probe_path(p)
    assert len(PROBE_PATHS) == len(set(PROBE_PATHS))


def test_frontend_url_is_not_probe() -> None:
    assert not is_probe_path("/async/v1/images/generations")


def test_observability_init_without_token_is_silent() -> None:
    """没配 token ⇒ 只在本地留日志，**不抛**、`sdk_configured=False`。"""
    obs = Observability().init(Settings(hailuo_token="x"))
    assert obs.sdk_configured is False
    obs.flush()          # 不该炸
    obs.event("x", a=1)  # 不该炸
    assert obs.status()["events"] == 1


def test_capture_upstream_false_keeps_only_safe_fields() -> None:
    """`capture_upstream=False` ⇒ 只留阶段名与状态码，业务报文不外发。"""
    obs = Observability()
    obs.capture_upstream = False
    captured: list[tuple[str, dict]] = []
    obs.event = lambda name, **attrs: captured.append((name, attrs))  # type: ignore
    obs.upstream("create_image", http_path="/x", http_status=200,
                 request_body='{"desc":"secret"}', yy="abc")
    assert captured[0][1] == {"http_path": "/x", "http_status": 200}


def test_upstream_event_carries_no_credentials_and_drops_the_query() -> None:
    """🔴 **凭据不上报** + 上游埋点**只给 pathname**。

    这条是"密钥不上报靠实现约束而不是过滤器"的可执行版本。
    想改回脱敏设 `OTEL_SCRUBBING=1`（只影响 SDK 自带 scrubber）。
    """
    from app.upstream.hailuo.client import HailuoClient

    from .conftest import FakeHailuo

    fake = FakeHailuo()
    c = HailuoClient(token="SUPER-SECRET-TOKEN", base_url="https://hailuoai.video",
                     device={"uuid": "u", "device_id": "d"},
                     transport=fake.transport())
    _, trace = c.fetch_processing()
    blob = repr(trace)
    assert "SUPER-SECRET-TOKEN" not in blob
    for key in ("uuid", "device_id", "unix", "browser_platform"):
        assert key not in trace["http_path"]
    assert "?" not in trace["http_path"]


def test_scrubbing_defaults_to_off() -> None:
    """`OTEL_SCRUBBING` 默认 0 —— 打开它会把带签名参数的产物 URL 整条打成
    `[Scrubbed due to 'Credential']`，面板直接不可读。"""
    assert Settings().otel_scrubbing is False


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _read_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in APP_DIR.rglob("*.py"))


def test_every_setting_knob_is_actually_read() -> None:
    """**没人读的配置项 = 假配置** —— 会让运维以为"我调过了"。

    动态部分（"读了有没有用"）由各功能用例覆盖；
    这里守的是"这个名字是否在 app/ 里出现过"。
    """
    source = _read_source()
    derived = {"startup_warnings", "db_target", "is_sqlite",
               "upstream_configured"}
    unread: list[str] = []
    for f in dataclasses.fields(Settings):
        if f.name in derived:
            continue
        # `settings.<name>` / `self.<name>` / `st.<name>` 都算读
        if not re.search(rf"\.{re.escape(f.name)}\b", source):
            unread.append(f.name)
    assert not unread, f"以下配置项没有任何人读（假配置）：{unread}"


def test_no_duplicate_knob_for_the_same_thing() -> None:
    """同一个概念**只许有一个旋钮**。

    曾经同时有 `HL_MAX_WAIT` 与 `TASK_TIMEOUT`（都指"任务等多久"）——
    改了一个另一个不生效，是典型的"改了没效果"来源。
    """
    fields = {f.name for f in dataclasses.fields(Settings)}
    assert not ({"hl_max_wait", "task_timeout"} <= fields), "超时语义不允许有两个旋钮"


def test_validate_rejects_nonsense() -> None:
    base = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    for kw, frag in [
        ({"hl_concurrency": 0}, "HL_CONCURRENCY"),
        ({"task_timeout": 0}, "TASK_TIMEOUT"),
        ({"hailuo_poll_interval": 0}, "HAILUO_POLL_INTERVAL"),
        ({"normalize_max_side": 8}, "NORMALIZE_MAX_SIDE"),
        ({"coordinator_lease": 1.0}, "COORDINATOR_LEASE"),
        ({"hailuo_screen_width": 0}, "HAILUO_SCREEN_WIDTH"),
    ]:
        st = base.replace(**kw)
        with pytest.raises(ConfigError) as ei:
            st.validate()
        assert frag in str(ei.value), f"{kw} 应当因 {frag} 被拒"


def test_lease_must_cover_two_polls() -> None:
    """租约比一次轮询还短 ⇒ 每轮都换主 = 等于没有选主。"""
    st = Settings(task_db=":memory:", hailuo_token="t",
                  hailuo_poll_interval=10.0, coordinator_lease=15.0)
    with pytest.raises(ConfigError):
        st.validate()


def test_startup_warnings_flag_unsafe_defaults() -> None:
    st = Settings(task_db=":memory:", hailuo_token="", hl_concurrency=8)
    st.validate()
    joined = " ".join(st.startup_warnings)
    #: 透传模式下**不存在**"鉴权关闭"这一档（Bearer 必须是 hailuo JWT）⇒
    #: 告警里不再有 API_KEYS；只提醒"脚本/嵌入模式需要 HAILUO_TOKEN"。
    assert "API_KEYS" not in joined
    assert "透传" in joined and "脚本" in joined
    assert "HL_CONCURRENCY=8" in joined


def test_upstream_configured_requires_token() -> None:
    assert Settings(hailuo_token="x").upstream_configured
    assert not Settings(hailuo_token="").upstream_configured
    assert not Settings(hailuo_token="   ").upstream_configured


def test_device_profile_has_every_signed_field() -> None:
    """设备指纹**唯一出口**，且必须覆盖签名需要的每个键。"""
    profile = Settings().device_profile()
    for key in ("uuid", "device_id", "lang", "os_name", "browser_name",
                "device_memory", "cpu_core_num", "browser_language",
                "browser_platform", "screen_width", "screen_height"):
        assert key in profile and profile[key] not in (None, ""), key


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------


def test_gate_allows_when_unconfigured() -> None:
    assert Gate().check().allowed


def test_gate_min_interval_blocks_then_allows() -> None:
    g = Gate(min_interval=10.0)
    g.note_submit(now=1000.0)
    blocked = g.check(now=1005.0)
    assert not blocked.allowed and blocked.wait_hint == pytest.approx(5.0)
    assert g.check(now=1010.5).allowed


def test_gate_per_minute_window() -> None:
    g = Gate(per_minute=2)
    g.note_submit(now=100.0)
    g.note_submit(now=110.0)
    assert not g.check(now=120.0).allowed
    # 滑出窗口后恢复
    assert g.check(now=161.0).allowed


def test_gate_cooldown_blocks_then_expires() -> None:
    g = Gate(cooldown=60.0)
    base = time.time()
    g.enter_cooldown("风控", seconds=60.0)
    d = g.check()
    assert not d.allowed and "冷却" in d.reason
    assert d.wait_hint is not None and d.wait_hint <= 60.0
    assert g.in_cooldown
    g.reset()
    assert g.check().allowed and not g.in_cooldown
    assert g.stats(now=base)["in_cooldown"] is False


def test_gate_stats_shape() -> None:
    g = Gate(min_interval=1.0, per_minute=5)
    g.note_submit()
    s = g.stats()
    assert s["submits_last_minute"] == 1 and s["per_minute"] == 5


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def test_store_roundtrip(store: TaskStore) -> None:
    tid = new_task_id()
    store.create_task(task_id=tid, credential_id="c", capability="hailuo-i2i",
                      request_json="{}", plan_json="{}", degradations_json="[]")
    assert store.get(tid)["status"] == ST_QUEUED
    store.update_task(tid, status=ST_IN_PROGRESS, upstream_batch_id="b1")
    assert store.get(tid)["upstream_batch_id"] == "b1"
    assert store.count_active() == 1
    assert store.count_queued() == 0


def test_new_task_id_is_unguessable_and_unique() -> None:
    ids = {new_task_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("hailuo_") and len(i) == len("hailuo_") + 32 for i in ids)


def test_store_degradations_are_deduped_and_appended(store: TaskStore) -> None:
    tid = new_task_id()
    store.create_task(task_id=tid, credential_id="c", capability="c",
                      request_json="{}", plan_json="{}", degradations_json="[]")
    store.add_degradation(tid, "a")
    store.add_degradation(tid, "b")
    store.add_degradation(tid, "a")     # 重复不叠加
    assert store.get_full(tid)["degradations"] == ["a", "b"]


def test_store_expire_stale_marks_failure(store: TaskStore) -> None:
    tid = new_task_id()
    store.create_task(task_id=tid, credential_id="c", capability="c",
                      request_json="{}", plan_json="{}", degradations_json="[]")
    with store.session() as s:
        from app.store import TaskRow
        s.query(TaskRow).filter_by(task_id=tid).update({"created_at": time.time() - 9999})

    expired = store.expire_stale(timeout=10.0)
    assert tid in expired
    full = store.get_full(tid)
    assert full["status"] == ST_FAILURE
    assert full["error"]["code"] == "task_timeout"
    # 文案必须提醒"上游可能还在跑并计费"
    assert "计费" in full["error"]["message"]


def test_store_lease_is_exclusive_and_preemptible(store: TaskStore) -> None:
    assert store.acquire_lease(owner="A", ttl=30.0)
    assert not store.acquire_lease(owner="B", ttl=30.0), "同一时刻只能有一个主"
    assert store.acquire_lease(owner="A", ttl=30.0), "主可以续租"
    store.release_lease("A")
    assert store.acquire_lease(owner="B", ttl=30.0)

    # 租约过期 ⇒ 崩溃的副本不会永久占位
    store.acquire_lease(owner="A", ttl=-1.0)
    assert store.acquire_lease(owner="B", ttl=30.0)


def test_store_fingerprint_secret_is_stable(store: TaskStore) -> None:
    assert store.fingerprint_secret() == store.fingerprint_secret()
    assert len(store.fingerprint_secret()) == 64


def test_store_stats_and_purge(store: TaskStore) -> None:
    store.create_task(credential_id="c", capability="c", request_json="{}",
                      plan_json="{}", degradations_json="[]")
    assert store.stats()["total"] == 1
    assert store.purge_old(retention_days=7) == 0, "新任务不该被清"


# ---------------------------------------------------------------------------
# 协调器
# ---------------------------------------------------------------------------


def test_coordinator_submits_and_polls_in_one_tick(service, fake) -> None:
    from app.coordinator import Coordinator

    from .conftest import PNG_1PX

    # 并发必须显式给 3：默认是 1（**计费上游的保守策略**），不是测试想要的
    service.settings = service.settings.replace(hl_concurrency=3)
    img = "https://cdn.hailuoai.video/ref/a.png"
    fake.add_image(img, PNG_1PX)
    for i in range(3):
        service.create({"prompt": f"p{i}", "image": [img]}, credential="c")

    coord = Coordinator(service, service.settings)
    coord.tick()
    assert coord.stats.submitted == 3
    assert len(fake.create_calls) == 3

    # 第二轮：**一次**查询推进 3 个任务
    before = fake.batch_calls
    coord.tick()
    assert fake.batch_calls - before == 1
    assert coord.stats.advanced == 3
    assert service.store.count_active() == 0


def test_coordinator_respects_concurrency(service, fake) -> None:
    from app.coordinator import Coordinator

    service.settings = service.settings.replace(hl_concurrency=1)
    for i in range(3):
        service.create({"prompt": f"p{i}"}, credential="c")
    coord = Coordinator(service, service.settings)
    coord.tick()
    assert coord.stats.submitted == 1, "并发上限 1 ⇒ 一轮只建一个"
    assert service.store.count_queued() == 2, "其余的留在队列里"


def test_coordinator_skips_work_when_not_leader(service, fake) -> None:
    """抢不到租约 ⇒ 只空转，**不建任务**（否则多副本会重复计费）。"""
    from app.coordinator import Coordinator

    service.store.acquire_lease(owner="someone-else", ttl=600.0)
    service.create({"prompt": "x"}, credential="c")
    coord = Coordinator(service, service.settings)
    coord.tick()
    assert coord.stats.leader_skips == 1
    assert coord.stats.submitted == 0
    assert fake.create_calls == []


def test_coordinator_gate_stops_submission(service, fake) -> None:
    from app.coordinator import Coordinator

    service.gate_for("c").min_interval = 9999.0
    service.gate_for("c").note_submit()
    service.create({"prompt": "x"}, credential="c")
    coord = Coordinator(service, service.settings)
    coord.tick()
    assert coord.stats.submitted == 0
    assert coord.stats.submit_skipped == 1


def test_coordinator_marks_task_failed_on_adapter_error(service, fake) -> None:
    """**可判定性**错误（额度耗尽、参数被拒）⇒ 任务进 `failure` 并带上错误码。

    不这么做的话任务会静默留在 `queued`，而调用方永远等不到终态。
    """
    from app.coordinator import Coordinator

    import httpx
    fake.force_error["/v2/api/multimodal/generate/image"] = httpx.Response(
        200, json={"statusInfo": {"code": 30, "message": "积分不足，请充值"}})
    rec = service.create({"prompt": "x"}, credential="c")
    Coordinator(service, service.settings).tick()
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_FAILURE
    assert full["error"]["code"] == "upstream_quota_exhausted"


def test_coordinator_retries_pre_create_failure(service, fake) -> None:
    """🔴 **建任务前**的瞬时失败（输入图上传/连接被掐）⇒ 留在 `queued` 重试。

    这类失败上游**一定没有**建任务 ⇒ 重试不会重复计费；直接判 failure
    会让一次网络抖动毁掉整个任务（实测 2026-09-21：i2i 提交因 request_policy
    连接被掐而失败）。
    """
    from app.coordinator import Coordinator

    import httpx
    from .conftest import PNG_1PX

    url = "https://cdn.example.com/ref.png"
    fake.add_image(url, PNG_1PX)
    fake.force_error["/v1/api/files/request_policy"] = httpx.Response(500, text="boom")
    rec = service.create({"prompt": "x", "image": [url]}, credential="c")
    coord = Coordinator(service, service.settings)
    coord.tick()

    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_QUEUED, "建任务前失败必须留在队列里等重试"
    assert full["attempts"] == 1, "重试计数要落库"
    assert fake.create_calls == [], "🔴 绝不能建任务（否则就是重复计费）"

    # 网络恢复 ⇒ 下一次 tick 重试成功
    del fake.force_error["/v1/api/files/request_policy"]
    coord.tick()
    assert service.store.get_full(rec["task_id"])["status"] == ST_IN_PROGRESS
    assert len(fake.create_calls) == 1, "重试成功后只建一次任务"


def test_coordinator_gives_up_after_max_attempts(service, fake) -> None:
    """重试**有界**：到 `SUBMIT_MAX_ATTEMPTS` 仍失败 ⇒ 判 failure（不无限拖）。"""
    from app.coordinator import Coordinator

    import httpx
    from .conftest import PNG_1PX

    settings = service.settings.replace(submit_max_attempts=2)
    service.settings = settings
    url = "https://cdn.example.com/ref.png"
    fake.add_image(url, PNG_1PX)
    fake.force_error["/v1/api/files/request_policy"] = httpx.Response(500, text="boom")
    rec = service.create({"prompt": "x", "image": [url]}, credential="c")
    coord = Coordinator(service, settings)

    coord.tick()  # 第 1 次：失败 → 留在队列
    assert service.store.get_full(rec["task_id"])["status"] == ST_QUEUED
    coord.tick()  # 第 2 次：到上限 → 判失败
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_FAILURE
    assert full["attempts"] == 2
    assert fake.create_calls == [], "全程不得建任务"


def test_coordinator_keeps_task_queued_on_risk_control(service, fake) -> None:
    """🔴 风控**不是**任务失败 —— 它是"整体停一会儿"。

    把风控判成任务失败是错的：任务本身没问题，只是**现在**不能提交。
    ⇒ 进冷却、任务**留在队列**，冷却结束后自动重试。
    """
    from app.coordinator import Coordinator

    import httpx
    fake.force_error["/v2/api/multimodal/generate/image"] = httpx.Response(
        200, json={"statusInfo": {"code": 31, "message": "操作过于频繁，请稍后再试"}})
    rec = service.create({"prompt": "x"}, credential="c")
    coord = Coordinator(service, service.settings)
    coord.tick()
    assert service.store.get_full(rec["task_id"])["status"] == ST_QUEUED, \
        "风控不该把任务判死"
    assert service.gate_for("c").in_cooldown, "风控必须触发冷却（重试会延长标记）"


def test_poll_grace_defers_just_submitted_tasks(service, fake) -> None:
    """`POLL_GRACE` 语义：刚提交的任务在本轮**不打上游**（省掉一次必然白问的请求）。

    这条用例的存在本身就是门禁：曾经 `POLL_GRACE` 被读了却没被消费（假配置）。
    """
    from .conftest import PNG_1PX

    service.settings = service.settings.replace(poll_grace=999.0)
    img = "https://cdn.hailuoai.video/ref/a.png"
    fake.add_image(img, PNG_1PX)
    rec = service.create({"prompt": "x", "image": [img]}, credential="c")
    service.submit(rec["task_id"])

    before = fake.batch_calls
    res = service.poll_many(service.store.in_flight())
    assert res["deferred"] == 1 and res["polled"] == 0
    assert fake.batch_calls == before, "宽限期内不该打上游"


def test_coordinator_enters_cooldown_on_poll_risk_control(service, fake) -> None:
    from app.coordinator import Coordinator

    service.create({"prompt": "x"}, credential="c")
    coord = Coordinator(service, service.settings)

    def boom(*a, **k):
        raise RiskControlChallenge("轮询命中风控")

    service.poll_many = boom  # type: ignore[assignment]
    service.store.update_task(service.store.due_for_submit()[0]["task_id"],
                              status=ST_IN_PROGRESS)
    coord.tick()
    assert service.gate.in_cooldown


def test_coordinator_stats_view_shape(service) -> None:
    from app.coordinator import Coordinator

    view = Coordinator(service, service.settings).stats_view()
    for key in ("enabled", "owner", "running", "ticks", "submitted", "polled_rounds",
                "advanced", "expired", "leader_skips", "errors"):
        assert key in view


def test_live_app_starts_and_stops_coordinator(live_client, service) -> None:
    """lifespan 真的会把协调器**起起来再停掉**（不靠人记得）。"""
    app = live_client.app
    assert app.state.coordinator._thread is not None
    assert app.state.coordinator._thread.is_alive()


# ---------------------------------------------------------------------------
# 能力注册表
# ---------------------------------------------------------------------------


def test_frozen_snapshot_is_structurally_sane() -> None:
    """冻结快照必须自洽：每个模型有 id、有上限、单价能查。"""
    assert len(models.FROZEN_SNAPSHOT) == 11, "抓取到 11 个图片模型"
    for m in models.FROZEN_SNAPSHOT:
        assert m.model_id and m.display_name
        assert m.aspect_ratios, f"{m.model_id} 应当有比例档位"
        assert m.costs or m.default_cost, f"{m.model_id} 应当有计价依据"


def test_cost_lookup_by_resolution_and_quality() -> None:
    nano = models.get_model("nano_banana21_flash")
    assert nano is not None
    assert nano.cost_for("1K") == 4 and nano.cost_for("4K") == 8
    assert nano.cost_for("9K") is None, "查不到就返回 None，不猜"

    gpt = models.get_model("gpt-image-2")
    assert gpt is not None
    assert gpt.cost_for("2K", "low") == 5
    assert gpt.cost_for("2K", "high") == 80
    assert gpt.cost_for("2K", "nonsense") is None


def test_default_model_exists_and_has_capture_evidence() -> None:
    """默认模型必须是**抓包里实测跑通**的那个（不是最便宜的那个）。"""
    m = models.get_model(models.DEFAULT_MODEL)
    assert m is not None
    assert m.model_id == "nano_banana21_flash"
    assert m.max_support_image_count == 14


def test_resolve_capability_hits_names_aliases_and_upstream_keys() -> None:
    for value in ("hailuo-i2i", "I2I", "图生图", "i2i", "edit"):
        cap, _ = models.resolve_capability(value, has_image=True)
        assert cap == "hailuo-i2i", value
    for value in ("hailuo-t2i", "t2i", "文生图", "text2image"):
        cap, _ = models.resolve_capability(value, has_image=False)
        assert cap == "hailuo-t2i", value
    cap, notes = models.resolve_capability("nano-banana2", has_image=True)
    assert cap == "hailuo-i2i"
    #: 🔴 原始枚举值**零映射零说明** —— 名字就是上游 modelID，没有"翻译"这回事
    assert notes == []


def test_model_aliases_resolve_to_canonical_ids() -> None:
    """模型别名（2026-09-21 定义）⇒ 规范 modelID，与精确枚举值**同待遇（零说明）**。

    🔴 `nano-banana-2`（flash）与 `nano-banana2`（pro）**只差一个连字符** ——
    上游就这么命名的，别名表必须一字不差。
    """
    assert models.canonical_model_id("nano-banana") == "nano_banana21_flash", \
        "裸 nano-banana ⇒ flash（newapi 默认短名，2026-10-10 加）"
    assert models.canonical_model_id("nano-banana-2") == "nano_banana21_flash"
    assert models.canonical_model_id("nano-banana-pro") == "nano-banana2"
    assert models.canonical_model_id("midjourney-v7") == "mj_v7"
    assert models.canonical_model_id("mj-niji7") == "mj_niji7"
    assert models.canonical_model_id("image-1.0") == "image-01"
    assert models.canonical_model_id("seedream-5.0-lite") == "seedream-5.0"
    assert models.canonical_model_id("nano-banana2") == "nano-banana2", "规范值原样返回"
    cap, notes = models.resolve_capability("nano-banana-pro", has_image=False)
    assert cap == "hailuo-t2i" and notes == []


def test_resolve_capability_rejects_unknown() -> None:
    from app.errors import InvalidParameterError

    with pytest.raises(InvalidParameterError):
        models.resolve_capability("gpt-5-turbo", has_image=False)


def test_catalog_lists_capabilities_and_models() -> None:
    data = models.catalog()
    ids = {d["id"] for d in data}
    assert {"hailuo-i2i", "hailuo-t2i", "hailuo-image"} <= ids
    assert "nano_banana21_flash" in ids
    cap = next(d for d in data if d["id"] == "hailuo-i2i")
    assert cap["requires_image"] is True and cap["kind"] == "capability"


def test_runtime_models_take_precedence_over_snapshot() -> None:
    assert models.status()["source"] == "frozen_snapshot"
    models.install_runtime_models([models.FROZEN_SNAPSHOT[0]])
    assert models.status()["source"] == "runtime"
    assert len(models.all_models()) == 1


def test_deliberate_absences_are_documented() -> None:
    """"没登记"必须是一个**决定**而不是遗漏 ⇒ 要有文字说明。"""
    assert models.DELIBERATE_ABSENCES
    assert "videoModels" in models.DELIBERATE_ABSENCES


# ---------------------------------------------------------------------------
# 启动目标（Dockerfile / gunicorn）
# ---------------------------------------------------------------------------


def test_dockerfile_cmd_target_resolves() -> None:
    """🔴 `gunicorn … "app.main:create_app()"` 里的**括号不能省**。

    目标是**工厂**而不是模块级 `app` 对象。写成 `app.main:app` 会得到
    `Failed to find attribute 'app' in 'app.main'` / `App failed to load.`
    —— 而**单测全绿也照样炸**，因为它们都直接调 `create_app()`。

    这条用例把"启动命令里的目标真的存在且可调用"钉死：它解析 Dockerfile 的 CMD
    与 gunicorn 配置里的绑定串，逐个 import 并确认拿得到工厂。
    """
    import importlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]

    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    cmd_match = re.search(r'^CMD\s+\[(.*)\]\s*$', dockerfile, re.MULTILINE)
    assert cmd_match, "Dockerfile 里找不到 CMD"
    argv = json.loads(f"[{cmd_match.group(1)}]")
    assert argv[-1].endswith(":create_app()"), f"CMD 目标必须带括号：{argv[-1]}"
    assert "app.main:app" not in " ".join(argv), "不许用模块级 app"

    targets = [argv[-1]]

    # gunicorn_conf.py 本身也要能 import（它读 env，别在 import 期炸）
    conf = importlib.import_module("gunicorn_conf")
    assert conf.workers >= 1
    assert conf.worker_class == "uvicorn.workers.UvicornWorker"

    # 文档/README 里的示例也必须带括号（否则照着抄的人会踩同一个坑）
    for name in ("README.md", "gunicorn_conf.py"):
        text = (root / name).read_text(encoding="utf-8")
        for m in re.finditer(r'"(app\.main:[^"]+)"', text):
            targets.append(m.group(1))
            assert m.group(1).endswith("()"), f"{name} 里的目标缺括号：{m.group(1)}"

    for target in targets:
        module_path, _, attr = target.partition(":")
        assert attr.endswith("()"), target
        func = getattr(importlib.import_module(module_path), attr[:-2])
        assert callable(func), f"{target} 不是可调用的工厂"


def test_app_factory_is_importable_by_gunicorn() -> None:
    """`app.main:create_app` 必须能在**不读环境**的情况下被 import（工厂惰性求值）。"""
    import importlib

    mod = importlib.import_module("app.main")
    assert callable(mod.create_app)
    assert callable(mod.app_factory)


def test_multi_account_queues_are_isolated(service, fake) -> None:
    """🔴 号池模式的核心前提：**每账号队列隔离**。

    · A 在途满额 ⇒ 只有 A 的后续任务排队，B 照常提交（旧实现是全局一个队列，
      B 会被 A 挤掉——这正是 newapi 号池接入前必须改掉的行为）；
    · A 闸门冷却 ⇒ 只停 A，B 的队列照常推进。
    """
    from app.coordinator import Coordinator

    service.settings = service.settings.replace(hl_concurrency=1)
    a1 = service.create({"prompt": "a1"}, credential="acct-a")
    a2 = service.create({"prompt": "a2"}, credential="acct-a")
    b1 = service.create({"prompt": "b1"}, credential="acct-b")

    Coordinator(service, service.settings).tick()
    assert service.store.get_full(a1["task_id"])["status"] == ST_IN_PROGRESS
    assert service.store.get_full(a2["task_id"])["status"] == ST_QUEUED, \
        "A 账号第二个任务必须在自己的队列里等（每账号 1 并发）"
    assert service.store.get_full(b1["task_id"])["status"] == ST_IN_PROGRESS, \
        "B 账号不能被 A 的满额在途挤掉"


def test_multi_account_gate_cooldown_is_per_account(service, fake) -> None:
    """A 命中风控进冷却 ⇒ 只停 A 自己；B 的提交不受影响。"""
    from app.coordinator import Coordinator

    service.settings = service.settings.replace(hl_concurrency=2)
    service.gate_for("acct-a").enter_cooldown("模拟 A 命中风控")
    a = service.create({"prompt": "a1"}, credential="acct-a")
    b = service.create({"prompt": "b1"}, credential="acct-b")

    Coordinator(service, service.settings).tick()
    assert service.store.get_full(a["task_id"])["status"] == ST_QUEUED, \
        "A 冷却期内必须被自己的闸门挡住"
    assert service.store.get_full(b["task_id"])["status"] == ST_IN_PROGRESS, \
        "A 的冷却绝不能拖累 B 的提交"
    assert len(fake.create_calls) == 1
    assert service.gate_for("acct-a").in_cooldown
    assert not service.gate_for("acct-b").in_cooldown
