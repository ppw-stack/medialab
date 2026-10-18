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
from app.adapters.sites.parsers import nexus_php
from app.adapters.sites.user_data import SiteUserSnapshot
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

#: 详情页 anchor href 里的 passkey download URL
#: ``/download.php?id=357094&passkey=74f6bd64e80ba32094b6f71485727f9a&https=1``
_SIGNED_DL_RE = re.compile(
    r"download\.php\?id=(\d+)&passkey=([a-f0-9]+)(?:&https=(\d))?",
    re.IGNORECASE,
)

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
        page_size: int = 200,
        page_number: int = 1,
    ) -> list[FeedItem]:
        """2. 抓取种子列表：GET /torrents.php?spstate=0，parsel CSS 解析。

        Ourbits 与 HDSky 同为标准 NexusPHP 主题，spstate=0 抓所有促销种子
        （永久 + 限时 + 折扣）。**注意**：旧版本用 spstate=7（仅30%折扣页），
        错误地把所有种子视为无 end_at 的 30% off——已修复为 spstate=0 抓全量。

        fetch_all_sites 拉全量入库（含无促销标记的普通种子）—— 是否免费
        留给 WindowParser enrich 阶段判断。

        返回的条目不带下载 URL；下载时调 :meth:`get_torrent_url` 抓详情页
        提取 ``passkey=...&https=Z`` 拼 download URL。
        """
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=0&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("OurBits 拉取失败：%s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("OurBits 拉取 HTTP %d", resp.status_code)
            return []
        items = OurbitsAdapter._parse_spstate7_html(resp.text)
        return items

    # ------------------------------------------------------------ 2.5 get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """返回 ``{base}/download.php?id=X&passkey=Y&https=Z`` 绝对 URL。

        流程：GET ``details.php?id=X``，从 anchor href 提取 ``passkey`` 与
        ``https`` flag 拼完整 download URL。
        """
        if not item_id:
            return None
        try:
            resp = await self._client.get(self._detail_url(item_id), timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("OurBits 详情页拉取失败 %s: %s", item_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        m = _SIGNED_DL_RE.search(resp.text)
        if not m:
            return None
        signed_id, passkey, https_flag = m.group(1), m.group(2), m.group(3) or "1"
        return (
            f"{self._base_url()}/download.php"
            f"?id={signed_id}&passkey={passkey}&https={https_flag}"
        )

    @staticmethod
    def _parse_spstate7_html(html: str) -> list[FeedItem]:
        """BS4 解析 Ourbits spstate=0 页面（与 hdsky 同源 NexusPHP 主题）。

        Ourbits 与 hdsky 共用标准 NexusPHP 表格主题：每个 torrent 占一个 ``<tr>``，
        不同 ``<td>`` 依次放 category / title / download / pub / size / seeders。
        """
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="torrents")
        if table is None:
            table = soup

        items: list[FeedItem] = []
        seen: set[str] = set()

        for tr in table.find_all("tr"):
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

            # ---------------- category
            # 兼容 HDSky (`c_movies`) / Ourbits (`c2_movies`) 两种前缀
            category: str | None = None
            cat_img = tr.find("img", class_=re.compile(r"^c\d?_"))
            if cat_img is not None:
                cat_title = str(cat_img.get("title") or cat_img.get("alt") or "")
                category = cat_title.strip() or None

            # ---------------- size：扫 td 匹配 <td>X.XX<br />UNIT</td>
            size_bytes = 0
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

            # ---------------- published_at：发布时间单元格（短相对时长 td），
            # 避免误取描述栏里的"优惠剩余时间"
            published_at = parse_publish_time(tr)

            # ---------------- 免费标记
            free_classes: list[str] = []
            _free_seen: set[str] = set()
            for img in tr.find_all("img", class_=re.compile(r"^pro_")):
                classes: list[str] = list(img.get("class") or [])
                for c in classes:
                    if c.startswith("pro_") and c in _PRO_IMG_MAP:
                        mapped = _PRO_IMG_MAP[c]
                        if mapped not in _free_seen:
                            free_classes.append(mapped)
                            _free_seen.add(mapped)
                        break
            for font in tr.find_all("font", class_=True):
                for c in font.get("class") or []:
                    if c in _PROMO_FONT_CLASSES and c not in _free_seen:
                        free_classes.append(c)
                        _free_seen.add(c)
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
                    free_payload=free_payload,
                )
            )

        return items

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 .torrent。

        流程与 :meth:`get_torrent_url` 一致：
        1. GET /details.php?id=X 提取 anchor href 中的 ``passkey=...`` 完整 URL
        2. GET 该完整 URL 拿 bencoded bytes

        注：裸 ``/download.php?id=X`` 在 OurBits 永远 404，必须带 passkey。
        """
        if not self._cookies:
            raise _TorrentDownloadError("缺少 cookies")
        # 抓详情页提取 passkey URL
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
            raise _TorrentDownloadError(f"未找到 passkey download URL（id={item_id}）")
        signed_url = (
            f"{self._base_url()}/download.php"
            f"?id={m.group(1)}&passkey={m.group(2)}&https={m.group(3) or '1'}"
        )
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
            # 重定向到 login.php（cookie 过期）
            return TorrentBrief.UNKNOWN
        if resp.status_code == 200:
            return TorrentBrief.EXISTS
        return TorrentBrief.UNKNOWN

    # ------------------------------------------------------------ 5. get_torrent_detail
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情：GET /details.php?id=X。

        解析 title（h1）+ download_url（anchor href 中的 passkey URL）。
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
        from parsel import Selector as _Sel

        sel = _Sel(text=html)
        title_text = sel.css("h1::text").get() or sel.css('a[title]::attr(title)').get()
        m = _SIGNED_DL_RE.search(html)
        download_url = None
        if m:
            download_url = (
                f"{self._base_url()}/download.php"
                f"?id={m.group(1)}&passkey={m.group(2)}&https={m.group(3) or '1'}"
            )
        return TorrentDetail(
            item_id=item_id,
            title=title_text,
            download_url=download_url,
            raw_html=html,
        )


    # ----------------------- FR-1.6 站点用户数据（精简版） -----------------------

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """ourbits 走 ``/userdetails.php``（NexusPHP 模板）。"""
        if not self._cookies:
            return None
        try:
            resp = await self._client.get(
                f"{self._base_url()}/userdetails.php",
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("ourbits get_user_snapshot HTTP 失败：%s", exc)
            return None
        if resp.status_code >= 400:
            return None
        return nexus_php.parse_user_snapshot(resp.text)



    async def close(self) -> None:
        await self._client.aclose()





class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["OurbitsAdapter", "_TorrentDownloadError"]