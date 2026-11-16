#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池存储与加密（第一期）。

判据来源：`.fixloop/criteria.md` R5/R6 + 两期口径（加密落库）。
"""
from __future__ import annotations

import base64
import time

import pytest
from sqlalchemy import create_engine

from app.accounts import AccountCipher, AccountStore
from app.credentials import JwtClaims
from tests.conftest import FAKE_JWT, FAKE_JWT_B

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()


def claims(subject: str = "tester", *, ttl: float = 86400,
           device_id: str = "dev-1") -> JwtClaims:
    return JwtClaims(exp=time.time() + ttl, device_id=device_id, subject=subject)


@pytest.fixture()
def cipher() -> AccountCipher:
    return AccountCipher.from_key_text(KEY)


@pytest.fixture()
def accounts(tmp_path) -> AccountStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/acc.db", future=True)
    return AccountStore(engine)


def test_cipher_roundtrip_and_ciphertext_has_no_plaintext(cipher: AccountCipher) -> None:
    blob = cipher.encrypt(FAKE_JWT)
    assert FAKE_JWT not in blob
    assert blob.startswith("v1.")
    assert cipher.decrypt(blob) == FAKE_JWT


def test_cipher_rejects_other_key(cipher: AccountCipher) -> None:
    blob = cipher.encrypt(FAKE_JWT)
    other = AccountCipher.from_key_text(base64.urlsafe_b64encode(b"x" * 32).decode())
    with pytest.raises(Exception):
        other.decrypt(blob)


def test_key_text_must_be_32_bytes() -> None:
    with pytest.raises(ValueError):
        AccountCipher.from_key_text(base64.urlsafe_b64encode(b"short").decode())


def test_store_persists_only_ciphertext(tmp_path, cipher: AccountCipher) -> None:
    dsn = f"sqlite+pysqlite:///{tmp_path}/acc.db"
    engine = create_engine(dsn, future=True)
    store = AccountStore(engine)
    rec = store.add(token=FAKE_JWT, claims=claims(), label="me better", cipher=cipher)
    assert rec["id"]
    engine.dispose()

    raw = (tmp_path / "acc.db").read_bytes()
    assert FAKE_JWT.encode() not in raw, "明文 token 出现在库里"
    # 密文可解回原文 ⇒ 加密确实生效，而不是把 token 丢掉的假实现
    store2 = AccountStore(create_engine(dsn, future=True))
    assert store2.token_of(rec["id"], cipher) == FAKE_JWT


def test_add_dedupes_by_fingerprint(accounts: AccountStore, cipher: AccountCipher) -> None:
    a = accounts.add(token=FAKE_JWT, claims=claims(), label="A", cipher=cipher)
    b = accounts.add(token=FAKE_JWT, claims=claims(), label="B", cipher=cipher)
    assert a["id"] == b["id"]
    assert accounts.get(a["id"])["label"] == "B"
    assert len(accounts.list()) == 1


def test_view_never_carries_token(accounts: AccountStore, cipher: AccountCipher) -> None:
    rec = accounts.add(token=FAKE_JWT, claims=claims(), cipher=cipher)
    assert FAKE_JWT not in repr(rec)
    assert "token_enc" not in rec


def test_pick_skips_disabled_expired_and_cooling(accounts: AccountStore,
                                                 cipher: AccountCipher) -> None:
    ok = accounts.add(token=FAKE_JWT, claims=claims(), cipher=cipher)
    dead = accounts.add(token=FAKE_JWT_B,
                        claims=claims(subject="b", ttl=-10), cipher=cipher)
    accounts.set_status(dead["id"], "disabled")

    assert accounts.pick()["id"] == ok["id"]

    accounts.set_status(ok["id"], "disabled")
    assert accounts.pick() is None, "全部不可用时必须回 None，不能硬塞一个账号"

    accounts.set_status(ok["id"], "active")
    accounts.record_failure(ok["id"], error="401", cooldown=300.0)
    assert accounts.pick() is None, "冷却中的账号不许被选中"

    assert accounts.pick(now=time.time() + 301)["id"] == ok["id"]


def test_pick_prefers_weight_then_least_recently_used(accounts: AccountStore,
                                                      cipher: AccountCipher) -> None:
    a = accounts.add(token=FAKE_JWT, claims=claims(), cipher=cipher)
    b = accounts.add(token=FAKE_JWT_B, claims=claims(subject="b"), cipher=cipher)
    # 同权重 ⇒ 先选最久未用的；用一次后应轮到另一个
    first = accounts.pick()
    assert first["id"] == a["id"]
    accounts.record_use(first["id"])
    assert accounts.pick()["id"] == b["id"]

    accounts.set_status(b["id"], "active")
    accounts.update(b["id"], weight=9)
    assert accounts.pick()["id"] == b["id"], "权重高的优先"


def test_failure_marks_dead_when_fatal(accounts: AccountStore, cipher: AccountCipher) -> None:
    rec = accounts.add(token=FAKE_JWT, claims=claims(), cipher=cipher)
    accounts.record_failure(rec["id"], error="401 invalid token", fatal=True)
    got = accounts.get(rec["id"])
    assert got["status"] == "dead"
    assert got["fail_streak"] == 1
    assert "401" in got["last_error"]


def test_record_check_keeps_status_and_stamps_time(accounts: AccountStore,
                                                   cipher: AccountCipher) -> None:
    rec = accounts.add(token=FAKE_JWT, claims=claims(), cipher=cipher)
    accounts.record_check(rec["id"], ok=True, detail="processing=False")
    got = accounts.get(rec["id"])
    assert got["last_check_ok"] is True
    assert got["last_checked_at"] > 0
    assert got["status"] == "active"

    accounts.record_check(rec["id"], ok=False, detail="401", fatal=True)
    assert accounts.get(rec["id"])["status"] == "dead"


def test_counts_shape(accounts: AccountStore, cipher: AccountCipher) -> None:
    accounts.add(token=FAKE_JWT, claims=claims(), cipher=cipher)
    accounts.add(token=FAKE_JWT_B, claims=claims(subject="b"), cipher=cipher)
    c = accounts.counts()
    assert c["total"] == 2
    assert c["active"] == 2
    assert c["expired"] == 0
