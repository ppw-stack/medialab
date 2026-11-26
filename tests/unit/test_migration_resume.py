"""迁移直链预取缓存与额度耗尽次日自动续传单元测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from app.services import migration_jobs
from app.services.migration import MigrationError
from app.services.migration_jobs import (
    STATE_RUNNING,
    MigrationParams,
    resume_due_paused_migrations,
    resume_paused_job,
)
from app.services.migration_link_cache import PrefetchedLinkCache


def _params() -> MigrationParams:
    return MigrationParams(
        source_driver_id=1,
        source_cid="cid-1",
        target_driver_id=2,
        target_file_id="dir-2",
    )


async def test_prefetched_link_cache_hit_expiry_and_invalidate() -> None:
    """直链未过期命中缓存，6 小时 TTL 到期自动重取，invalidate 立即清除。"""
    clock = [1000.0]
    cache = PrefetchedLinkCache(ttl_sec=100.0, now_fn=lambda: clock[0])
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return f"https://cdn.test/download?v={calls}"

    url1, cached1 = await cache.get_or_fetch(1, "fid-1", fetch)
    assert url1.endswith("v=1") and cached1 is False

    # 未过期直接复用
    clock[0] = 1050.0
    url2, cached2 = await cache.get_or_fetch(1, "fid-1", fetch)
    assert url2 == url1 and cached2 is True
    assert calls == 1

    # 超过 TTL 自动重取
    clock[0] = 1101.0
    url3, cached3 = await cache.get_or_fetch(1, "fid-1", fetch)
    assert url3.endswith("v=2") and cached3 is False
    assert calls == 2

    # 主动失效后重新取链
    cache.invalidate(1, "fid-1")
    url4, cached4 = await cache.get_or_fetch(1, "fid-1", fetch)
    assert url4.endswith("v=3") and cached4 is False


async def test_resume_paused_job_and_next_bj_day_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同北京自然日不硬撞额度；跨自然日后自动续传，且不会重复续传。"""
    migration_jobs.clear_all()
    started: list[MigrationParams] = []

    async def _fake_start(params: MigrationParams) -> migration_jobs.MigrationJob:
        started.append(params)
        job = await migration_jobs.create_job(params)
        job.state = STATE_RUNNING
        return job

    monkeypatch.setattr(migration_jobs, "start_migration_job", _fake_start)

    try:
        # 2026-11-26 23:00:00 北京时间 (= 15:00:00 UTC) 暂停
        paused_ts = int(datetime(2026, 11, 26, 15, 0, 0, tzinfo=UTC).timestamp())
        same_day_ts = int(datetime(2026, 11, 26, 15, 50, 0, tzinfo=UTC).timestamp())
        next_day_ts = int(datetime(2026, 11, 26, 16, 10, 0, tzinfo=UTC).timestamp())

        job = await migration_jobs.create_job(_params())
        job.pause("云添加每日次数超限")
        job.finished_at = paused_ts

        # 同日不续传
        r1 = await resume_due_paused_migrations(now_ts=same_day_ts)
        assert r1["resumed"] == 0
        assert started == []

        # 跨北京时间 00:00 后自动续传
        r2 = await resume_due_paused_migrations(now_ts=next_day_ts)
        assert r2["resumed"] == 1
        assert r2["source_job_id"] == job.id
        assert job.resumed_by == r2["job_id"]
        assert len(started) == 1

        # 已有运行中任务 → 手动续传被拒绝
        with pytest.raises(MigrationError, match="已有迁移任务在跑"):
            await resume_paused_job(job.id)

        # 把新任务标为完成，再尝试续传原任务 → 拒绝重复续传
        new_job = await migration_jobs.get_job(str(r2["job_id"]))
        assert new_job is not None
        new_job.state = migration_jobs.STATE_SUCCESS
        with pytest.raises(MigrationError, match="已被续传"):
            await resume_paused_job(job.id)
    finally:
        migration_jobs.clear_all()
        await asyncio.sleep(0)
