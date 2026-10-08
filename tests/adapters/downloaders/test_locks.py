"""下载器单实例操作锁测试（FR-1.2 / R5 / 非功能·并发安全）。"""

from __future__ import annotations

import asyncio

import pytest
from app.adapters.downloaders.locks import DownloaderLocks


@pytest.mark.asyncio
async def test_same_downloader_serializes() -> None:
    """并发写同一下载器必须串行。"""
    locks = DownloaderLocks()
    order: list[str] = []

    async def worker(tag: str, delay: float) -> None:
        async with locks.acquire("qbt-main"):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    await asyncio.gather(worker("a", 0.02), worker("b", 0.0))
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


@pytest.mark.asyncio
async def test_different_downloaders_run_concurrently() -> None:
    """不同实例互不阻塞。"""
    locks = DownloaderLocks()
    order: list[str] = []

    async def worker(key: str) -> None:
        async with locks.acquire(key):
            order.append(f"{key}-in")
            await asyncio.sleep(0.01)
            order.append(f"{key}-out")

    await asyncio.gather(worker("qbt-a"), worker("qbt-b"))
    assert order[:2] == ["qbt-a-in", "qbt-b-in"]


@pytest.mark.asyncio
async def test_lock_released_after_exception() -> None:
    """异常不得导致死锁。"""
    locks = DownloaderLocks()

    with pytest.raises(RuntimeError):
        async with locks.acquire("qbt"):
            raise RuntimeError("boom")

    async with locks.acquire("qbt"):
        pass


@pytest.mark.asyncio
async def test_is_busy_reports_state() -> None:
    locks = DownloaderLocks()
    assert locks.is_busy("qbt") is False
    async with locks.acquire("qbt"):
        assert locks.is_busy("qbt") is True
    assert locks.is_busy("qbt") is False