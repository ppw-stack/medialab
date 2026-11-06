"""双通道分流 / 全局去重 / 溢出回灌 / 动态并发闸 的单元测试。

**背景**（2026-11-07 生产「上传队列已满」刷屏）：
单 worker 串行（大文件占队头 40 分钟）+ 小文件每个 ~2-3s 纯协议开销
→ 1000 槽队列被打满 → 逐条丢弃并刷爆日志环。改造为：

- 双通道：大文件恒串行（实测并发有害），小文件并发（实测唯一安全路径）
- 全局「处理中」去重：同文件绝不被两个协程同时传（光鸭同名上传自动
  改名 ``xxx(1).bin``，双传 = 云端垃圾副本）
- 溢出暂存：队列满不丢，worker 空闲回灌
- 熔断：连续失败自动降为串行（复刻实测「并发卡死」的保险丝）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.services import watch_manager

from tests.unit.upload_fakes import FakeDriver, FakeUploader


@pytest.fixture(autouse=True)
def _clean_manager():
    """每个用例前后清空管理器状态（与 test_watch_manager 同款）。"""
    from app.services.upload_tasks import clear_all

    for registry in (
        watch_manager._WATCHERS,
        watch_manager._WORKERS,
        watch_manager._QUEUES_LARGE,
        watch_manager._QUEUES_SMALL,
        watch_manager._CONTEXTS,
        watch_manager._SYNC_LOCKS,
        watch_manager._INFLIGHT,
        watch_manager._OVERFLOW,
        watch_manager._SMALL_GATES,
    ):
        registry.clear()
    clear_all()
    yield
    for registry in (
        watch_manager._WATCHERS,
        watch_manager._WORKERS,
        watch_manager._QUEUES_LARGE,
        watch_manager._QUEUES_SMALL,
        watch_manager._CONTEXTS,
        watch_manager._SYNC_LOCKS,
        watch_manager._INFLIGHT,
        watch_manager._OVERFLOW,
        watch_manager._SMALL_GATES,
    ):
        registry.clear()
    clear_all()


def _make_queues(maxsize: int = 10, task_id: int = 1) -> tuple[asyncio.Queue, asyncio.Queue]:
    """造一对双通道队列并**登记进模块注册表**——回灌从注册表取队列。"""
    large: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    small: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    watch_manager._QUEUES_LARGE[task_id] = large
    watch_manager._QUEUES_SMALL[task_id] = small
    return large, small


# ---------------------------------------- 分流阈值


async def test_按阈值分流_小于进小通道(tmp_path: Path):
    """size < 阈值 → 小文件队列。"""
    large, small = _make_queues()
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "poster.jpg", 500)

    assert small.qsize() == 1
    assert large.qsize() == 0


async def test_按阈值分流_恰好等于阈值进大通道(tmp_path: Path):
    """**边界**：size == 阈值 → 大文件通道（``<`` 严格小于）。"""
    large, small = _make_queues()
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "a.mkv", 1024)

    assert large.qsize() == 1
    assert small.qsize() == 0


async def test_阈值0_全部走大通道(tmp_path: Path):
    """阈值 0 = 关闭小通道（全部串行——回滚开关）。"""
    large, small = _make_queues()
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 0)

    await dispatch(tmp_path / "poster.jpg", 1)

    assert large.qsize() == 1
    assert small.qsize() == 0


# ---------------------------------------- 全局去重


async def test_同路径重复事件去重(tmp_path: Path):
    """同一路径已在队列 / 处理中 → 静默跳过（不占第二个坑）。"""
    large, small = _make_queues()
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "poster.jpg", 500)
    await dispatch(tmp_path / "poster.jpg", 500)
    await dispatch(tmp_path / "poster.jpg", 500)

    assert small.qsize() == 1


async def test_处理后允许再次入队(tmp_path: Path):
    """worker 处理完移除「处理中」标记——文件再次变更可再次入队。"""
    large, small = _make_queues()
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "poster.jpg", 500)
    watch_manager._INFLIGHT[1].discard("poster.jpg")  # 模拟 worker 处理完

    await dispatch(tmp_path / "poster.jpg", 500)
    assert small.qsize() == 2


# ---------------------------------------- 溢出暂存与回灌


async def test_溢出暂存后回灌(tmp_path: Path):
    """队列满 → 暂存；worker 取走一个后 → 回灌成功并登记。"""
    large, small = _make_queues(maxsize=1)
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "a.jpg", 500)   # 占住小队列
    await dispatch(tmp_path / "b.jpg", 500)   # 满 → 暂存
    assert list(watch_manager._OVERFLOW[1]) == ["b.jpg"]

    # worker 消费一个 → 队列有空位 → 回灌
    small.get_nowait()
    watch_manager._drain_overflow(1)

    assert small.qsize() == 1
    assert not watch_manager._OVERFLOW[1]
    assert "b.jpg" in watch_manager._INFLIGHT[1]


async def test_回灌按原大小路由回对应通道(tmp_path: Path):
    """暂存记录 size——回灌时重新按阈值路由（大文件不串进小通道）。"""
    large, small = _make_queues(maxsize=1)
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "a.mkv", 5_000_000)  # 占住大队列
    await dispatch(tmp_path / "b.jpg", 500)        # 小文件暂存
    await dispatch(tmp_path / "c.mkv", 5_000_000)  # 大文件暂存

    large.get_nowait()
    watch_manager._drain_overflow(1)

    # 各回各的通道
    assert large.qsize() == 1 and large.get_nowait().path.name == "c.mkv"
    assert small.qsize() == 1 and small.get_nowait().path.name == "b.jpg"


async def test_暂存满才真丢并计数(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """暂存也满 → 真丢 + ``dropped`` 计数（「立即全量同步」兜底）。"""
    monkeypatch.setattr(watch_manager, "_OVERFLOW_LIMIT", 1)
    large, small = _make_queues(maxsize=1)
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "a.jpg", 500)    # 占住队列
    await dispatch(tmp_path / "b.jpg", 500)    # 进暂存
    await dispatch(tmp_path / "c.jpg", 500)    # 暂存满 → 真丢

    from app.services.upload_tasks import get_runtime

    runtime = get_runtime(1)
    assert runtime.dropped == 1
    assert list(watch_manager._OVERFLOW[1]) == ["b.jpg"]


async def test_全量同步接手后暂存份作废(tmp_path: Path):
    """全量同步正在传的文件，溢出暂存里的重复份在回灌时作废。"""
    large, small = _make_queues(maxsize=1)
    dispatch = watch_manager._make_dispatcher(1, tmp_path, large, small, 1024)

    await dispatch(tmp_path / "a.jpg", 500)   # 占住队列
    await dispatch(tmp_path / "b.jpg", 500)   # 暂存

    # 模拟全量同步接手 b.jpg（登记「处理中」）
    watch_manager._INFLIGHT[1].add("b.jpg")
    watch_manager._drain_overflow(1)

    assert "b.jpg" not in watch_manager._OVERFLOW[1]
    # b.jpg 没有被回灌（作废）——队列里只剩占位的 a.jpg
    assert small.qsize() == 1
    assert small.get_nowait().path.name == "a.jpg"


# ---------------------------------------- 动态并发闸


def test_闸默认放开到大文件让路后恢复():
    """无大文件 → K；大文件上传中 → 1；传完 → K。"""
    gate = watch_manager._SmallGate(4)
    assert gate.effective_limit() == 4

    gate.big_active = True
    assert gate.effective_limit() == 1

    gate.big_active = False
    assert gate.effective_limit() == 4


def test_闸熔断触发与恢复():
    """连续失败 3 次 → 熔断（冷却 60s + 降级）；连续成功 10 次 → 恢复。"""
    gate = watch_manager._SmallGate(4)

    assert gate.record_failure() is False
    assert gate.record_failure() is False
    assert gate.record_failure() is True  # 第 3 次触发

    assert gate.derated is True
    # 冷却期内 → 1
    assert gate.effective_limit() == 1

    # 冷却过了，但还没攒够恢复成功数 → 仍串行
    gate.resume_at = 0.0
    assert gate.effective_limit() == 1

    for _ in range(9):
        gate.record_success()
    assert gate.derated is True  # 9 次还不够

    gate.record_success()  # 第 10 次 → 恢复
    assert gate.derated is False
    assert gate.effective_limit() == 4


def test_闸成功清零失败计数():
    """失败 2 次 + 成功 1 次 + 失败 2 次 → 不熔断（连续才计数）。"""
    gate = watch_manager._SmallGate(4)
    gate.record_failure()
    gate.record_failure()
    gate.record_success()  # 清零
    assert gate.record_failure() is False
    assert gate.record_failure() is False


async def test_闸实际限流():
    """K=2：6 个任务最多 2 个同时在跑。"""
    gate = watch_manager._SmallGate(2)
    active = 0
    peak = 0

    async def _job(ordinal: int) -> None:
        nonlocal active, peak
        await gate.acquire(ordinal)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        await gate.release(ordinal)

    await asyncio.gather(*[_job(i) for i in range(6)])
    assert peak == 2


async def test_闸在大文件上传时降为串行():
    """big_active=True：6 个任务全部串行（peak == 1）。"""
    gate = watch_manager._SmallGate(2)
    gate.big_active = True
    active = 0
    peak = 0

    async def _job(ordinal: int) -> None:
        nonlocal active, peak
        await gate.acquire(ordinal)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        await gate.release(ordinal)

    await asyncio.gather(*[_job(i) for i in range(6)])
    assert peak == 1


async def test_闸等待者被释放唤醒():
    """占用者释放后，等待者能拿到名额（不依赖超时轮询也能前进）。"""
    gate = watch_manager._SmallGate(1)
    entered: list[int] = []

    async def _job(ordinal: int) -> None:
        await gate.acquire(ordinal)
        entered.append(ordinal)
        await asyncio.sleep(0.02)
        await gate.release(ordinal)

    # 0 先拿到；1、2 排队——不 sleep 也能在 0 释放后前进
    await asyncio.gather(*[_job(i) for i in range(3)])
    assert sorted(entered) == [0, 1, 2]


# ---------------------------------------- 熔断集成（worker → 闸）


def _ctx(driver: FakeDriver, uploader: FakeUploader, root: Path):
    from app.services.cloud_index import CloudDirIndex
    from app.services.uploader import SyncContext

    return SyncContext(
        driver=driver,
        uploader=uploader,
        index=CloudDirIndex(driver, "root"),
        root=root,
        delete_retries=2,
        delete_delay=0.01,
    )


async def test_小通道连续失败触发熔断(tmp_path: Path):
    """FakeUploader 连续失败 3 次 → 闸降级（串行）。"""
    driver, uploader = FakeDriver(), FakeUploader(fail=True)
    ctx = _ctx(driver, uploader, tmp_path)
    for i in range(3):
        (tmp_path / f"f{i}.jpg").write_bytes(b"x")

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(3):
        queue.put_nowait(
            watch_manager._Item(path=tmp_path / f"f{i}.jpg", size=1)
        )
    gate = watch_manager._SmallGate(2)

    worker = asyncio.create_task(
        watch_manager._small_worker_loop(1, queue, ctx, ordinal=0, gate=gate)
    )
    await asyncio.sleep(0.2)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert gate.derated is True
    from app.services.upload_tasks import get_runtime

    logs = get_runtime(1).logs
    assert any("熔断" in line for line in logs)


async def test_小通道成功不触发熔断(tmp_path: Path):
    """正常上传 → 闸保持并发。"""
    driver, uploader = FakeDriver(), FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)
    for i in range(3):
        (tmp_path / f"f{i}.jpg").write_bytes(b"x" * 10)

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(3):
        queue.put_nowait(
            watch_manager._Item(path=tmp_path / f"f{i}.jpg", size=10)
        )
    gate = watch_manager._SmallGate(2)

    worker = asyncio.create_task(
        watch_manager._small_worker_loop(1, queue, ctx, ordinal=0, gate=gate)
    )
    await asyncio.sleep(0.2)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert gate.derated is False
    assert gate.fail_streak == 0


# ---------------------------------------- 全量同步与事件流互斥


async def test_sync_now跳过正在处理的文件(tmp_path: Path):
    """全量同步扫到「已在事件队列」的文件 → 跳过（防同文件双传）。"""
    driver, uploader = FakeDriver(), FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)
    (tmp_path / "a.mkv").write_bytes(b"x" * 100)

    from app.services.watcher import Watcher

    watch_manager._WATCHERS[1] = Watcher(root=tmp_path)
    watch_manager._CONTEXTS[1] = ctx
    watch_manager._INFLIGHT[1] = {"a.mkv"}  # 模拟事件队列正在处理

    result = await watch_manager.sync_now(1)

    assert result["skipped_inflight"] == 1
    assert uploader.uploaded == []
    # 跳过不登记「处理中」（那是事件流协程的职责）
    assert watch_manager._INFLIGHT[1] == {"a.mkv"}


async def test_sync_now正常文件不受去重影响(tmp_path: Path):
    """不在队里的文件照常上传——去重只拦「正在处理」的。"""
    driver, uploader = FakeDriver(), FakeUploader()
    ctx = _ctx(driver, uploader, tmp_path)
    (tmp_path / "a.mkv").write_bytes(b"x" * 100)
    (tmp_path / "b.mkv").write_bytes(b"y" * 200)

    from app.services.watcher import Watcher

    watch_manager._WATCHERS[1] = Watcher(root=tmp_path)
    watch_manager._CONTEXTS[1] = ctx
    watch_manager._INFLIGHT[1] = {"a.mkv"}

    result = await watch_manager.sync_now(1)

    assert result["uploaded"] == 1
    assert uploader.uploaded == [("b.mkv", 200)]


# ---------------------------------------- 运行态可观测


async def test_runtime暴露双通道水位与丢弃计数(tmp_path: Path):
    """``to_dict`` 带上 queue_large / queue_small / overflow / dropped。

    **为什么**：过去队列满只能靠刷屏的丢弃日志感知——看不到积压程度。
    """
    from app.services.upload_tasks import get_runtime

    runtime = get_runtime(1)
    runtime.queue_large = asyncio.Queue(maxsize=5)
    runtime.queue_small = asyncio.Queue(maxsize=5)
    await runtime.queue_large.put("x")
    runtime.overflow = 3
    runtime.dropped = 1
    runtime.current_small = "a/poster.jpg"

    data = runtime.to_dict()

    assert data["queue_large"] == 1
    assert data["queue_small"] == 0
    assert data["overflow"] == 3
    assert data["dropped"] == 1
    assert data["current_small"] == "a/poster.jpg"
    # 旧字段保留（大文件通道），前端兼容
    assert "current" in data
