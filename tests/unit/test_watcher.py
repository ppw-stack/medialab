"""监听器（watchfiles 封装）的测试。

**不真的起 watchfiles**——直接测事件处理、稳态判定、过滤逻辑。
watchfiles 本身是第三方库，不该在我们的测试里被依赖。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.watcher import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    Watcher,
)


@pytest.fixture
def watcher(tmp_path: Path):
    """监听器实例（记录投递的文件）。

    **时钟可注入**——稳态判定依赖「距最后一次变化多久」，真实等待会让
    测试变慢且不稳定。故用假时钟手动推进。
    """
    dispatched: list[Path] = []
    now = [1000.0]

    async def _dispatch(path: Path) -> None:
        dispatched.append(path)

    w = Watcher(
        root=tmp_path,
        include_subdirs=True,
        polling=True,
        poll_ms=100,
        debounce_ms=50,
        stable_check_sec=1.5,
        dispatch=_dispatch,
        clock=lambda: now[0],
    )
    w.dispatched = dispatched  # type: ignore[attr-defined]
    w.now = now  # type: ignore[attr-defined]
    return w


async def test_新增事件进入待确认(watcher: Watcher, tmp_path: Path) -> None:
    """新增文件 → 进入待确认表（不立即投递）。"""
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 10)

    await watcher._on_event(CHANGE_ADDED, path)

    assert watcher.stats.events == 1
    assert watcher.stats.dispatched == 0
    assert path in watcher._pending


async def test_删除事件被忽略(watcher: Watcher, tmp_path: Path) -> None:
    """删除事件不处理（本地删了不动云端）。"""
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 10)
    await watcher._on_event(CHANGE_ADDED, path)
    await watcher._on_event(CHANGE_DELETED, path)

    assert path not in watcher._pending
    assert watcher.stats.dispatched == 0


async def test_临时文件被过滤(watcher: Watcher, tmp_path: Path) -> None:
    """临时文件不进待确认表。"""
    path = tmp_path / "a.mkv.part"
    path.write_bytes(b"x")

    await watcher._on_event(CHANGE_ADDED, path)

    assert path not in watcher._pending
    assert watcher.stats.filtered == 1


async def test_软链逃逸被拒(watcher: Watcher, tmp_path: Path) -> None:
    """软链指向根外 → 计入 rejected。"""
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        return  # 平台不支持软链

    await watcher._on_event(CHANGE_ADDED, link)

    assert watcher.stats.rejected == 1
    assert link not in watcher._pending


async def test_稳态后投递(watcher: Watcher, tmp_path: Path) -> None:
    """文件已写完且静默够久 → 投递。"""
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 10)

    await watcher._on_event(CHANGE_ADDED, path)
    await watcher._check_stable_once()
    # 内容没变，但还没静默够久 → 不投递
    assert watcher.stats.dispatched == 0

    watcher.now[0] += 2.0  # 静默超过 stable_check_sec
    await watcher._check_stable_once()

    assert watcher.stats.dispatched == 1
    assert path in watcher.dispatched  # type: ignore[attr-defined]
    assert path not in watcher._pending


async def test_还在写则继续等(watcher: Watcher, tmp_path: Path) -> None:
    """文件持续变大 → 不投递（会一直等到稳定）。

    写入必须发生在**检查之前**——检查时看到的是当时的 size。
    """
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 10)
    await watcher._on_event(CHANGE_ADDED, path)

    # 每轮：先写（size 变），再检查 → 每次都判定「还在变」
    for size in (100, 200, 300):
        path.write_bytes(b"x" * size)
        watcher.now[0] += 2.0
        await watcher._check_stable_once()

    assert watcher.stats.dispatched == 0
    assert path in watcher._pending


async def test_写入停止后投递(watcher: Watcher, tmp_path: Path) -> None:
    """写到一半 → 停了 → 静默够久后投递。

    模拟真实写入：先建文件（size 0），事件触发，再慢慢写。
    """
    path = tmp_path / "a.mkv"
    path.write_bytes(b"")  # 先建空文件
    await watcher._on_event(CHANGE_ADDED, path)

    # 第一轮：size 还是 0，与采样一致 → 判定「静默够久」后就会投递
    # 但真实场景里 size 会继续变。先写一部分，再检查
    path.write_bytes(b"x" * 50)
    watcher.now[0] += 2.0
    await watcher._check_stable_once()
    assert watcher.stats.dispatched == 0  # size 变了 → 重新计时

    # 继续写
    path.write_bytes(b"x" * 100)
    watcher.now[0] += 2.0
    await watcher._check_stable_once()
    assert watcher.stats.dispatched == 0  # 又变了

    # 停了 → 静默够久 → 投递
    watcher.now[0] += 2.0
    await watcher._check_stable_once()
    assert watcher.stats.dispatched == 1


async def test_相对路径是posix风格(watcher: Watcher, tmp_path: Path) -> None:
    """相对路径用 ``/`` 分隔。"""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    path = sub / "c.mkv"
    assert watcher.relative(path) == "a/b/c.mkv"


async def test_启动停止(watcher: Watcher, tmp_path: Path) -> None:
    """启动后 running 为 True，停止后为 False。"""
    await watcher.start()
    assert watcher.running is True
    await watcher.stop()
    assert watcher.running is False


async def test_全量投递(watcher: Watcher, tmp_path: Path) -> None:
    """``dispatch_existing`` 把目录里已有的文件全投出去。"""
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")
    (tmp_path / "c.mkv.part").write_bytes(b"x")

    count = await watcher.dispatch_existing()

    assert count == 2
    assert watcher.stats.dispatched == 2
