"""任务队列幂等测试（§6 幂等性 / AC-09）。

核心断言：**同一幂等键重复入队不会产生第二行**，这是崩溃重放不重复添加
的真正保障——依赖数据库唯一约束，而非业务代码自觉。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.timeutils import utcnow
from app.services.task_queue import (
    TaskKind,
    TaskQueueService,
    TaskStatus,
    make_idem_key,
)


def key(site: int, h: str) -> str:
    return make_idem_key(TaskKind.ADD_TORRENT, site_id=site, info_hash=h)


H = "a" * 40


# ------------------------------------------------------------ 幂等键构造
def test_idem_key_is_case_insensitive_on_hash() -> None:
    assert key(1, H.upper()) == key(1, H)


def test_idem_key_differs_by_site() -> None:
    assert key(1, H) != key(2, H)


def test_idem_key_differs_by_kind() -> None:
    a = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash=H)
    b = make_idem_key(TaskKind.REMOVE_TORRENT, site_id=1, info_hash=H)
    assert a != b


def test_idem_key_handles_missing_site() -> None:
    k = make_idem_key(TaskKind.ADD_TORRENT, site_id=None, info_hash=H)
    assert k.startswith("add_torrent|0|")


def test_idem_key_extra_discriminator() -> None:
    a = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash=H)
    b = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash=H, extra="retry")
    assert a != b


# ------------------------------------------------------------ 行为
async def test_enqueue_creates_row(db) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(TaskKind.ADD_TORRENT, {"x": 1}, idem_key=key(1, H))
        assert r.created is True


async def test_duplicate_enqueue_is_idempotent(db) -> None:
    """AC-09：重放队列不产生重复任务。"""
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        first = await svc.enqueue(TaskKind.ADD_TORRENT, {"x": 1}, idem_key=key(1, H))
        second = await svc.enqueue(TaskKind.ADD_TORRENT, {"x": 1}, idem_key=key(1, H))
        assert first.created is True
        assert second.created is False
        assert first.task_id == second.task_id


async def test_enqueue_after_done_is_still_idempotent(db) -> None:
    """已完成的任务再次入队仍视为已处理——否则重启后会被重复执行。"""

    from app.db.models import TaskQueue
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        task = await s.get(TaskQueue, r.task_id)
        assert task is not None
        await svc.mark_done(task)

    async with session_scope() as s:
        svc = TaskQueueService(s)
        again = await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        assert again.created is False


async def test_claim_due_marks_running(db) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        claimed = await svc.claim_due()
        assert len(claimed) == 1
        assert claimed[0].status == TaskStatus.RUNNING


async def test_claim_due_skips_future_tasks(db) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        await svc.enqueue(
            TaskKind.ADD_TORRENT,
            {},
            idem_key=key(1, H),
            run_at=utcnow() + timedelta(hours=1),
        )
        assert await svc.claim_due() == []


async def test_mark_failed_retries_then_gives_up(db) -> None:
    """§6：失败自动重试 3 次。"""
    from app.db.models import TaskQueue
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        task = await s.get(TaskQueue, r.task_id)
        assert task is not None
        for i in range(3):
            await svc.mark_failed(task, f"err{i}")
        assert task.status == TaskStatus.FAILED
        assert task.attempts == 3


async def test_failed_task_not_reclaimed(db) -> None:
    from app.db.models import TaskQueue
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        task = await s.get(TaskQueue, r.task_id)
        assert task is not None
        for _ in range(3):
            await svc.mark_failed(task, "x")
        assert await svc.claim_due() == []


async def test_requeue_running_recovers_crashed_tasks(db) -> None:
    """崩溃恢复：残留 running 必须复位，否则永远卡住。"""
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        await svc.claim_due()

    async with session_scope() as s:
        svc = TaskQueueService(s)
        assert await svc.requeue_running() == 1

    async with session_scope() as s:
        svc = TaskQueueService(s)
        assert len(await svc.claim_due()) == 1, "复位后应可被重新消费"


async def test_pending_count(db) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(1, H))
        await svc.enqueue(TaskKind.ADD_TORRENT, {}, idem_key=key(2, H))
        assert await svc.pending_count() == 2


@pytest.mark.parametrize("kind", list(TaskKind))
async def test_all_kinds_have_stable_keys(db, kind: str) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(kind, {}, idem_key=make_idem_key(kind, site_id=1, info_hash=H))
        assert r.created is True