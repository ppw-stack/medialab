"""单实例租约：防止多 worker / 多副本同时运行。

本项目有大量**进程内**状态：APScheduler 调度中心、四套 ``*_jobs`` 的
内存作业表、``watch_manager`` 的 10 组全局字典、QR 登录 session、
``aggregate_sync`` 的分组锁。它们都假设「全进程只有一个实例」。
多 worker（``uvicorn --workers N``）或多副本共享同一 DB 时，这些状态
各自为政 → 作业重复执行、404、缓存不一致。

Dockerfile 里的 ``--workers 1`` 只是**部署约定**，没有运行期约束力。
本模块在 lifespan 中抢一把数据库租约：只有一个实例能拿到，拿不到的
直接拒绝启动（fail-fast）。租约带 TTL 与心跳，进程崩溃后自动过期。

测试环境（``settings.env == "test"``）跳过租约——测试固件会在同一个
临时 DB 上多次 ``create_app``，且每个用例独立重建 engine。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import CursorResult, text

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.session import session_scope

logger = get_logger(__name__)

#: 租约有效期（秒）。进程崩溃后最多等这么久才能被新实例接管。
LEASE_TTL_SEC = 60.0

#: 心跳间隔（秒）—— 必须显著小于 TTL。
HEARTBEAT_INTERVAL_SEC = 15.0

#: 租约表 DDL。放在本模块自包含创建，不进 SQLAlchemy metadata —— 它属于
#: 运行期协调设施，不是业务数据；``CREATE TABLE IF NOT EXISTS`` 幂等。
_LEASE_DDL = """
CREATE TABLE IF NOT EXISTS app_instance_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at REAL NOT NULL
)
"""

_UPSERT_SQL = """
INSERT INTO app_instance_lease
    (id, instance_id, hostname, pid, acquired_at, expires_at)
VALUES (1, :iid, :host, :pid, :now, :exp)
ON CONFLICT(id) DO UPDATE SET
    instance_id = excluded.instance_id,
    hostname = excluded.hostname,
    pid = excluded.pid,
    acquired_at = excluded.acquired_at,
    expires_at = excluded.expires_at
WHERE app_instance_lease.expires_at < :now
"""

_READ_OWNER_SQL = "SELECT instance_id FROM app_instance_lease WHERE id = 1"

_HEARTBEAT_SQL = """
UPDATE app_instance_lease
SET expires_at = :exp
WHERE id = 1 AND instance_id = :iid
"""

_RELEASE_SQL = "DELETE FROM app_instance_lease WHERE id = 1 AND instance_id = :iid"


class InstanceLeaseError(RuntimeError):
    """已经有另一个实例持有租约。"""


@dataclass
class InstanceLease:
    """已获取的租约。``close()`` 释放并停止心跳。"""

    instance_id: str
    _heartbeat: asyncio.Task[None] | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    async def close(self) -> None:
        """停止心跳并释放租约（带 instance_id 校验，不误删他实例的续租）。"""
        if self._closed:
            return
        self._closed = True
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None
        async with session_scope() as db:
            await db.execute(text(_RELEASE_SQL), {"iid": self.instance_id})
        logger.info("实例租约已释放：%s", self.instance_id)


async def acquire_instance_lease() -> InstanceLease | None:
    """抢单实例租约。

    Returns:
        ``None`` —— 测试环境，无需租约。
        :class:`InstanceLease` —— 抢到。
    Raises:
        InstanceLeaseError: 已有活着的实例持有租约。
    """
    if get_settings().env == "test":
        return None

    instance_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    now = time.time()
    expires = now + LEASE_TTL_SEC

    async with session_scope() as db:
        await db.execute(text(_LEASE_DDL))
        await db.execute(
            text(_UPSERT_SQL),
            {
                "iid": instance_id,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "now": now,
                "exp": expires,
            },
        )
        # 回读确认 —— 并发下 UPSERT 的 WHERE 可能让本次写入被跳过。
        owner = (
            await db.execute(text(_READ_OWNER_SQL))
        ).scalar_one_or_none()
        if owner != instance_id:
            logger.error(
                "启动被拒绝：已有实例持有租约（owner=%s，本实例=%s）。"
                "本项目只支持单 worker / 单副本运行。",
                owner,
                instance_id,
            )
            raise InstanceLeaseError(
                "检测到另一个正在运行的实例（多 worker / 多副本）；"
                "本项目使用进程内调度与作业表，只能单实例运行"
            )

    lease = InstanceLease(instance_id=instance_id)
    lease._heartbeat = asyncio.create_task(_heartbeat(instance_id))
    logger.info("实例租约已获取：%s（%s，TTL %.0fs）", instance_id, bj_now(), LEASE_TTL_SEC)
    return lease


async def _heartbeat(instance_id: str) -> None:
    """定期续租。DB 故障只记日志——让正在运行的实例继续工作。"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        try:
            async with session_scope() as db:
                result = cast(
                    CursorResult[object],
                    await db.execute(
                        text(_HEARTBEAT_SQL),
                        {"exp": time.time() + LEASE_TTL_SEC, "iid": instance_id},
                    ),
                )
                if result.rowcount == 0:
                    logger.error(
                        "实例租约丢失（%s）——另一实例可能已接管，请检查是否多副本运行",
                        instance_id,
                    )
                    return
        except Exception as exc:
            logger.warning("实例租约心跳失败：%s", exc)


__all__ = ["InstanceLease", "InstanceLeaseError", "acquire_instance_lease"]
