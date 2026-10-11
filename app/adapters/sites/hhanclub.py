"""HHanClub 站点适配器（FR-1.5）。

hhanclub.net 是 NexusPHP v1.8.6 架构的 PT 站点，基于 cookie 鉴权（无 x-api-key JSON API）。

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: GET /torrents.php?spstate=7，BeautifulSoup 解析（Tailwind 自定义主题）
3. download_torrent: GET /download.php?id=X
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X

HHan 使用 Tailwind 自定义主题，与标准 NexusPHP 表格布局不同，DOM 结构散乱
（嵌套 div / 自定义 class），因此用 BS4 容错遍历而非 parsel CSS。

免费标记格式（实测）：
- 限免/2X：``<span title="绝对时刻">X时Y分钟</span>`` 在 `[剩余时间：...]` 块内
- 永久免费：标题前的绿色图标 / ``class="free"``（保留兼容性）
- 折扣：``<font class="halfdown">30%</font>`` 等

cookie 模式：用户手动从浏览器获取 session cookies（c_secure_uid / c_secure_pass 等），
加密存储在 sites.cookies_enc，由 AdapterFactory.site 解密注入。
不自动登录（AGENTS.md：不引入验证码绕过）。
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

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

#: 详情页锚点 href 中的 id 提取
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)", re.IGNORECASE)

#: 绝对时刻（title 属性）：``2026-10-11 12:41:28``
_ABS_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

#: "剩余时间"文案包裹（Tailwind 自定义）：``剩余时间：<span title="...">X时Y分钟</span>``
_REMAINING_BLOCK_RE = re.compile(r"剩余时间[:：]\s*<span\s+title=\"([^\"]+)\"", re.IGNORECASE)

#: 登录态检测：登录后页面含 userdetails.php 或 logout.php 链接
_LOGGED_IN_RE = re.compile(r"(userdetails\.php|logout\.php|takelogout\.php)", re.IGNORECASE)

#: 登录页重定向检测
_LOGIN_REDIRECT_RE = re.compile(r"login\.php", re.IGNORECASE)

#: CSS class 白名单（用于 free/twoupfree/halfdown 等促销 class 识别）
_PROMO_CLASSES: frozenset[str] = frozenset(
    {"free", "twoupfree", "twoup", "halfdown", "twouphalfdown", "thirtypercent"}
)


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
        """2. 抓取免费种子列表：GET /torrents.php?spstate=2，BS4 解析。

        HHan 自定义主题的 spstate=7（所有免费）是 JS 动态加载，HTML 不含
        torrent 链接。spstate=2（永久免费）反而是 SSR 全量返回，所以用
        spstate=2 抓取。

        注：当前只覆盖"永久免费"窗口；限时活动（HHan 自定义等 → 一）
        因 JS 渲染暂不可见，需要 site 升级为 API 抓取或 headless 浏览器
        才能识别（不引入反爬对抗，AGENTS.md）。
        """
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=2&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("HHanClub 拉取失败：%s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("HHanClub 拉取 HTTP %d", resp.status_code)
            return []
        return HhanclubAdapter._parse_spstate2_html(resp.text, limit=page_size)

    @staticmethod
    def _parse_spstate2_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """BS4 解析 HHanClub spstate=2 页面（永久免费 SSR）。

        HHan 使用 Tailwind 自定义主题：
        - 锚点 ``href="https://hhanclub.net/details.php?id=N"`` 含完整域名
        - 限时块 ``<span title="2026-10-11 12:41:28">X时Y分钟</span>`` 紧邻
          "剩余时间" 文案
        - 永久免费条目无剩余时间文案，但有 ``class="free"`` 的图标 span

        过滤：排除 ``userdetails.php?id=N``（用户名导航），只保留 torrent。
        """
        soup = BeautifulSoup(html, "lxml")
        items: list[FeedItem] = []
        seen: set[str] = set()

        # 找所有指向 details.php?id=N 的锚点（排除 userdetails.php）
        for a in soup.find_all("a", href=_DETAILS_ID_RE):
            href = a.get("href", "")
            # 排除 userdetails 链接
            if "userdetails.php" in href:
                continue
            m = _DETAILS_ID_RE.search(href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in seen:
                continue
            seen.add(item_id)

            # 标题：title 属性优先，其次文本
            title = (a.get("title") or a.get_text(strip=True) or "").strip()

            # 向上找 torrent 容器：通常最近的 <tr> 或包含"剩余时间"的块
            container = _find_torrent_container(a)

            # 限时绝对时刻：<span title="YYYY-MM-DD HH:MM:SS">
            remaining_abs = None
            if container is not None:
                for span in container.find_all("span"):
                    t = span.get("title", "")
                    if _ABS_DATETIME_RE.match(t):
                        remaining_abs = t
                        break

            # 免费 class 标记：img class="pro_XXX" 或 font class="free/halfdown/..."
            free_classes: list[str] = []
            if container is not None:
                for tag in container.find_all(class_=True):
                    classes = tag.get("class", [])
                    for cls in classes:
                        if cls in _PROMO_CLASSES:
                            free_classes.append(cls)
                        elif cls.startswith("pro_"):
                            # 兼容 hdsky/ourbits 风格
                            mapped = cls[4:]
                            if mapped in _PROMO_CLASSES:
                                free_classes.append(mapped)

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


def _find_torrent_container(anchor: object) -> object | None:
    """向上查找 torrent 容器（最近的 <tr> 或最近的 <div>）。"""
    # 优先：最近 <tr>（标准 NexusPHP 表格行）
    tr = anchor.find_parent("tr")  # type: ignore[attr-defined]
    if tr is not None:
        return tr
    # 兜底：最近的 <div>（HHan Tailwind 自定义主题，每个 torrent 卡片是一个 div）
    # 限制深度 8 层，避免把整个 body 当作 container
    parent = anchor.parent  # type: ignore[attr-defined]
    seen: set[int] = set()
    depth = 0
    max_depth = 8
    while parent is not None and id(parent) not in seen and depth < max_depth:
        seen.add(id(parent))
        depth += 1
        # 找到块级容器（div 或 article 或 section）
        if getattr(parent, "name", None) in {"div", "article", "section"}:
            return parent
        parent = parent.parent  # type: ignore[attr-defined]
    return None


class _BreakerTripped(RuntimeError):
    """hhanclub 站点熔断（FR-2.2：429/403 立即熔断）。"""


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HhanclubAdapter", "_BreakerTripped", "_TorrentDownloadError"]