#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外错误分类体系。

一个 `AdapterError` 子类 = 一个 `code` = 一个 HTTP 状态码 = **一句"下一步该做什么"**。

设计纪律（照搬 jimeng 的结论，因为它是对的）：
  · **区分"上游没有"与"你写错了"** —— 前者进 `degradations`，后者才 4xx；
  · **部署问题不是调用方的问题** —— 没配 `token` 回 503，不回 401；
  · **`Retry-After` 只在真知道时给** —— 编一个数字等于伪造事实；
  · **失败也回 200**（任务完成了，只是结果是失败），否则会误触发调用方的重试。
"""
from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """所有对外错误的基类。HTTP 状态码**来自错误类本身**，不来自调用点。"""

    status_code: int = 502
    code: str = "upstream_error"
    error_type: str = "upstream_error"

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        retry_after: float | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.retry_after = retry_after
        self.detail = detail

    def to_error(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code,
        }
        if self.param:
            err["param"] = self.param
        if self.retry_after is not None:
            err["retry_after"] = self.retry_after
        if self.detail is not None:
            err["detail"] = self.detail
        return {"error": err}


# ---------------------------------------------------------------------------
# 4xx —— 调用方的问题
# ---------------------------------------------------------------------------


class InvalidParameterError(AdapterError):
    """请求写错了。`param` 指出是哪个字段 —— 这是调用方唯一需要的线索。"""

    status_code = 400
    code = "invalid_parameter"
    error_type = "invalid_request_error"


class ContentPolicyError(AdapterError):
    """上游送审 / 版权拦截。**换 prompt 或换图**，重试无效。

    🔴 HTTP **451**（Unavailable For Legal Reasons，2026-10-15 用户口径）：
    内容违规对调用方来说必须一眼可辨、且**不可重试**——200+failure 会被
    当成普通失败重试，400 语义太泛。同义错误（受理期上游同步拒 /
    轮询终态敏感 / 负缓存命中）都走这一个类，状态码天然一致。
    """

    status_code = 451
    code = "content_policy_violation"
    error_type = "invalid_request_error"


class AuthError(AdapterError):
    """调用方的 Key 不对。"""

    status_code = 401
    code = "invalid_api_key"
    error_type = "invalid_request_error"


class TaskNotFoundError(AdapterError):
    """任务不存在，或不属于当前 Key（**刻意不区分** —— 区分开等于确认 id 存在）。"""

    status_code = 404
    code = "task_not_found"
    error_type = "invalid_request_error"


# ---------------------------------------------------------------------------
# 429 —— 可以退避重试，或明确不能
# ---------------------------------------------------------------------------


class UpstreamRateLimited(AdapterError):
    """上游限流。**可退避重试**。"""

    status_code = 429
    code = "upstream_rate_limited"
    error_type = "rate_limit_error"


class UpstreamQuotaExhausted(AdapterError):
    """积分 / 日额度耗尽。**重试无效** —— 别让调用方白等。"""

    status_code = 429
    code = "upstream_quota_exhausted"
    error_type = "rate_limit_error"


class RiskControlChallenge(AdapterError):
    """命中风控。重试会**延长**标记，服务侧已进入冷却。"""

    status_code = 429
    code = "risk_control_challenge"
    error_type = "rate_limit_error"


# ---------------------------------------------------------------------------
# 5xx —— 我们这边的问题
# ---------------------------------------------------------------------------


class UpstreamError(AdapterError):
    """上游 5xx / 非 JSON / WAF 页。"""

    status_code = 502
    code = "upstream_error"
    error_type = "upstream_error"


class UpstreamTimeout(AdapterError):
    status_code = 504
    code = "upstream_timeout"
    error_type = "upstream_error"


class UpstreamNotConfigured(AdapterError):
    """服务未配上游凭据 —— **部署问题**，不是调用方的错（故不是 401）。"""

    status_code = 503
    code = "upstream_not_configured"
    error_type = "service_unavailable"


class CredentialUnavailable(AdapterError):
    """透传凭据不在内存池里（进程重启 / 已过期 / 被容量淘汰）。

    **不是 401**：调用方那一次请求当时是合法的 —— 是服务端在这一刻拿不出
    对应凭据（明文凭据不落库，重启即丢）。任务会以本错误失败并**明确说明原因**，
    绝不静默改用服务端账号（那会把费用记到错误的人头上）。
    """

    status_code = 503
    code = "credential_unavailable"
    error_type = "service_unavailable"


class SyncUnavailable(AdapterError):
    """同步端点没有任务推进者（协调器线程没在跑，如 COORDINATOR_ENABLED=0）。

    **快速 503** 而不是让调用方白等一整个预算 —— 那种部署形态下任务根本
    不会被推进，同步等待等满预算也是白等。
    """

    status_code = 503
    code = "sync_unavailable"
    error_type = "service_unavailable"


class SyncBusy(AdapterError):
    """同步等待槽位已满（并发护栏，`SYNC_MAX_CONCURRENCY`）。

    推进者活着、服务没坏 —— 只是当前同步等待数到了上限。**429 + `Retry-After`**
    秒回（不等预算），调用方退避重试或改走异步端点。护栏的意义：同步等待
    阻塞的是线程池线程（默认 40 个），不设上限的话大量慢请求会把线程池吃光，
    **殃及同进程其他同步路径** —— 槽满秒回把最坏情况钉死在有限线程上。
    """

    status_code = 429
    code = "sync_busy"
    error_type = "rate_limit_error"


class CapabilityUnavailable(AdapterError):
    """能力当前不可用（如上游能力表读不到且无冻结快照）。"""

    status_code = 503
    code = "capability_unavailable"
    error_type = "service_unavailable"


class TaskNotDeletable(AdapterError):
    """未终态的任务不能删 —— 上游**没有取消端点**，本地置删不会让上游停下来。"""

    status_code = 400
    code = "task_not_deletable"
    error_type = "invalid_request_error"


__all__ = [
    "AdapterError",
    "AuthError",
    "CapabilityUnavailable",
    "ContentPolicyError",
    "InvalidParameterError",
    "RiskControlChallenge",
    "TaskNotDeletable",
    "TaskNotFoundError",
    "UpstreamError",
    "UpstreamNotConfigured",
    "UpstreamQuotaExhausted",
    "UpstreamRateLimited",
    "UpstreamTimeout",
]
