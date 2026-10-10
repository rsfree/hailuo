#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务存储（SQLAlchemy 2.0）。

## 为什么要有库，而不是内存字典

**任务库是本服务的"事实源"。** 三个硬理由：

1. **受理不碰上游**（建任务交给协调器）⇒ 受理那一刻任务必须先落地，
   否则进程一重启，调用方手里的 `task_id` 就指向一个不存在的任务；
2. **并发上限按库计数**（`count(status='in_progress')`），进程重启安全、天然跨 worker
   —— 用 `Semaphore` 的话重启归零，而库里那些任务其实还在上游跑；
3. **协调器靠数据库租约选主** ⇒ 多 worker 下只有一个真正在执行。

## 四张表

| 表 | 作用 |
|---|---|
| `tasks` | **图片**任务本身（`TaskRow`） |
| `video_tasks` | **视频**任务本身（`VideoTaskRow`）—— 见下 |
| `meta` | 进程级键值（目前只存**指纹密钥**——让明文 Key 永不落库） |
| `leases` | 协调器租约（`leader_key` → `expires_at`） |

## 为什么视频用**另一张表**而不是加一列

图片任务的 id 是 `hailuo_<32hex>`，视频（火山 Seedance 协议）的 id 是
`cgt-<时间戳>-<随机>` ——两套 id 空间，且两侧的**列表**端点都要各自干净
（`GET /async/v1/images/generations` 不该冒出视频任务，反之亦然）。

加一列 `kind` 需要迁移既有库（`create_all` 只建表不改列 ⇒ 老库会缺列而炸），
而新开一张表对既有部署是**纯加性**的。两者共享同一个 `TaskStore` 实现
（`row_cls` 参数化），没有复制第二份存储逻辑。

## 凭据纪律

调用方的 Key **不落库**：落库的是 `credential_id = HMAC-SHA256(secret, key)`
（不可逆）。`secret` 首次启动随机生成、存在 `meta` 表 ⇒ **明文 Key 永不落库**。
"""
from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from loguru import logger
from sqlalchemy import (
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

#: 任务状态机（**对外**的三个取值 + 两个中间态）
ST_QUEUED = "queued"
ST_IN_PROGRESS = "in_progress"
ST_SUCCEEDED = "succeeded"
ST_FAILURE = "failure"
ST_DELETED = "DELETED"

TERMINAL: frozenset[str] = frozenset({ST_SUCCEEDED, ST_FAILURE})


class Base(DeclarativeBase):
    pass


class _TaskColumns(Base):
    """任务行的**列定义**（`__abstract__` ⇒ 不建表，只被下面两张表复用）。

    图片与视频任务结构完全一致，差别只在"放哪张表" —— 用抽象基类而不是
    映射继承，是因为**映射继承会走 joined-table**：它要求子表与父表各有一行
    且带外键，那是把两种任务重新焊在一起，正是这里要避开的。
    """

    __abstract__ = True

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 对外能力名（图片：hailuo-i2i / hailuo-t2i…；视频：hailuo-video / hailuo-2.3 …）
    capability: Mapped[str] = mapped_column(String(32))
    #: 调用方请求的原始 model 字符串
    model: Mapped[str] = mapped_column(String(64), default="")
    #: 真正发到上游的 modelID
    upstream_model: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), index=True, default=ST_QUEUED)

    __abstract__ = True

    request_json: Mapped[str] = mapped_column(Text, default="{}")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    degradations_json: Mapped[str] = mapped_column(Text, default="[]")

    upstream_batch_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    upstream_feed_id: Mapped[str] = mapped_column(String(64), default="")
    upstream_status: Mapped[int] = mapped_column(Integer, default=-1)

    result_json: Mapped[str] = mapped_column(Text, default="")
    error_json: Mapped[str] = mapped_column(Text, default="")
    #: 提交尝试次数（含重试）
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[float] = mapped_column(Float)
    submitted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            #: 🔴 归属校验靠它 —— 漏了它会让**每一个**带 Key 的查询都 404
            "credential_id": self.credential_id,
            "capability": self.capability,
            "model": self.model,
            "upstream_model": self.upstream_model,
            "status": self.status,
            "upstream_batch_id": self.upstream_batch_id,
            "upstream_feed_id": self.upstream_feed_id,
            "upstream_status": self.upstream_status,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "submitted_at": self.submitted_at,
            "finished_at": self.finished_at,
            "degradations": json.loads(self.degradations_json or "[]"),
        }


class TaskRow(_TaskColumns):
    """图片任务（`/async/v1/images/generations`）。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_created", "status", "created_at"),
    )


