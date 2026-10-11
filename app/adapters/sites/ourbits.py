"""OurBits 站点适配器（FR-1.5）。

ourbits.club 是基于 NexusPHP（xiaomlove fork, 1.10.x）的 PT 站点，cookie 鉴权。

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: GET /torrents.php?spstate=7，parsel CSS 解析
3. download_torrent: GET /download.php?id=X
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X

ourbits 在 Cloudflare 后，需 cf_clearance cookie。

免费标记格式：
- ``<img class="pro_free">`` / ``<img class="pro_30pctdown">`` 等
- ``<font class="free">``（白名单过滤）
- ``<span title="绝对时刻">剩余时间文案</span>``
"""

from __future__ import annotations

import re

import httpx
from parsel import Selector

from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: ``<img class="pro_XXX">`` → 标准免费 class 映射（供 free_payload 填充）
_PRO_IMG_MAP: dict[str, str] = {
    "pro_free": "free",
    "pro_free2up": "twoupfree",
    "pro_2up": "twoup",
    "pro_50pctdown": "halfdown",
    "pro_50pctdown2up": "twouphalfdown",
    "pro_30pctdown": "thirtypercent",
}

#: 真正的促销 ``<font class="free">`` 白名单（排除 new/gray 等 UI 徽章 class）
_PROMO_FONT_CLASSES: frozenset[str] = frozenset(
    {"free", "twoupfree", "twoup", "halfdown", "twouphalfdown", "thirtypercent"}
)

#: 详情页锚点 href 中的 id 提取
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)", re.IGNORECASE)

#: 绝对时刻（title 属性）：``2026-10-11 12:41:28``
_ABS_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

#: 登录态检测
_LOGGED_IN_RE = re.compile(r"(userdetails\.php|logout\.php|takelogout\.php)", re.IGNORECASE)
_LOGIN_REDIRECT_RE = re.compile(r"login\.php", re.IGNORECASE)


class OurbitsAdapter(SiteAdapter):
    """OurBits (ourbits.club) NexusPHP cookie 鉴权适配器。"""

    adapter_type = "ourbits"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        cookies: str | None = None,
        apikey: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(profile, cookies=cookies, apikey=apikey, timeout=timeout)
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
        return f"{self._base_url()}/details.php?id={item_id}"

    def _download_url(self, item_id: str) -> str:
        return f"{self._base_url()}/download.php?id={item_id}"

    # ------------------------------------------------------------ 1. check_reachability
    async def check_reachability(self) -> SiteHealth:
        """1. 校验域名可达与登录态：GET /index.php。"""
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

    # ------------------------------------------------------------ 2. search_torrents
    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 100,
        page_number: int = 1,
    ) -> list[FeedItem]:
        """2. 抓取免费种子列表：GET /torrents.php?spstate=7，parsel CSS 解析。"""
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=7&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("OurBits 拉取失败：%s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("OurBits 拉取 HTTP %d", resp.status_code)
            return []
        return OurbitsAdapter._parse_spstate7_html(resp.text, limit=page_size)

    @staticmethod
    def _parse_spstate7_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """parsel CSS 解析 Ourbits spstate=7 页面（与 hdsky 同源，结构一致）。"""
        sel = Selector(text=html)
        items: list[FeedItem] = []
        seen: set[str] = set()

        for a in sel.css('a[href*="details.php?id="][title]'):
            href = a.attrib.get("href", "")
            m = _DETAILS_ID_RE.search(href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in seen:
                continue
            seen.add(item_id)
            title = a.attrib.get("title", "").strip()

            tr = a.xpath("./ancestor::tr[1]").get("")
            if not tr:
                continue

            tr_sel = Selector(text=tr)

            pro_imgs = tr_sel.css('img[class*="pro_"]::attr(class)').getall()
            mapped_pro = []
            for cls in pro_imgs:
                for c in cls.split():
                    if c.startswith("pro_"):
                        mapped_pro.append(_PRO_IMG_MAP.get(c, c))
                        break

            font_classes = tr_sel.css("font::attr(class)").getall()
            mapped_font = [c for c in font_classes if c in _PROMO_FONT_CLASSES]

            free_classes = mapped_pro + mapped_font

            remaining_abs = None
            for ts in tr_sel.css("span[title]::attr(title)").getall():
                if _ABS_DATETIME_RE.match(ts):
                    remaining_abs = ts
                    break

            free_payload: dict[str, object] | None = None
            if free_classes or remaining_abs:
                free_payload = {
                    "free_classes": free_classes,
                    "remaining_abs": remaining_abs,
                }

            items.append(
                FeedItem(
                    title=title,
                    link=f"details.php?id={item_id}",
                    item_id=item_id,
                    info_hash=None,
                    size=0,
                    category=None,
                    description="",
                    published_at=None,
                    torrent_url=f"download.php?id={item_id}",
                    free_payload=free_payload,
                )
            )
            if len(items) >= limit:
                break

        return items[:limit]

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent：GET /download.php?id=X。"""
        if not self._cookies:
            raise _TorrentDownloadError("缺少 cookies")
        try:
            resp = await self._client.get(
                self._download_url(item_id),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise _TorrentDownloadError(f"下载失败：{exc}") from exc
        if resp.status_code >= 400:
            raise _TorrentDownloadError(f"HTTP {resp.status_code}")
        content = resp.content
        if not content.startswith(b"d"):
            raise _TorrentDownloadError("响应不是 bencoded torrent")
        return content

    # ------------------------------------------------------------ 4. check_status
    async def check_status(self, item_id: str) -> TorrentBrief:
        """4. 种子状态检查：GET /details.php?id=X。"""
        if not self._cookies:
            return TorrentBrief.UNKNOWN
        try:
            resp = await self._client.get(
                self._detail_url(item_id),
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            return TorrentBrief.UNKNOWN
        if resp.status_code == 404:
            return TorrentBrief.DELETED
        if resp.status_code == 200:
            return TorrentBrief.EXISTS
        return TorrentBrief.UNKNOWN

    # ------------------------------------------------------------ 5. get_torrent_detail
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情：GET /details.php?id=X。返回 HTML。"""
        if not self._cookies:
            return TorrentDetail(item_id=item_id, error="缺少 cookies")
        try:
            resp = await self._client.get(
                self._detail_url(item_id),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return TorrentDetail(item_id=item_id, error=f"detail 网络错误：{exc}")
        if resp.status_code >= 400:
            return TorrentDetail(item_id=item_id, error=f"HTTP {resp.status_code}")
        return TorrentDetail(item_id=item_id, raw_html=resp.text[:20000])

    async def close(self) -> None:
        await self._client.aclose()


class _BreakerTripped(RuntimeError):
    """ourbits 站点熔断（FR-2.2：429/403 立即熔断）。"""


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["OurbitsAdapter", "_BreakerTripped", "_TorrentDownloadError"]