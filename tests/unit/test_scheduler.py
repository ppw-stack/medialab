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
from app.services import scheduler
from app.services.scheduler import JobSpec, RunResult, SchedulerCenter
from sqlalchemy.exc import OperationalError


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
    center.apply_overrides({"j1": False})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["enabled"] is False


def test_enabled_overrides_absent_falls_back_to_spec(center: SchedulerCenter) -> None:
    """DB 里没有的作业用 spec 默认值——新增作业不被历史行影响。"""
    center.apply_overrides({"other": False})
    center.register(JobSpec(name="j1", func=_ok_job, cron="*/10 * * * *"))
    assert center.list_jobs()[0]["enabled"] is True


def test_enabled_overrides_unknown_job_ignored(center: SchedulerCenter) -> None:
    """DB 里已下线的作业名不影响当前作业。"""
    center.apply_overrides({"gone": False})
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
        "drain_task_queue",
        "daily_checkin",
        "cross_seed_maintenance",
        "scrape_metadata",
        "organize_files",
        "aggregate_sync",
        "cleanup_link_cache",
        "purge_dav_auth_cache",
    }


def test_job_meta_covers_all_jobs() -> None:
    """每个作业都要有中文名 + 描述（前端直接展示，不硬编码映射表）。

    漏了 title/description → 调度页显示原始 job 名（如 ``scan_orphans``）
    和 "—" 描述。这正是本条契约要防的回归：以前前端硬编码一份
    job → 中文名的映射表，每加一个作业就要手改前端，漏一处就露馅。
    """
    from app.services.job_registry import DEFAULT_CRON, JOB_META

    meta_names = {m.name for m in JOB_META}
    assert meta_names == set(DEFAULT_CRON), "JOB_META 与 DEFAULT_CRON 必须一致"

    for meta in JOB_META:
        assert meta.title.strip(), f"{meta.name} 缺中文名"
        assert meta.description.strip(), f"{meta.name} 缺描述"


def test_list_jobs_includes_title_and_description() -> None:
    """``GET /scheduler`` 的每个作业都要带 title + description。

    前端据此渲染标题与说明——前端不再硬编码映射表，故后端必须给全。
    """
    from app.services.factory import AdapterFactory
    from app.services.job_registry import JOB_META, register_jobs

    center = SchedulerCenter()
    for meta in JOB_META:
        center.register(
            JobSpec(
                name=meta.name,
                func=meta.make_func(AdapterFactory()),
                cron="*/10 * * * *",
                title=meta.title,
                description=meta.description,
            )
        )

    jobs = center.list_jobs()
    assert len(jobs) == len(JOB_META)
    for job in jobs:
        assert job["title"].strip(), f"{job['name']} 没下发 title"
        assert job["description"].strip(), f"{job['name']} 没下发 description"

    # register_jobs 走真实路径也要带上（防止只测构造不测注册）
    assert callable(register_jobs)


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
    # 明细字段是新增的，默认 None（**向后兼容**：现有 60+ 处调用零改动）
    assert step.items is None
    assert step.items_total is None


def test_job_step_item_defaults() -> None:
    from app.services.scheduler import JobStepItem

    item = JobStepItem("站名", "ok")
    assert item.name == "站名"
    assert item.status == "ok"
    assert item.count is None
    assert item.detail is None


def test_job_step_nested_items_are_serializable() -> None:
    """嵌套的 ``JobStepItem`` 能被 ``asdict`` 递归转换。

    ``_jsonable`` 靠 ``asdict`` 的递归性拿到嵌套 dict——
    若这里坏了，前端就拿不到 ``items`` 里的字段。
    """
    from dataclasses import asdict

    from app.services.scheduler import JobStep, JobStepItem

    step = JobStep(
        "拉取种子列表", "warn", 2, "2 站失败",
        items=[JobStepItem("hddolby", "fail", None, "DNS 解析失败")],
        items_total=9,
    )
    data = asdict(step)
    assert data["items"] == [
        {"name": "hddolby", "status": "fail", "count": None,
         "detail": "DNS 解析失败"}
    ]
    assert data["items_total"] == 9


def test_build_items_truncates_and_reports_total() -> None:
    """超限时截断并返回**完整**条数。"""
    from app.services.scheduler import JobStepItem, build_items

    items = [JobStepItem(f"n{i}", "ok") for i in range(60)]

    out, total = build_items(items, limit=50)
    assert len(out) == 50
    assert total == 60
    # 保留**最早**的（明细按执行顺序产生，最早发生的更利于排查）
    assert out[0].name == "n0"
    assert out[-1].name == "n49"


