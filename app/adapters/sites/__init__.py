"""站点适配层（FR-1.5）：统一接口 + 配置驱动的通用实现。"""

from __future__ import annotations

from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFeature,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
)
from app.adapters.sites.generic import GenericSiteAdapter

__all__ = [
    "FeedItem",
    "GenericSiteAdapter",
    "SiteAdapter",
    "SiteFeature",
    "SiteHealth",
    "SiteProfile",
    "TorrentBrief",
]