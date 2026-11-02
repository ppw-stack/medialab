"""watch_manager 的单元测试。

**不真的起 watchfiles**——测的是编排逻辑：队列投递、去重、worker 处理、
``sync_now`` 的全量比对。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.adapters.drivers.base import DriverError
from app.services import watch_manager
from app.services.cloud_index import CloudDirIndex
from app.services.uploader import SyncContext

from tests.unit.upload_fakes import FakeDriver, FakeUploader


@pytest.fixture(autouse=True)
def _clean_manager():
    """每个用例前后清空管理器状态。"""
    from app.services.upload_tasks import clear_all

    watch_manager._WATCHERS.clear()
    watch_manager._WORKERS.clear()
    watch_manager._QUEUES.clear()
    watch_manager._CONTEXTS.clear()
    watch_manager._SYNC_LOCKS.clear()
    # 运行态是**全局**的（``_RUNTIMES``）——不清会跨用例串
    # （统计是累计值，上个用例传了几个，这个用例就带着）
    clear_all()
    yield
    watch_manager._WATCHERS.clear()
    watch_manager._WORKERS.clear()
    watch_manager._QUEUES.clear()
    watch_manager._CONTEXTS.clear()
    watch_manager._SYNC_LOCKS.clear()
    clear_all()


def _ctx(driver: FakeDriver, uploader: FakeUploader, root: Path) -> SyncContext:
    return SyncContext(
        driver=driver,
        uploader=uploader,
        index=CloudDirIndex(driver, "root"),
        root=root,
        delete_retries=2,
        delete_delay=0.01,
    )


async def test_投递到队列(tmp_path: Path):
    """dispatcher 把文件塞进队列。"""
    queue: asyncio.Queue[Path] = asyncio.Queue(maxsize=10)
    dispatch = watch_manager._make_dispatcher(1, queue)

    await dispatch(tmp_path / "a.mkv")

    assert queue.qsize() == 1
    assert queue.get_nowait().name == "a.mkv"


async def test_队列满时丢弃不阻塞(tmp_path: Path):
    """队列满 → 丢弃并记日志（不阻塞监听）。"""
    queue: asyncio.Queue[Path] = asyncio.Queue(maxsize=1)
    dispatch = watch_manager._make_dispatcher(1, queue)

    await dispatch(tmp_path / "a.mkv")
    await dispatch(tmp_path / "b.mkv")  # 满了 → 丢弃

    assert queue.qsize() == 1


async def test_worker处理文件(tmp_path: Path):
    """worker 从队列取文件并上传。"""
    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    queue: asyncio.Queue[Path] = asyncio.Queue()
    await queue.put(path)

    worker = asyncio.create_task(
        watch_manager._worker_loop(1, queue, ctx, 1)
    )
    await asyncio.sleep(0.1)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert uploader.uploaded == [("a.mkv", 100)]


async def test_同步已存在则跳过(tmp_path: Path):
    """云端已有同名同大小 → 跳过。"""
    driver = FakeDriver()
    uploader = FakeUploader()
    driver.add_file("root", "a.mkv", 100)
    ctx = _ctx(driver, uploader, tmp_path)

    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    await watch_manager._process_one(1, ctx, path)

    assert uploader.uploaded == []


async def test_同步跳过临时文件(tmp_path: Path):
    """临时文件不进上传流程。"""
    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    path = tmp_path / "a.mkv.part"
    path.write_bytes(b"x")

    await watch_manager._process_one(1, ctx, path)

    assert uploader.uploaded == []


async def test_sync_now全量比对(tmp_path: Path):
    """``sync_now`` 全量遍历并比对。"""
    from app.services.watcher import Watcher

    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    (tmp_path / "a.mkv").write_bytes(b"x" * 100)
    (tmp_path / "b.mkv").write_bytes(b"y" * 200)
    (tmp_path / "c.mkv.part").write_bytes(b"z")

    # 云端已有同名同大小
    driver.add_file("root", "a.mkv", 100)

    watcher = Watcher(root=tmp_path)
    watch_manager._WATCHERS[1] = watcher
    watch_manager._CONTEXTS[1] = ctx

    result = await watch_manager.sync_now(1)

    assert result["unchanged"] == 1
    assert result["uploaded"] == 1
    assert uploader.uploaded == [("b.mkv", 200)]


async def test_sync_now有文件级日志(tmp_path: Path):
    """**回归测试**：全量同步必须逐文件记日志。

    曾经只有「开始全量同步…」和「全量同步完成：…」两行——92 个文件
    传十几分钟，面板上完全看不出在干什么（像卡死了）。
    """
    from app.services.upload_tasks import get_runtime
    from app.services.watcher import Watcher

    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    (tmp_path / "a.mkv").write_bytes(b"x" * 100)
    (tmp_path / "b.mkv").write_bytes(b"y" * 200)

    watcher = Watcher(root=tmp_path)
    watch_manager._WATCHERS[1] = watcher
    watch_manager._CONTEXTS[1] = ctx

    await watch_manager.sync_now(1)

    logs = get_runtime(1).logs
    # 每个文件至少一行（处理中）
    assert any("a.mkv" in line for line in logs)
    assert any("b.mkv" in line for line in logs)
    # 带进度序号（**边扫边同步**，不知道总数 → 不带 /total）
    assert any("[1]" in line for line in logs)
    assert any("[2]" in line for line in logs)
    # 结果行
    assert any("全量同步完成" in line for line in logs)


async def test_sync_now任务未运行报错():
    """任务没在跑 → 报错。"""
    with pytest.raises(DriverError, match="未在运行"):
        await watch_manager.sync_now(999)


async def test_sync_now把统计写回运行态(tmp_path: Path):
    """**回归测试**：全量同步必须把统计**同步到运行态**。

    曾经只累加局部变量（``uploaded`` / ``total_bytes``），
    ``runtime.total_uploaded`` 全程不动 → 面板上「上传 N」在跑
    全量同步时**恒为 0**（生产实测：传了 286 个文件，面板显示 0）。

    运行态是面板唯一的统计来源（DB 里的统计列已删），故必须同步。
    """
    from app.services.upload_tasks import get_runtime
    from app.services.watcher import Watcher

    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    (tmp_path / "a.mkv").write_bytes(b"x" * 100)
    (tmp_path / "b.mkv").write_bytes(b"y" * 200)
    # 云端已有同名同大小 → unchanged（不计入上传数）
    driver.add_file("root", "c.mkv", 300)
    (tmp_path / "c.mkv").write_bytes(b"z" * 300)

    watcher = Watcher(root=tmp_path)
    watch_manager._WATCHERS[1] = watcher
    watch_manager._CONTEXTS[1] = ctx

    runtime = get_runtime(1)
    runtime.total_uploaded = 0
    runtime.total_bytes = 0
    runtime.total_failed = 0

    result = await watch_manager.sync_now(1)

    assert result["uploaded"] == 2
    assert result["unchanged"] == 1
    # 运行态必须跟着涨（面板显示的就是它）
    assert runtime.total_uploaded == 2
    assert runtime.total_bytes == 100 + 200
    assert runtime.total_failed == 0


async def test_sync_now失败计入运行态(tmp_path: Path):
    """失败也要计入运行态的 ``total_failed``（面板据此显示失败数）。"""
    from app.services.upload_tasks import get_runtime
    from app.services.watcher import Watcher

    driver = FakeDriver()
    uploader = FakeUploader(fail=True)
    ctx = _ctx(driver, uploader, tmp_path)

    (tmp_path / "a.mkv").write_bytes(b"x" * 100)

    watcher = Watcher(root=tmp_path)
    watch_manager._WATCHERS[1] = watcher
    watch_manager._CONTEXTS[1] = ctx

    runtime = get_runtime(1)
    runtime.total_failed = 0

    result = await watch_manager.sync_now(1)

    assert result["failed"] == 1
    assert runtime.total_failed == 1
    assert runtime.total_uploaded == 0


async def test_停用会取消全量同步(tmp_path: Path):
    """**回归测试**：停用任务必须**真的停掉**全量同步。

    曾经「停用」只停了监听器，``sync_now`` 是独立协程 → 上传还在继续
    （生产实测：用户点了停用，7GB 仍在传）。
    """
    from app.services.upload_tasks import get_runtime
    from app.services.watcher import Watcher

    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    # 造 20 个文件
    for i in range(20):
        (tmp_path / f"f{i:03d}.mkv").write_bytes(b"x" * 100)

    # **让上传变慢** —— 否则瞬间传完，来不及停
    original_upload = uploader.upload_file

    async def _slow_upload(p, parent_id, *, name=None, progress=None):
        await asyncio.sleep(0.05)
        return await original_upload(p, parent_id, name=name, progress=progress)

    uploader.upload_file = _slow_upload  # type: ignore[method-assign]

    watcher = Watcher(root=tmp_path)
    watch_manager._WATCHERS[1] = watcher
    watch_manager._CONTEXTS[1] = ctx

    # 起一个后台同步
    task = asyncio.create_task(watch_manager.sync_now(1))
    await asyncio.sleep(0.1)

    # 同步已注册
    assert 1 in watch_manager._SYNC_TASKS

    # 停用 → 必须取消
    await watch_manager.stop_watcher(1)
    await asyncio.sleep(0.1)

    assert task.done() or task.cancelled()
    assert get_runtime(1).syncing is False


async def test_启动监听器会登记进_WATCHERS(tmp_path: Path, monkeypatch):
    """``start_watcher`` 必须把 watcher 登记进 ``_WATCHERS``。

    **回归测试**：曾经只放进 ``runtime.watcher``，导致 ``get_watcher`` /
    ``sync_now`` / ``stop_all`` 都查不到 →「立即全量同步」误报
    「任务未在运行」。
    """
    driver = FakeDriver()
    uploader = FakeUploader()

    async def _fake_clients(_driver_id: int):
        return driver, uploader

    monkeypatch.setattr(watch_manager, "_build_clients", _fake_clients)

    watcher = await watch_manager.start_watcher(
        task_id=42,
        driver_id=1,
        target_file_id="root",
        watch_root=str(tmp_path),
        include_subdirs=True,
        concurrency=1,
        name="测试",
    )

    assert watch_manager.get_watcher(42) is watcher
    assert 42 in watch_manager.active_task_ids()

    await watch_manager.stop_watcher(42)
    assert watch_manager.get_watcher(42) is None


async def test_concurrency真的起多个worker(tmp_path: Path, monkeypatch):
    """**回归测试**：``concurrency`` 必须真的起 N 个 worker。

    曾经 ``concurrency`` 传进 ``_worker_loop`` 却**从未被使用**，
    worker 永远只有 1 个——面板上把并发调成 4 也毫无效果（真 bug）。

    实测 prod：并发 5 时 OSS 传输快 2 倍（30.97s → 15.40s）。
    """
    driver = FakeDriver()
    uploader = FakeUploader()

    async def _fake_clients(_driver_id: int):
        return driver, uploader

    monkeypatch.setattr(watch_manager, "_build_clients", _fake_clients)

    await watch_manager.start_watcher(
        task_id=77,
        driver_id=1,
        target_file_id="root",
        watch_root=str(tmp_path),
        include_subdirs=True,
        concurrency=3,
        name="测试并发",
    )

    workers = watch_manager._WORKERS.get(77)
    assert workers is not None, "没登记 worker"
    assert len(workers) == 3, f"应起 3 个 worker，实际 {len(workers)}"
    # 每个都是活的协程
    assert all(not w.done() for w in workers)

    await watch_manager.stop_watcher(77)
    # 停用后全部结束
    assert 77 not in watch_manager._WORKERS


async def test_多个worker不会重复处理同一文件(tmp_path: Path, monkeypatch):
    """**回归测试**：多 worker 下每个文件只被处理一次。

    队列的 ``get()`` 天然互斥——但如果实现有误（比如每个 worker 各自
    建了队列），同一文件会被传两次 → 云端出现重复副本。
    """
    driver = FakeDriver()
    uploader = FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)

    processed: list[str] = []
    orig = watch_manager._process_one

    async def _spy(task_id: int, ctx_: object, path: Path) -> None:
        processed.append(path.name)
        await orig(task_id, ctx_, path)

    monkeypatch.setattr(watch_manager, "_process_one", _spy)

    queue: asyncio.Queue[Path] = asyncio.Queue()
    workers = [
        asyncio.create_task(watch_manager._worker_loop(1, queue, ctx, 1))
        for _ in range(4)
    ]
    files = []
    for i in range(20):
        p = tmp_path / f"f{i:03d}.mkv"
        p.write_bytes(b"x" * 10)
        files.append(p)

    for p in files:
        await queue.put(p)
    await queue.join()

    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    assert sorted(processed) == sorted(p.name for p in files)
    assert len(processed) == len(set(processed)), "有文件被重复处理"


async def test_同步锁让全量同步与worker串行(tmp_path: Path):
    """``_SYNC_LOCKS`` 让全量同步与事件流 worker 互斥——同一时刻只有一个
    协程在 ``sync_file`` 里。

    用一个**慢 sync_file**（sleep 0.1s）验证：两个并发调 ``_process_one``
    的协程不会同时进入 ``sync_file``（第二个的 ``enter`` 时间戳必须 ≥
    第一个的 ``exit``）。
    """
    import time

    from app.services.uploader import RESULT_UPLOADED, SyncOutcome

    # 装一把锁（_process_one 会用它包住 sync_file 调用）
    watch_manager._SYNC_LOCKS[1] = asyncio.Lock()

    timeline: list[tuple[str, float]] = []

    async def _slow_sync(ctx, path, rel_path, on_progress=None):
        # 锁由 _process_one 外层持有，这里只记录时间线
        t0 = time.monotonic()
        timeline.append(("enter", t0))
        await asyncio.sleep(0.1)
        timeline.append(("exit", time.monotonic()))
        return SyncOutcome(RESULT_UPLOADED, rel_path, size=10)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(watch_manager, "sync_file", _slow_sync)

    driver, uploader = FakeDriver(), FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 10)

    # 并发跑两个 _process_one
    await asyncio.gather(
        watch_manager._process_one(1, ctx, path),
        watch_manager._process_one(1, ctx, path),
    )

    # 断言：两次 enter 之间夹着一个 exit（串行，不重叠）
    enters = [t for tag, t in timeline if tag == "enter"]
    exits = [t for tag, t in timeline if tag == "exit"]
    assert len(enters) == 2 and len(exits) == 2
    # 第二次进入必须在第一次退出之后（串行证据）
    assert enters[1] >= exits[0]
    monkeypatch.undo()
