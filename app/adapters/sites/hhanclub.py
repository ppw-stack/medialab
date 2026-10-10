"""HHanClub 站点适配器（FR-1.5）。

hhanclub.net 是 NexusPHP v1.8.6 架构的 PT 站点，基于 cookie 鉴权（无 x-api-key JSON API）。

核心端点（host = ``https://hhanclub.net``）：

- ``GET /index.php``         测连/登录态校验（登录后页面含 userdetails.php 链接）
- ``GET /torrents.php``      种子列表页（HTML），免费标记在 ``<font class="free">`` 等 class 中
- ``GET /details.php?id=xxx`` 详情页（HTML）
- ``GET /download.php?id=xxx`` 下载 .torrent 文件（需 cookie）

NexusPHP 免费标记（HTML class → FreeType）：

- ``free``（无过期）→ ``freeleech_permanent``
- ``free``（有剩余时间）→ ``freeleech_timed``
- ``twoupfree`` → ``twoup_timed``（2X 上传 + 免费）
- ``twoup`` → ``twoup_timed``（仅 2X 上传）
- ``halfdown`` → ``neutral_leech``（50% 下载折扣）

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

#: NexusPHP 免费标记 class → FreeType 映射（旧版 NexusPHP 兼容）
_FREE_CLASS_MAP: dict[str, str] = {
    "free": FreeType.UNKNOWN,  # 需配合剩余时间判断
    "twoupfree": FreeType.TWOU_TIMED,
    "twoup": FreeType.TWOU_TIMED,
    "halfdown": FreeType.NEUTRAL,
    "nodead": FreeType.UNKNOWN,
    "highlight": FreeType.UNKNOWN,
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

#: HHan 主题种子卡片分隔符（每个种子卡片以 torrent-table-for-spider 开头）
_CARD_SPLIT_RE = re.compile(r'torrent-table-for-spider', re.IGNORECASE)

#: 从种子名链接提取 ID + 标题：
#: <a href='details.php?id=220825&hit=1' class='... torrent-info-text-name'>Title</a>
_NAME_RE = re.compile(
    r"details\.php\?id=(\d+)[^>]*?torrent-info-text-name'[^>]*>([^<]+)",
    re.IGNORECASE,
)

#: 从下载链接提取 ID + passkey：
#: <a href="download.php?id=220825&passkey=xxx">下载</a>
_DOWNLOAD_RE = re.compile(
    r'download\.php\?id=(\d+)&passkey=([a-f0-9]+)',
    re.IGNORECASE,
)

#: 从大小单元格提取文件大小：
#: <div class="torrent-info-text torrent-info-text-size">56.64 GB</div>
_SIZE_RE = re.compile(
    r'torrent-info-text-size[^>]*>\s*([\d.]+\s*\w+)\s*<',
    re.IGNORECASE,
)

#: HHan 主题免费标记提取：
#: <span class="promotion-tag promotion-tag-free">免费</span>
_PROMO_TAG_RE = re.compile(
    r'promotion-tag\s+promotion-tag-(\w+)',
    re.IGNORECASE,
)

#: HHan 主题剩余时间绝对时刻提取：
#: 剩余时间：<span title="2026-10-11 11:27:07">23时2分钟</span>
_REMAINING_TIME_RE = re.compile(
    r'剩余时间：<span title="([^"]+)"',
    re.IGNORECASE,
)

#: 登录态检测：登录后页面含 userdetails.php 或 logout.php 链接
_LOGGED_IN_RE = re.compile(r'(userdetails\.php|logout\.php|takelogout\.php)', re.IGNORECASE)

#: 登录页重定向检测
_LOGIN_REDIRECT_RE = re.compile(r'login\.php', re.IGNORECASE)

#: 大小单位转换
_SIZE_FACTORS = {
    "tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024,
}

#: HHan 主题促销 class → FreeType 映射
_PROMO_CLASS_MAP: dict[str, str] = {
    "free": FreeType.UNKNOWN,  # 需配合剩余时间判断
    "2xfree": FreeType.TWOU_TIMED,
    "50": FreeType.NEUTRAL,  # 50% 下载折扣
    "30": FreeType.NEUTRAL,  # 30% 下载折扣
}


class HhanclubAdapter(SiteAdapter):
    """HHanClub (hhanclub.net) NexusPHP cookie 鉴权适配器。"""

    adapter_type = "hhanclub"

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
            delta = HhanclubAdapter._parse_remaining_delta(remaining_text)
            if delta is not None:
                return FreeType.TIMED, delta, 0.95
            # 无剩余时间文案的 free → 视为永久（NexusPHP 默认行为）
            return FreeType.PERMANENT, None, 0.85

        # 4-5) 其他促销 class
        for cls in free_classes:
            cls_lower = cls.lower()
            mapped = _FREE_CLASS_MAP.get(cls_lower)
            if mapped and mapped != FreeType.UNKNOWN:
                delta = HhanclubAdapter._parse_remaining_delta(remaining_text)
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
        """从 "2026-10-11 11:27:07" 格式解析为 naive UTC datetime。

        HHan 主题剩余时间的 title 属性使用站点本地时间（无时区），
        按站点配置时区转换为 UTC。
        """
        if not text:
            return None
        try:
            dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.strip())
            except ValueError:
                return None
        # HHan 主题时间无时区标记，按 UTC 处理（ClockOffset 校正偏差）
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
        优先走 RSS 获取种子列表（infohash 更可靠），免费标记再从 HTML 补充。
        """
        # 优先尝试 RSS（如果传入了有效的 RSS URL）
        if url and ("rss" in url.lower() or "torrentrss" in url.lower()):
            rss_items = await self._fetch_via_rss(url)
            if rss_items:
                # RSS 成功，但免费信息需从 HTML 补充
                # 逐条从 HTML 补充免费标记（惰性，避免大量请求）
                return rss_items

        # cookie 模式：直接 scrape HTML 列表页
        return await self._fetch_via_html(page_size=page_size)

    async def _fetch_via_html(self, *, page_size: int = 100) -> list[FeedItem]:
        """GET ``torrents.php`` HTML 列表页，正则解析种子行。"""
        if not self._cookies:
            logger.warning("hhanclub %s 无 cookies，无法拉取 HTML 列表", self.name)
            return []
        try:
            resp = await self._client.get(
                f"{self._base_url()}/torrents.php",
                params={"inclbookmarked": "0", "incldead": "1"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("hhanclub %s HTML 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code in (403, 429):
            raise _BreakerTripped(f"hhanclub {self.name} 返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            logger.warning("hhanclub %s HTML 返回 %s", self.name, resp.status_code)
            return []
        if _LOGIN_REDIRECT_RE.search(resp.text[:2000]):
            logger.warning("hhanclub %s cookie 已过期，重定向到 login", self.name)
            return []
        return self._parse_torrents_html(resp.text, limit=page_size)

    @staticmethod
    def _parse_torrents_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """正则解析 HHan 主题 torrents.php HTML。

        HHan 主题使用 div 布局（非传统表格），每个种子卡片以
        ``torrent-table-for-spider`` class 标识。卡片内含：

        - ``details.php?id=xxx`` + ``torrent-info-text-name`` → 种子 ID + 标题
        - ``torrent-info-text-size`` → 文件大小（如 "56.64 GB"）
        - ``download.php?id=xxx&passkey=xxx`` → 下载链接
        - ``promotion-tag promotion-tag-XXX`` → 免费标记（free/2xfree/50/30）
        - ``剩余时间：<span title="2026-10-11 11:27:07">23时2分钟</span>``
          → 绝对截止时刻（title 属性）+ 剩余时长文案
        """
        items: list[FeedItem] = []
        # 按 torrent-table-for-spider 分割为卡片
        cards = _CARD_SPLIT_RE.split(html)
        for card in cards[1:]:  # 第一段是页面头部
            # 提取种子 ID + 标题
            name_m = _NAME_RE.search(card)
            if not name_m:
                continue
            torrent_id = name_m.group(1)
            title = name_m.group(2).strip()

            # 提取下载链接 ID
            dl_m = _DOWNLOAD_RE.search(card)
            dl_id = dl_m.group(1) if dl_m else torrent_id

            # 提取大小
            size_m = _SIZE_RE.search(card)
            size = 0
            if size_m:
                size = HhanclubAdapter._parse_size_str(size_m.group(1))

            # 提取免费标记 class（HHan 主题 promotion-tag-XXX）
            promo_classes = [m.group(1) for m in _PROMO_TAG_RE.finditer(card)]
            # 兼容旧版 NexusPHP <font class="free">
            font_classes = [
                m.group(1)
                for m in re.finditer(
                    r'<font[^>]*class=["\']([^"\']+)["\'][^>]*>',
                    card, re.IGNORECASE,
                )
            ]
            free_classes = promo_classes + font_classes

            # 提取剩余时间（优先用 title 中的绝对时刻）
            time_m = _REMAINING_TIME_RE.search(card)
            remaining_abs = time_m.group(1) if time_m else None

            # 提取剩余时长文案（作为兜底）
            remaining_m = _REMAINING_RE.search(card)
            remaining_text = remaining_m.group(0) if remaining_m else None

            # 组装 free_payload（供 parse_window 用结构化字段精确识别）
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
            logger.warning("hhanclub %s RSS 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code in (403, 429):
            raise _BreakerTripped(f"hhanclub {self.name} RSS 返回 {resp.status_code}")
        if resp.status_code >= 400:
            return []
        return self._parse_rss(resp.text)

    @staticmethod
    def _parse_rss(xml_text: str) -> list[FeedItem]:
        """解析 NexusPHP RSS XML。"""
        try:
            root = ET.fromstring(xml_text.encode("utf-8"))
        except ET.ParseError as exc:
            logger.warning("hhanclub RSS XML 解析失败：%s", exc)
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
            # 从 link 提取种子 ID（details.php?id=xxx 或 download.php?id=xxx）
            id_match = re.search(r'details\.php\?id=(\d+)', link, re.IGNORECASE)
            torrent_id = id_match.group(1) if id_match else ""
            if not torrent_id:
                dl_match = _DOWNLOAD_RE.search(link)
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

        dict 路径（HTML scrape 结果）：从 free_classes + remaining_text 提取。
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
            remaining_text: str | None = remaining_raw if isinstance(remaining_raw, str) else None

            # HHan 主题：剩余时间的 <span title="2026-10-11 11:27:07"> 含绝对时刻
            remaining_abs_raw = raw.get("remaining_abs")
            remaining_abs: str | None = (
                remaining_abs_raw if isinstance(remaining_abs_raw, str) else None
            )

            free_type, remaining_delta, confidence = HhanclubAdapter._classify_from_html(
                free_classes, remaining_text,
            )

            # 优先使用绝对截止时刻（title 属性），其次用剩余时长 + clock_offset 换算
            end_at: datetime | None = None
            if remaining_abs:
                end_at = HhanclubAdapter._parse_abs_datetime(remaining_abs)
            if end_at is None and remaining_delta is not None:
                end_at = self._clock_offset.remaining_to_absolute(remaining_delta)

            # 有绝对截止时刻 → 限时类型（覆盖 _classify_from_html 的 PERMANENT 误判）
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

            # 永久免费标记（无截止时刻）
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
                # 有促销标记但无时间 → 永久（per NexusPHP 默认行为）
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

        NexusPHP: GET ``download.php?id={id}``，cookie 鉴权。
        """
        if not self._cookies:
            return None
        url = f"{self._base_url()}/download.php?id={item_id}"
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("hhanclub %s 下载 torrent 失败：%s", self.name, exc)
            return None
        if resp.status_code >= 400:
            return None
        content = resp.content
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
    """hhanclub 站点熔断（FR-2.2：429/403 立即熔断）。"""


def _stringify_safe(raw: dict[str, Any]) -> str:
    """把 dict 压成可读字符串，便于 FreeWindow.raw_text 调试。"""
    parts: list[str] = []
    for k in ("id", "title", "free_classes", "remaining_text", "remaining_abs"):
        if k in raw and raw[k] is not None:
            parts.append(f"{k}={raw[k]}")
    return " ".join(parts)[:2000]


__all__ = ["HhanclubAdapter", "_BreakerTripped"]
