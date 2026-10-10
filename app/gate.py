#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节奏闸门 —— 建任务（**计费动作**）前的最后一道门。

## 为什么闸门只管"建任务"，不管"轮询"

轮询是**只读**的（batch 列表），它不花钱、也不构成"提交风暴"；
建任务才是计费动作，也是上游风控最敏感的维度。把两者放进同一个闸门
会导致"轮询被限"这种毫无必要的自伤。

## 三道闸 + 一道冷却

| 闸 | 配置 | 作用 |
|---|---|---|
| 最小间隔 | `HL_MIN_INTERVAL` | 相邻两次提交之间的最短墙钟间隔 |
| 每分钟上限 | `HL_PER_MINUTE` | 滑动窗口内的提交次数上限 |
| 风控冷却 | `HL_COOLDOWN` | 命中风控后**整体停建**一段时间 |

## 闸门的隔离维度：**按账号（凭据）**

每个凭据指纹一把独立 `Gate` 实例（`Service.gate_for` 惰性创建）：
账号 A 的风控冷却/节奏不影响账号 B —— 这是多账号号池模式的前提
（总并发 = N 账号 × `HL_CONCURRENCY`）。

## 🔴 进程内状态 = 扩容约束

闸门状态在**进程内** ⇒ 副本数 N 等于把限速整体乘 N，
恰好踩在上游风控最敏感的维度上。所以默认 `WORKERS=1` 是**架构约束**不是保守参数。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateDecision:
    allowed: bool
    reason: str = ""
    #: 被拒时**建议等多久**（秒）；`None` = 不知道（**不编数字**）
    wait_hint: float | None = None


@dataclass
class Gate:
    """建任务闸门。**线程安全**（协调器在后台线程）。"""

    min_interval: float = 0.0
    per_minute: int = 0
    cooldown: float = 600.0

    _last_submit: float = 0.0
    #: 最近提交的墙钟时间戳（滑动窗口）
    _stamps: deque[float] = field(default_factory=deque)
    _cooldown_until: float = 0.0
    _cooldown_reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ 判定

    def check(self, *, now: float | None = None) -> GateDecision:
        now = time.time() if now is None else now
        with self._lock:
            if now < self._cooldown_until:
                return GateDecision(
                    False,
                    f"风控冷却中（{self._cooldown_until - now:.0f}s 后解除）："
                    f"{self._cooldown_reason}",
                    wait_hint=self._cooldown_until - now,
                )
            self._trim(now)
            if self.per_minute and len(self._stamps) >= self.per_minute:
                oldest = self._stamps[0]
                return GateDecision(
                    False,
                    f"已达每分钟上限 {self.per_minute}（最近一次 {now - oldest:.1f}s 前）",
                    wait_hint=max(0.0, 60.0 - (now - oldest)),
                )
            if self.min_interval and self._last_submit:
                gap = now - self._last_submit
                if gap < self.min_interval:
                    return GateDecision(
                        False,
                        f"距上次提交仅 {gap:.2f}s < 最小间隔 {self.min_interval}s",
                        wait_hint=self.min_interval - gap,
                    )
            return GateDecision(True)

    def note_submit(self, *, now: float | None = None) -> None:
        """记一次**成功提交**。只在真的发出去了之后调用。"""
        now = time.time() if now is None else now
        with self._lock:
            self._last_submit = now
            self._stamps.append(now)
            self._trim(now)

    def enter_cooldown(self, reason: str, *, seconds: float | None = None) -> None:
        """命中风控 ⇒ 整体停建。**重试会延长标记**，所以这里是硬停不是退避。"""
        with self._lock:
            self._cooldown_until = time.time() + (self.cooldown if seconds is None else seconds)
            self._cooldown_reason = reason

    def _trim(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] > 60.0:
            self._stamps.popleft()

    # ------------------------------------------------------------------ 观测

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            self._trim(now)
            return {
                "min_interval": self.min_interval,
                "per_minute": self.per_minute,
                "submits_last_minute": len(self._stamps),
                "last_submit_ago": (round(now - self._last_submit, 2)
                                    if self._last_submit else None),
                "in_cooldown": now < self._cooldown_until,
                "cooldown_remaining": (round(self._cooldown_until - now, 1)
                                       if now < self._cooldown_until else 0.0),
                "cooldown_reason": self._cooldown_reason,
            }

    def reset(self) -> None:
        with self._lock:
            self._last_submit = 0.0
            self._stamps.clear()
            self._cooldown_until = 0.0
            self._cooldown_reason = ""


def build(settings: Any) -> Gate:
    """从配置造闸门。**唯一出口** —— 别在别处手搓 `Gate(...)`。"""
    return Gate(min_interval=settings.hl_min_interval,
                per_minute=settings.hl_per_minute,
                cooldown=settings.hl_cooldown)


__all__ = ["Gate", "GateDecision", "build"]
