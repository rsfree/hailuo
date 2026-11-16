#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口凭据解析：**透传**与**号池**两条路，一个出口。

## 两条路怎么分（判据是 token 形态，不是配置）

- 三段式 `header.payload.signature` ⇒ **透传**：这个 token 就是上游凭证，
  行为与本项目旧版本完全一致（费用记在调用方自己账上）；
- 否则必须逐字命中 `SERVICE_KEYS` 之一 ⇒ **池化出号**：服务从账号表挑一个，
  解密其 token、登记进内存池，任务归属键 = 该账号指纹。

## 为什么不能"服务 Key 失败就回落透传"

回落等于把一次本该 401 的请求变成一次**计费动作**，而且记在服务自己的账号上。
两种失败语义必须分开报：

- Key 不认识 ⇒ **401 `invalid_api_key`**（调用方写错）；
- 号池此刻没有可用账号 ⇒ **503 `credential_unavailable`**（服务端拿不出号）。

## 已知边界（不粉饰）

池化模式下任务按**账号指纹**归属 ⇒ 多个服务 Key 共用同一账号时会看到彼此的任务。
单租户部署下这是可接受的；多租户要隔离，得给任务表加 owner 列（第二期未做，
见 `.fixloop/criteria.md` 的 U2）。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .accounts import AccountCipher, AccountStore
from .config import parse_service_keys
from .credentials import parse_jwt
from .errors import AuthError, CredentialUnavailable


@dataclass(frozen=True)
class Caller:
    """入口调用方的双身份。

    · `credential_id` —— **用哪个号跑**（决定上游客户端与闸门，可能来自号池）；
    · `owner_id` —— **谁能看见**（任务归属）。池化模式下多个服务 Key 共用同一账号，
      但彼此看不到对方的任务；透传模式下两者同值，行为与旧版逐字一致。
    """

    credential_id: str
    owner_id: str
    #: 服务 Key 的标签与每日配额（透传模式两者都为空/0）
    label: str = ""
    quota: int = 0


class PoolAuth:
    """把入口 Bearer 解析成 `credential_id`（任务归属键）。"""

    def __init__(self, *, settings: Any, service: Any,
                 accounts: AccountStore, cipher: AccountCipher | None) -> None:
        self.settings = settings
        self.service = service
        self.accounts = accounts
        self.cipher = cipher

    @property
    def service_keys(self) -> tuple[str, ...]:
        return tuple(self.settings.service_keys or ())

    def spec_of(self, key: str) -> tuple[str, int] | None:
        """常量时间匹配（不短路），命中返回 `(label, quota)`。"""
        specs = parse_service_keys(self.service_keys)
        hit: tuple[str, int] | None = None
        for k, spec in specs.items():
            if secrets.compare_digest(key, k):
                hit = spec
        return hit

    def resolve(self, key: str) -> Caller:
        if key.count(".") == 2:
            cred = self.service.register_credential(key)
            return Caller(credential_id=cred, owner_id=cred)
        spec = self.spec_of(key)
        if spec is None:
            raise AuthError(
                "Authorization 既不是 hailuo JWT（三段式），也不是本服务配置的服务 Key。"
                "透传请传你的 hailuo 登录 token；号池请用 SERVICE_KEYS 里的 Key。")
        return self._issue_from_pool(key, *spec)

    def _is_service_key(self, key: str) -> bool:
        #: 逐字常量时间比较：`in` 会短路，长度/前缀可被计时侧信道利用。
        return any(secrets.compare_digest(key, k) for k in self.service_keys)

    def _busy_fingerprints(self) -> set[str]:
        """已达**单账号并发上限**的指纹集合（按库里的在途任务计数）。

        轮换的意义不只是"分摊额度"，还包括"别把请求压到一个正在跑的号上"：
        `HL_CONCURRENCY=1` 时一个号同时只能跑一条，压上去只会白排队。
        """
        limit = max(1, int(getattr(self.settings, "hl_concurrency", 1)))
        counts = self.service.store.active_counts_by_credential()
        return {fp for fp, n in counts.items() if fp and n >= limit}

    def _issue_from_pool(self, key: str, label: str, quota: int) -> Caller:
        if self.cipher is None:
            raise CredentialUnavailable(
                "号池未配置加密主密钥（ACCOUNT_ENC_KEY），无法解密账号 token —— "
                "请在环境变量里配置 32 字节密钥后重启。")
        rec = self.accounts.pick(busy_fps=self._busy_fingerprints())
        if rec is None:
            raise CredentialUnavailable(
                "号池当前没有可用账号（全部为停用/过期/冷却中）。"
                "请在管理面板补充账号或检查巡检结果。")
        token = self.accounts.token_of(rec["id"], self.cipher)
        if not token:
            raise CredentialUnavailable(
                f"账号 {rec['label'] or rec['id']} 的密文无法解密（密钥可能已更换）。")
        try:
            claims = parse_jwt(token)
        except AuthError as e:
            #: 库里过期却没被选号过滤掉 ⇒ 判死并如实抛，不换号重试。
            self.accounts.record_failure(rec["id"], error=str(e), fatal=True)
            logger.warning(f"号池账号 {rec['id'][:8]} 本地校验失败：{e}")
            raise CredentialUnavailable(
                f"号池账号 {rec['label'] or rec['id']} 的 token 已过期或结构非法：{e}") from e

        #: 登记进内存池：后续 `clients_for()` 按同一个指纹取客户端。
        self.service.credentials.put(rec["fp"], token, claims)
        self.accounts.record_use(rec["id"])
        #: 归属 = **该服务 Key** 的指纹（不是账号指纹）：隔离的落点在这里。
        fp = self.service.credential_of(key)
        owner = f"svc-{label}-{fp[:8]}" if label else "svc-" + fp
        return Caller(credential_id=rec["fp"], owner_id=owner,
                      label=label, quota=quota)


__all__ = ["Caller", "PoolAuth", "parse_service_keys"]
