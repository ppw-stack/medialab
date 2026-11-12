"""任务队列排空作业（``app.services.task_drain``）集成测试。

重点覆盖 **作业历史清理**（``job_runs``）——作业历史只增不删会无限增长：
现在每次运行还额外存了执行日志与逐项明细，单条 ``result_json``
已有 10–30KB。

也覆盖排空作业本身的「僵死回收 + 认领执行 + 逐任务明细」。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import respx
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import (
    DownloaderInstance,
    Driver,
    JobRun,
    ProbeResult,
    Site,
    TaskQueue,
    TorrentIndex,
)
from app.db.session import session_scope
from app.services import task_drain
from app.services.factory import AdapterFactory
from app.services.task_drain import (
    _JOB_RUN_PURGE_DAYS,
    _PROBE_RESULT_PURGE_DAYS,
    drain_task_queue,
)
from sqlalchemy import select


async def _add_job_run(started_at: str) -> int:
    async with session_scope() as session:
        run = JobRun(
            job_name="j1",
            trigger="cron",
            started_at=started_at,
            ok=True,
            result_json='{"summary": "ok"}',
        )
        session.add(run)
        await session.flush()
        return run.id


async def _count_job_runs() -> int:
    async with session_scope() as session:
        rows = (await session.execute(select(JobRun))).scalars().all()
        return len(rows)


# ------------------------------------------------------------------ 历史清理

async def test_清理超期作业历史(db: None) -> None:
    """超 30 天的作业历史被删掉。"""
    old = format_bj(bj_now_dt() - timedelta(days=_JOB_RUN_PURGE_DAYS + 1))
    await _add_job_run(old)
    assert await _count_job_runs() == 1

    result = await drain_task_queue(AdapterFactory())

    assert await _count_job_runs() == 0
    assert result["job_runs_purged"] == 1
    # 清理步骤要在 steps 里可见
    step = next(s for s in result["steps"] if s.name == "清理作业历史")
    assert step.status == "ok"
    assert step.count == 1


async def test_保留期内的作业历史不动(db: None) -> None:
    """保留期内的历史**不能**被删（排障窗口）。"""
    recent = format_bj(bj_now_dt() - timedelta(days=_JOB_RUN_PURGE_DAYS - 1))
    await _add_job_run(recent)
    assert await _count_job_runs() == 1

    result = await drain_task_queue(AdapterFactory())

    assert await _count_job_runs() == 1
    assert result["job_runs_purged"] == 0
    step = next(s for s in result["steps"] if s.name == "清理作业历史")
    assert step.status == "skip"


async def test_清理无任务时也执行(db: None) -> None:
    """**无到期任务时也要清理**——历史清理不该依赖队列有没有活。"""
    old = format_bj(bj_now_dt() - timedelta(days=_JOB_RUN_PURGE_DAYS + 1))
    await _add_job_run(old)

    result = await drain_task_queue(AdapterFactory())

    assert await _count_job_runs() == 0
    assert result["claimed"] == 0
    assert result["job_runs_purged"] == 1
    step = next(s for s in result["steps"] if s.name == "清理作业历史")
    assert step.count == 1


async def test_无历史时清理步骤为skip(db: None) -> None:
    """没有超期历史 → 步骤是 skip（不是 fail）。"""
    result = await drain_task_queue(AdapterFactory())

    step = next(s for s in result["steps"] if s.name == "清理作业历史")
    assert step.status == "skip"
    assert step.count == 0
    assert result["job_runs_purged"] == 0


# --------------------------------------------------- 停用站点的遗留任务

async def _seed_disabled_site_task() -> int:
    """建「站点已停用 + 停用前入队的遗留任务」，返回 task id。

    场景：推送作业入队后、站点被停用前，任务还在队列里（pending）。
    drain 排空时会重试它——那正是「站点已停用却还在推」的第二条路径。
    """
    async with session_scope() as session:
        site = Site(
            name="s1", domain="s1.test", adapter="hdsky", enabled=False
        )
        session.add(site)
        dl = DownloaderInstance(
            name="dl1",
            type="qbittorrent",
            url="http://dl.test:8080",
            is_default=True,
            enabled=True,
        )
        session.add(dl)
        await session.flush()
        item = TorrentIndex(
            site_id=site.id,
            item_id="i1",
            name="t1",
            free_type="freeleech_permanent",
        )
        session.add(item)
        await session.flush()
        task = TaskQueue(
            idem_key=f"add_torrent|{site.id}|i1|dl={dl.id}",
            kind="add_torrent",
            payload={"index_id": item.id, "downloader_id": dl.id},
            status="pending",
            run_at=bj_now(),
        )
        session.add(task)
        await session.flush()
        return task.id


async def test_停用站点的遗留任务不再推送(
    db: None, no_network: respx.MockRouter
) -> None:
    """站点停用后，遗留队列任务**不再推送**（fatal 死信，不退避重试）。

    候选查询已过滤停用站点，但**停用前已入队**的任务会被 drain 重试——
    执行层必须再拦一道（``execute_add_torrent`` 检查 ``site.enabled``）。
    """
    task_id = await _seed_disabled_site_task()

    result = await drain_task_queue(AdapterFactory())

    assert result["claimed"] == 1
    assert result["failed"] == 1
    assert result["done"] == 0
    assert result["retried"] == 0

    async with session_scope() as session:
        task = (
            await session.execute(
                select(TaskQueue).where(TaskQueue.id == task_id)
            )
        ).scalar_one()
        # 死信：终态 failed（不是 pending → 不会无限重试）
        assert task.status == "failed"
        assert "已停用" in (task.last_error or "")
        # 种子没被推：push_status 未变
        item = (await session.execute(select(TorrentIndex))).scalar_one()
        assert item.push_status is None

    # 连 .torrent 都没下载（检查发生在下载之前）
    assert not no_network.calls



class TestPurgeBatching:
    """历史清理**分批**（2026-11-12 生产撞锁后固化）。

    一次性 DELETE 几千行（``job_runs`` 单条 ``result_json`` 有 10–30KB）
    会长时间持有 SQLite 写锁，与扫描/调度作业的写事务互相撞
    （``database is locked``）。分批后每批只持锁毫秒级，且单次作业有
    上限——删不完下轮继续，任何时刻都不会长时间持锁。
    """

    async def test_超量时按批删且不超过单次上限(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(task_drain, "_PURGE_BATCH", 10)
        monkeypatch.setattr(task_drain, "_PURGE_MAX_BATCHES", 2)
        old = format_bj(bj_now_dt() - timedelta(days=_JOB_RUN_PURGE_DAYS + 1))
        for _ in range(25):
            await _add_job_run(old)

        result = await drain_task_queue(AdapterFactory())

        # 单次只删 2 批 × 10 = 20 条（剩下 5 条留给下一轮）
        assert result["job_runs_purged"] == 20
        assert await _count_job_runs() == 5
        step = next(s for s in result["steps"] if s.name == "清理作业历史")
        assert step.status == "ok"
        assert step.count == 20

        # 下一轮继续删完
        result2 = await drain_task_queue(AdapterFactory())
        assert result2["job_runs_purged"] == 5
        assert await _count_job_runs() == 0

    async def test_不足一批时一次删完(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """行数少于一批上限 → 一次删完（不空转第二轮）。"""
        monkeypatch.setattr(task_drain, "_PURGE_BATCH", 10)
        monkeypatch.setattr(task_drain, "_PURGE_MAX_BATCHES", 2)
        old = format_bj(bj_now_dt() - timedelta(days=_JOB_RUN_PURGE_DAYS + 1))
        for _ in range(7):
            await _add_job_run(old)

        result = await drain_task_queue(AdapterFactory())

        assert result["job_runs_purged"] == 7
        assert await _count_job_runs() == 0

    async def test_probe_results_超期也清理(self, db: None) -> None:
        """探测历史清理此前零覆盖——同样走分批路径。"""
        old = format_bj(bj_now_dt() - timedelta(days=_PROBE_RESULT_PURGE_DAYS + 1))
        async with session_scope() as session:
            driver = Driver(name="d1", type="guangya", credential="{}", enabled=True)
            session.add(driver)
            await session.flush()
            session.add(
                ProbeResult(
                    driver_id=driver.id,
                    used_gb=1.0,
                    total_gb=2.0,
                    available=True,
                    probed_at=old,
                )
            )

        result = await drain_task_queue(AdapterFactory())

        async with session_scope() as session:
            rows = (await session.execute(select(ProbeResult))).scalars().all()
            assert len(rows) == 0
        step = next(s for s in result["steps"] if s.name == "清理探测历史")
        assert step.status == "ok"
        assert step.count == 1
