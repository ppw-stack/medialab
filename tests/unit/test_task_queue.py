"""任务队列测试（Phase 0）。

崩溃恢复语义是本模块存在的唯一理由——``claim`` 落库先于网络执行，
进程在 claim 后崩溃时任务停在 running，需被回收重试。语义一旦漂移，
推送链路会静默丢任务或无限重试。

覆盖：幂等入队 / 认领 / 终态推进 / 指数退避 / 僵死回收 / 历史清理。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import TaskQueue
from app.db.session import session_scope
from app.services.task_queue import (
    MAX_ATTEMPTS,
    STALE_RUNNING_MINUTES,
    EnqueueResult,
    TaskKind,
    TaskQueueService,
    TaskStatus,
    make_idem_key,
)
from sqlalchemy import text

# ------------------------------------------------------------------ 幂等键

def test_make_idem_key_format() -> None:
    key = make_idem_key(TaskKind.ADD_TORRENT, site_id=3, info_hash="ABC")
    assert key == "add_torrent|3|abc"


def test_make_idem_key_lowercases_hash() -> None:
    a = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash="ABCDEF")
    b = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash="abcdef")
    assert a == b


def test_make_idem_key_escapes_pipe() -> None:
    """item_id 是站点任意字符串，可能含 '|'——必须转义否则破坏字段语义。"""
    key = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash="a|b")
    assert "|" not in key.split("|", 1)[1].replace("|", "", 1) or key.count("|") == 2
    assert key == "add_torrent|1|a_b"


def test_make_idem_key_extra() -> None:
    key = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash="h", extra="dl=2")
    assert key == "add_torrent|1|h|dl=2"


def test_make_idem_key_site_id_none_becomes_zero() -> None:
    key = make_idem_key(TaskKind.ADD_TORRENT, site_id=None, info_hash="h")
    assert key == "add_torrent|0|h"


# -------------------------------------------------------------------- 入队

async def test_enqueue_creates_task(db: None) -> None:
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(
            TaskKind.ADD_TORRENT, {"index_id": 1}, idem_key="k1"
        )
        assert res.created is True
        # 新建路径不返回既有状态（语义：仅幂等命中时有值）
        assert res.existing_status is None
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        assert row.status == TaskStatus.PENDING
        assert row.attempts == 0


async def test_enqueue_is_idempotent(db: None) -> None:
    """同 idem_key 二次入队 → 幂等命中，不新建。"""
    async with session_scope() as session:
        svc = TaskQueueService(session)
        first = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        second = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 2}, idem_key="k1")
        assert first.created is True
        assert second.created is False
        assert second.task_id == first.task_id
        # 幂等命中时返回既有任务状态
        assert second.existing_status == TaskStatus.PENDING


async def test_concurrent_enqueue_same_key_is_idempotent(db: None) -> None:
    """并发入队同键 → 一个创建成功，另一个幂等命中（**不抛异常**）。

    回归：唯一约束冲突后必须先 rollback 再查询，否则 session 处于
    「事务已回滚」状态，查询会抛 PendingRollbackError。
    """
    import asyncio

    async def _enq() -> EnqueueResult:
        async with session_scope() as session:
            svc = TaskQueueService(session)
            return await svc.enqueue(
                TaskKind.ADD_TORRENT, {"i": 1}, idem_key="race"
            )

    results = await asyncio.gather(_enq(), _enq(), return_exceptions=True)
    assert not any(isinstance(r, Exception) for r in results), (
        f"并发入队不应抛异常：{results}"
    )
    created = [r for r in results if r.created]
    assert len(created) == 1, "只应创建一个任务"
    assert len({r.task_id for r in results}) == 1, "两个结果应指向同一任务"


async def test_enqueue_different_keys_coexist(db: None) -> None:
    async with session_scope() as session:
        svc = TaskQueueService(session)
        a = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        b = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 2}, idem_key="k2")
        assert a.task_id != b.task_id


# -------------------------------------------------------------------- 认领

async def test_claim_one_pending_to_running(db: None) -> None:
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        assert task.status == TaskStatus.RUNNING
        assert task.attempts == 1


async def test_claim_one_nonexistent_returns_none(db: None) -> None:
    async with session_scope() as session:
        svc = TaskQueueService(session)
        assert await svc.claim_one(999999) is None


async def test_claim_one_already_running_returns_none(db: None) -> None:
    """已被认领的任务不能重复认领（并发安全）。"""
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        assert await svc.claim_one(res.task_id) is not None
        assert await svc.claim_one(res.task_id) is None


async def test_claim_one_is_conditional_update(db: None) -> None:
    """认领必须是**条件更新**：另一个执行者先认领后，本次必须拿不到。

    背景（2026-11-22 加固）：``claim_one`` 曾是「先 get 读状态 → 改 → flush」，
    理论上两个执行者（push 就地认领 / drain 批量认领）读到同一 pending 会
    双双执行。现在用 ``UPDATE ... WHERE status='pending'`` + RETURNING 让
    数据库判定。这里用两个独立 session 模拟两个执行者。
    """
    async with session_scope() as s1:
        res = await TaskQueueService(s1).enqueue(
            TaskKind.ADD_TORRENT, {"i": 1}, idem_key="dup"
        )
        task_id = res.task_id

    # 执行者 A：认领成功
    async with session_scope() as s1:
        a = await TaskQueueService(s1).claim_one(task_id)
    assert a is not None and a.attempts == 1

    # 执行者 B：另一个 session，必须拿不到（且 attempts 不再自增）
    async with session_scope() as s2:
        assert await TaskQueueService(s2).claim_one(task_id) is None
    async with session_scope() as s3:
        row = await s3.get(TaskQueue, task_id)
        assert row is not None
        assert row.status == TaskStatus.RUNNING
        assert row.attempts == 1


async def test_claim_due_returns_only_actually_claimed(db: None) -> None:
    """``claim_due`` 只返回**本次 UPDATE 真正认领到**的行。

    若先被别人认领（status 已是 running），本轮的候选 id 里带着它也不能
    返回——否则 drain 会把 push 正在执行的任务再跑一遍。
    """
    past = format_bj(bj_now_dt() - timedelta(minutes=5))
    async with session_scope() as s1:
        svc = TaskQueueService(s1)
        r1 = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="a", run_at=past)
        r2 = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 2}, idem_key="b", run_at=past)
        taken = r1.task_id

    # 先由 push 的就地认领拿走一条
    async with session_scope() as s2:
        assert await TaskQueueService(s2).claim_one(taken) is not None

    # drain 再来认领：只应拿到另一条
    async with session_scope() as s3:
        claimed = await TaskQueueService(s3).claim_due(bj_now())
    ids = {t.id for t in claimed}
    assert ids == {r2.task_id}, f"drain 认领到了别人正在跑的任务：{ids}"


async def test_claim_due_only_claims_due_tasks(db: None) -> None:
    now = bj_now()
    past = format_bj(bj_now_dt() - timedelta(minutes=5))
    future = format_bj(bj_now_dt() + timedelta(minutes=5))
    async with session_scope() as session:
        svc = TaskQueueService(session)
        await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="due", run_at=past)
        await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 2}, idem_key="notdue", run_at=future)
        claimed = await svc.claim_due(now)
        assert len(claimed) == 1
        assert claimed[0].idem_key == "due"
        assert claimed[0].attempts == 1


async def test_claim_due_respects_limit(db: None) -> None:
    past = format_bj(bj_now_dt() - timedelta(minutes=5))
    async with session_scope() as session:
        svc = TaskQueueService(session)
        for i in range(5):
            await svc.enqueue(
                TaskKind.ADD_TORRENT, {"i": i}, idem_key=f"k{i}", run_at=past
            )
        claimed = await svc.claim_due(bj_now(), limit=2)
        assert len(claimed) == 2


# ------------------------------------------------------------------ 状态推进

async def test_mark_done(db: None) -> None:
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        await svc.mark_done(res.task_id)
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        assert row.status == TaskStatus.DONE


async def test_mark_failed_forever(db: None) -> None:
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        await svc.mark_failed_forever(res.task_id, "fatal error")
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        assert row.status == TaskStatus.FAILED
        assert row.last_error == "fatal error"


async def test_mark_failed_returns_pending_with_backoff(db: None) -> None:
    """首次失败 → 回 pending + 退避 1 分钟（不是终止）。"""
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        final = await svc.mark_failed(res.task_id, "network error")
        assert final == TaskStatus.PENDING
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        assert row.status == TaskStatus.PENDING
        assert row.last_error == "network error"


async def test_mark_failed_exhausted_attempts_becomes_permanent(db: None) -> None:
    """尝试次数用尽 → 永久 failed（死信），不再重试。"""
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        task.attempts = MAX_ATTEMPTS
        await session.flush()
        final = await svc.mark_failed(res.task_id, "still failing")
        assert final == TaskStatus.FAILED
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        assert row.status == TaskStatus.FAILED


async def test_backoff_grows_exponentially(db: None) -> None:
    """退避：第 N 次失败延迟 min(2**(N-1), 30) 分钟。"""
    delays: list[float] = []
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        for attempt in range(1, 5):
            task = await svc.claim_one(res.task_id)
            assert task is not None
            task.attempts = attempt
            await session.flush()
            before = bj_now_dt()
            await svc.mark_failed(res.task_id, f"fail {attempt}")
            row = await session.get(TaskQueue, res.task_id)
            assert row is not None
            from app.core.timeutils import parse_bj

            run_at = parse_bj(row.run_at)
            assert run_at is not None
            delays.append((run_at - before).total_seconds() / 60)
    # 期望 1, 2, 4, 8 分钟（允许 1 分钟误差）
    for got, want in zip(delays, [1, 2, 4, 8], strict=True):
        assert abs(got - want) < 1.0


async def test_backoff_max_reachable_is_8_minutes(db: None) -> None:
    """当前 MAX_ATTEMPTS=5 下，**实际可达**的最大退避是 8 分钟（attempts=4）。

    attempts=5 时已转永久 failed，不再进入退避分支。
    """
    from app.core.timeutils import parse_bj

    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        task.attempts = MAX_ATTEMPTS - 1  # 4
        await session.flush()
        before = bj_now_dt()
        final = await svc.mark_failed(res.task_id, "fail")
        assert final == TaskStatus.PENDING
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        run_at = parse_bj(row.run_at)
        assert run_at is not None
        minutes = (run_at - before).total_seconds() / 60
        assert minutes == pytest.approx(8, abs=1.0)


def test_backoff_cap_is_unreachable_at_current_max_attempts() -> None:
    """记录当前真实行为：MAX_ATTEMPTS=5 时最大退避是 8 分钟，够不到 30 上限。

    这是对实现现状的**如实记录**，不是断言期望行为——若将来调大
    MAX_ATTEMPTS，这条会失败并提醒同步更新退避相关文档。
    """
    reachable = [min(2 ** (a - 1), 30) for a in range(1, MAX_ATTEMPTS)]
    assert max(reachable) == 8
    assert max(reachable) < 30


# ------------------------------------------------------------------ 僵死回收

async def test_requeue_stale_recovers_running_tasks(db: None) -> None:
    """进程崩溃后停在 running 的任务必须被回收为 pending。"""
    stale_before = format_bj(bj_now_dt() - timedelta(minutes=STALE_RUNNING_MINUTES + 1))
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        # 用原生 SQL 改 updated_at：ORM 的 onupdate=_bj_now 会在 flush
        # 时把手动赋值覆盖掉（这正是「僵死回收」依赖 updated_at 的原因）。
        await session.execute(
            text("UPDATE task_queue SET updated_at = :ts WHERE id = :id"),
            {
                "ts": format_bj(bj_now_dt() - timedelta(hours=2)),
                "id": res.task_id,
            },
        )
        await session.flush()

        count = await svc.requeue_stale(stale_before)
        assert count == 1
        row = await session.get(TaskQueue, res.task_id)
        assert row is not None
        assert row.status == TaskStatus.PENDING


async def test_requeue_stale_leaves_fresh_running(db: None) -> None:
    """正在运行（未超时）的任务不能被回收。"""
    stale_before = format_bj(bj_now_dt() - timedelta(minutes=STALE_RUNNING_MINUTES + 1))
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        assert await svc.requeue_stale(stale_before) == 0


# ------------------------------------------------------------------ 历史清理

async def test_purge_finished_removes_old_terminal_tasks(db: None) -> None:
    """终态（done/failed）且超期的行被清理。"""
    old = format_bj(bj_now_dt() - timedelta(days=30))
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        task = await svc.claim_one(res.task_id)
        assert task is not None
        await svc.mark_done(res.task_id)
        # 同上：用原生 SQL 改 updated_at（ORM onupdate 会覆盖手动赋值）
        await session.execute(
            text("UPDATE task_queue SET updated_at = :ts WHERE id = :id"),
            {"ts": old, "id": res.task_id},
        )
        await session.flush()
        purged = await svc.purge_finished(format_bj(bj_now_dt() - timedelta(days=7)))
        assert purged == 1


async def test_purge_finished_keeps_pending_tasks(db: None) -> None:
    old = format_bj(bj_now_dt() - timedelta(days=30))
    async with session_scope() as session:
        svc = TaskQueueService(session)
        res = await svc.enqueue(TaskKind.ADD_TORRENT, {"i": 1}, idem_key="k1")
        # 同上：原生 SQL 改 updated_at
        await session.execute(
            text("UPDATE task_queue SET updated_at = :ts WHERE id = :id"),
            {"ts": old, "id": res.task_id},
        )
        await session.flush()
        purged = await svc.purge_finished(format_bj(bj_now_dt() - timedelta(days=7)))
        assert purged == 0


# ------------------------------------------------------------------ 常量

def test_stale_running_minutes_is_30() -> None:
    assert STALE_RUNNING_MINUTES == 30


def test_max_attempts_is_5() -> None:
    assert MAX_ATTEMPTS == 5


@pytest.mark.parametrize(
    "kind",
    [
        TaskKind.ADD_TORRENT,
    ],
)
def test_task_kind_values_are_stable(kind: TaskKind) -> None:
    """任务类型字符串是 DB 里的持久化值，改名会丢历史任务。"""
    assert isinstance(kind.value, str)
    assert kind.value == kind.value.lower()
