#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容违规 451 + 负缓存 —— 零出网单元测试。

覆盖：
1. `view()`：content_policy 失败 ⇒ **451**（其余失败仍 200）；
2. 受理命中负缓存 ⇒ 抛 451 的 ContentPolicyError（不落库）；
3. 轮询终态敏感 / 提交阶段 ContentPolicyError ⇒ 写入负缓存；
4. 不同 prompt/图不受影响；TTL 过期后放行；开关关闭恒放行。
"""
from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.errors import ContentPolicyError
from app.service import Service, view
from app.upstream.hailuo.client import Feed

PROMPT_BAD = "违规提示词"
PROMPT_OK = "a cute cat"
IMG = "https://cdn.example.com/a.png"


def make_svc(**kw) -> Service:
    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t", **kw)
    st.validate()
    return Service(st, fetch_capabilities=False)


def make_store_with_task(svc: Service, task_id: str, prompt: str) -> None:
    """往内存任务库塞一条带 plan 的 queued 任务（供 _fail_task/_apply_feeds 用）。"""
    svc.store.create_task(
        task_id=task_id, credential_id="c", capability="hailuo-t2i",
        model="gpt-image-2.5-flare", upstream_model="gpt-image-2.5-flare",
        status="queued", request_json="{}",
        plan_json='{"desc": %s, "image_urls": ["%s"], "forecast_credits": 2}'
        % (repr(prompt).replace("'", '"'), IMG),
        degradations_json="[]")


# ---------------------------------------------------------------------------
# view()：451 映射
# ---------------------------------------------------------------------------


def test_view_content_policy_failure_is_451() -> None:
    rec = {"task_id": "t1", "status": "failure",
           "error": {"message": "违反社区规范", "type": "invalid_request_error",
                     "code": "content_policy_violation"}}
    code, body = view(rec)
    assert code == 451
    assert body["error"]["code"] == "content_policy_violation"


def test_view_other_failures_stay_200() -> None:
    rec = {"task_id": "t1", "status": "failure",
           "error": {"message": "上游生成失败。", "type": "upstream_error",
                     "code": "task_failed"}}
    code, _ = view(rec)
    assert code == 200, "只有内容违规才 451，其余失败仍 200（避免误触发重试）"


# ---------------------------------------------------------------------------
# 负缓存：命中 / 写入 / 隔离 / TTL / 开关
# ---------------------------------------------------------------------------


def test_create_rejects_on_negcache_hit() -> None:
    svc = make_svc()
    try:
        svc._negcache_put(svc._negcache_key(PROMPT_BAD, [IMG]))
        with pytest.raises(ContentPolicyError) as ei:
            svc.create({"model": "gpt-image-2.5-flare",
                        "prompt": PROMPT_BAD, "image": [IMG]}, credential="c")
        assert ei.value.status_code == 451
        assert "负缓存" in str(ei.value)
        assert svc.store.stats()["counts"].get("queued", 0) == 0, "命中 ⇒ 不落库"
    finally:
        svc.close()


def test_negcache_written_on_sensitive_terminal() -> None:
    """轮询终态命中敏感（feed status=5）⇒ 写负缓存 ⇒ 同组合二次受理 451。"""
    from app.upstream.hailuo.client import ST_SENSITIVE

    svc = make_svc()
    try:
        make_store_with_task(svc, "t1", PROMPT_BAD)
        feed = Feed(feed_id="f1", batch_id="b1", status=ST_SENSITIVE,
                    feed_type=1, create_time=1, url="")
        assert svc._apply_feeds("t1", [feed]) is True
        assert svc._negcache_hit(svc._negcache_key(PROMPT_BAD, [IMG]))
        with pytest.raises(ContentPolicyError):
            svc.create({"model": "gpt-image-2.5-flare",
                        "prompt": PROMPT_BAD, "image": [IMG]}, credential="c")
    finally:
        svc.close()


def test_negcache_written_on_submit_policy_error() -> None:
    """提交阶段上游同步拒（ContentPolicyError）⇒ _fail_task 顺带写负缓存。"""
    svc = make_svc()
    try:
        make_store_with_task(svc, "t1", PROMPT_BAD)
        svc._fail_task("t1", ContentPolicyError("检测到提示词或图片违规"))
        assert svc._negcache_hit(svc._negcache_key(PROMPT_BAD, [IMG]))
    finally:
        svc.close()


def test_negcache_key_distinguishes_prompt_and_images() -> None:
    """不同 prompt / 不同图 ⇒ 不同 key（互不误伤）。"""
    svc = make_svc()
    try:
        svc._negcache_put(svc._negcache_key(PROMPT_BAD, [IMG]))
        # 换 prompt ⇒ 放行
        plan, _, _ = svc.build_plan({"model": "gpt-image-2.5-flare",
                                     "prompt": PROMPT_OK, "image": [IMG]})
        assert plan.desc == PROMPT_OK
        svc.create({"model": "gpt-image-2.5-flare", "prompt": PROMPT_OK,
                    "image": [IMG]}, credential="c")
        # 换图 ⇒ 放行
        svc.create({"model": "gpt-image-2.5-flare", "prompt": PROMPT_BAD,
                    "image": ["https://cdn.example.com/other.png"]},
                   credential="c")
    finally:
        svc.close()


def test_negcache_ttl_expiry() -> None:
    svc = make_svc(negcache_ttl=1.0)
    try:
        key = svc._negcache_key(PROMPT_BAD, [IMG])
        svc._negcache_put(key)
        assert svc._negcache_hit(key)
        with pytest.raises(ContentPolicyError):
            svc.create({"model": "gpt-image-2.5-flare", "prompt": PROMPT_BAD,
                        "image": [IMG]}, credential="c")
        # 人为把过期时刻拨回过去 ⇒ 过期放行
        svc._negcache[key] = time.time() - 1
        assert not svc._negcache_hit(key)
        svc.create({"model": "gpt-image-2.5-flare", "prompt": PROMPT_BAD,
                    "image": [IMG]}, credential="c")
    finally:
        svc.close()


def test_negcache_disabled_switch() -> None:
    svc = make_svc(negcache_enabled=False)
    try:
        key = svc._negcache_key(PROMPT_BAD, [IMG])
        svc._negcache_put(key)
        assert svc._negcache == {}, "关闭时不写入"
        assert not svc._negcache_hit(key)
        svc.create({"model": "gpt-image-2.5-flare", "prompt": PROMPT_BAD,
                    "image": [IMG]}, credential="c")
    finally:
        svc.close()


def test_negcache_stats_visible() -> None:
    svc = make_svc()
    try:
        svc._negcache_put(svc._negcache_key(PROMPT_BAD, [IMG]))
        st = svc.status()
        assert st["negcache"] == {"enabled": True, "entries": 1, "ttl": 86400.0}
    finally:
        svc.close()
