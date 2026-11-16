#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池：账号表 + 信封加密 + 选号。

## 与旧纪律的关系（**明确变更**）

旧设计是「明文凭据永不落库」：库里只有指纹，token 只活进程内存 ⇒ 重启即空。
号池要能跨重启管理，就必须存 token。2026-11-16 用户口径是**加密落库**：

- 密文 = `AES-256-GCM`，随机 nonce，格式 `v1.<base64url(nonce|ciphertext)>`；
- 主密钥来自环境变量 `ACCOUNT_ENC_KEY`（base64url/hex 的 32 字节），**不入库**——
  库被拿走而密钥不在，密文就是垃圾；反之若把密钥也写进同一个库，等于没加密；
- 库里仍然只暴露**指纹**（`HMAC-SHA256(secret, token)`，与 `Service.credential_of` 同一实现）
  ⇒ 指纹可以继续做任务归属键，不必解密密文。

## 选号判据（可解释，不神秘）

`status=active` 且 `exp > now + 60` 且 `cooldown_until <= now`，按
**权重降序 → 最久未用 → 最早到期** 排序取第一个：

- 权重让运维能手加压测/主力号；
- 最久未用把流量摊开（避免同一账号被连续打到风控）；
- 最早到期者先用 ⇒ 快到期的余额不浪费。

失败处理分两级：`fatal=True`（401/403，token 死了）直接 `dead`；
否则记 `fail_streak` 并按调用方给的 `cooldown` 冷却。**绝不静默换号重试上游计费动作**——
本模块只负责"下次别选它"。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import uuid
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Boolean, Float, Integer, String, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from .store import Base
from .upstream.hailuo.client import HailuoClient

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_DEAD = "dead"
_STATUSES = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_DEAD)

#: `pick()` 的过期余量：与 `parse_jwt` 的 60s 时钟偏差容忍同值。
EXP_SKEW = 60.0


class AccountCipher:
    """AES-256-GCM 信封加密。**解密失败必须抛**，不许返回空串。"""

    PREFIX = "v1."
    NONCE_BYTES = 12

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"号池主密钥必须是 32 字节，当前 {len(key)} 字节")
        self._aead = AESGCM(key)

    @classmethod
    def from_key_text(cls, text: str) -> "AccountCipher":
        """接受 base64url / base64 / 64 位 hex 三种写法（运维不该被编码绊倒）。"""
        raw = (text or "").strip()
        if not raw:
            raise ValueError("ACCOUNT_ENC_KEY 为空")
        key: bytes | None = None
        if len(raw) == 64:
            try:
                key = bytes.fromhex(raw)
            except ValueError:
                key = None
        if key is None:
            padded = raw + "=" * (-len(raw) % 4)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                try:
                    candidate = decoder(padded)
                except Exception:  # noqa: BLE001
                    continue
                if len(candidate) == 32:
                    key = candidate
                    break
        if key is None:
            raise ValueError(
                "ACCOUNT_ENC_KEY 必须是 32 字节：64 位 hex 或 base64（url-safe 亦可）")
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(self.NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return self.PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt(self, blob: str) -> str:
        if not (blob or "").startswith(self.PREFIX):
            raise ValueError("密文格式不认识（缺少 v1. 前缀）")
        raw = base64.urlsafe_b64decode(blob[len(self.PREFIX):])
        nonce, ct = raw[:self.NONCE_BYTES], raw[self.NONCE_BYTES:]
        return self._aead.decrypt(nonce, ct, None).decode("utf-8")


class AccountRow(Base):
    """号池账号。`token_enc` 是密文，**永远不进对外视图**。"""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    fp: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_enc: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(String(64), default="")
    device_id: Mapped[str] = mapped_column(String(64), default="")
    exp: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)
    last_used_at: Mapped[float] = mapped_column(Float, default=0.0)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[float] = mapped_column(Float, default=0.0)
    last_checked_at: Mapped[float] = mapped_column(Float, default=0.0)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")


