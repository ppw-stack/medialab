"""PT 站点适配层（FR-1.5）。

每个站点一个文件（mteam.py、ourbits.py、...），实现 5+1 个契约方法：
- check_reachability: 校验域名可达与登录态
- search_torrents: 种子搜索
- download_torrent: 下载 .torrent 文件字节
- check_status: 种子状态检查
- get_torrent_detail: 种子详情查询
- get_torrent_url: 实时返回 item 的可下载绝对 URL（push 时调用）
- get_user_snapshot: 上传/下载/做种/下载中（FR-1.6 精简版）
- search_subtitles: 字幕搜索（可选能力，默认不支持）

约束：适配层不写数据库、不操作下载器，保持可独立单测。
窗口解析（FreeWindow）由业务层 :mod:`app.services.window_parser` 负责。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import httpx
from bs4.element import Tag

from app.adapters.sites.user_data import SiteUserSnapshot


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
    """归一化的种子条目（search_torrents 返回值）。

    注：本类不带 ``torrent_url`` 字段——URL 的获取由
    :meth:`SiteAdapter.get_torrent_url` 在消费时（push / manual push）实时
    调用，避免"列表页刷时拼"与"详情页拼"的不一致。

    历史上有 ``link`` 字段（各 HTML 站填 ``details.php?id=<站内 ID>``），
    但**全仓零读取**——详情页 URL 由 :meth:`SiteAdapter.get_torrent_url`
    实时生成，``link`` 只是重复信息，已删除。需要详情页 URL 的地方
    调 ``get_torrent_url(item_id)``。
    """

    title: str
    item_id: str | None = None  # 站内 ID（用于 detail/download/status）
    info_hash: str | None = None
    size: int = 0
    category: str | None = None
    published_at: str | None = None
    description: str = ""
    download_factor: float | None = None
    free_payload: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SiteProfile:
    """站点配置。"""

    name: str
    domain: str


@dataclass(frozen=True, slots=True)
class SubtitleItem:
    """站点字幕搜索结果（``search_subtitles`` 返回值）。

    字段与规则包的 ``subtitles.fields`` 对应；缺失字段为 ``None``。
    """

    title: str
    #: 语言（站点页面声明的语言名，如「简体中文」）
    language: str | None = None
    #: 字幕文件下载地址（绝对 URL 或 magnet）
    download_url: str | None = None
    #: 字幕详情页
    page_url: str | None = None
    size: int = 0
    published_at: str | None = None
    uploader: str | None = None
    #: 举报页面（站点提供时）
    report_url: str | None = None
    #: 语言图标（站点提供时）
    language_icon: str | None = None
    #: 关联种子的站内 ID（从下载链接参数里提取，可能为空）
    torrent_id: str | None = None
    #: 站点字幕 ID
    subtitle_id: str | None = None
    #: 下载文件名的建议值
    file_name: str | None = None


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


class SiteFetchError(RuntimeError):
    """站点拉取失败（网络 / DNS / HTTP 错误 / 解析失败）。

    与"站点确实没有种子"区分开：后者是**正常的空结果**（返回 ``[]``），
    前者是**故障**——必须抛异常让上层统计成失败站。

    之前各 adapter 的 ``search_torrents`` 把 ``httpx.HTTPError`` 吞掉返回
    ``[]``，导致 DNS 挂掉 / 站点被墙时作业照常报"全部成功"，
    故障完全不可见（prod 的 hddolby 就是这样被掩盖的）。
    """


class SiteAdapter(ABC):
    """站点统一接口（FR-1.5）。

    5+1 个契约方法：
    1. check_reachability: 校验域名可达与登录态
    2. search_torrents: 种子搜索
    3. download_torrent: 下载 .torrent 字节
    4. check_status: 种子状态检查
    5. get_torrent_detail: 种子详情查询
    6. get_torrent_url: 实时返回 item 的可下载绝对 URL（push 时调用）
    """

    #: 适配器类型标识（站点配置 ``adapter`` 字段）
    adapter_type: str = "generic"
    #: HTTP 客户端由子类 __init__ 按站点 headers/cookies 创建
    _client: httpx.AsyncClient
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

        **拉取失败必须抛 :class:`SiteFetchError`**（或让原始
        ``httpx.HTTPError`` 向上传播），不能吞掉异常返回 ``[]``——
        返回空列表代表"站点确实没有种子"，与"站点挂了"是两回事，
        混用会让作业统计看不出故障站。
        """

    @abstractmethod
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent 原始字节。

        失败时抛出异常或返回 ``b""``。
        """

    @abstractmethod
    async def get_torrent_url(self, item_id: str) -> str | None:
        """6. 实时返回该 item 的可下载绝对 URL。

        语义：
        - 返回 ``None``：无法提供可消费的 URL（如鉴权失败、详情页不可达）。
        - 返回 ``http(s)://`` 绝对 URL：可直接被 qB/Tr 抓取。
        - 返回 ``magnet:?xt=urn:btih:...``：BTIH 链接。

        基类不提供默认实现——各 adapter 必须实现。push / manual push 链路
        在选种/推送时调此方法，URL 不再持久化到 ``torrent_index`` 表。
        """

    @abstractmethod
    async def check_status(self, item_id: str) -> TorrentBrief:
        """4. 种子状态检查（按站内 ID）。

        不暴露状态的站点应返回 ``TorrentBrief.UNKNOWN``。
        """

    @abstractmethod
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情查询。返回统一结构（含 download_url / free_payload / raw_html）。"""

    async def search_subtitles(
        self,
        query: dict[str, object],
        *,
        page_number: int = 1,
    ) -> list[SubtitleItem]:
        """字幕搜索（可选能力）。

        ``query`` 键：``keyword`` / ``imdbid`` 等，适配器按站点能力取用。
        默认返回 ``[]``：**该站没有字幕配置**（不是失败）。
        拉取/解析失败必须抛 :class:`SiteFetchError`——与种子搜索同一契约，
        不能把故障伪装成「没有字幕」。
        """
        return []

    # ----------------------- FR-1.6 站点用户数据契约（精简版） -----------------------

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """7. 拉取 4 业务字段（upload/download/seeding/leeching）+ 时魔。

        默认实现：返回 ``None``——**仅表示「该站不支持 / 无凭据」**
        （service 层记为 ``skipped``）。

        **失败（网络 / HTTP / 解析）必须抛 :class:`SiteFetchError`**，
        不能吞掉返回 ``None``：返回 ``None`` 代表「该站没有这个功能」，
        与「站点挂了」是两回事——混用会让作业统计报「全部成功」而实际
        漏掉故障站（参见 2026-10-21 prod 事故的教训）。

        解析规则用**真实页面**固化成 fixture（``tests/fixtures/*_mybonus.html``），
        不要手写最小 HTML——真实文案里常有意外（各站时魔叫法不同、
        进度条 class 带工具类、合计值在跨行单元格里）。
        """
        return None

    # ----------------------- FR-3.6 站点每日签到契约 -----------------------

    async def close(self) -> None:
        """释放 HTTP 连接池。子类按需覆盖。"""