def test_build_items_no_truncation_returns_none_total() -> None:
    """未超限 → 第二项为 None（前端不显示「显示 N / 共 M」）。"""
    from app.services.scheduler import JobStepItem, build_items

    items = [JobStepItem(f"n{i}", "ok") for i in range(10)]
    out, total = build_items(items, limit=50)
    assert len(out) == 10
    assert total is None


def test_build_items_empty() -> None:
    from app.services.scheduler import build_items

    out, total = build_items([])
    assert out == []
    assert total is None


# ---------------------------------- 作业模块预热（2026-11-07 prod 事故）


def test_preflight_job_imports_loads_all_lazy_modules() -> None:
    """``preflight_job_imports`` 后，全部作业模块都已加载（幂等可重复调用）。

    作业入口是**函数内惰性导入**——作业第一次触发才在事件循环线程上读盘
    加载模块链。QNAP 冷存储上一次几分钟，循环冻结、面板假死
    （2026-11-07 prod：push_free 首次触发卡死循环，连锁 database is locked）。
    预热必须在调度启动之前于线程里完成。
    """
    import sys

    from app.services.job_registry import preflight_job_imports

    preflight_job_imports()
    preflight_job_imports()  # 幂等

    for mod in (
        "app.services.runtime_params",
        "app.services.jobs",
        "app.services.push_free",
        "app.services.monitor",
        "app.services.user_data",
        "app.services.task_drain",
        "app.services.checkin",
        "app.services.cross_seed_maintenance",
    ):
        assert mod in sys.modules, f"预热漏了 {mod}"


def test_preflight_covers_all_lazy_imports() -> None:
    """AST 校验：job_registry 里所有**函数内的** ``from app.*`` 惰性导入
    必须被 ``preflight_job_imports`` 覆盖。

    这条测试防脱节——新增作业时忘了把新模块加进预热清单会直接红。
    （顶层 import 不算——它们在进程启动时已加载，不在风险面上。）
    """
    import ast
    import sys
    from pathlib import Path

    from app.services import job_registry

    src = Path(job_registry.__file__).read_text(encoding="utf-8")
    lazy_modules: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if getattr(node, "col_offset", 0) == 0:  # 顶层 import 不算惰性
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app."):
                lazy_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    lazy_modules.add(alias.name)

    assert lazy_modules, "源码里没解析到任何惰性导入——解析逻辑坏了"

    job_registry.preflight_job_imports()
    missing = sorted(m for m in lazy_modules if m not in sys.modules)
    assert not missing, f"preflight_job_imports 漏了这些模块：{missing}"


# ------------------------------------------------- 作业历史落库（_record_run）

class _FakeSession:
    """假 session：commit 按次数抛 OperationalError（模拟 SQLite 锁竞争）。"""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def add(self, obj: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise OperationalError(
                "INSERT INTO job_runs", {}, Exception("database is locked")
            )


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _call_record_run() -> None:
    scheduler._record_run(
        job_name="j1",
        trigger="cron",
        started_at="2026-11-12 15:00:00",
        finished_at="2026-11-12 15:00:05",
        duration_ms=5000,
        ok=True,
        error=None,
        result={"ok": 1},
    )


@pytest.mark.usefixtures()
class TestRecordRun:
    """job_runs 落库的 SQLite 锁竞争降级（2026-11-12 生产撞锁后固化）。

    扫描/调度批量写库期间，``_persist`` 的 commit 会撞锁——退避重试
    一次，仍失败降级 warning。任务必须**干净完成**：任何异常逃逸都会
    刷「Task exception was never retrieved」ERROR 日志。
    """

    async def test_lock_conflict_retries_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.db import session as db_session

        fake = _FakeSession(fail_times=1)
        monkeypatch.setattr(
            db_session, "get_session_factory", lambda: _FakeFactory(fake)
        )
        _call_record_run()
        # 退避 0.5s + 重试落库
        await asyncio.sleep(0.65)

        assert fake.commits == 2
        assert scheduler._record_tasks == set()

    async def test_lock_conflict_twice_degrades_without_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        from app.db import session as db_session

        fake = _FakeSession(fail_times=2)
        monkeypatch.setattr(
            db_session, "get_session_factory", lambda: _FakeFactory(fake)
        )
        with caplog.at_level(logging.WARNING):
            _call_record_run()
        await asyncio.sleep(0.65)

        assert fake.commits == 2
        # 任务干净完成（不被 asyncio 异常处理器记账）
        assert scheduler._record_tasks == set()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("锁竞争" in r.message for r in caplog.records)