def view(row: AccountRow, *, now: float | None = None) -> dict[str, Any]:
    """对外视图。**唯一出口** —— 这里漏一个字段，明文就可能顺着列表接口出去。"""
    now = time.time() if now is None else now
    expired = bool(row.exp and row.exp <= now)
    cooling = row.cooldown_until > now
    return {
        "id": row.id,
        "label": row.label,
        "subject": row.subject,
        "fp": row.fp,
        "exp": row.exp,
        "status": row.status,
        "weight": row.weight,
        "note": row.note,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_used_at": row.last_used_at,
        "use_count": row.use_count,
        "fail_streak": row.fail_streak,
        "cooldown_until": row.cooldown_until,
        "last_checked_at": row.last_checked_at,
        "last_check_ok": bool(row.last_check_ok),
        "last_error": row.last_error,
        "expired": expired,
        "cooling": cooling,
        "usable": row.status == STATUS_ACTIVE and not expired and not cooling,
    }


class AccountStore:
    """账号表。与任务表**共用一个引擎**（`create_app` 里传进来），不另开连接池。"""

    def __init__(self, engine: Engine,
                 fingerprint: Callable[[str], str] | None = None) -> None:
        self.engine = engine
        #: 指纹函数必须与 `Service.credential_of` 同一个 ⇒ 任务归属键才对得上。
        #: 未注入时退回 sha256（仅独立使用/测试场景）。
        self.fingerprint = fingerprint or (
            lambda token: hashlib.sha256(token.encode()).hexdigest())
        Base.metadata.create_all(engine)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 写

    def add(self, *, token: str, claims: Any, cipher: AccountCipher,
            label: str = "", note: str = "", weight: int = 1) -> dict[str, Any]:
        """新增或按指纹更新（**幂等**：同一 token 再导入只更新展示字段）。"""
        fp = self.fingerprint(token)
        now = time.time()
        with Session(self.engine) as s:
            row = s.scalar(select(AccountRow).where(AccountRow.fp == fp))
            if row is None:
                row = AccountRow(
                    id=uuid.uuid4().hex[:16], fp=fp, token_enc=cipher.encrypt(token),
                    subject=getattr(claims, "subject", "") or "",
                    device_id=getattr(claims, "device_id", "") or "",
                    exp=getattr(claims, "exp", None),
                    created_at=now, updated_at=now,
                    label=label or (getattr(claims, "subject", "") or "account"),
                    note=note, weight=max(1, int(weight)),
                )
                s.add(row)
            else:
                row.token_enc = cipher.encrypt(token)
                row.exp = getattr(claims, "exp", None)
                row.subject = getattr(claims, "subject", "") or row.subject
                row.device_id = getattr(claims, "device_id", "") or row.device_id
                row.label = label or row.label
                row.note = note or row.note
                row.weight = max(1, int(weight))
                row.updated_at = now
            s.commit()
            s.refresh(row)
            return view(row)

    def update(self, account_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"label", "note", "weight", "status"}
        patch = {k: v for k, v in fields.items() if k in allowed}
        if "status" in patch and patch["status"] not in _STATUSES:
            raise ValueError(f"未知状态：{patch['status']}")
        if "weight" in patch:
            patch["weight"] = max(1, int(patch["weight"]))
        if not patch:
            return self.get(account_id)
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            if row is None:
                return None
            for k, v in patch.items():
                setattr(row, k, v)
            row.updated_at = time.time()
            s.commit()
            s.refresh(row)
            return view(row)

    def set_status(self, account_id: str, status: str) -> dict[str, Any] | None:
        return self.update(account_id, status=status)

    def remove(self, account_id: str) -> bool:
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def record_use(self, account_id: str) -> None:
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            if row is None:
                return
            row.use_count += 1
            row.last_used_at = time.time()
            s.commit()

    def record_failure(self, account_id: str, *, error: str,
                       cooldown: float = 0.0, fatal: bool = False) -> None:
        now = time.time()
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            if row is None:
                return
            row.fail_streak += 1
            row.last_error = (error or "")[:500]
            row.updated_at = now
            if fatal:
                row.status = STATUS_DEAD
            elif cooldown > 0:
                row.cooldown_until = now + cooldown
            s.commit()

    def record_check(self, account_id: str, *, ok: bool, detail: str = "",
                     fatal: bool = False) -> dict[str, Any] | None:
        now = time.time()
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            if row is None:
                return None
            row.last_checked_at = now
            row.last_check_ok = ok
            row.updated_at = now
            if ok:
                row.last_error = ""
                row.fail_streak = 0
                if row.status == STATUS_DEAD:
                    row.status = STATUS_ACTIVE   # 巡检通过 ⇒ 复活（运维显式动作）
            else:
                row.last_error = (detail or "")[:500]
                row.fail_streak += 1
                if fatal:
                    row.status = STATUS_DEAD
            s.commit()
            s.refresh(row)
            return view(row)

    # ------------------------------------------------------------------ 读

    def get(self, account_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            return view(row) if row else None

    def get_by_fp(self, fp: str) -> dict[str, Any] | None:
        """按指纹取账号（上游失败回灌时用：任务只带指纹，不带账号 id）。"""
        with Session(self.engine) as s:
            row = s.scalar(select(AccountRow).where(AccountRow.fp == fp))
            return view(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        with Session(self.engine) as s:
            rows = s.scalars(select(AccountRow).order_by(
                AccountRow.created_at, AccountRow.id)).all()
            return [view(r, now=now) for r in rows]

    def token_of(self, account_id: str, cipher: AccountCipher) -> str | None:
        """取出**明文** token —— 仅供选号后登记进内存池，绝不返回给调用方。"""
        with Session(self.engine) as s:
            row = s.get(AccountRow, account_id)
            if row is None:
                return None
            return cipher.decrypt(row.token_enc)

    def pick(self, *, now: float | None = None,
             busy_fps: frozenset[str] | set[str] = frozenset()) -> dict[str, Any] | None:
        """挑一个账号。

        `busy_fps` = 已达并发上限的账号指纹集合 ⇒ **先排除**它们；若排除后为空，
        再退回全体可用账号（宁可排队，也不因为"暂时都忙"把请求判 503）。
        """
        now = time.time() if now is None else now
        with Session(self.engine) as s:
            rows = s.scalars(select(AccountRow).where(
                AccountRow.status == STATUS_ACTIVE)).all()
            usable = [r for r in rows
                      if (r.exp is None or r.exp > now + EXP_SKEW)
                      and r.cooldown_until <= now]
            idle = [r for r in usable if r.fp not in busy_fps]
            if idle:
                usable = idle
            if not usable:
                return None
            usable.sort(key=lambda r: (-r.weight, r.last_used_at or 0.0,
                                       r.exp if r.exp is not None else float("inf")))
            return view(usable[0], now=now)

    def counts(self) -> dict[str, int]:
        now = time.time()
        rows = self.list()
        return {
            "total": len(rows),
            "active": sum(1 for r in rows if r["status"] == STATUS_ACTIVE),
            "disabled": sum(1 for r in rows if r["status"] == STATUS_DISABLED),
            "dead": sum(1 for r in rows if r["status"] == STATUS_DEAD),
            "expired": sum(1 for r in rows if r["expired"]),
            "usable": sum(1 for r in rows if r["usable"]),
            "expiring_soon": sum(
                1 for r in rows
                if r["exp"] is not None and now < r["exp"] <= now + 3 * 86400),
        }


def make_prober(settings: Any, transport: Any = None) -> Callable[[str], tuple[bool, bool, str]]:
    """零成本巡检：`my/processing` 只回答"我有没有在跑的任务"，不建任务、不计费。

    返回 `(ok, fatal, detail)`。`fatal=True` 仅用于**凭据本身坏了**（401/403）——
    网络抖动不该把账号判死，否则一次 DNS 抖动就会清空可用池。
    """
    def probe(token: str) -> tuple[bool, bool, str]:
        client = HailuoClient(token=token, base_url=settings.hailuo_base_url,
                              device=settings.device_profile(), transport=transport)
        try:
            ok, detail = client.probe_token()
        finally:
            client.close()
        fatal = (not ok) and ("AuthError" in detail or "401" in detail
                              or "403" in detail)
        return ok, fatal, detail

    return probe


__all__ = [
    "AccountCipher",
    "AccountRow",
    "AccountStore",
    "EXP_SKEW",
    "STATUS_ACTIVE",
    "STATUS_DEAD",
    "STATUS_DISABLED",
    "make_prober",
    "view",
]
