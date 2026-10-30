"""WebDAV 审计的单测。

重点覆盖两个**旧实现踩过的坑**：

1. 审计条目被丢弃（``queue.get()`` 已消费后又判空再取）
2. 进程级 ``asyncio.Queue`` 跨 event loop 崩
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.config import AppSettings
from app.db.models import DavAuditLog
from app.db.session import session_scope
from app.services.dav_audit import DavAuditor, cleanup_audit
from sqlalchemy import select


async def _count() -> int:
    async with session_scope() as db:
        rows = (await db.execute(select(DavAuditLog))).scalars().all()
        return len(rows)


async def _rows() -> list[DavAuditLog]:
    async with session_scope() as db:
        return list((await db.execute(select(DavAuditLog))).scalars().all())


async def test_record_and_flush(db_engine, settings: AppSettings):
    """记录后 flush 能落库。"""
    auditor = DavAuditor()
    auditor.record(
        user_id=1, username="alice", method="GET", path="/电影/x.strm", status=200
    )
    await auditor._flush()
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].username == "alice"
    assert rows[0].path == "/电影/x.strm"
    assert rows[0].status == 200


async def test_no_entry_lost_on_flush(db_engine, settings: AppSettings):
    """**关键回归**：flush 不丢条目。

    旧实现的 bug：``queue.get()`` 已消费一条后，后面又用
    ``if not queue.empty()`` 判空再取 —— 那条已消费的记录没被计入，
    直接丢了。这里记 10 条，落库必须也是 10 条。
    """
    auditor = DavAuditor()
    for i in range(10):
        auditor.record(
            user_id=1, username="alice", method="GET", path=f"/f{i}.strm", status=200
        )
    await auditor._flush()
    assert await _count() == 10


async def test_background_loop_writes(db_engine, settings: AppSettings):
    """后台 flush 协程能自动落库。"""
    auditor = DavAuditor()
    auditor.start()
    for i in range(5):
        auditor.record(
            user_id=1, username="alice", method="GET", path=f"/f{i}.strm", status=200
        )
    # 等后台协程处理
    for _ in range(100):
        if await _count() >= 5:
            break
        await asyncio.sleep(0.02)
    await auditor.close()
    assert await _count() == 5


async def test_close_flushes_remaining(db_engine, settings: AppSettings):
    """停机时把剩余条目落库。"""
    auditor = DavAuditor()
    auditor.start()
    auditor.record(user_id=1, username="alice", method="GET", path="/a", status=200)
    await auditor.close()
    assert await _count() == 1


def test_queue_rebind_on_new_loop(settings: AppSettings):
    """**关键回归**：跨 event loop 不崩。

    进程级单例在测试里每个用例一个新 loop —— 若不重建队列会抛
    ``Queue is bound to a different event loop``。

    这里用**同步**测试跑两个独立的 loop（async 测试里不能调
    ``asyncio.run`` —— 那会撞上正在运行的 loop）。
    """
    auditor = DavAuditor()

    async def _in_loop() -> None:
        auditor.record(user_id=1, username="a", method="GET", path="/x", status=200)
        await auditor._flush()

    asyncio.run(_in_loop())  # 第一个 loop
    asyncio.run(_in_loop())  # 第二个 loop —— 不该抛异常


async def test_client_is_truncated(db_engine, settings: AppSettings):
    """User-Agent 超长被截断。"""
    auditor = DavAuditor()
    auditor.record(
        user_id=1,
        username="alice",
        method="GET",
        path="/a",
        status=200,
        client="X" * 1000,
    )
    await auditor._flush()
    rows = await _rows()
    assert len(rows[0].client) == settings.dav_audit_client_max_len


async def test_path_is_truncated(db_engine, settings: AppSettings):
    """超长路径被截断（防超长 URL 撑爆表）。"""
    auditor = DavAuditor()
    auditor.record(
        user_id=1, username="alice", method="GET", path="/" + "a" * 5000, status=200
    )
    await auditor._flush()
    rows = await _rows()
    assert len(rows[0].path) == 1024


async def test_dropped_counted(db_engine, settings: AppSettings):
    """队列满时丢弃并计数（不阻塞请求）。"""
    auditor = DavAuditor()
    for _ in range(1100):
        auditor.record(user_id=1, username="a", method="GET", path="/a", status=200)
    assert auditor.stats()["dropped"] > 0


async def test_cleanup_audit_removes_old(db_engine, settings: AppSettings):
    """留存期外的记录被清理。"""
    settings.dav_audit_retention_days = 1
    auditor = DavAuditor()
    # 造一条 10 天前的记录
    auditor.record(user_id=1, username="a", method="GET", path="/old", status=200)
    await auditor._flush()
    async with session_scope() as db:
        row = (await db.execute(select(DavAuditLog))).scalars().one()
        row.created_at = int(__import__("time").time()) - 10 * 86400

    deleted = await cleanup_audit(settings)
    assert deleted == 1
    assert await _count() == 0


async def test_cleanup_audit_keeps_recent(db_engine, settings: AppSettings):
    """留存期内的记录保留。"""
    settings.dav_audit_retention_days = 90
    auditor = DavAuditor()
    auditor.record(user_id=1, username="a", method="GET", path="/new", status=200)
    await auditor._flush()
    assert await cleanup_audit(settings) == 0
    assert await _count() == 1


async def test_cleanup_audit_disabled(db_engine, settings: AppSettings):
    """留存期 0 = 永久保留。"""
    settings.dav_audit_retention_days = 0
    auditor = DavAuditor()
    auditor.record(user_id=1, username="a", method="GET", path="/a", status=200)
    await auditor._flush()
    assert await cleanup_audit(settings) == 0
    assert await _count() == 1


@pytest.mark.parametrize("method", ["GET", "HEAD", "PROPFIND", "OPTIONS"])
async def test_methods_recorded(db_engine, settings: AppSettings, method: str):
    """各种方法都能记。"""
    auditor = DavAuditor()
    auditor.record(user_id=1, username="a", method=method, path="/a", status=207)
    await auditor._flush()
    rows = await _rows()
    assert rows[0].method == method
