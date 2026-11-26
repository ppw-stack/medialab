"""迁移暂停任务的**跨重启续传**（DB 持久化）测试。

覆盖两条曾会静默失效的路径：

1. 容器重启清空内存任务表 → 暂停任务仍能从 DB 回灌并续传；
2. 内存表超过 ``MAX_JOBS`` → 待续传的暂停任务不被误当「已完成」淘汰。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from app.services import migration_jobs
from app.services.migration import MigrationError
from app.services.migration_jobs import (
    MAX_JOBS,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_SUCCESS,
    MigrationJobNotFound,
    MigrationParams,
    resume_due_paused_migrations,
    resume_paused_job,
)


def _params() -> MigrationParams:
    return MigrationParams(
        source_driver_id=1,
        source_cid="cid-1",
        target_driver_id=2,
        target_file_id="dir-2",
        min_size_mb=10,
        delete_source=True,
    )


@pytest.fixture(autouse=True)
def _clean_jobs() -> None:
    migration_jobs.clear_all()


async def test_paused_job_survives_restart_and_resumes_once(db: None) -> None:
    """暂停任务落库 → 内存清空（模拟重启）→ 列表可见、能续传、续传后不再复活。"""
    job = await migration_jobs.create_job(_params())
    job.pause("云添加每日次数超限")
    await migration_jobs._persist_paused(job)

    # 模拟容器重启：进程内任务表被清空
    migration_jobs.clear_all()
    assert migration_jobs._JOBS.get(job.id) is None

    jobs = await migration_jobs.list_jobs()
    assert [j.id for j in jobs] == [job.id]
    assert jobs[0].state == STATE_PAUSED
    assert jobs[0].params == _params()

    started: list[MigrationParams] = []

    async def _fake_start(params: MigrationParams) -> migration_jobs.MigrationJob:
        started.append(params)
        new_job = await migration_jobs.create_job(params)
        new_job.state = STATE_SUCCESS
        return new_job

    original = migration_jobs.start_migration_job
    migration_jobs.start_migration_job = _fake_start  # type: ignore[assignment]
    try:
        new_job = await resume_paused_job(job.id)
    finally:
        migration_jobs.start_migration_job = original  # type: ignore[assignment]

    assert started == [_params()]
    assert new_job.params == _params()
    assert new_job.id != job.id

    # 已续传的记录被删除 → 再次「重启」不会把它重新拉回内存，也不能重复续传
    migration_jobs.clear_all()
    assert await migration_jobs.list_jobs() == []
    with pytest.raises(MigrationJobNotFound):
        await resume_paused_job(job.id)
    await asyncio.sleep(0)


async def test_paused_job_resumes_next_bj_day_after_restart(db: None) -> None:
    """重启后定时续传仍能扫到 DB 里的暂停任务（跨北京自然日）。"""
    paused_ts = int(datetime(2026, 11, 26, 15, 0, 0, tzinfo=UTC).timestamp())
    job = await migration_jobs.create_job(_params())
    job.pause("额度耗尽")
    job.finished_at = paused_ts
    await migration_jobs._persist_paused(job)

    migration_jobs.clear_all()  # 重启

    started: list[MigrationParams] = []

    async def _fake_start(params: MigrationParams) -> migration_jobs.MigrationJob:
        started.append(params)
        new_job = await migration_jobs.create_job(params)
        new_job.state = STATE_RUNNING
        return new_job

    original = migration_jobs.start_migration_job
    migration_jobs.start_migration_job = _fake_start  # type: ignore[assignment]
    try:
        # 同一北京自然日不硬撞额度
        same_day = paused_ts + 30 * 60
        assert (await resume_due_paused_migrations(now_ts=same_day))["resumed"] == 0
        assert started == []

        # 跨过北京时间 00:00 → 自动续传
        next_day = paused_ts + 2 * 3600
        result = await resume_due_paused_migrations(now_ts=next_day)
    finally:
        migration_jobs.start_migration_job = original  # type: ignore[assignment]

    assert result["resumed"] == 1
    assert result["source_job_id"] == job.id
    assert started == [_params()]
    await asyncio.sleep(0)


async def test_evict_old_keeps_unresumed_paused_jobs(db: None) -> None:
    """超过 MAX_JOBS 时淘汰旧任务，但绝不淘汰待续传的暂停任务。"""
    paused = await migration_jobs.create_job(_params())
    paused.pause("额度耗尽")

    for _ in range(MAX_JOBS + 3):
        done = await migration_jobs.create_job(_params())
        done.state = STATE_SUCCESS
        done.finished_at = int(datetime.now(UTC).timestamp())

    jobs = await migration_jobs.list_jobs()
    assert len(jobs) <= MAX_JOBS + 1  # 暂停任务被保留，其余按上限淘汰
    assert await migration_jobs.get_job(paused.id) is not None
    assert (await migration_jobs.get_job(paused.id)).state == STATE_PAUSED  # type: ignore[union-attr]

    # 清理：避免 DB 里的暂停行影响后续用例（本 fixture 每用例重建库，双保险）
    await migration_jobs._delete_paused_row(paused.id)


async def test_resume_rejects_second_job_while_placeholder_running(db: None) -> None:
    """续传占位 running 后，并发的第二次续传必须被拒绝（全局单迁移）。"""
    job = await migration_jobs.create_job(_params())
    job.pause("额度耗尽")

    gate = asyncio.Event()

    async def _slow_start(params: MigrationParams) -> migration_jobs.MigrationJob:
        await gate.wait()
        new_job = await migration_jobs.create_job(params)
        new_job.state = STATE_SUCCESS
        return new_job

    original = migration_jobs.start_migration_job
    migration_jobs.start_migration_job = _slow_start  # type: ignore[assignment]
    try:
        first = asyncio.create_task(resume_paused_job(job.id))
        await asyncio.sleep(0.05)  # 让第一次进入占位窗口
        with pytest.raises(MigrationError, match="已有迁移任务在跑"):
            await resume_paused_job(job.id)
        gate.set()
        await first
    finally:
        migration_jobs.start_migration_job = original  # type: ignore[assignment]
    await asyncio.sleep(0)
