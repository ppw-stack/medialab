"""HDSky 站点适配器（FR-1.5）。

hdsky.me 是 NexusPHP v1.5 Beta 5 架构的 PT 站点，基于 cookie 鉴权（无 x-api-key JSON API）。

使用标准 NexusPHP 表格布局（``<table class="torrents">`` + ``<tr>`` 行），
免费标记在 ``<img class="pro_free">`` 或 ``<font class="free">`` 中。

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: 无搜索 API，返回 []
3. download_torrent: GET /download.php?id=X
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X
"""

from __future__ import annotations

import re

import httpx

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

#: 种子标题锚点（HDSky 真实结构，嵌套表格）：
#: ``<a title="Full Title" href="details.php?id=10001&hit=1">``
_ANCHOR_RE = re.compile(
    r'<a\b([^>]*href="details\.php\?id=(\d+)&hit=1"[^>]*)>',
    re.IGNORECASE,
)

#: 从锚点属性段提取 title
_ANCHOR_TITLE_RE = re.compile(r'title="([^"]+)"', re.IGNORECASE)

#: 任意 download.php?id=NNN 引用（提取下载 ID）
_ANY_DOWNLOAD_RE = re.compile(
    r'download\.php\?id=(\d+)',
    re.IGNORECASE,
)

#: 从 ``<img class="pro_free">`` 提取促销 class
_PRO_IMG_RE = re.compile(
    r'<img[^>]*class=["\'](pro_\w+)["\']',
    re.IGNORECASE,
)

#: 从 ``<font class="free">`` 提取促销 class
_FREE_FONT_RE = re.compile(
    r'<font[^>]*class=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)

#: HDSky 优惠剩余时间（可见 span，绝对时刻在 title 属性）
_PROMO_TIME_RE = re.compile(
    r'优惠剩余时间：<b><span title="([^"]+)"',
    re.IGNORECASE,
)

#: 文件大小提取（HDSky 用 ``30.76<br />GB`` 分隔数字与单位）
_SIZE_RE = re.compile(
    r'([\d.]+)\s*(?:<br\s*/?>\s*)?(TB|GB|MB|KB|TiB|GiB|MiB|KiB)',
    re.IGNORECASE,
)

#: 登录态检测
_LOGGED_IN_RE = re.compile(r'(userdetails\.php|logout\.php|takelogout\.php)', re.IGNORECASE)
_LOGIN_REDIRECT_RE = re.compile(r'login\.php', re.IGNORECASE)

#: 大小单位转换
_SIZE_FACTORS = {
    "tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024,
    "tib": 1024**4, "gib": 1024**3, "mib": 1024**2, "kib": 1024,
}

#: ``<img class="pro_XXX">`` → 标准免费 class 映射（仅用于 free_payload 填充）
_PRO_IMG_MAP: dict[str, str] = {
    "pro_free": "free",
    "pro_free2up": "twoupfree",
    "pro_2up": "twoup",
    "pro_50pctdown": "halfdown",
    "pro_50pctdown2up": "twouphalfdown",
    "pro_30pctdown": "thirtypercent",
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

    @staticmethod
    def _parse_size_str(text: str) -> int:
        m = re.match(r"([\d.]+)\s*(\w+)", text.strip())
        if not m:
            return 0
        try:
            num = float(m.group(1))
        except ValueError:
            return 0
        factor = _SIZE_FACTORS.get(m.group(2).lower(), 1)
        return int(num * factor)

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
        """2. 种子搜索：NexusPHP 无搜索 API，返回空列表。"""
        return []

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent：GET /download.php?id=X。

        HDSky 使用签名表单，签名为 path 中的 t/sign 参数。这里直接走 GET 即可。
        """
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
        """4. 种子状态检查：GET /details.php?id=X。

        - 200 → EXISTS
        - 404 → DELETED
        - 其他 → UNKNOWN
        """
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

    # ------------------------------------------------------------ HTML 解析（仅作 free_payload 填充）
    @staticmethod
    def parse_torrents_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """公开：供 feed_fetcher 工具调用。解析 HDSky 真实 torrents.php HTML。

        HDSky 使用嵌套表格，单个种子跨多个 ``<tr>``（无法按行切分）。
        解析策略：以标题锚点为界，把列表切成一串"种子窗口"，每个窗口内提取：
        - 标题（title 属性）
        - ``<img class="pro_free">``
        - 优惠剩余时间（``优惠剩余时间：<b><span title="绝对时刻">``）
        - 签名下载表单
        - 大小
        """
        items: list[FeedItem] = []
        anchors = list(_ANCHOR_RE.finditer(html))
        for i, m in enumerate(anchors):
            attrs = m.group(1)
            torrent_id = m.group(2)
            title_m = _ANCHOR_TITLE_RE.search(attrs)
            title = title_m.group(1).strip() if title_m else f"torrent-{torrent_id}"

            start = m.start()
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
            window = html[start:end]

            dl_m = _ANY_DOWNLOAD_RE.search(window)
            dl_id = dl_m.group(1) if dl_m else torrent_id

            size_m = _SIZE_RE.search(window)
            size = 0
            if size_m:
                size = HdskyAdapter._parse_size_str(
                    f"{size_m.group(1)} {size_m.group(2)}"
                )

            pro_classes = [mm.group(1) for mm in _PRO_IMG_RE.finditer(window)]
            mapped_pro = [_PRO_IMG_MAP.get(c.lower(), c) for c in pro_classes]
            font_classes = [mm.group(1) for mm in _FREE_FONT_RE.finditer(window)]
            free_classes = mapped_pro + font_classes

            time_m = _PROMO_TIME_RE.search(window)
            remaining_abs = time_m.group(1) if time_m else None

            free_payload: dict[str, object] | None = None
            if free_classes or remaining_abs:
                free_payload = {
                    "free_classes": free_classes,
                    "remaining_abs": remaining_abs,
                }

            items.append(
                FeedItem(
                    title=title,
                    link=f"details.php?id={torrent_id}",
                    item_id=torrent_id,
                    info_hash=None,
                    size=size,
                    category=None,
                    description="",
                    published_at=None,
                    torrent_url=f"download.php?id={dl_id}",
                    free_payload=free_payload,
                )
            )
            if len(items) >= limit:
                break
        return items[:limit]


class _BreakerTripped(RuntimeError):
    """hdsky 站点熔断（FR-2.2：429/403 立即熔断）。"""


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HdskyAdapter", "_BreakerTripped", "_TorrentDownloadError"]
