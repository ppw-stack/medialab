"""调度中心测试（Phase 0）。

调度是所有作业的入口。核心契约：

- **作业级异常隔离（R8）**：单作业失败不中断整轮。
- **手动触发的异常必须抛回调用方**（cron 走 _wrap 吞掉是为了保护调度器）。
- **启停状态跨重启保持**（DB 持久化，用户停用的作业不该"复活"）。
- **优雅停机**：停止新调度 → 等待在途作业。

注意：这些用例**不启动 APScheduler**（``scheduler_enabled=False``），
只测调度中心自身的注册/启停/手动触发逻辑。
"""

from __future__ import annotations

import asyncio

import pytest
from app.services.scheduler import JobSpec, RunResult, SchedulerCenter


async def _ok_job() -> dict[str, object]:
    return {"ok": 1}


async def _fail_job() -> dict[str, object]:
    raise RuntimeError("boom")


@pytest.fixture
def center() -> SchedulerCenter:
    """独立调度中心实例（不用全局单例，避免用例间互相影响）。"""
    return SchedulerCenter(timezone="Asia/Shanghai")


# -------------------------------------------------------------------- 注册

def test_register_lists_job(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    jobs = center.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "j1"
    assert jobs[0]["cron"] == "*/10 * * * *"
    assert jobs[0]["enabled"] is True
    assert jobs[0]["running"] is False


def test_enable_unknown_job_returns_false(center: SchedulerCenter) -> None:
    assert center.enable("nope", False) is False


def test_update_cron_unknown_job_returns_false(center: SchedulerCenter) -> None:
    assert center.update_cron("nope", "*/5 * * * *") is False


def test_update_cron_valid(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.update_cron("j1", "*/5 * * * *") is True
    assert center.list_jobs()[0]["cron"] == "*/5 * * * *"


def test_update_cron_invalid_raises(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    with pytest.raises(ValueError):
        center.update_cron("j1", "not-a-cron")


def test_enable_toggles_enabled_flag(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.enable("j1", False) is True
    assert center.list_jobs()[0]["enabled"] is False
    assert center.enable("j1", True) is True
    assert center.list_jobs()[0]["enabled"] is True


# ------------------------------------------------------- enabled 覆盖（持久化）

def test_enabled_overrides_take_precedence_over_spec(center: SchedulerCenter) -> None:
    """DB 里的停用状态优先于 spec 默认值——用户停用的作业重启后不该复活。"""
    center.apply_enabled_overrides({"j1": False})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["enabled"] is False


def test_enabled_overrides_absent_falls_back_to_spec(center: SchedulerCenter) -> None:
    """DB 里没有的作业用 spec 默认值——新增作业不被历史行影响。"""
    center.apply_enabled_overrides({"other": False})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["enabled"] is True


def test_enabled_overrides_unknown_job_ignored(center: SchedulerCenter) -> None:
    """DB 里已下线的作业名不影响当前作业。"""
    center.apply_enabled_overrides({"gone": False})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["enabled"] is True


# ------------------------------------------------------- cron 覆盖（持久化）

def test_cron_overrides_take_precedence_over_spec(center: SchedulerCenter) -> None:
    """DB 里的 cron 优先于 spec 默认值——页面改的 cron 重启后不该丢。"""
    center.apply_overrides({}, {"j1": "15,45 * * * *"})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["cron"] == "15,45 * * * *"


def test_cron_overrides_absent_falls_back_to_spec(center: SchedulerCenter) -> None:
    """DB 里没有该作业的 cron → 用代码里的默认值。"""
    center.apply_overrides({}, {"other": "*/7 * * * *"})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["cron"] == "*/10 * * * *"


def test_cron_overrides_unknown_job_ignored(center: SchedulerCenter) -> None:
    """DB 里已下线的作业名的 cron 不影响当前作业。"""
    center.apply_overrides({}, {"gone": "*/7 * * * *"})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["cron"] == "*/10 * * * *"


def test_overrides_apply_both_enabled_and_cron(center: SchedulerCenter) -> None:
    """启停与 cron 两类覆盖同时生效。"""
    center.apply_overrides({"j1": False}, {"j1": "15,45 * * * *"})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    job = center.list_jobs()[0]
    assert job["enabled"] is False
    assert job["cron"] == "15,45 * * * *"


def test_default_cron_covers_all_registered_jobs() -> None:
    """代码里的默认 cron 常量必须覆盖每个已注册作业。

    漏一个 = 该作业在 :meth:`SchedulerCenter.register` 时 KeyError。
    """
    from app.services.job_registry import DEFAULT_CRON

    assert set(DEFAULT_CRON) == {
        "fetch_all_sites",
        "push_free_torrents",
        "monitor_free_window",
        "refresh_user_data",
        "scan_cross_seed",
        "drain_task_queue",
        "daily_checkin",
    }


def test_default_cron_values_are_valid() -> None:
    """默认 cron 必须是合法的 5 字段表达式（否则启动时 APScheduler 抛错）。"""
    from app.services.job_registry import DEFAULT_CRON
    from apscheduler.triggers.cron import CronTrigger

    for name, cron in DEFAULT_CRON.items():
        assert len(cron.split()) == 5, f"{name} 的 cron 不是 5 字段：{cron}"
        # 不合法会抛 ValueError
        CronTrigger.from_crontab(cron)


# ------------------------------------------------------------------ 手动触发

async def test_run_now_success(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    res: RunResult = await center.run_now("j1")
    assert res.ok is True
    assert res.error == ""
    assert res.duration_ms >= 0
    assert res.result == {"ok": 1}


async def test_run_now_unknown_job_fails(center: SchedulerCenter) -> None:
    res = await center.run_now("nope")
    assert res.ok is False
    assert "unknown job" in res.error


async def test_run_now_propagates_exception(center: SchedulerCenter) -> None:
    """手动触发的异常必须抛回调用方（不能像 cron 那样吞掉）。"""
    center.register(JobSpec(name="j1", func=_fail_job, cron="*/10 * * * *"))
    res = await center.run_now("j1")
    assert res.ok is False
    assert "boom" in res.error


async def test_run_now_rejected_when_not_accepting_new_work(center: SchedulerCenter) -> None:
    """停机期间拒绝新作业。"""
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    center.accepting_new_work = False
    res = await center.run_now("j1")
    assert res.ok is False
    assert "not accepting new work" in res.error


async def test_run_now_concurrent_rejected(center: SchedulerCenter) -> None:
    """同作业并发手动触发 → 第二次被拒。"""
    release = asyncio.Event()

    async def _slow() -> dict[str, object]:
        await release.wait()
        return {"ok": 1}

    center.register(JobSpec(name="j1", func=_slow, cron="*/10 * * * *"))
    task = asyncio.create_task(center.run_now("j1"))
    await asyncio.sleep(0)  # 让第一个任务开始执行
    res2 = await center.run_now("j1")
    release.set()
    res1 = await task
    assert res2.ok is False
    assert "already running" in res2.error
    assert res1.ok is True


async def test_inflight_count_tracks_manual_run(center: SchedulerCenter) -> None:
    release = asyncio.Event()

    async def _slow() -> dict[str, object]:
        await release.wait()
        return {"ok": 1}

    center.register(JobSpec(name="j1", func=_slow, cron="*/10 * * * *"))
    assert center.inflight_count() == 0
    task = asyncio.create_task(center.run_now("j1"))
    await asyncio.sleep(0)
    assert center.inflight_count() == 1
    release.set()
    await task
    assert center.inflight_count() == 0


async def test_running_flag_cleared_after_run(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    await center.run_now("j1")
    assert center.list_jobs()[0]["running"] is False
    # 跑完还能再跑（running 标志没泄漏）
    res = await center.run_now("j1")
    assert res.ok is True


async def test_running_flag_cleared_after_failure(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_fail_job, cron="*/10 * * * *"))
    await center.run_now("j1")
    assert center.list_jobs()[0]["running"] is False


# ------------------------------------------------------------------ 停机

async def test_drain_with_no_inflight_returns_immediately(center: SchedulerCenter) -> None:
    await center.drain(timeout=1.0)


async def test_shutdown_sets_accepting_new_work_false(center: SchedulerCenter) -> None:
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    await center.shutdown(grace_seconds=1)
    assert center.accepting_new_work is False
    res = await center.run_now("j1")
    assert res.ok is False


# ------------------------------------------------------------------ JobStep

def test_job_step_defaults() -> None:
    from app.services.scheduler import JobStep

    step = JobStep("名称", "ok")
    assert step.name == "名称"
    assert step.status == "ok"
    assert step.count is None
    assert step.detail is None
