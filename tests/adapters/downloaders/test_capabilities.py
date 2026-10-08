"""下载器能力与降级测试（FR-1.3 / R12）。"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import (
    Capabilities,
    DownloaderAdapter,
    Feature,
    TorrentState,
)
from app.adapters.downloaders.qbittorrent import QbittorrentAdapter


class _NoFilesAdapter(DownloaderAdapter):
    """模拟 Transmission/Deluge 这类无逐任务文件清单的客户端。"""

    client_type = "fake"

    async def ping(self) -> bool:
        return True

    async def list_torrents(self, filt=None) -> list:
        return []

    async def add_torrent(self, payload) -> object:
        return object()

    async def remove(self, hashes, *, delete_data: bool) -> bool:
        return True

    async def set_pause(self, hashes, paused: bool) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            frozenset(
                {
                    Feature.ADD_TORRENT,
                    Feature.CATEGORY,
                    # 故意缺 Feature.GET_FILES：模拟 Transmission/Deluge
                }
            )
        )


def test_qbittorrent_supports_full_feature_set() -> None:
    ad = QbittorrentAdapter("qbt", "http://localhost:8080", "admin", "pass")
    caps = ad.capabilities()
    for feature in (
        Feature.ADD_TORRENT,
        Feature.CATEGORY,
        Feature.TAGS,
        Feature.SAVE_PATH,
        Feature.RATE_LIMIT_PER_TASK,
        Feature.RATE_LIMIT_GLOBAL,
        Feature.NATIVE_RSS,
        Feature.GET_FILES,
    ):
        assert caps.has(feature), feature
    assert ad.can_delete_data


def test_missing_get_files_disables_data_deletion() -> None:
    """R12：无文件清单的客户端必须禁用数据删除。"""
    ad = _NoFilesAdapter()
    caps = ad.capabilities()
    assert not caps.has(Feature.GET_FILES)
    assert not caps.supports_data_deletion()
    assert not ad.can_delete_data
    reason = caps.degrade_reason()
    assert reason and "get_files" in reason


def test_full_capabilities_no_degrade_reason() -> None:
    assert Capabilities(frozenset({Feature.GET_FILES})).degrade_reason() is None


def test_unsupported_methods_return_safe_defaults() -> None:
    """FR-1.3：不抛 NotImplementedError，返回安全默认值。"""
    import asyncio

    ad = _NoFilesAdapter()
    assert asyncio.run(ad.get_files("abc")) == []
    assert asyncio.run(ad.set_rate_limit(up=1)) is False
    assert asyncio.run(ad.set_save_path(["h"], "/data")) is False
    assert asyncio.run(ad.get_log()) == []


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


def test_state_classification_helpers() -> None:
    assert TorrentState.SEEDING.is_completed
    assert TorrentState.DOWNLOADING.is_downloading
    assert TorrentState.PAUSED_DL.is_paused
    assert TorrentState.ERROR.is_fatal
    assert TorrentState.MISSING_FILES.is_fatal
    assert not TorrentState.CHECKING.is_fatal


def test_remaining_bytes_computed() -> None:
    from app.adapters.downloaders.base import TorrentInfo

    t = TorrentInfo(hash="h", name="n", size=1000, downloaded=300)
    assert t.remaining_bytes == 700