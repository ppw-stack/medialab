"""HDSky 站点适配器（FR-1.5）。

hdsky.me 是 NexusPHP v1.5 Beta 5 架构的 PT 站点，基于 cookie 鉴权（无 x-api-key JSON API）。

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: GET /torrents.php?spstate=7，parsel CSS 解析（所有免费状态）
3. download_torrent: GET /download.php?id=X
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X

免费标记格式：
- ``<img class="pro_free">`` / ``<img class="pro_30pctdown">`` 等
- ``<span title="绝对时刻">剩余时间文案</span>``

spstate 含义（NexusPHP 标准）：
- 7 = 所有免费状态（永久 + 限时 + 折扣）
- 2 = 永久免费
- 3 = 2X 上传免费
"""

from __future__ import annotations

import contextlib
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
    parse_abs_datetime,
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

#: CSS class 白名单（用于 free font class 过滤，排除 new/gray 等 UI 徽章）
_PROMO_FONT_CLASSES: frozenset[str] = frozenset(
    {"free", "twoupfree", "twoup", "halfdown", "twouphalfdown", "thirtypercent"}
)

#: 详情页锚点 href 中的 id 提取
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)", re.IGNORECASE)

#: 详情页 form action 里的签名 download URL（兼容 ``&amp;`` HTML 实体）
#: ``download.php?id=123&t=1791689096&sign=6a47635b3134c1b3275984768eb3b2a6``
_SIGNED_DL_RE = re.compile(
    r"download\.php\?id=(\d+)(?:&amp;|&)t=(\d+)(?:&amp;|&)sign=([a-f0-9]+)",
    re.IGNORECASE,
)

#: 绝对时刻（title 属性）：``2026-10-11 12:41:28``
_ABS_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

