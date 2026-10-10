"""PT 站点适配层（FR-1.5）。

每个站点一个文件（mteam.py、ourbits.py、...），实现 5 个契约方法：
- check_reachability: 校验域名可达与登录态
- search_torrents: 种子搜索
- download_torrent: 下载 .torrent 文件字节
- check_status: 种子状态检查
- get_torrent_detail: 种子详情查询

约束：适配层不写数据库、不操作下载器，保持可独立单测。
窗口解析（FreeWindow）由业务层 :mod:`app.services.window_parser` 负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TorrentBrief(StrEnum):
    """种子在站点的状态。"""

    EXISTS = "exists"
    DELETED = "deleted"
    VIOLATION = "violation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SiteHealth:
    """连通性结果（check_reachability 返回值）。"""

    reachable: bool
    logged_in: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FeedItem:
    """归一化的种子条目（search_torrents 返回值）。"""

    title: str
    link: str
    item_id: str | None = None  # 站内 ID（用于 detail/download/status）
    info_hash: str | None = None
    size: int = 0
    category: str | None = None
    published_at: datetime | None = None
    description: str = ""
    torrent_url: str | None = None
    download_factor: float | None = None
    free_payload: dict[str, object] | None = None


@dataclass(slots=True)
class SiteProfile:
    """站点配置。"""

    name: str
    domain: str


class SiteAdapter(ABC):
    """站点统一接口（FR-1.5）。

    5 个契约方法：
    1. check_reachability: 校验域名可达与登录态
    2. search_torrents: 种子搜索
    3. download_torrent: 下载 .torrent 字节
    4. check_status: 种子状态检查
    5. get_torrent_detail: 种子详情查询
    """

    #: 适配器类型标识（站点配置 ``adapter`` 字段）
    adapter_type: str = "generic"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        cookies: str | None = None,
        apikey: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.profile = profile
        self._cookies = cookies
        self._apikey = apikey
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self.profile.name

    @abstractmethod
    async def check_reachability(self) -> SiteHealth:
        """1. 校验域名可达与登录态。"""

    @abstractmethod
    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 100,
        page_number: int = 1,
    ) -> list[FeedItem]:
        """2. 种子搜索。

        ``query`` 为可空字典，键如 keyword/categories/free_only 等。
        不支持搜索的站点应返回 ``[]``。
        """

    @abstractmethod
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent 原始字节。

        失败时抛出异常或返回 ``b""``。
        """

    @abstractmethod
    async def check_status(self, item_id: str) -> TorrentBrief:
        """4. 种子状态检查（按站内 ID）。

        不暴露状态的站点应返回 ``TorrentBrief.UNKNOWN``。
        """

    @abstractmethod
    async def get_torrent_detail(self, item_id: str) -> dict[str, object]:
        """5. 种子详情查询。返回原始 dict（含 download_url / status 等）。"""

    async def close(self) -> None:
        """释放 HTTP 连接池。子类按需覆盖。"""
