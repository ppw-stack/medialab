"""下载器适配层精简测试。

聚焦 5 个核心契约 + TorrentInfo/TorrentState/TorrentFile 数据类。
"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import (
    AddTorrentPayload,
    DownloaderAdapter,
    DownloaderError,
    TorrentFile,
    TorrentFilter,
    TorrentInfo,
    TorrentState,
)
from app.adapters.downloaders.qbittorrent import QbittorrentAdapter


class _StubAdapter(DownloaderAdapter):
    """最小存根：所有抽象方法返回安全值。"""

    client_type = "stub"

    async def ping(self) -> bool:
        return True

    async def add_torrent(self, payload: AddTorrentPayload) -> object:
        return object()

    async def list_torrents(self, filt: TorrentFilter | None = None) -> list[TorrentInfo]:
        return []

    async def remove(self, hashes, *, delete_data: bool) -> bool:
        return True

    async def set_pause(self, hashes, paused: bool) -> bool:
        return True


# ---- TorrentState 属性分类 ----


def test_state_is_completed() -> None:
    assert TorrentState.SEEDING.is_completed
    assert TorrentState.UPLOADING.is_completed
    assert not TorrentState.DOWNLOADING.is_completed


def test_state_is_downloading() -> None:
    assert TorrentState.DOWNLOADING.is_downloading
    assert TorrentState.STALLED_DL.is_downloading
    assert TorrentState.CHECKING.is_downloading
    assert not TorrentState.SEEDING.is_downloading


def test_state_is_paused() -> None:
    assert TorrentState.PAUSED_DL.is_paused
    assert TorrentState.PAUSED_UP.is_paused
    assert not TorrentState.SEEDING.is_paused


def test_state_is_fatal() -> None:
    """FR-3.4 表：不等窗口，立即中止。"""
    assert TorrentState.ERROR.is_fatal
    assert TorrentState.MISSING_FILES.is_fatal
    assert not TorrentState.CHECKING.is_fatal
    assert not TorrentState.PAUSED_DL.is_fatal


# ---- TorrentInfo 计算属性 ----


def test_remaining_bytes_computed() -> None:
    t = TorrentInfo(hash="h", name="n", size=1000, downloaded=300)
    assert t.remaining_bytes == 700


def test_remaining_bytes_clamped_to_zero() -> None:
    t = TorrentInfo(hash="h", name="n", size=100, downloaded=200)
    assert t.remaining_bytes == 0


# ---- 默认值（不支持时返回安全值）----


def test_get_files_default_returns_empty() -> None:
    """基类 get_files 默认返回 []。"""
    ad = _StubAdapter()
    import asyncio

    assert asyncio.run(ad.get_files("abc")) == []


def test_close_default_noop() -> None:
    """基类 close 默认 no-op。"""
    ad = _StubAdapter()
    import asyncio

    asyncio.run(ad.close())  # 不抛异常


# ---- 契约保护 ----


def test_qbittorrent_implements_all_abstracts() -> None:
    """QbittorrentAdapter 必须实现全部抽象方法（FR-1.3）。"""
    ad = QbittorrentAdapter("qbt", "http://localhost:8080", "admin", "pass")
    import inspect

    abstracts = {
        name for name, m in inspect.getmembers(DownloaderAdapter, predicate=inspect.isabstract)
        if not isinstance(m, property)
    }
    assert abstracts <= {
        "ping", "add_torrent", "list_torrents", "remove", "set_pause"
    }


# ---- qbittorrent 状态映射（内部 _STATE_MAP）----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("downloading", TorrentState.DOWNLOADING),
        ("stalledDL", TorrentState.STALLED_DL),
        ("uploading", TorrentState.UPLOADING),
        ("stalledUP", TorrentState.SEEDING),
        ("pausedUP", TorrentState.PAUSED_UP),
        ("error", TorrentState.ERROR),
        ("missingFiles", TorrentState.MISSING_FILES),
        ("checkingResumeData", TorrentState.CHECKING),
        ("whatever", TorrentState.UNKNOWN),
    ],
)
def test_qbt_state_mapping(raw: str, expected: TorrentState) -> None:
    from app.adapters.downloaders.qbittorrent import _STATE_MAP

    assert _STATE_MAP.get(raw, TorrentState.UNKNOWN) is expected


# ---- DownloaderError.transient ----


def test_downloader_error_transient_flag() -> None:
    e = DownloaderError("transient", transient=True)
    assert e.transient is True
    assert str(e) == "transient"


def test_downloader_error_default_not_transient() -> None:
    e = DownloaderError("permanent")
    assert e.transient is False