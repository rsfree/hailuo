#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游对账：把"已建到上游、本地没记下 batch id"的任务认回来。

这是唯一会造成**资损**的路径：请求发出去了、上游建了任务并计费，但响应丢在网络上
（或进程恰在此刻被杀）。没有对账，调用方看到"失败"，钱和产物都白扔。

判据（四条同时成立，少一条都不认）：批次未被占用 / model 相同 / prompt 逐字相同 /
批次时间与本地提交时间偏差 ≤ window。命中唯一才认领，多个记 ambiguous 放弃。
"""
from __future__ import annotations

import time

import pytest

from app.service import Service

PROMPT = "reconcile-probe-a-red-cube-on-golden-sand"


def _make(service: Service, credential: str = "cred-1") -> str:
    rec = service.create({"model": "nano_banana21_flash", "prompt": PROMPT,
                          "resolution": "1K"}, credential=credential)
    return rec["task_id"]


def test_lost_response_is_adopted(service, fake) -> None:
    """建任务成功但响应丢失 ⇒ 对账必须把上游批次认回来，任务变成功。"""
    task_id = _make(service)
    out = service.submit(task_id)
    assert out["submitted"] is True
    batch_id = service.store.get_full(task_id)["upstream_batch_id"]

    #: 模拟"响应丢了"：本地没有 batch id，任务被判失败
    service.store.update_task(task_id, upstream_batch_id="", status="failure")

    stats = service.reconcile(window=600)
    assert stats["adopted"] == 1, stats
    full = service.store.get_full(task_id)
    assert full["status"] == "succeeded"
    assert full["upstream_batch_id"] == batch_id
    assert full["result"]["data"][0]["url"]


def test_claimed_batch_is_not_reassigned(service, fake) -> None:
    """已被占用的批次不许再认给别的任务（一图不认二主）。"""
    owner_task = _make(service)
    service.submit(owner_task)
    batch_id = service.store.get_full(owner_task)["upstream_batch_id"]

    orphan = _make(service)          # 同 prompt，但从未提交
    service.store.update_task(orphan, status="failure")

    stats = service.reconcile(window=600)
    assert stats["adopted"] == 0, stats
    assert service.store.get_full(orphan)["upstream_batch_id"] == ""
    assert service.store.get_full(owner_task)["upstream_batch_id"] == batch_id


def test_time_window_rejects_far_away_batch(service, fake) -> None:
    """时间偏差超过 window ⇒ 不认（防止把**历史**批次错认成这次的）。

    ⚠️ 不能靠把 window 设成 0.001s —— 批次创建与本地提交相差只有几毫秒，
    那样测的是运气。直接把批次时间改到一小时前，才是确定性的判据。
    """
    task_id = _make(service)
    service.submit(task_id)
    batch_id = service.store.get_full(task_id)["upstream_batch_id"]
    fake.feed_meta[batch_id]["create_time"] = int((time.time() - 3600) * 1000)
    service.store.update_task(task_id, upstream_batch_id="", status="failure")

    stats = service.reconcile(window=600)
    assert stats["adopted"] == 0, stats
    assert service.store.get_full(task_id)["upstream_batch_id"] == ""


def test_prompt_mismatch_is_not_adopted(service, fake) -> None:
    """prompt 不同 ⇒ 不认（同模型同时间的其它任务是别人的）。"""
    task_id = _make(service)
    service.submit(task_id)
    service.store.update_task(task_id, upstream_batch_id="", status="failure",
                              request_json='{"model":"nano_banana21_flash","prompt":"完全不同的提示词","resolution":"1K"}')
    stats = service.reconcile(window=600)
    assert stats["adopted"] == 0


def test_reconcile_is_noop_without_orphans(service) -> None:
    stats = service.reconcile()
    assert stats == {"scanned": 0, "adopted": 0, "ambiguous": 0,
                     "unmatched": 0, "errors": 0}


@pytest.mark.parametrize("enabled", [False])
def test_coordinator_skips_when_disabled(service, fake, enabled) -> None:
    from app.coordinator import Coordinator

    service.settings = service.settings.replace(reconcile_enabled=enabled)
    coord = Coordinator(service, service.settings)
    coord._maybe_reconcile()
    assert coord._last_reconcile == 0.0, "关掉时不应打分对账时间戳"
