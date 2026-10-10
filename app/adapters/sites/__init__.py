"""站点适配层（FR-1.5）：统一接口 + 站点特化实现。

每个站点一个文件（mteam.py、ourbits.py、...），通过 :mod:`_registry` 解析。

5 个契约方法：
1. check_reachability
2. search_torrents
3. download_torrent
4. check_status
5. get_torrent_detail
"""

from __future__ import annotations

from app.adapters.sites._registry import list_available, resolve
from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
)

__all__ = [
    "FeedItem",
    "SiteAdapter",
    "SiteHealth",
    "SiteProfile",
    "TorrentBrief",
    "list_available",
    "resolve",
]
