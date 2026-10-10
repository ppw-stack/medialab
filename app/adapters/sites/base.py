"""PT 站点适配层（FR-1.5）。

与下载器适配层对称，把站点差异收拢在一层。两条不可违反的约束：

- **解析层不判定**（FR-1.5.3）：``parse_window`` 只如实返回窗口信息，
  是否中止由业务层决定。站点逻辑不得渗入引擎。
- **不碰下载器、不写数据库**：本层不得直接操作下载器或持久化判定结果，
  保持可脱离 FastAPI 与数据库独立单测。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.timeutils import ClockOffset
from app.core.windows import FreeWindow


class SiteFeature(StrEnum):
    """站点能力（FR-1 ``capabilities()``）。"""

    RSS = "rss"
    SEARCH_API = "search_api"
    DETAIL_API = "detail_api"
    TIMESTAMP_FIELD = "timestamp_field"
    STATUS_VERIFY = "status_verify"
    CLOCK_API = "clock_api"


class TorrentBrief(StrEnum):
    """站点侧种子状态（FR-4.1「种子被站点删除」判定）。"""

    EXISTS = "exists"
    DELETED = "deleted"
    VIOLATION = "violation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SiteHealth:
    """连通性结果（FR-1.5 ``ping()``）。"""

    reachable: bool
    logged_in: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FeedItem:
    """归一化的 RSS 条目（FR-1.5.1）。"""

    title: str
    link: str
    info_hash: str | None = None
    size: int = 0
    category: str | None = None
    published_at: datetime | None = None
    description: str = ""
    torrent_url: str | None = None
    download_factor: float | None = None
    #: 站点适配层已解析好的免费窗口信息（m-team 等 API 站点直接塞 status dict），
    #: 供 :meth:`SiteAdapter.parse_window` 用结构化字段精确识别（不污染 description）
    free_payload: dict[str, object] | None = None


@dataclass(slots=True)
class SiteProfile:
    """站点配置。"""

    name: str
    domain: str
    features: frozenset[SiteFeature] = frozenset()

    def detail_url(self, **kw: str) -> str | None:
        return None


class SiteAdapter(ABC):
    """站点统一接口（FR-1.5）。"""

    #: 适配器类型标识（站点配置 ``adapter`` 字段）
    adapter_type: str = "generic"

    def __init__(self, profile: SiteProfile) -> None:
        self.profile = profile

    @property
    def name(self) -> str:
        return self.profile.name

    @abstractmethod
    async def ping(self) -> SiteHealth:
        """校验域名可达与登录态。"""

    @abstractmethod
    async def fetch_feed(self, url: str) -> list[FeedItem]:
        """拉取并归一化 RSS。"""

    async def list_torrents(self, query: dict[str, object]) -> list[FeedItem]:
        """搜索/列表（免费种候选来源 P0，FR-3.1）。不支持时返回空。"""
        return []

    async def torrent_detail(self, item_id: str) -> dict[str, object]:
        """详情页/API 原始字段。不支持时返回空 dict。"""
        return {}

    async def fetch_infohash(self, item_id: str) -> str | None:
        """获取单一种子的 InfoHash（FR-1.5）。

        多数站点 RSS 自带 hash；m-team 等 API 站点 search 不返回 hash，
        需下载 torrent 文件解析。不支持时返回 None，由业务层按无 hash 处理。
        """
        return None

    @abstractmethod
    async def parse_window(
        self, raw: str | dict[str, object], *, source_hint: str | None = None
    ) -> FreeWindow:
        """**核心扩展点**（FR-1.5.3）。

        只解析不判定：识别 ``freeleech_permanent`` 必须给最高优先级（R10），
        永久免费返回 ``free_end_at=None``。``source_hint`` 用于在文案解析
        时消解 ``list_text`` / ``rss_desc`` 的歧义。
        """

    async def site_clock(self) -> datetime | None:
        """站点服务器时间，用于 ``clock_offset`` 探测（FR-3.0.2）。"""
        return None

    async def verify_status(self, info_hash: str) -> TorrentBrief:
        """站点是否已删种/违规。不支持时返回 UNKNOWN。"""
        return TorrentBrief.UNKNOWN

    @abstractmethod
    def capabilities(self) -> frozenset[SiteFeature]:
        """声明站点能力。"""

    async def clock_offset(self) -> ClockOffset:
        """探测时钟偏差。站点不提供时间时返回零偏移。"""
        site_time = await self.site_clock()
        if site_time is None:
            return ClockOffset.zero()
        from app.core.timeutils import utcnow

        return ClockOffset.from_probe(site_time, utcnow())

    async def close(self) -> None:
        """释放 HTTP 连接池。子类按需覆盖。"""