# ---------------------------------------------------------------- 工具函数

#: ``"50.54 GB"`` / ``"1191.58 MB"`` → ``int`` 字节
#:
#: 数值部分允许千分位逗号：``"1,234.56 TB"``。NexusPHP 站点的
#: userdetails 页在数值 ≥ 1000 时会带逗号分隔，不支持会把这类值
#: 全部解析成 0（静默丢数据）。
_SIZE_RE = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)\s*$",
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
    # 去千分位逗号（"1,234.56" → "1234.56"），float() 不接受逗号
    value, unit = m.group(1).replace(",", ""), m.group(2).upper()
    try:
        return int(float(value) * _SIZE_FACTORS[unit])
    except (KeyError, ValueError):
        return 0


def parse_size_from_br(text: str | None) -> int:
    """把 ``"50.54<br />GB"`` 解析为字节整数（HTML 跨 tag 用）。

    比 :func:`parse_size_text` 更宽松：匹配 ``<br />`` / ``<br>`` 之间的数字与单位。

    数值部分支持千分位（``"1,234.56<br />GB"``）——与 :func:`parse_size_text`
    一致，否则 ≥1000 的体积会被截断成错值（"1,234.56" → "234.56"）。
    """
    if not text:
        return 0
    m = re.search(
        r"([\d,]+(?:\.\d+)?)\s*<br\s*/?>\s*(TB|GB|MB|KB)",
        text,
        re.IGNORECASE,
    )
    if m is None:
        return parse_size_text(text)
    value, unit = m.group(1).replace(",", ""), m.group(2).upper()
    try:
        return int(float(value) * _SIZE_FACTORS[unit])
    except (KeyError, ValueError):
        return 0


