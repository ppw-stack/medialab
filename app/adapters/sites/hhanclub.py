"""HHanClub 站点适配器（FR-1.5）。

hhanclub.net 是 NexusPHP v1.8.6 架构的 PT 站点，基于 cookie 鉴权（无 x-api-key JSON API）。

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: 无搜索 API，返回 []
3. download_torrent: GET /download.php?id=X
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X

NexusPHP 免费标记 HTML 解析仍保留（_parse_torrents_html/_classify_from_html），
但只产生 free_payload，由业务层 WindowParser 解析。

cookie 模式：用户手动从浏览器获取 session cookies（c_secure_uid / c_secure_pass 等），
加密存储在 sites.cookies_enc，由 AdapterFactory.site 解密注入。
不自动登录（AGENTS.md：不引入验证码绕过）。
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

#: HHan 主题种子卡片分隔符（每个种子卡片以 torrent-table-for-spider 开头）
_CARD_SPLIT_RE = re.compile(r'torrent-table-for-spider', re.IGNORECASE)

#: 从种子名链接提取 ID + 标题（兼容单/双引号）
_NAME_RE = re.compile(
    r"details\.php\?id=(\d+)[^>]*?torrent-info-text-name[\"'][^>]*>([^<]+)",
    re.IGNORECASE,
)

#: 从下载链接提取 ID + passkey
_DOWNLOAD_RE = re.compile(
    r'download\.php\?id=(\d+)&passkey=([a-f0-9]+)',
    re.IGNORECASE,
)

#: 从大小单元格提取文件大小
_SIZE_RE = re.compile(
    r'torrent-info-text-size[^>]*>\s*([\d.]+\s*\w+)\s*<',
    re.IGNORECASE,
)

#: HHan 主题免费标记提取
_PROMO_TAG_RE = re.compile(
    r'promotion-tag\s+promotion-tag-(\w+)',
    re.IGNORECASE,
)

#: HHan 主题剩余时间绝对时刻提取
_REMAINING_TIME_RE = re.compile(
    r'剩余时间：<span title="([^"]+)"',
    re.IGNORECASE,
)

#: "剩余 X天Y小时" 文案正则（兼容简体/繁体/英文）
_REMAINING_RE = re.compile(
    r"(?:剩余|剩餘|剩|left|remaining)\s*"
    r"(?:(?P<d>\d+)\s*(?:天|日|d|days?)\s*)?"
    r"(?:(?P<h>\d+)\s*(?:小时|小時|時|时|h|hours?)\s*)?"
    r"(?:(?P<m>\d+)\s*(?:分|分鐘|m|min)\s*)?",
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
        """2. 种子搜索：NexusPHP 无搜索 API，返回空列表。

        站点种子发现走 RSS 拉取（不在 adapter 契约中，由 feed_fetcher 工具接管）。
        """
        return []

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
        """4. 种子状态检查：GET /details.php?id=X。

        NexusPHP 不暴露删除/违规标记 → 仅基于 HTTP 状态判断：
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
        """公开：供 feed_fetcher 工具调用。解析 HHan 主题 torrents.php HTML。

        提取站内 ID、标题、大小、免费标记 class、剩余时间文案，组装为 FeedItem。
        免费窗口解析不在此完成（由业务层 WindowParser 接管）。
        """
        items: list[FeedItem] = []
        cards = _CARD_SPLIT_RE.split(html)
        for card in cards[1:]:
            name_m = _NAME_RE.search(card)
            if not name_m:
                continue
            torrent_id = name_m.group(1)
            title = name_m.group(2).strip()

            dl_m = _DOWNLOAD_RE.search(card)
            dl_id = dl_m.group(1) if dl_m else torrent_id

            size_m = _SIZE_RE.search(card)
            size = 0
            if size_m:
                size = HhanclubAdapter._parse_size_str(size_m.group(1))

            promo_classes = [m.group(1) for m in _PROMO_TAG_RE.finditer(card)]
            font_classes = [
                m.group(1)
                for m in re.finditer(
                    r'<font[^>]*class=["\']([^"\']+)["\'][^>]*>',
                    card, re.IGNORECASE,
                )
            ]
            free_classes = promo_classes + font_classes

            time_m = _REMAINING_TIME_RE.search(card)
            remaining_abs = time_m.group(1) if time_m else None

            remaining_m = _REMAINING_RE.search(card)
            remaining_text = remaining_m.group(0) if remaining_m else None

            free_payload: dict[str, object] | None = None
            if free_classes or remaining_abs or remaining_text:
                free_payload = {
                    "free_classes": free_classes,
                    "remaining_text": remaining_text,
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
    """hhanclub 站点熔断（FR-2.2：429/403 立即熔断）。"""


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HhanclubAdapter", "_BreakerTripped", "_TorrentDownloadError"]
