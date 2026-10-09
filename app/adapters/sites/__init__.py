"""站点适配层（FR-1.5）：统一接口 + 站点特化实现。

每个站点一个文件（mteam.py、ourbits.py、...），通过 :mod:`_registry` 解析。
"""

from __future__ import annotations

from app.adapters.sites._registry import list_available, resolve
from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFeature,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
)

__all__ = [
    "FeedItem",
    "SiteAdapter",
    "SiteFeature",
    "SiteHealth",
    "SiteProfile",
    "TorrentBrief",
    "list_available",
    "resolve",
]
