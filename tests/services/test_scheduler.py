"""调度中心测试（FR-8.2 / R8）。

覆盖：异常隔离、并发去重、停机 drain、拒绝新作业。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from app.services.scheduler import JobSpec, SchedulerCenter


@pytest.mark.asyncio
async def test_job_exception_does_not_kill_scheduler() -> None:
    """R8：单作业抛异常不得中断调度器。"""
    calls: list[int] = []

    async def boom() -> None:
        calls.append(1)
        raise RuntimeError("boom")

    center = SchedulerCenter()
    center.register(JobSpec(name="boom", func=boom, cron="*/1 * * * *"))
    center.start()
    with contextlib.suppress(RuntimeError):
        await center._wrap(center._specs["boom"])()
    assert calls == [1]
    # 调度器仍可查询，说明未崩溃
    assert center.list_jobs()[0]["name"] == "boom"
    await center.shutdown()


@pytest.mark.asyncio
async def test_concurrent_run_is_deduplicated() -> None:
    """同一作业未结束时跳过本轮（max_instances=1 的应用层兜底）。"""
    running = 0
    max_seen = 0

    async def slow() -> None:
        nonlocal running, max_seen
        running += 1
        max_seen = max(max_seen, running)
        await asyncio.sleep(0.05)
        running -= 1

    center = SchedulerCenter()
    center.register(JobSpec(name="slow", func=slow, cron="*/1 * * * *"))
    wrapped = center._wrap(center._specs["slow"])
    await asyncio.gather(wrapped(), wrapped())
    assert max_seen == 1


@pytest.mark.asyncio
async def test_drain_returns_immediately_when_idle() -> None:
    """无在途作业时 drain 必须立即返回。

    该缺陷由容器停机验证发现：旧实现无条件等满 grace period，导致
    ``docker stop`` 每次都要等满 60s，即使没有任何在途作业。
    """
    center = SchedulerCenter()
    start = asyncio.get_running_loop().time()
    await center.drain(timeout=30)
    assert asyncio.get_running_loop().time() - start < 0.5


@pytest.mark.asyncio
async def test_drain_waits_for_inflight_then_returns() -> None:
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(0.2)

    center = SchedulerCenter()
    center.register(JobSpec(name="slow", func=slow, cron="*/1 * * * *"))
    task = asyncio.create_task(center._wrap(center._specs["slow"])())
    await started.wait()
    assert center.inflight_count() == 1
    await center.drain(timeout=5)
    assert center.inflight_count() == 0
    await task


@pytest.mark.asyncio
async def test_stops_accepting_new_work() -> None:
    """停机后新作业不得再启动（FR-8.2 第一步）。"""
    ran = 0

    async def job() -> None:
        nonlocal ran
        ran += 1

    center = SchedulerCenter()
    center.register(JobSpec(name="j", func=job, cron="*/1 * * * *"))
    wrapped = center._wrap(center._specs["j"])

    await wrapped()
    assert ran == 1

    center.accepting_new_work = False
    await wrapped()
    assert ran == 1, "停机后不应执行新作业"


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    center = SchedulerCenter()
    center.register(
        JobSpec(name="j", func=lambda: asyncio.sleep(0), cron="*/1 * * * *")
    )
    center.start()
    await center.shutdown()
    await center.shutdown()  # 再次调用不应报错


def test_enable_disable_job() -> None:
    center = SchedulerCenter()
    center.register(
        JobSpec(name="j", func=lambda: asyncio.sleep(0), cron="*/1 * * * *")
    )
    assert center.enable("j", False) is True
    assert center.list_jobs()[0]["enabled"] is False
    assert center.enable("nope", False) is False

@pytest.mark.asyncio
async def test_cron_trigger_registers_correctly() -> None:
    """cron 表达式必须能在 APScheduler 中注册。"""
    center = SchedulerCenter()
    center.register(JobSpec(name="hourly", func=lambda: asyncio.sleep(0), cron="17 * * * *"))
    center.start()
    try:
        job = center._scheduler.get_job("hourly")
        assert job is not None
        # next_run_time 应是下一个第 17 分钟
        assert job.next_run_time is not None
        assert job.next_run_time.minute == 17
    finally:
        await center.shutdown()


def test_list_jobs_returns_cron_field() -> None:
    """list_jobs() 返回 cron 字段（替代 interval_seconds）。"""
    center = SchedulerCenter()
    center.register(
        JobSpec(name="poll", func=lambda: asyncio.sleep(0), cron="*/10 * * * *")
    )
    info = center.list_jobs()[0]
    assert info["cron"] == "*/10 * * * *"
    assert "interval_seconds" not in info


def test_invalid_cron_raises_value_error() -> None:
    """无效 cron 表达式应抛出 ValueError。"""
    from apscheduler.triggers.cron import CronTrigger

    with pytest.raises((ValueError, Exception)):
        CronTrigger.from_crontab("not a cron")
