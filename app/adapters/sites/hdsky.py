"""HDSky 站点适配器（FR-1.5）。

hdsky.me 是 NexusPHP v1.5 Beta 5 架构的 PT 站点，基于 cookie 鉴权（无 JSON API）。
使用标准 NexusPHP 表格布局（``<table class="torrents">`` + ``<tr>`` 行），
LightBlue 内置主题。

核心端点（host = ``https://hdsky.me``）：

- ``GET /index.php``         测连/登录态校验（登录后页面含 userdetails.php 链接）
- ``GET /torrents.php``      种子列表页（HTML 表格），免费标记在 ``<img class="pro_free">``
  或 ``<font class="free">`` 中
- ``GET /details.php?id=xxx`` 详情页（HTML）
- ``GET /download.php?id=xxx`` 下载 .torrent 文件（需 cookie）

HDSky 特有：下载链接用 ``<form action="download.php?id=xxx">`` 而非 ``<a href>``。

NexusPHP 免费标记（HTML class → FreeType）：

- ``free`` / ``pro_free``（无过期）→ ``freeleech_permanent``
- ``free`` / ``pro_free``（有剩余时间）→ ``freeleech_timed``
- ``twoupfree`` / ``pro_free2up`` → ``twoup_timed``（2X 上传 + 免费）
- ``twoup`` / ``pro_2up`` → ``twoup_timed``（仅 2X 上传）
- ``halfdown`` / ``pro_50pctdown`` → ``neutral_leech``（50% 下载折扣）

cookie 模式：用户手动从浏览器获取 session cookies（``c_secure_uid`` / ``c_secure_pass`` 等），
加密存储在 ``sites.cookies_enc``，由 :class:`AdapterFactory.site` 解密注入。
不自动登录（AGENTS.md：不引入验证码绕过）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.adapters.sites._bencode import infohash_from_torrent
from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFeature,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
)
from app.core.logging import get_logger
from app.core.timeutils import ClockOffset
from app.core.windows import FreeWindow
from app.db.models import FreeSource, FreeType

logger = get_logger(__name__)

#: 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: NexusPHP 免费标记 class → FreeType 映射
_FREE_CLASS_MAP: dict[str, str] = {
    "free": FreeType.UNKNOWN,
    "twoupfree": FreeType.TWOU_TIMED,
    "twoup": FreeType.TWOU_TIMED,
    "halfdown": FreeType.NEUTRAL,
    "twouphalfdown": FreeType.NEUTRAL,
    "thirtypercent": FreeType.NEUTRAL,
    "nodead": FreeType.UNKNOWN,
    "highlight": FreeType.UNKNOWN,
}

#: ``<img class="pro_XXX">`` → 标准免费 class 映射
_PRO_IMG_MAP: dict[str, str] = {
    "pro_free": "free",
    "pro_free2up": "twoupfree",
    "pro_2up": "twoup",
    "pro_50pctdown": "halfdown",
    "pro_50pctdown2up": "twouphalfdown",
    "pro_30pctdown": "thirtypercent",
}

#: 永久免费标记（出现在 class 或文本中时视为 PERMANENT）
_PERMANENT_MARKERS = ("permanent", "forever", "lifetime", "永久免费")

#: "剩余 X天Y小时" 文案正则（支持简体/繁体/英文）
_REMAINING_RE = re.compile(
    r"(?:剩余|剩餘|剩|left|remaining)\s*"
    r"(?:(?P<d>\d+)\s*(?:天|日|d|days?)\s*)?"
    r"(?:(?P<h>\d+)\s*(?:小时|小時|時|时|h|hours?)\s*)?"
    r"(?:(?P<m>\d+)\s*(?:分|分鐘|m|min)\s*)?",
    re.IGNORECASE,
)

#: 种子标题锚点（HDSky 真实结构，嵌套表格）：
#: ``<a title="Full Title" href="details.php?id=10001&hit=1">``
#: 捕获整段属性 + 种子 ID；title 属性从属性段内二次提取
_ANCHOR_RE = re.compile(
    r'<a\b([^>]*href="details\.php\?id=(\d+)&hit=1"[^>]*)>',
    re.IGNORECASE,
)

#: 从锚点属性段提取 title
_ANCHOR_TITLE_RE = re.compile(r'title="([^"]+)"', re.IGNORECASE)

#: HDSky 的签名下载表单：
#: <form action="https://hdsky.me/download.php?id=10001&t=1791609025&sign=xxx" method="POST">
_SIGNED_FORM_RE = re.compile(
    r'<form[^>]*action=["\'](https?://[^"\']*download\.php\?[^"\']*&sign=[^"\']*)["\']',
    re.IGNORECASE,
)

#: 任意 download.php?id=NNN 引用（提取下载 ID）
_ANY_DOWNLOAD_RE = re.compile(
    r'download\.php\?id=(\d+)',
    re.IGNORECASE,
)

#: 从 ``<img class="pro_free">`` 提取促销 class（完整 class 名含 pro_ 前缀）
_PRO_IMG_RE = re.compile(
    r'<img[^>]*class=["\'](pro_\w+)["\']',
    re.IGNORECASE,
)

#: 从 ``<font class="free">`` 提取促销 class
_FREE_FONT_RE = re.compile(
    r'<font[^>]*class=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)

#: HDSky 优惠剩余时间（可见 span，绝对时刻在 title 属性）：
#: [优惠剩余时间：<b><span title="2026-10-11 00:22:47">11时12分</span></b>]
_PROMO_TIME_RE = re.compile(
    r'优惠剩余时间：<b><span title="([^"]+)"',
    re.IGNORECASE,
)

#: 文件大小提取（HDSky 用 ``30.76<br />GB`` 分隔数字与单位）
_SIZE_RE = re.compile(
    r'([\d.]+)\s*(?:<br\s*/?>\s*)?(TB|GB|MB|KB|TiB|GiB|MiB|KiB)',
    re.IGNORECASE,
)

#: 登录态检测：登录后页面含 userdetails.php 或 logout.php 链接
_LOGGED_IN_RE = re.compile(r'(userdetails\.php|logout\.php|takelogout\.php)', re.IGNORECASE)

#: 登录页重定向检测
_LOGIN_REDIRECT_RE = re.compile(r'login\.php', re.IGNORECASE)

#: 大小单位转换
_SIZE_FACTORS = {
    "tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024,
    "tib": 1024**4, "gib": 1024**3, "mib": 1024**2, "kib": 1024,
}


class HdskyAdapter(SiteAdapter):
    """HDSky (hdsky.me) NexusPHP cookie 鉴权适配器。"""

    adapter_type = "hdsky"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        cookies: str | None = None,
        apikey: str | None = None,
        timeout: float = 20.0,
        clock_offset: ClockOffset | None = None,
    ) -> None:
        super().__init__(profile)
        self._cookies = cookies
        self._apikey = apikey
        self._timeout = timeout
        self._clock_offset = clock_offset or ClockOffset.zero()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            cookies=self._parse_cookies(cookies),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _parse_cookies(raw: str | None) -> dict[str, str]:
        if not raw:
            return {}
        out: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _base_url(self) -> str:
        domain = (self.profile.domain or "").strip()
        if not domain:
            return ""
        domain = domain.replace("https://", "").replace("http://", "")
        return f"https://{domain}"

    def _detail_url(self, item_id: str) -> str:
        if self.profile.detail_path_template:
            path = self.profile.detail_path_template.format(id=item_id, hash=item_id)
        else:
            path = f"/details.php?id={item_id}"
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url()}{path}"

    # ------------------------------------------------------------ 免费窗口解析
    @staticmethod
    def _classify_from_html(
        free_classes: list[str],
        remaining_text: str | None = None,
    ) -> tuple[str, timedelta | None, float]:
        """从 NexusPHP HTML 免费标记 + 剩余时间文案提取免费窗口。

        返回 ``(free_type, remaining_delta, confidence)``。
        ``remaining_delta`` 是剩余时长（timedelta），由 ``parse_window`` 通过
        ``self._clock_offset`` 转换为绝对时刻。无剩余时间时为 ``None``。

        优先级：
        1. 永久免费标记（permanent/forever/永久免费）→ PERMANENT
        2. free class + 剩余时间 → TIMED
        3. free class 无剩余时间 → PERMANENT（NexusPHP 无过期时间的 free 通常为永久）
        4. twoupfree/twoup → TWOU_TIMED
        5. halfdown → NEUTRAL
        6. 无标记 → UNKNOWN
        """
        combined = " ".join(free_classes).lower() + " " + (remaining_text or "").lower()

        # 1) 永久免费标记
        if any(m in combined for m in _PERMANENT_MARKERS):
            return FreeType.PERMANENT, None, 1.0

        # 2-3) free class
        has_free = any(c.lower() == "free" for c in free_classes)
        if has_free:
            delta = HdskyAdapter._parse_remaining_delta(remaining_text)
            if delta is not None:
                return FreeType.TIMED, delta, 0.95
            return FreeType.PERMANENT, None, 0.85

        # 4-5) 其他促销 class
        for cls in free_classes:
            cls_lower = cls.lower()
            mapped = _FREE_CLASS_MAP.get(cls_lower)
            if mapped and mapped != FreeType.UNKNOWN:
                delta = HdskyAdapter._parse_remaining_delta(remaining_text)
                if delta is not None and mapped != FreeType.NEUTRAL:
                    return mapped, delta, 0.9
                return mapped, None, 0.85

        return FreeType.UNKNOWN, None, 0.0

    @staticmethod
    def _parse_remaining_delta(text: str | None) -> timedelta | None:
        """从"剩余 X天Y小时"文案解析为 timedelta。

        支持简体/繁体/英文，返回 timedelta 或 None（无法解析时）。
        """
        if not text:
            return None
        m = _REMAINING_RE.search(text)
        if not m:
            return None
        days = int(m.group("d") or 0)
        hours = int(m.group("h") or 0)
        minutes = int(m.group("m") or 0)
        delta = timedelta(days=days, hours=hours, minutes=minutes)
        if delta.total_seconds() <= 0:
            return None
        return delta

    @staticmethod
    def _parse_abs_datetime(text: str) -> datetime | None:
        """从 "2026-10-11 11:27:07" 格式解析为 naive UTC datetime。"""
        if not text:
            return None
        try:
            dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.strip())
            except ValueError:
                return None
        return dt.replace(tzinfo=None)

    # ------------------------------------------------------------ 接口
    async def ping(self) -> SiteHealth:
        """测连：GET 首页，检查登录态。"""
        if not self._cookies:
            return SiteHealth(reachable=False, detail="缺少 cookies（cookie 鉴权必需）")
        try:
            resp = await self._client.get(
                f"{self._base_url()}/index.php",
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return SiteHealth(reachable=False, detail=str(exc))
        if resp.status_code >= 500:
            return SiteHealth(reachable=False, detail=f"HTTP {resp.status_code}")
        text = resp.text
        if _LOGGED_IN_RE.search(text):
            return SiteHealth(reachable=True, logged_in=True, detail="ok")
        if _LOGIN_REDIRECT_RE.search(text) or "login.php" in resp.url.path:
            return SiteHealth(reachable=True, logged_in=False, detail="cookie 已过期，需重新获取")
        return SiteHealth(reachable=True, logged_in=False, detail="无法确认登录态")

    async def fetch_feed(
        self,
        url: str,
        *,
        page_size: int = 100,
        page_number: int = 1,
        categories: list[str] | None = None,
    ) -> list[FeedItem]:
        """拉取种子列表。

        cookie 模式：忽略传入的 url（RSS），直接 GET ``torrents.php`` HTML 列表页。
        也尝试 RSS（``torrentrss.php``）——如果 url 是有效的 RSS 地址且有 passkey，
        优先走 RSS 获取种子列表，免费标记再从 HTML 补充。
        """
        if url and ("rss" in url.lower() or "torrentrss" in url.lower()):
            rss_items = await self._fetch_via_rss(url)
            if rss_items:
                return rss_items

        return await self._fetch_via_html(page_size=page_size)

    async def _fetch_via_html(self, *, page_size: int = 100) -> list[FeedItem]:
        """GET ``torrents.php`` HTML 列表页，正则解析种子行。"""
        if not self._cookies:
            logger.warning("hdsky %s 无 cookies，无法拉取 HTML 列表", self.name)
            return []
        try:
            resp = await self._client.get(
                f"{self._base_url()}/torrents.php",
                params={"inclbookmarked": "0", "incldead": "1"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("hdsky %s HTML 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code in (403, 429):
            raise _BreakerTripped(f"hdsky {self.name} 返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            logger.warning("hdsky %s HTML 返回 %s", self.name, resp.status_code)
            return []
        if _LOGIN_REDIRECT_RE.search(resp.text[:2000]):
            logger.warning("hdsky %s cookie 已过期，重定向到 login", self.name)
            return []
        return self._parse_torrents_html(resp.text, limit=page_size)

    @staticmethod
    def _parse_torrents_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """正则解析 HDSky 真实 torrents.php HTML。

        HDSky 使用嵌套表格，单个种子跨多个 ``<tr>``（无法按行切分）。
        解析策略：以标题锚点为界，把列表切成一串"种子窗口"：

        - 每个种子窗口从标题锚点开始，到下一个标题锚点结束
        - 窗口内含：标题（title 属性）、``<img class="pro_free">``、
          优惠剩余时间（``优惠剩余时间：<b><span title="绝对时刻">``）、
          签名下载表单（``<form action="download.php?id=...&t=...&sign=...">``）、
          大小（位于窗口末尾的 ``<td class="rowfollow">30.76<br />GB</td>``）

        标题锚点形如：
        ``<a target="_blank" title="Full Title" href="details.php?id=10001&hit=1">``
        """
        items: list[FeedItem] = []
        anchors = list(_ANCHOR_RE.finditer(html))
        for i, m in enumerate(anchors):
            attrs = m.group(1)
            torrent_id = m.group(2)
            title_m = _ANCHOR_TITLE_RE.search(attrs)
            title = title_m.group(1).strip() if title_m else f"torrent-{torrent_id}"

            # 种子窗口：本锚点 → 下一锚点
            start = m.start()
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
            window = html[start:end]

            # 下载链接 ID（签名表单或普通引用）
            dl_m = _ANY_DOWNLOAD_RE.search(window)
            dl_id = dl_m.group(1) if dl_m else torrent_id

            # 大小（窗口末尾的 rowfollow 单元格）
            size_m = _SIZE_RE.search(window)
            size = 0
            if size_m:
                size = HdskyAdapter._parse_size_str(
                    f"{size_m.group(1)} {size_m.group(2)}"
                )

            # 免费标记：img.pro_XXX → 标准 class；兼容 <font class="free">
            pro_classes = [mm.group(1) for mm in _PRO_IMG_RE.finditer(window)]
            mapped_pro = [_PRO_IMG_MAP.get(c.lower(), c) for c in pro_classes]
            font_classes = [mm.group(1) for mm in _FREE_FONT_RE.finditer(window)]
            free_classes = mapped_pro + font_classes

            # 优惠剩余时间（绝对时刻在可见 span 的 title 属性中）
            time_m = _PROMO_TIME_RE.search(window)
            remaining_abs = time_m.group(1) if time_m else None

            # 剩余时长文案（兜底）
            remaining_m = _REMAINING_RE.search(window)
            remaining_text = remaining_m.group(0) if remaining_m else None

            free_payload: dict[str, object] | None = None
            if free_classes or remaining_abs or remaining_text:
                free_payload = {
                    "free_classes": free_classes,
                    "remaining_text": remaining_text,
                    "remaining_abs": remaining_abs,
                }

            detail_url = f"details.php?id={torrent_id}"
            download_url = f"download.php?id={dl_id}"

            items.append(
                FeedItem(
                    title=title,
                    link=detail_url,
                    info_hash=None,
                    size=size,
                    category=None,
                    description="",
                    published_at=None,
                    torrent_url=download_url,
                    free_payload=free_payload,
                )
            )
            if len(items) >= limit:
                break
        return items[:limit]

    async def _fetch_via_rss(self, url: str) -> list[FeedItem]:
        """RSS 拉取（如果站点 RSS URL 可用）。"""
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("hdsky %s RSS 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code in (403, 429):
            raise _BreakerTripped(f"hdsky {self.name} RSS 返回 {resp.status_code}")
        if resp.status_code >= 400:
            return []
        return self._parse_rss(resp.text)

    @staticmethod
    def _parse_rss(xml_text: str) -> list[FeedItem]:
        """解析 NexusPHP RSS XML。"""
        try:
            root = ET.fromstring(xml_text.encode("utf-8"))
        except ET.ParseError as exc:
            logger.warning("hdsky RSS XML 解析失败：%s", exc)
            return []
        items: list[FeedItem] = []
        for node in root.iter("item"):
            fields: dict[str, str] = {}
            for c in node:
                local = c.tag.rsplit("}", 1)[-1].lower()
                val = (c.get("href") or c.get("url") or c.text or "").strip()
                if local not in fields:
                    fields[local] = val
            link = fields.get("link") or fields.get("guid") or ""
            if not link:
                continue
            id_match = re.search(r'details\.php\?id=(\d+)', link, re.IGNORECASE)
            torrent_id = id_match.group(1) if id_match else ""
            if not torrent_id:
                dl_match = _ANY_DOWNLOAD_RE.search(link)
                torrent_id = dl_match.group(1) if dl_match else ""

            desc = fields.get("description", "")[:20000]
            cat_node = node.find("category")
            enc_node = node.find("enclosure")
            enc_len = (
                int(enc_node.get("length") or 0)
                if enc_node is not None and enc_node.get("length")
                else 0
            )
            enc_url = (
                enc_node.get("url")
                if enc_node is not None and enc_node.get("url")
                else link
            )
            cat_text = (
                cat_node.text if cat_node is not None and cat_node.text else None
            )
            items.append(
                FeedItem(
                    title=fields.get("title", ""),
                    link=link,
                    info_hash=None,
                    size=enc_len,
                    category=cat_text,
                    description=desc,
                    published_at=None,
                    torrent_url=enc_url,
                    free_payload=None,
                )
            )
            if len(items) >= _MAX_ITEMS_PER_PULL:
                break
        return items[:_MAX_ITEMS_PER_PULL]

    # ------------------------------------------------------------ 解析
    async def parse_window(
        self,
        raw: str | dict[str, object],
        *,
        source_hint: str | None = None,
    ) -> FreeWindow:
        """FR-1.5.3：解析单条目的免费窗口。

        dict 路径（HTML scrape 结果）：从 free_classes + remaining_text + remaining_abs 提取。
        str 路径（HTML 详情页或 RSS 描述）：保留"剩余 X"文案兜底。
        """
        if isinstance(raw, dict):
            free_classes_raw = raw.get("free_classes")
            free_classes: list[str] = []
            if isinstance(free_classes_raw, list):
                free_classes = [str(c) for c in free_classes_raw]
            elif isinstance(free_classes_raw, str):
                free_classes = [free_classes_raw]
            remaining_raw = raw.get("remaining_text")
            remaining_text: str | None = (
                remaining_raw if isinstance(remaining_raw, str) else None
            )
            remaining_abs_raw = raw.get("remaining_abs")
            remaining_abs: str | None = (
                remaining_abs_raw if isinstance(remaining_abs_raw, str) else None
            )

            free_type, remaining_delta, confidence = HdskyAdapter._classify_from_html(
                free_classes, remaining_text,
            )

            # 优先使用绝对截止时刻，其次用剩余时长 + clock_offset 换算
            end_at: datetime | None = None
            if remaining_abs:
                end_at = HdskyAdapter._parse_abs_datetime(remaining_abs)
            if end_at is None and remaining_delta is not None:
                end_at = self._clock_offset.remaining_to_absolute(remaining_delta)

            # 有绝对截止时刻 → 限时类型
            if end_at is not None and free_type in (
                FreeType.PERMANENT,
                FreeType.TIMED,
                FreeType.UNKNOWN,
            ):
                return FreeWindow(
                    free_type=FreeType.TIMED,
                    free_end_at=end_at,
                    free_source=FreeSource.API,
                    raw_text=_stringify_safe(raw),
                    confidence=0.95,
                )

            if free_type == FreeType.PERMANENT:
                return FreeWindow(
                    free_type=FreeType.PERMANENT,
                    free_end_at=None,
                    free_source=FreeSource.API,
                    raw_text=_stringify_safe(raw),
                    confidence=confidence,
                )
            if end_at is not None:
                return FreeWindow(
                    free_type=free_type,
                    free_end_at=end_at,
                    free_source=FreeSource.API,
                    raw_text=_stringify_safe(raw),
                    confidence=confidence,
                )
            if (
                free_type != FreeType.UNKNOWN
                and free_type in (FreeType.TWOU_TIMED, FreeType.NEUTRAL)
            ):
                return FreeWindow(
                    free_type=free_type,
                    free_end_at=None,
                    free_source=FreeSource.API,
                    raw_text=_stringify_safe(raw),
                    confidence=confidence,
                )
            return FreeWindow(
                free_type=FreeType.UNKNOWN,
                free_end_at=None,
                free_source=FreeSource.SITE_DEFAULT,
                raw_text=_stringify_safe(raw),
                confidence=confidence,
            )

        # str 路径（兼容 HTML 详情页或 RSS description）
        text = raw or ""
        lowered = text.lower()
        if any(m in lowered for m in _PERMANENT_MARKERS):
            return FreeWindow(
                free_type=FreeType.PERMANENT,
                free_end_at=None,
                free_source=FreeSource.LIST_TEXT,
                raw_text=text,
                confidence=0.9,
            )
        m = _REMAINING_RE.search(text)
        if m:
            days = int(m.group("d") or 0)
            hours = int(m.group("h") or 0)
            minutes = int(m.group("m") or 0)
            delta = timedelta(days=days, hours=hours, minutes=minutes)
            if delta.total_seconds() > 0:
                end = self._clock_offset.remaining_to_absolute(delta)
                return FreeWindow(
                    free_type=FreeType.TIMED,
                    free_end_at=end,
                    free_source=FreeSource.LIST_TEXT,
                    raw_text=text,
                    confidence=0.8,
                )
        return FreeWindow(
            free_type=FreeType.UNKNOWN,
            free_end_at=None,
            free_source=FreeSource.SITE_DEFAULT,
            raw_text=text,
            confidence=0.0,
        )

    # ------------------------------------------------------------ 详情与下载
    async def torrent_detail(self, item_id: str) -> dict[str, object]:
        """拉取详情：GET ``details.php?id={id}``，返回 HTML 文本。"""
        url = self._detail_url(item_id)
        if not url:
            return {}
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError:
            return {}
        if resp.status_code >= 400:
            return {}
        return {"id": item_id, "_html": resp.text[:20000]}

    async def fetch_infohash(self, item_id: str) -> str | None:
        """下载 .torrent 文件解析 infohash。

        HDSky 的 ``download.php?id=N`` 直接 GET 返回 404——必须先访问详情页
        （``details.php?id=N``）拿到带 ``t``/``sign`` 的签名下载表单，再 POST 该
        URL 才会返回 torrent（FR-1.5）。

        流程：
        1. GET ``details.php?id=N``
        2. 从 ``<form action="download.php?id=N&t=...&sign=...">`` 提取签名 URL
        3. POST 该 URL → torrent bytes
        4. bencode 解析 info dict 取 SHA1 infohash
        """
        if not self._cookies:
            return None
        detail_url = f"{self._base_url()}/details.php?id={item_id}"
        try:
            resp = await self._client.get(detail_url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("hdsky %s 拉取详情页失败：%s", self.name, exc)
            return None
        if resp.status_code >= 400:
            return None
        form_m = _SIGNED_FORM_RE.search(resp.text)
        if not form_m:
            logger.debug("hdsky %s 详情页无签名下载表单（id=%s）", self.name, item_id)
            return None
        dl_url = form_m.group(1).replace("&amp;", "&")
        try:
            dl_resp = await self._client.post(dl_url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("hdsky %s 下载 torrent 失败：%s", self.name, exc)
            return None
        if dl_resp.status_code >= 400:
            return None
        content = dl_resp.content
        if not content.startswith(b"d"):
            return None
        return infohash_from_torrent(content)

    async def verify_status(self, info_hash: str) -> TorrentBrief:
        """NexusPHP 不暴露删除/违规状态 → UNKNOWN。"""
        return TorrentBrief.UNKNOWN

    async def site_clock(self) -> datetime | None:
        """探测站点服务器时间（FR-3.0.2）。"""
        try:
            resp = await self._client.head(
                f"{self._base_url()}/index.php", timeout=self._timeout
            )
        except httpx.HTTPError:
            return None
        from email.utils import parsedate_to_datetime
        raw = resp.headers.get("date")
        if not raw:
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed

    def capabilities(self) -> frozenset[SiteFeature]:
        return frozenset(
            {
                SiteFeature.RSS,
                SiteFeature.CLOCK_API,
                SiteFeature.TIMESTAMP_FIELD,
            }
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_size_str(text: str) -> int:
        """大小字符串 → 字节数（如 "56.64 GB" → 60763234091）。"""
        m = re.match(r"([\d.]+)\s*(\w+)", text.strip())
        if not m:
            return 0
        try:
            num = float(m.group(1))
        except ValueError:
            return 0
        factor = _SIZE_FACTORS.get(m.group(2).lower(), 1)
        return int(num * factor)


class _BreakerTripped(RuntimeError):
    """hdsky 站点熔断（FR-2.2：429/403 立即熔断）。"""


def _stringify_safe(raw: dict[str, Any]) -> str:
    """把 dict 压成可读字符串，便于 FreeWindow.raw_text 调试。"""
    parts: list[str] = []
    for k in ("id", "title", "free_classes", "remaining_text", "remaining_abs"):
        if k in raw and raw[k] is not None:
            parts.append(f"{k}={raw[k]}")
    return " ".join(parts)[:2000]


__all__ = ["HdskyAdapter", "_BreakerTripped"]
