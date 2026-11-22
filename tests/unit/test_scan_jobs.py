"""扫描任务（后台任务 + 进度轮询）的契约测试。

**背景**：限速 1s/请求后扫一棵大树要几分钟。同步 HTTP 让用户只看到转圈，
且反代/网关的 60s 超时会掐断请求。故改为后台任务 + 轮询进度。

本模块的契约：

1. 任务状态机：running → success / failed / cancelled
2. 日志环形缓冲（不无限增长）
3. 同一存储源不允许并发扫描
4. 进度回调在扫描过程中**逐文件**被调用（否则前端看不到进度）
"""

from __future__ import annotations

import time

import pytest
from app.db.models import Driver, MediaFile
from app.services import scan_jobs
from app.services.media import scan_driver
from sqlalchemy import select


@pytest.fixture(autouse=True)
def clean_jobs():
    """每个用例前后清空任务表。"""
    scan_jobs.clear_all()
    yield
    scan_jobs.clear_all()


@pytest.fixture
async def driver_row(db_session) -> Driver:
    row = Driver(
        id=1,
        type="fake",
        name="测试源",
        credential=None,
        enabled=True,
        media_cid="12345",
        media_path="/media",
        output_dir="/tmp/unused",
        video_exts="mkv,mp4",
        image_exts="jpg",
        data_exts="nfo",
        min_size_mb=0,
        include_subdirs=True,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestJobState:
    def test_create_job_starts_running(self) -> None:
        job = scan_jobs.create_job(1, "测试源")
        assert job.state == scan_jobs.STATE_RUNNING
        assert job.done is False
        assert job.id

    def test_log_appends_with_timestamp(self) -> None:
        job = scan_jobs.create_job(1, "测试源")
        job.log("hello")
        assert len(job.logs) == 1
        assert "hello" in job.logs[0]
        # 形如 [HH:MM:SS] hello
        assert job.logs[0].startswith("[")

    def test_log_is_ring_buffer(self) -> None:
        """日志不能无限增长——扫几万个文件会把内存吃满。"""
        job = scan_jobs.create_job(1, "测试源")
        for i in range(scan_jobs.MAX_LOGS + 100):
            job.log(f"line {i}")
        assert len(job.logs) == scan_jobs.MAX_LOGS
        # 保留的是**最新的**
        assert f"line {scan_jobs.MAX_LOGS + 99}" in job.logs[-1]

    def test_to_dict_serializable(self) -> None:
        job = scan_jobs.create_job(1, "测试源")
        job.log("x")
        d = job.to_dict()
        assert d["id"] == job.id
        assert d["state"] == "running"
        assert isinstance(d["logs"], list)
        # task 不参与序列化
        assert "task" not in d

    async def test_get_and_list(self) -> None:
        j1 = scan_jobs.create_job(1, "A")
        j2 = scan_jobs.create_job(2, "B")
        assert (await scan_jobs.get_job(j1.id)) is j1
        jobs = await scan_jobs.list_jobs()
        assert len(jobs) == 2
        # 最新在前
        assert jobs[0].id == j2.id

    async def test_list_filters_by_driver(self) -> None:
        scan_jobs.create_job(1, "A")
        scan_jobs.create_job(2, "B")
        assert len(await scan_jobs.list_jobs(driver_id=1)) == 1

    async def test_find_running(self) -> None:
        """同一存储源不允许并发扫描。"""
        j = scan_jobs.create_job(1, "A")
        assert (await scan_jobs.find_running(1)) is j
        assert (await scan_jobs.find_running(2)) is None

        j.state = scan_jobs.STATE_SUCCESS
        assert (await scan_jobs.find_running(1)) is None

    def test_evict_keeps_recent(self) -> None:
        """超出上限时清掉最旧的**已完成**任务。"""
        old = scan_jobs.create_job(1, "old")
        old.state = scan_jobs.STATE_SUCCESS
        for i in range(scan_jobs.MAX_JOBS):
            scan_jobs.create_job(1, f"job{i}")
        # 最旧的已完成任务被清掉
        assert old.id not in {j.id for j in scan_jobs._JOBS.values()}


class TestProgressCallback:
    async def test_callback_called_per_file(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """**核心**：进度回调必须逐文件调用，否则前端看不到进度。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        for i in range(5):
            fake_driver.add_file(f"pc{i}", f"m{i}.mkv", path=f"/media/m{i}.mkv")

        seen: list[tuple[str, int]] = []

        async def on_progress(path: str, stats, processed: int) -> None:
            seen.append((path, processed))

        await scan_driver(
            db_session, settings, driver_row, on_progress=on_progress
        )

        assert len(seen) == 5
        assert [p for _path, p in seen] == [1, 2, 3, 4, 5]

    async def test_callback_sees_live_stats(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """回调里拿到的 stats 是**累积中**的（不是最终值）。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        for i in range(3):
            fake_driver.add_file(f"pc{i}", f"m{i}.mkv", path=f"/media/m{i}.mkv")

        created_counts: list[int] = []

        async def on_progress(path: str, stats, processed: int) -> None:
            created_counts.append(stats.created)

        await scan_driver(
            db_session, settings, driver_row, on_progress=on_progress
        )

        # 第一次回调时还没写文件 → created=0；之后递增
        assert created_counts[0] == 0
        assert created_counts == sorted(created_counts)

    async def test_no_callback_works(self, db_session, settings, driver_row, fake_driver, tmp_path):
        """不传回调也能正常扫描（向后兼容）。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "m.mkv", path="/media/m.mkv")

        stats = await scan_driver(db_session, settings, driver_row)
        assert stats.created == 1


class TestBatchCommit:
    async def test_commits_in_batches(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """分批提交——中途失败不至于全丢。

        扫到第 1 个文件后抛错，已提交的部分应留在库里。
        """
        from app.services import media as media_mod

        driver_row.output_dir = str(tmp_path)
        await db_session.commit()

        # 制造「第一批提交后失败」
        original = media_mod.COMMIT_EVERY
        media_mod.COMMIT_EVERY = 2
        try:
            for i in range(5):
                fake_driver.add_file(f"pc{i}", f"m{i}.mkv", path=f"/media/m{i}.mkv")

            calls = {"n": 0}

            async def on_progress(path: str, stats, processed: int) -> None:
                calls["n"] += 1
                if calls["n"] == 4:
                    raise RuntimeError("模拟中断")

            with pytest.raises(RuntimeError):
                await scan_driver(
                    db_session, settings, driver_row, on_progress=on_progress
                )

            # 前 2 个已提交（第一批）。
            #
            # **必须用独立 session 查**——原 session 里还挂着未提交的第 3 个对象，
            # 查询会触发 autoflush 把它也写进去，读到的就不是「已提交」的数量了。
            from app.db.session import session_scope

            async with session_scope() as fresh:
                committed = (
                    await fresh.execute(select(MediaFile))
                ).scalars().all()
            assert len(committed) == 2
        finally:
            media_mod.COMMIT_EVERY = original


class TestRunnerSyncsFinalStats:
    async def test_final_stats_match_disk(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """任务完成后 job 的统计必须与**最终**结果一致。

        回归背景：on_progress 最后一次调用发生在处理完最后一个文件**之前**，
        曾出现「日志说新增 7861、API 返回 7860」的不一致。
        """
        from app.services import scan_runner

        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        for i in range(3):
            fake_driver.add_file(f"pc{i}", f"m{i}.mkv", path=f"/media/m{i}.mkv")

        job = scan_runner.start_scan_job(driver_row.id, driver_row.name)
        assert job.task is not None
        await job.task

        assert job.state == scan_jobs.STATE_SUCCESS
        assert job.created == 3
        assert job.processed == 3
        # 磁盘上确实有 3 个文件
        assert len(list(tmp_path.rglob("*.strm"))) == 3

    async def test_failed_job_records_error(
        self, db_session, settings, driver_row, fake_driver
    ):
        """扫描失败时 job 落 failed + error。"""
        from app.services import scan_runner

        driver_row.media_cid = None  # 触发配置校验失败
        await db_session.commit()

        job = scan_runner.start_scan_job(driver_row.id, driver_row.name)
        assert job.task is not None
        await job.task

        assert job.state == scan_jobs.STATE_FAILED
        assert job.error
        assert job.finished_at is not None

    async def test_runner_sets_current_path_blank_on_finish(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """结束后清空 current_path——否则界面会一直显示「当前：xxx」。"""
        from app.services import scan_runner

        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "m.mkv", path="/media/m.mkv")

        job = scan_runner.start_scan_job(driver_row.id, driver_row.name)
        assert job.task is not None
        await job.task

        assert job.current_path == ""