#: 登录态检测
_LOGGED_IN_RE = re.compile(r"(userdetails\.php|logout\.php|takelogout\.php)", re.IGNORECASE)
_LOGIN_REDIRECT_RE = re.compile(r"login\.php", re.IGNORECASE)


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
        """2. 抓取种子列表：GET /torrents.php?spstate=0，parsel CSS 解析。

        HDSky 使用标准 NexusPHP 嵌套表格，spstate=0 聚合所有促销种子
        （永久 + 限时 + 折扣）。**注意**：旧版本用 spstate=7（仅30%折扣页），
        错误地把所有种子视为无 end_at 的 30% off——已修复为 spstate=0 抓全量。

        fetch_all_sites 拉全量入库（含无促销标记的普通种子）—— 是否免费
        留给 WindowParser enrich 阶段判断。

        返回的 FeedItem.free_payload 含：
        - ``free_classes``: 标准 free/twoupfree/halfdown/thirtypercent 等
        - ``remaining_abs``: 限时结束绝对时刻（仅限时条目有）

        返回的 FeedItem.torrent_url 字段（可选）已携带 ``t=...&sign=...``
        签名参数，可直接 GET 下载 .torrent（绕过 `/download.php?id=X` 的 404）。
        """
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=0&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("HDSky 拉取失败：%s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("HDSky 拉取 HTTP %d", resp.status_code)
            return []
        items = HdskyAdapter._parse_spstate7_html(resp.text, limit=page_size)
        # 批量抓详情页提取 t&sign，HDSky 的 /download.php?id=X 直链 404，
        # 需用 details.php 中的 form action（含 t&sign）才能下载。
        await self._fill_download_signs(items)
        return items

    async def _fill_download_signs(self, items: list[FeedItem]) -> None:
        """批量抓 details 页提取 ``t=...&sign=...`` 完整 download URL。

        FeedItem 是 frozen dataclass，用 ``dataclasses.replace`` 重建实例并替换原位。
        """
        if not items:
            return
        for idx, item in enumerate(items):
            if item.torrent_url or not item.item_id:
                continue
            try:
                resp = await self._client.get(self._detail_url(item.item_id))
            except httpx.HTTPError as exc:
                logger.debug("HDSky 详情页拉取失败 %s: %s", item.item_id, exc)
                continue
            if resp.status_code >= 400:
                continue
            m = _SIGNED_DL_RE.search(resp.text)
            if m:
                item_id, ts, sign = m.group(1), m.group(2), m.group(3)
                from dataclasses import replace

                items[idx] = replace(
                    item,
                    torrent_url=(
                        f"{self._base_url()}/download.php"
                        f"?id={item_id}&t={ts}&sign={sign}"
                    ),
                )

    @staticmethod
    def _parse_spstate7_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """BS4 解析 HDSky spstate=0 页面（fetch_all_sites 拉全量入库）。

        HDSky 使用标准 NexusPHP 主题，``torrents.php?spstate=0`` 渲染
        ``<table class="torrents">``，每个 torrent 占一个 ``<tr>`` 包含多 ``<td>``：

        - td0 = 分类图标（``<img class="c_tvseries">``）
        - td1 = 标题（``<a target="_blank" title="..." href="details.php?id=...">``）
                + 促销标记（``<img class="pro_free">`` + onmouseover 内的剩余时间）
        - td2..td4 = download / zip / bookmark 操作
        - td5 = 评论数
        - td6 = 发布时间（``<span title="2026-10-12 14:51:34">3时<br />41分</span>``）
        - td7 = size（``<td>50.54<br />GB</td>``）
        - td8 = seeders / leechers

        解析流程：先定位 ``table.torrents`` 主表格，遍历每个 tr 找 details 锚点，
        从同一 tr 的不同 td 提取全字段。
        """
        soup = BeautifulSoup(html, "lxml")
        # 定位主表格（NexusPHP 标准 class="torrents"）；无则 fallback 到全文档
        table = soup.find("table", class_="torrents")
        if table is None:
            table = soup

        items: list[FeedItem] = []
        seen: set[str] = set()

        for tr in table.find_all("tr"):
            if len(items) >= limit:
                break
            # 找 details 锚点（带 title 属性）
            a = tr.find("a", href=_DETAILS_ID_RE, attrs={"title": True})
            if a is None:
                continue
            href: str = str(a.get("href") or "")
            if "userdetails.php" in href:
                continue
            m = _DETAILS_ID_RE.search(href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in seen:
                continue
            seen.add(item_id)
            title: str = str(a.get("title") or "").strip()

            # ---------------- category: <img class="c_xxx" title="...">
            # 兼容 HDSky (`c_movies`) / Ourbits (`c2_movies`) 两种前缀
            category: str | None = None
            cat_img = tr.find("img", class_=re.compile(r"^c\d?_"))
            if cat_img is not None:
                cat_title = str(cat_img.get("title") or cat_img.get("alt") or "")
                category = cat_title.strip() or None

            # ---------------- size / pub_at: 扫所有 td，匹配 <td>X.XX<br />UNIT</td>
            # 与 <span title="YYYY-MM-DD HH:MM:SS">
            size_bytes = 0
            published_at = None
            for td in tr.find_all("td"):
                td_html: str = td.decode()
                if size_bytes == 0:
                    m_sz = re.search(
                        r"(\d+(?:\.\d+)?)\s*<br\s*/?>\s*(TB|GB|MB|KB)",
                        td_html,
                        re.IGNORECASE,
                    )
                    if m_sz is not None:
                        with contextlib.suppress(KeyError, ValueError):
                            size_bytes = int(
                                float(m_sz.group(1))
                                * {
                                    "TB": 1024 ** 4,
                                    "GB": 1024 ** 3,
                                    "MB": 1024 ** 2,
                                    "KB": 1024,
                                }[m_sz.group(2).upper()]
                            )
                if published_at is None:
                    span = td.find("span", attrs={"title": True})
                    if span is not None:
                        t = str(span.get("title") or "")
                        if _ABS_DATETIME_RE.match(t):
                            published_at = parse_abs_datetime(t)

            # ---------------- 免费标记：img class="pro_XXX" → 标准 class
            free_classes: list[str] = []
            for img in tr.find_all("img", class_=re.compile(r"^pro_")):
                classes: list[str] = list(img.get("class") or [])
                for c in classes:
                    if c.startswith("pro_") and c in _PRO_IMG_MAP:
                        free_classes.append(_PRO_IMG_MAP[c])
                        break
            # font class="free|twoupfree|..." 标记
            for font in tr.find_all("font", class_=True):
                for c in font.get("class") or []:
                    if c in _PROMO_FONT_CLASSES:
                        free_classes.append(c)
                        break

            # ---------------- 限时绝对时刻
            remaining_abs: str | None = None
            for span in tr.find_all("span", attrs={"title": True}):
                t = str(span.get("title") or "")
                if _ABS_DATETIME_RE.match(t):
                    remaining_abs = t
                    break
            if remaining_abs is None:
                for img in tr.find_all("img", attrs={"onmouseover": True}):
                    ov: str = str(img.get("onmouseover") or "")
                    decoded = (
                        ov.replace("&quot;", '"')
                        .replace("&amp;", "&")
                        .replace("&lt;", "<")
                        .replace("&gt;", ">")
                    )
                    m2 = re.search(
                        r"<span title=\"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\"",
                        decoded,
                    )
                    if m2:
                        remaining_abs = m2.group(1)
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
                    size=size_bytes,
                    category=category,
                    description="",
                    published_at=published_at,
                    torrent_url=None,  # 由 _fill_download_signs 后填（含 sign）
                    free_payload=free_payload,
                )
            )

        return items[:limit]

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent。

        流程（与 pull 阶段 `_fill_download_signs` 一致）：
        1. GET /details.php?id=X 提取 form action 中的 ``t=...&sign=...`` 完整 URL
        2. GET 该完整 URL 拿 bencoded bytes

        注：裸 ``/download.php?id=X`` 在 HDSky 永远 404，必须带签名参数。
        """
        if not self._cookies:
            raise _TorrentDownloadError("缺少 cookies")
        # 抓详情页提取签名 URL
        try:
            resp = await self._client.get(
                self._detail_url(item_id),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise _TorrentDownloadError(f"详情页拉取失败：{exc}") from exc
        if resp.status_code >= 400:
            raise _TorrentDownloadError(f"详情页 HTTP {resp.status_code}")
        m = _SIGNED_DL_RE.search(resp.text)
        if not m:
            raise _TorrentDownloadError(f"未找到签名 download URL（id={item_id}）")
        signed_url = (
            f"{self._base_url()}/download.php"
            f"?id={m.group(1)}&t={m.group(2)}&sign={m.group(3)}"
        )
        # GET 完整签名 URL
        try:
            dl_resp = await self._client.get(
                signed_url,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise _TorrentDownloadError(f"下载失败：{exc}") from exc
        if dl_resp.status_code >= 400:
            raise _TorrentDownloadError(f"HTTP {dl_resp.status_code}")
        content = dl_resp.content
        if not content.startswith(b"d"):
            raise _TorrentDownloadError("响应不是 bencoded torrent")
        return content

    # ------------------------------------------------------------ 4. check_status
    async def check_status(self, item_id: str) -> TorrentBrief:
        """4. 种子状态检查：GET /details.php?id=X。

        NexusPHP 不暴露删除/违规标记 → 仅基于 HTTP 状态判断：
        - 200 → EXISTS
        - 404 → DELETED
        - 302/401/403 → UNKNOWN（cookie 过期重定向到 login.php，避免误判 EXISTS）
        - 其他 → UNKNOWN

        注：用 follow_redirects=False 才能识别 302 重定向（默认会跟到 login.php 的 200 误判为 EXISTS）。
        """
        if not self._cookies:
            return TorrentBrief.UNKNOWN
        try:
            resp = await self._client.get(
                self._detail_url(item_id),
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return TorrentBrief.UNKNOWN
        if resp.status_code == 404:
            return TorrentBrief.DELETED
        if resp.status_code == 302:
            # 重定向到 login.php（cookie 过期）
            return TorrentBrief.UNKNOWN
        if resp.status_code == 200:
            return TorrentBrief.EXISTS
        return TorrentBrief.UNKNOWN

    # ------------------------------------------------------------ 5. get_torrent_detail
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情：GET /details.php?id=X。

        解析：title（h1/title 锚点）+ size（保留在 raw_html 中）+ download_url（form action 中的 sign URL）。
        """
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

        html = resp.text[:20000]
        # 解析 title：h1 或 title 属性
        from parsel import Selector as _Sel

        sel = _Sel(text=html)
        title_text = sel.css("h1::text").get() or sel.css('a[title]::attr(title)').get()
        # 解析 download URL（form action 中的 sign）
        m = _SIGNED_DL_RE.search(html)
        download_url = None
        if m:
            download_url = (
                f"{self._base_url()}/download.php"
                f"?id={m.group(1)}&t={m.group(2)}&sign={m.group(3)}"
            )
        return TorrentDetail(
            item_id=item_id,
            title=title_text,
            download_url=download_url,
            raw_html=html,
        )

    async def close(self) -> None:
        await self._client.aclose()


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HdskyAdapter", "_TorrentDownloadError"]