#: ``"2026-10-15 14:51:34"`` → 北京时间字符串
_ABS_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$")


def parse_abs_datetime(text: str | None) -> str | None:
    """把 ``"2026-10-15 14:51:34"`` 原样返回为北京时间字符串。

    站点时间本身就是北京时间（无时区标记），不再做时区转换。
    无法解析（含 ``None`` / 空串 / 格式异常）时返回 ``None``。

    格式合法但**日历上不存在**的时间（``"2026-13-45 99:99:99"``）也返回
    ``None``——正则只管形状，这类值进了 DB 会被当成真实时间展示。
    """
    if not text:
        return None
    raw = text.strip()
    m = _ABS_DATETIME_RE.match(raw)
    if m is None:
        return None
    # 日历合法性校验（月份 1-12、日 1-31、时 0-23、分秒 0-59）
    try:
        datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return raw


#: 发布时间单元格文本（纯相对时长）：``"5时14分"`` / ``"2天18时"`` / ``"1月0天"`` / ``"1分钟"``
#: 模式：``\d+`` + 单位字符 + 可重复 ``(\d+ + 单位字符)``
_REL_TIME_RE = re.compile(r"^\d+[年月日周天时分秒钟]+(?:\d+[年月日周天时分秒钟]+)*$")

#: 即时发布文案（无数字前缀）：``"刚刚"`` / ``"秒前"`` / ``"刚刚发布"``
_JUST_NOW_RE = re.compile(r"^刚刚(?:发布)?$|^秒前$")

#: 发布时间单元格文本（绝对日期）：``"2025-11-23"`` / ``"2025-11-23 12:39:58"``
_ABS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?$")


def parse_publish_time(row: Tag) -> str | None:
    """从 NexusPHP 列表 ``<tr>`` 提取发布时间。

    发布时间单元格形如::

        <td class="rowfollow nowrap"><span title="2026-10-12 14:51:34">5时14分</span></td>

    **注意**：同一 ``<tr>`` 的描述栏还可能是"优惠剩余时间"
    ``<span title="2026-10-15 14:51:34">2天18时</span>``——两者文本都是相对时长，
    只按 ``span[title]`` 顺序取会误把优惠结束时刻当成发布时间。
    这里用"文本为纯相对时长（如 ``5时14分``）的 ``<td>``"精确定位发布时间单元格。
    """
    for td in row.find_all("td"):
        text = td.get_text(" ", strip=True)
        norm = re.sub(r"\s+", "", text)
        # 情况 A：相对时长（"5时14分" / "1月0天" / "刚刚"）→ 从内层 span 的绝对 title 取值
        if (_REL_TIME_RE.match(norm) or _JUST_NOW_RE.match(norm)) and len(norm) <= 10:
            span = td.find("span", attrs={"title": True})
            if span is not None:
                parsed = parse_abs_datetime(str(span.get("title") or ""))
                if parsed is not None:
                    return parsed
        # 情况 B：老条目直接显示绝对日期（"2025-11-23" / "2025-11-23 12:39:58"）
        elif _ABS_DATE_RE.match(text):
            parsed = parse_abs_datetime(
                text if len(text) > 10 else f"{text} 00:00:00"
            )
            if parsed is not None:
                return parsed
    return None
