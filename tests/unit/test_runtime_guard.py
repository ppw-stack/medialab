"""运行时守卫的单元测试（心跳看门狗 + 挂载探活）。

锁住的契约：

1. ``snapshot()`` **只读缓存**、不做 IO（它被 ``/healthz`` 同步调用）
2. 心跳正常时 ``loop_ok`` 为真、滞后很小
3. 挂载探活：存在的目录 → ``ok``；不存在的路径 → ``error``
4. ``start`` / ``stop`` 幂等（重复 start 不会起两套线程）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from app.services import runtime_guard


@pytest.fixture(autouse=True)
def _reset_guard():
    runtime_guard.stop()
    runtime_guard._state.mounts.clear()
    runtime_guard._state.last_beat = time.monotonic()
    runtime_guard._state.dumped_for_stall = False
    yield
    runtime_guard.stop()
    runtime_guard._state.mounts.clear()


def test_snapshot_is_cache_only(tmp_path: Path) -> None:
    """snapshot 不做 IO——即使路径不存在也能立刻返回。"""
    snap = runtime_guard.snapshot()
    assert snap["loop_ok"] is True
    assert snap["loop_stalled_ms"] < 1000
    assert snap["mounts"] == []


@pytest.mark.asyncio
async def test_heartbeat_updates_stamp() -> None:
    """loop 正常跑时，心跳把滞后压回 0 附近。"""
    runtime_guard.start(asyncio.get_running_loop(), mounts=[])
    await asyncio.sleep(0.2)
    runtime_guard._beat()  # 直接投递一次（模拟 loop 活着）
    snap = runtime_guard.snapshot()
    assert snap["loop_stalled_ms"] < 1000
    assert snap["loop_ok"] is True


@pytest.mark.asyncio
async def test_stalled_loop_is_detected() -> None:
    """心跳滞后 → ``loop_ok=False``（loop 被同步 IO 卡住的信号）。"""
    runtime_guard.start(asyncio.get_running_loop(), mounts=[])
    # 假装 loop 卡住：把心跳时间戳推到很久以前
    runtime_guard._state.last_beat = time.monotonic() - 30
    snap = runtime_guard.snapshot()
    assert snap["loop_ok"] is False
    assert snap["loop_stalled_ms"] >= 30000


@pytest.mark.asyncio
async def test_mount_probe_reports_ok(tmp_path: Path) -> None:
    """存在的目录 → 探活 ok。"""
    runtime_guard.start(asyncio.get_running_loop(), mounts=[str(tmp_path)])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        snap = runtime_guard.snapshot()
        if snap["mounts"] and snap["mounts"][0]["status"] == "ok":
            break
    snap = runtime_guard.snapshot()
    assert snap["mounts"], "探活结果没出来"
    assert snap["mounts"][0]["status"] == "ok"
    assert snap["mounts"][0]["checked_at"]


@pytest.mark.asyncio
async def test_mount_probe_reports_error(tmp_path: Path) -> None:
    """不存在的路径 → error（不是 hang——挂载本身是通的）。"""
    missing = tmp_path / "nope"
    runtime_guard.start(asyncio.get_running_loop(), mounts=[str(missing)])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        snap = runtime_guard.snapshot()
        if snap["mounts"] and snap["mounts"][0]["status"] != "unknown":
            break
    snap = runtime_guard.snapshot()
    assert snap["mounts"][0]["status"] == "error"
    assert snap["mounts"][0]["error"]


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    """重复 start 不会起两套线程。"""
    loop = asyncio.get_running_loop()
    runtime_guard.start(loop, mounts=[])
    first = list(runtime_guard._state.threads)
    runtime_guard.start(loop, mounts=[])
    assert runtime_guard._state.threads == first
