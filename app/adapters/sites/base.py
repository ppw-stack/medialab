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

import re
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


@dataclass(frozen=True, slots=True)
class SiteProfile:
    """站点配置。"""

    name: str
    domain: str


@dataclass(frozen=True, slots=True)
class TorrentDetail:
    """种子详情（get_torrent_detail 返回值）。

    所有站点统一返回此结构，业务层不需要猜 dict 键。
    """

    item_id: str
    title: str | None = None
    size: int = 0
    download_url: str | None = None
    free_payload: dict[str, object] | None = None
    raw_html: str | None = None
    error: str | None = None


class BreakerTripped(RuntimeError):
    """站点熔断（FR-2.2：429/403 立即熔断 1 小时）。

    统一异常类：适配层抛出，由 :mod:`app.services.feed_fetcher` 捕获后
    写入 ``feeds.breaker_until``。各 adapter 不再自定义同名类。
    """


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

    async def download_torrent_url(self, url: str) -> bytes:
        """下载已签名的 .torrent URL（绕过裸 /download.php 的 404）。

        默认实现走内部 ``_client``；子类可覆盖以注入站点特定的 headers
        （referer / cookies）。失败时抛出异常。
        """

        resp = await self._client.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.content

    @abstractmethod
    async def check_status(self, item_id: str) -> TorrentBrief:
        """4. 种子状态检查（按站内 ID）。

        不暴露状态的站点应返回 ``TorrentBrief.UNKNOWN``。
        """

    @abstractmethod
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情查询。返回统一结构（含 download_url / free_payload / raw_html）。"""

    async def close(self) -> None:
        """释放 HTTP 连接池。子类按需覆盖。"""


# ---------------------------------------------------------------- 工具函数

#: ``"50.54 GB"`` / ``"1191.58 MB"`` → ``int`` 字节
_SIZE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)\s*$",
    re.IGNORECASE,
)
_SIZE_FACTORS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
}


def parse_size_text(text: str | None) -> int:
    """把 ``"50.54 GB"`` / ``"32.23 GB"`` / ``"1.89 MB"`` 解析为字节整数。

    无法解析（含 ``None`` / 空串 / 单位异常）时返回 0。适配层 parser 用于
    从 ``torrents.php`` 列表页 td / div 文本中提取 size。
    """
    if not text:
        return 0
    m = _SIZE_RE.match(text.strip())
    if m is None:
        return 0
    value, unit = m.group(1), m.group(2).upper()
    try:
        return int(float(value) * _SIZE_FACTORS[unit])
    except (KeyError, ValueError):
        return 0


def parse_size_from_br(text: str | None) -> int:
    """把 ``"50.54<br />GB"`` 解析为字节整数（HTML 跨 tag 用）。

    比 :func:`parse_size_text` 更宽松：匹配 ``<br />`` / ``<br>`` 之间的数字与单位。
    """
    if not text:
        return 0
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*<br\s*/?>\s*(TB|GB|MB|KB)",
        text,
        re.IGNORECASE,
    )
    if m is None:
        return parse_size_text(text)
    value, unit = m.group(1), m.group(2).upper()
    try:
        return int(float(value) * _SIZE_FACTORS[unit])
    except (KeyError, ValueError):
        return 0


#: ``"2026-10-15 14:51:34"`` → :class:`datetime` (naive UTC)
_ABS_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$")


def bs4_attr(value: object) -> str:
    """把 BS4 ``Tag.get()`` 返值（``str | list[str] | None``）归一为 ``str``。

    BS4 属性值可能是 list（class 等）或 None，统一 cast 为字符串便于 regex 处理。
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


def parse_abs_datetime(text: str | None) -> datetime | None:
    """把 ``"2026-10-15 14:51:34"`` 解析为 naive UTC datetime。

    输入为站点本地时间（Asia/Shanghai），业务层 timestamp 用作历史排序。
    无法解析（含 ``None`` / 空串 / 格式异常）时返回 ``None``。
    """
    if not text:
        return None
    m = _ABS_DATETIME_RE.match(text.strip())
    if m is None:
        return None
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