class VideoTaskRow(_TaskColumns):
    """视频任务（火山 Seedance 协议出口）。

    与 `TaskRow` **同构但不同表** —— 两套管线的 id 空间不同
    （`hailuo_<hex>` vs `cgt-<时间戳>-<随机>`），列表端点也各自干净。
    """

    __tablename__ = "video_tasks"
    __table_args__ = (
        Index("ix_video_tasks_status_created", "status", "created_at"),
    )


class MetaRow(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class LeaseRow(Base):
    __tablename__ = "leases"

    leader_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)


def new_task_id(prefix: str = "hailuo") -> str:
    """`hailuo_<32 hex>` —— 128 位随机，**不可猜**（id 本身就是读接口的凭据）。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def new_video_task_id(*, now: float | None = None, suffix_len: int = 10) -> str:
    """火山 Seedance 形态的任务 id：`cgt-YYYYMMDDHHMMSS-<随机>`。

    · 前缀与时间戳段**逐字段**对齐原生 `cgt-` 形态（调用方按原生解析即可）；
    · 随机段取 **10 位十六进制**（原生样本是 5 位）—— 5 位只有 100 万分之一
      的空间，同一秒内并发受理会有可观的碰撞概率；id 同时是**读接口的凭据**，
      所以宁可比原生长一点，也不要可猜。

    ⚠️ 时间戳用 **UTC** —— 服务端没有调用方时区，编一个时区等于伪造事实。
    """
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(now if now is not None else time.time()))
    return f"cgt-{stamp}-{uuid.uuid4().hex[:suffix_len]}"


class TaskStore:
    """任务库。**所有对 tasks 表的读写都要经过这里。**"""

    #: **行模型** —— 所有查询都走它，子类换一张表即可复用全部存取逻辑。
    row_cls: type = TaskRow

    def __init__(self, dsn: str, *, pool_size: int = 5, max_overflow: int = 10,
                 pool_recycle: int = 1800, pre_ping: bool = True,
                 connect_timeout: int = 10) -> None:
        self.dsn = dsn
        kwargs: dict[str, Any] = {"future": True}
        if dsn.startswith("sqlite"):
            # SQLite 只在测试/联调用：多线程共享连接需要 check_same_thread=False
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        else:
            kwargs.update({
                "pool_size": pool_size, "max_overflow": max_overflow,
                "pool_recycle": pool_recycle, "pool_pre_ping": pre_ping,
                "connect_args": {"connect_timeout": connect_timeout},
            })
        self.engine = create_engine(dsn, **kwargs)
        self._session = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    # ------------------------------------------------------------------ 会话

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(select(func.count()).select_from(self.row_cls))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"任务库不可连：{type(e).__name__}: {e}")
            return False

    def close(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        with self.session() as s:
            row = s.get(MetaRow, key)
            return row.value if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.session() as s:
            row = s.get(MetaRow, key)
            if row:
                row.value = value
            else:
                s.add(MetaRow(key=key, value=value))

    def fingerprint_secret(self) -> str:
        """取（或首次生成）指纹密钥 —— **明文 API Key 永不落库** 的关键。

        幂等：并发启动时以先写入者为准（后续读到同一个值，
        所以指纹在进程间是稳定的）。
        """
        existing = self.get_meta("fingerprint_secret")
        if existing:
            return existing
        secret = secrets.token_hex(32)
        try:
            self.set_meta("fingerprint_secret", secret)
            logger.info("已生成指纹密钥（明文 API Key 不会落库）")
        except Exception:  # noqa: BLE001
            return self.get_meta("fingerprint_secret") or secret
        return self.get_meta("fingerprint_secret") or secret

    # ------------------------------------------------------------------ 写

    def create_task(self, **fields: Any) -> dict[str, Any]:
        now = time.time()
        row = self.row_cls(
            task_id=fields.pop("task_id", None) or new_task_id(),
            created_at=now, updated_at=now, **fields,
        )
        with self.session() as s:
            s.add(row)
        return row.to_dict()

    def update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        if "degradations" in fields:
            fields["degradations_json"] = json.dumps(
                fields.pop("degradations"), ensure_ascii=False)
        if "result" in fields:
            fields["result_json"] = json.dumps(
                fields.pop("result"), ensure_ascii=False)
        if "error" in fields:
            fields["error_json"] = json.dumps(
                fields.pop("error"), ensure_ascii=False)
        with self.session() as s:
            s.execute(update(self.row_cls).where(self.row_cls.task_id == task_id).values(**fields))

    def add_degradation(self, task_id: str, note: str) -> None:
        with self.session() as s:
            row = s.get(self.row_cls, task_id)
            if not row:
                return
            notes = json.loads(row.degradations_json or "[]")
            if note not in notes:
                notes.append(note)
            row.degradations_json = json.dumps(notes, ensure_ascii=False)
            row.updated_at = time.time()

    def delete_task(self, task_id: str) -> bool:
        with self.session() as s:
            res = s.execute(delete(self.row_cls).where(self.row_cls.task_id == task_id))
            return bool(res.rowcount)

    # ------------------------------------------------------------------ 读

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(self.row_cls, task_id)
            return row.to_dict() if row else None

    def get_full(self, task_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(self.row_cls, task_id)
            if not row:
                return None
            out = row.to_dict()
            out.update({
                "request": json.loads(row.request_json or "{}"),
                "plan": json.loads(row.plan_json or "{}"),
                "result": json.loads(row.result_json) if row.result_json else None,
                "error": json.loads(row.error_json) if row.error_json else None,
            })
            return out

    def list_for_credential(self, credential_id: str, *, limit: int = 50) -> list[dict]:
        with self.session() as s:
            rows = s.scalars(
                select(self.row_cls).where(self.row_cls.credential_id == credential_id)
                .order_by(self.row_cls.created_at.desc()).limit(limit)
            ).all()
            return [r.to_dict() for r in rows]

    def count_active(self, credential_id: str | None = None) -> int:
        """在途任务数 —— **`HL_CONCURRENCY` 的判据**（按库计数，重启安全）。

        `credential_id` 给定 ⇒ 只数**该账号**的（多账号号池下，并发上限按账号隔离）；
        `None` = 全部账号合计（观测/兼容旧语义）。
        """
        with self.session() as s:
            conds = [self.row_cls.status == ST_IN_PROGRESS]
            if credential_id is not None:
                conds.append(self.row_cls.credential_id == credential_id)
            return int(s.scalar(
                select(func.count()).select_from(self.row_cls).where(*conds)) or 0)

    def active_counts_by_credential(self) -> dict[str, int]:
        """每个账号（凭据指纹）各有多少在途任务 —— 协调器**按账号分槽位**用。

        一次分组查询拿全量，替代"每个账号各查一次 count_active"
        （账号多时那是 N 次查询；排队判定是每 tick 都要跑的热路径）。
        """
        with self.session() as s:
            rows = s.execute(
                select(self.row_cls.credential_id, func.count())
                .where(self.row_cls.status == ST_IN_PROGRESS)
                .group_by(self.row_cls.credential_id)
            ).all()
            return {(cred or ""): int(n) for cred, n in rows}

    def count_queued(self) -> int:
        with self.session() as s:
            return int(s.scalar(
                select(func.count()).select_from(self.row_cls)
                .where(self.row_cls.status == ST_QUEUED)) or 0)

    def due_for_submit(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """该建任务的任务（`queued`，按创建时间先进先出）。"""
        with self.session() as s:
            rows = s.scalars(
                select(self.row_cls).where(self.row_cls.status == ST_QUEUED)
                .order_by(self.row_cls.created_at.asc()).limit(limit)
            ).all()
            return [r.to_dict() for r in rows]

    def in_flight(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """在途任务（`in_progress`）—— 协调器一轮要查的集合。"""
        with self.session() as s:
            rows = s.scalars(
                select(self.row_cls).where(self.row_cls.status == ST_IN_PROGRESS)
                .order_by(self.row_cls.submitted_at.asc()).limit(limit)
            ).all()
            return [r.to_dict() for r in rows]

    def expire_stale(self, *, timeout: float) -> list[str]:
        """把超时未终态的任务收成 `failure`。返回被收的任务 id。"""
        now = time.time()
        cutoff = now - timeout
        with self.session() as s:
            rows = s.scalars(
                select(self.row_cls).where(
                    self.row_cls.status.in_((ST_QUEUED, ST_IN_PROGRESS)),
                    self.row_cls.created_at < cutoff,
                )
            ).all()
            ids: list[str] = []
            for row in rows:
                row.status = ST_FAILURE
                row.finished_at = now
                row.updated_at = now
                row.error_json = json.dumps({
                    "message": f"任务超过 {int(timeout)}s 未到终态，看门狗已判定失败。"
                               "⚠️ 上游任务可能仍在跑并计费。",
                    "type": "timeout", "code": "task_timeout",
                }, ensure_ascii=False)
                ids.append(row.task_id)
            return ids

    def purge_old(self, *, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self.session() as s:
            res = s.execute(
                delete(self.row_cls).where(
                    self.row_cls.created_at < cutoff,
                    self.row_cls.status.in_(tuple(TERMINAL)),
                )
            )
            return int(res.rowcount or 0)

    # ------------------------------------------------------------------ 租约

    def acquire_lease(self, *, owner: str, ttl: float,
                      leader_key: str = "coordinator") -> bool:
        """尝试成为协调器主。**租约过期即可抢占** —— 崩溃的副本不会永久占位。

        🔴 `leader_key` **必须按管线区分**（图片 `coordinator` / 视频
        `coordinator-video`）：两条管线各有自己的协调器，共用一把锁会让
        先启动的那个把另一个**永久饿死**（任务停在 `queued`、`attempts=0`，
        而且日志上看起来一切正常）。这是真实端到端实测才暴露出来的缺陷 ——
        单测里每个用例只建一个协调器，永远碰不到。
        """
        now = time.time()
        with self.session() as s:
            row = s.get(LeaseRow, leader_key)
            if row is None:
                s.add(LeaseRow(leader_key=leader_key, owner=owner,
                               expires_at=now + ttl))
                return True
            if row.expires_at < now or row.owner == owner:
                row.owner = owner
                row.expires_at = now + ttl
                return True
            return False

    def renew_lease(self, *, owner: str, ttl: float,
                    leader_key: str = "coordinator") -> bool:
        return self.acquire_lease(owner=owner, ttl=ttl, leader_key=leader_key)

    def release_lease(self, owner: str, leader_key: str = "coordinator") -> None:
        with self.session() as s:
            row = s.get(LeaseRow, leader_key)
            if row and row.owner == owner:
                row.expires_at = 0.0

    # ------------------------------------------------------------------ 统计

    def stats(self) -> dict[str, Any]:
        with self.session() as s:
            rows = s.execute(
                select(self.row_cls.status, func.count()).group_by(self.row_cls.status)
            ).all()
        counts = {str(k): int(v) for k, v in rows}
        return {
            "backend": "sqlite" if self.dsn.startswith("sqlite") else "postgresql",
            "counts": counts,
            "total": sum(counts.values()),
        }


class VideoTaskStore(TaskStore):
    """视频任务库 —— 与 `TaskStore` **逐方法相同**，只换一张表。

    为什么单独起一个实例而不是塞进同一个：协调器与列表端点都按 store 取任务，
    两套管线各自一个实例 ⇒ 图片任务绝不会出现在视频的列表/轮询集合里。
    """

    row_cls: type = VideoTaskRow


def healthcheck_env() -> dict[str, str]:
    """给脚本/容器用：从环境推导 DSN（`.env` 里 `TASK_DB`）。"""
    return {"TASK_DB": os.environ.get("TASK_DB", "sqlite+pysqlite:///./hailuo.db")}


__all__ = [
    "ST_DELETED",
    "ST_FAILURE",
    "ST_IN_PROGRESS",
    "ST_QUEUED",
    "ST_SUCCEEDED",
    "TERMINAL",
    "TaskStore",
    "TERMINAL",
    "VideoTaskRow",
    "VideoTaskStore",
    "new_task_id",
    "new_video_task_id",
]
