"""下载器适配层（FR-1）：统一接口 + 客户端实现 + 单实例操作锁。"""

from __future__ import annotations

from app.adapters.downloaders.base import (
    AddTorrentPayload,
    DownloaderAdapter,
    DownloaderError,
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
    "DownloaderAdapter",
    "DownloaderError",
    "QbittorrentAdapter",
    "TorrentFile",
    "TorrentFilter",
    "TorrentHandle",
    "TorrentInfo",
    "TorrentState",
    "locks",
]