"""Audiences 站点适配器（FR-1.5）。

audiences.me 是 NexusPHP 架构（自定义主题）的 PT 站点，基于 cookie 鉴权（无 x-api-key JSON API）。

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: GET /torrents.php?spstate=0，BS4 解析（data-torrent-row 属性）
3. download_torrent: GET /download.php?id=X（直链，无需 passkey/sign 签名）
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X

与 hdsky/ourbits 的关键差异：
- 下载链接：裸 ``/download.php?id=X`` 直接返回 bencoded torrent，无需从详情页提取
  ``passkey=`` 或 ``t=&sign=`` 签名参数。因此 ``search_torrents`` 不调用
  ``_fill_download_signs``，``download_torrent`` 也不抓详情页。
- 种子行定位：audiences.me 自定义主题用 ``<tr data-torrent-row="1">``
  标记种子行，并携带 ``data-torrent-id`` / ``data-size-bytes`` 等 data-* 属性，
  优先从这些属性提取数据（比解析 TD 内容更可靠）。
- spstate 语义：audiences.me 的 spstate 与标准 NexusPHP 不同——
  ``0``=全部, ``2``=免费, ``7``=30%（不是"所有免费"）。用 ``spstate=0`` 抓全量。

免费标记格式（与 hdsky/ourbits 一同）：
- ``<img class="pro_free">`` / ``<img class="pro_50pctdown">`` 等
- ``<span title="绝对时刻">剩余时间文案</span>``（紧跟 ``剩余时间：`` 文案）

cookie 模式：用户手动从浏览器获取 session cookies（c_secure_uid / c_secure_pass 等），
加密存储在 sites.cookies_enc，由 AdapterFactory.site 解密注入。
不自动登录（AGENTS.md：不引入验证码绕过）。
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
    parse_publish_time,
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

#: 绝对时刻（title 属性）：``2026-10-11 12:41:28``
_ABS_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

#: 登录态检测
_LOGGED_IN_RE = re.compile(r"(userdetails\.php|logout\.php|takelogout\.php)", re.IGNORECASE)
_LOGIN_REDIRECT_RE = re.compile(r"login\.php", re.IGNORECASE)


class AudiencesAdapter(SiteAdapter):
    """Audiences (audiences.me) NexusPHP cookie 鉴权适配器。"""

    adapter_type = "audiences"

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
        """2. 抓取种子列表：GET /torrents.php?spstate=0，BS4 解析。

        audiences.me 用 ``spstate=0`` 抓全量种子（含无促销标记的普通种子）——
        是否免费留给 WindowParser enrich 阶段判断。

        注：audiences.me 的 spstate 语义与标准 NexusPHP 不同——
        ``0``=全部, ``2``=免费, ``7``=30%（不是"所有免费"）。

        返回的 FeedItem.torrent_url 字段已携带直链 ``download.php?id=X``，
        可直接 GET 下载 .torrent（无需从详情页提取签名参数）。
        """
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=0&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("Audiences 拉取失败：%s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("Audiences 拉取 HTTP %d", resp.status_code)
            return []
        items = AudiencesAdapter._parse_torrents_html(resp.text, limit=page_size)
        return items

    @staticmethod
    def _parse_torrents_html(html: str, *, limit: int = 100) -> list[FeedItem]:
        """BS4 解析 audiences.me torrents.php 页面。

        audiences.me 自定义主题：每个种子占一个 ``<tr data-torrent-row="1">``,
        携带丰富的 data-* 属性（``data-torrent-id`` / ``data-size-bytes`` 等）。
        优先从 data-* 属性提取数据，兜底走 BS4 DOM 解析。

        种子行结构（简化）::

            <tr data-torrent-row="1" data-torrent-id="732416"
                data-size-bytes="46072820699" ...>
              <td class="rowfollow ... torrent-type-cell">
                <img class="c_movies" title="电影" />
              </td>
              <td class="rowfollow torrents-box">
                <a class="torrent-title-link" title="..."
                   href="details.php?id=732416&hit=1">...</a>
                <img class="pro_free" />
                剩余时间：<span title="2026-10-15 21:20:57">1天9时</span>
              </td>
              <td class="rowfollow torrent-time-cell">
                <span title="2026-10-14 09:17:34">2时<br />27分</span>
              </td>
              <td class="rowfollow torrent-size-cell">42.91<br />GB</td>
              ...
            </tr>
        """
        soup = BeautifulSoup(html, "lxml")
        items: list[FeedItem] = []
        seen: set[str] = set()

        for tr in soup.find_all("tr", attrs={"data-torrent-row": "1"}):
            if len(items) >= limit:
                break

            # ---------------- item_id: 优先 data-torrent-id，兜底 details 锚点
            item_id: str | None = None
            tid_attr = tr.get("data-torrent-id")
            if tid_attr:
                item_id = str(tid_attr).strip()
            if not item_id:
                a = tr.find("a", href=_DETAILS_ID_RE)
                if a is not None:
                    href = str(a.get("href") or "")
                    if "userdetails.php" not in href:
                        m = _DETAILS_ID_RE.search(href)
                        if m:
                            item_id = m.group(1)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)

            # ---------------- title: torrent-title-link 的 title 属性
            title: str = ""
            a_title = tr.find("a", class_="torrent-title-link")
            if a_title is not None:
                title = str(a_title.get("title") or a_title.get_text(strip=True) or "").strip()
            if not title:
                a = tr.find("a", href=_DETAILS_ID_RE, attrs={"title": True})
                if a is not None:
                    title = str(a.get("title") or "").strip()

            # ---------------- category: <img class="c_XXX" title="...">
            category: str | None = None
            cat_img = tr.find("img", class_=re.compile(r"^c\d?_"))
            if cat_img is not None:
                cat_title = str(cat_img.get("title") or cat_img.get("alt") or "")
                category = cat_title.strip() or None

            # ---------------- size: 优先 data-size-bytes，兜底 torrent-size-cell
            size_bytes = 0
            size_attr = tr.get("data-size-bytes")
            if size_attr:
                with contextlib.suppress(ValueError, TypeError):
                    size_bytes = int(str(size_attr))
            if size_bytes == 0:
                size_td = tr.find("td", class_="torrent-size-cell")
                if size_td is not None:
                    td_html = size_td.decode()
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

            # ---------------- published_at: torrent-time-cell 的 span[title]
            published_at = parse_publish_time(tr)
            if published_at is None:
                time_td = tr.find("td", class_="torrent-time-cell")
                if time_td is not None:
                    sp = time_td.find("span", attrs={"title": True})
                    if sp is not None:
                        published_at = parse_abs_datetime(
                            str(sp.get("title") or "")
                        )

            # ---------------- 免费标记：img class="pro_XXX" → 标准 class
            free_classes: list[str] = []
            for img in tr.find_all("img", class_=re.compile(r"^pro_")):
                classes: list[str] = list(img.get("class") or [])
                for c in classes:
                    if c.startswith("pro_") and c in _PRO_IMG_MAP:
                        free_classes.append(_PRO_IMG_MAP[c])
                        break
            for font in tr.find_all("font", class_=True):
                for c in font.get("class") or []:
                    if c in _PROMO_FONT_CLASSES:
                        free_classes.append(c)
                        break

            # ---------------- 限时绝对时刻（排除发布时间单元格自身）
            remaining_abs: str | None = None
            for span in tr.find_all("span", attrs={"title": True}):
                t = str(span.get("title") or "")
                if not _ABS_DATETIME_RE.match(t):
                    continue
                if published_at is not None and parse_abs_datetime(t) == published_at:
                    continue
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

            # ---------------- torrent_url: 直链（无需签名参数）
            torrent_url = f"download.php?id={item_id}"

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
                    torrent_url=torrent_url,
                    free_payload=free_payload,
                )
            )

        return items[:limit]

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent。

        audiences.me 的 ``/download.php?id=X`` 直链直接返回 bencoded torrent，
        无需从详情页提取 ``passkey=`` 或 ``t=&sign=`` 签名参数。
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

        注：用 follow_redirects=False 才能识别 302 重定向（cookie 过期 → login.php）。
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
            return TorrentBrief.UNKNOWN
        if resp.status_code == 200:
            return TorrentBrief.EXISTS
        return TorrentBrief.UNKNOWN

    # ------------------------------------------------------------ 5. get_torrent_detail
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情：GET /details.php?id=X。

        解析 title（h1）+ download_url（直链 download.php?id=X）。
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
        soup = BeautifulSoup(html, "lxml")

        title_text: str | None = None
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title_text = h1.get_text(strip=True)
        else:
            a = soup.find("a", attrs={"title": True})
            if a:
                title_text = str(a.get("title") or "")

        download_url = self._download_url(item_id)

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


__all__ = ["AudiencesAdapter", "_TorrentDownloadError"]
