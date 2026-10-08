"""下载器适配层（FR-1）：统一接口 + 客户端实现 + 单实例操作锁。"""

from __future__ import annotations

from app.adapters.downloaders.base import (
    AddTorrentPayload,
    Capabilities,
    DownloaderAdapter,
    DownloaderError,
    Feature,
    GlobalStats,
    TorrentFile,
    TorrentFilter,
    TorrentHandle,
    TorrentInfo,
    TorrentState,
)
from app.adapters.downloaders.locks import locks
from app.adapters.downloaders.qbittorrent import QbittorrentAdapter

__all__ = [
    "AddTorrentPayload",
    "Capabilities",
    "DownloaderAdapter",
    "DownloaderError",
    "Feature",
    "GlobalStats",
    "QbittorrentAdapter",
    "TorrentFile",
    "TorrentFilter",
    "TorrentHandle",
    "TorrentInfo",
    "TorrentState",
    "locks",
]