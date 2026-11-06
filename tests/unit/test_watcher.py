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
    dispatched: list[tuple[Path, int]] = []
    now = [1000.0]

    async def _dispatch(path: Path, size: int) -> None:
        dispatched.append((path, size))

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
    # 投递带 size（分流依据）——稳态采样时的值
    assert any(p == path and s == 10 for p, s in watcher.dispatched)  # type: ignore[attr-defined]
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


# ---------------------------------------- 依赖导入预热（2026-11-07 prod 事故）


def test_预热导入幂等且加载后端() -> None:
    """``_preflight_watchdeps``：加载 watchfiles 与 anyio 后端，可重复调用。

    awatch 首次调用会在**事件循环线程上**同步 ``import_module("anyio.
    _backends._asyncio")``——2026-11-07 prod：CIFS 挂死引发 FS 拥塞，
    这个导入卡 8 分钟把整个循环冻结（面板假死）。预热必须让导入提前
    在线程里完成，之后循环上的调用是纯内存查找。
    """
    import sys

    from app.services import watcher as watcher_mod

    watcher_mod._preflight_watchdeps()
    watcher_mod._preflight_watchdeps()  # 幂等（导入本就一次性）

    assert "watchfiles.main" in sys.modules
    assert "anyio._backends._asyncio" in sys.modules


async def test_收事件先预热再调awatch(
    watcher: Watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_watch_events`` 必须先完成线程内预热，再调用 ``awatch``。

    顺序是这次修复的全部意义：预热在 to_thread 里跑（循环保持响应），
    awatch 首次调用才不会把同步导入带回事件循环。
    """
    from app.services import watcher as watcher_mod

    order: list[str] = []
    monkeypatch.setattr(
        watcher_mod, "_preflight_watchdeps", lambda: order.append("preflight")
    )

    async def _fake_changes(*args: object, **kwargs: object):
        order.append("awatch")
        yield set()  # 一批空事件后结束，_watch_events 自然返回

    monkeypatch.setattr("watchfiles.awatch", _fake_changes)

    await watcher._watch_events()

    assert order == ["preflight", "awatch"]
