"""HDDolby 站点适配器（FR-1.5）。

www.hddolby.com 是基于 NexusPHP 的 PT 站点，cookie 鉴权（无 x-api-key JSON API），
部署在 Cloudflare 后端（需 ``cf_clearance`` 绕过 5s JS challenge）。

关键差异 vs 其他 NexusPHP 适配器（hdsky/hhanclub/hdhome）：
- 下载链接签名是 ``downhash={hex}x{hex}...`` 多段式（与 hdhome 同类，**不可合成**），
  必须从详情页 HTML 解析完整 URL。
- 标题在 ``<a href="details.php?id=N"><b>标题</b></a>`` 模式（hdsky 用 ``<a title="...">``）。
- 列表页表格是 ``<table class="mainouter">`` 内嵌 ``<table class="main">``，
  但 BS4 ``find_all('tr')`` 仍可一视同仁解析。

契约方法（5 个）：
1. check_reachability: GET /index.php
2. search_torrents: GET /torrents.php?spstate=0
3. download_torrent: GET /details.php?id=X 解析 downhash 完整 URL → 拉 bytes
4. check_status: GET /details.php?id=X
5. get_torrent_detail: GET /details.php?id=X

合规性（AGENTS.md R2 风险）：
- cf_clearance 由用户浏览器登录后获取，属于"用户授权合法访问"
- 不尝试合成 downhash（不破解签名算法）
- 频率与其他适配器一致（10 分钟 cron 间隔、单次 ≤3 页）
"""

from __future__ import annotations

import contextlib
import re

import httpx
from bs4 import BeautifulSoup

from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFetchError,
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
#: hddolby 列表页观察到的 free 图标：pro_free, pro_free2up, pro_50pctdown
_PRO_IMG_MAP: dict[str, str] = {
    "pro_free": "free",
    "pro_free2up": "twoupfree",
    "pro_2up": "twoup",
    "pro_50pctdown": "halfdown",
    "pro_50pctdown2up": "twouphalfdown",
    "pro_30pctdown": "thirtypercent",
}

#: CSS class 白名单（用于 free font class 过滤）
_PROMO_FONT_CLASSES: frozenset[str] = frozenset(
    {"free", "twoupfree", "twoup", "halfdown", "twouphalfdown", "thirtypercent"}
)

#: 详情页锚点 href 中的 id 提取
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)", re.IGNORECASE)

#: hddolby 详情页 download URL 模式（downhash 多段式签名，**不可合成**）：
#: ``<a href="download.php?id=258522&downhash=ccc99235x1792248964x36026x258522x32027400...">``
#: 兼容 ``&amp;`` HTML 实体；downhash 分段以 ``x`` 分隔（32hex MD5 类签名）
_DOWNHASH_DL_RE = re.compile(
    r"download\.php\?id=(\d+)(?:&amp;|&)downhash=([a-f0-9x]+)",
    re.IGNORECASE,
)

#: 列表页 ``<a href="details.php?id=N"><b>标题</b></a>`` 内的标题（hddolby 模式）
_TITLE_B_RE = re.compile(r"<b[^>]*>(.*?)</b>", re.IGNORECASE | re.DOTALL)

#: 绝对时刻（title 属性）：``2026-10-11 12:41:28``
_ABS_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

#: 登录态检测
_LOGGED_IN_RE = re.compile(r"(userdetails\.php|logout\.php|takelogout\.php)", re.IGNORECASE)
_LOGIN_REDIRECT_RE = re.compile(r"login\.php", re.IGNORECASE)


class HddolbyAdapter(SiteAdapter):
    """HDDolby (www.hddolby.com) NexusPHP cookie 鉴权适配器。"""

    adapter_type = "hddolby"

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
        """2. 抓取种子列表：GET /torrents.php?spstate=0，BS4 解析。

        spstate=0 在 hddolby 语义为"促销/免费"（100 条/页），
        共 ~1280+ 页（12.8 万种）。fetch_all_sites 拉前 3 页（300 条）入库。
        """
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=0&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            # 拉取失败必须抛出：返回 [] 表示"站点确实没有种子"，
            # 与"站点挂了"混淆会让作业统计看不出故障站
            raise SiteFetchError(f"HDDolby 拉取失败：{exc}") from exc
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"HDDolby 拉取 HTTP {resp.status_code}"
                f"（第 {page_number} 页）"
            )
        items = HddolbyAdapter._parse_html(resp.text)
        return items[:_MAX_ITEMS_PER_PULL]

    # ------------------------------------------------------------ 2.5 get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """返回 ``{base}/download.php?id=X&downhash=...`` 完整签名 URL。

        hddolby 的 downhash 多段签名（与 hdhome 同类）**不可合成**，
        必须从 ``details.php?id=X`` 的 form action 或 ``<a>`` href 中解析。
        """
        if not item_id:
            return None
        try:
            resp = await self._client.get(self._detail_url(item_id), timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("HDDolby 详情页拉取失败 %s: %s", item_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        m = _DOWNHASH_DL_RE.search(resp.text)
        if not m:
            return None
        signed_id, downhash = m.group(1), m.group(2)
        return (
            f"{self._base_url()}/download.php"
            f"?id={signed_id}&downhash={downhash}"
        )

    @staticmethod
    def _parse_html(html: str) -> list[FeedItem]:
        """BS4 解析 hddolby spstate=0 列表页（fetch_all_sites 拉全量入库）。

        hddolby 列表页 DOM 结构（与 hdsky 略有差异）：
        - 表格是 ``<table class="mainouter">`` 内嵌 ``<table class="main">``
        - 每个 torrent 占一个 ``<tr>``，含多 ``<td>``：
          * td0 = 分类图标 ``<img class="c_xxx">``
          * td1 = 标题 ``<a href="details.php?id=N"><b>标题</b></a>``
                  + 促销标记 ``<img class="pro_free">`` + onmouseover 内的剩余时间
          * td2..td4 = download / zip / bookmark 操作
          * td5 = 评论数
          * td6 = 发布时间 ``<span title="...">短相对时长</span>``
          * td7 = size ``<td>X.XX<br />GB</td>``
          * td8 = seeders / leechers

        解析流程：``find_all('tr')`` 全文档扫，定位含 details 锚点的 tr，
        从同一 tr 提取 title / size / free / published_at。
        """
        soup = BeautifulSoup(html, "lxml")
        # 定位主表格（hddolby 用 class="mainouter"）；无则 fallback 全文档
        table = soup.find("table", class_="mainouter") or soup.find("table", class_="torrents")
        if table is None:
            table = soup

        items: list[FeedItem] = []
        seen: set[str] = set()

        for tr in table.find_all("tr"):
            # 找 details 锚点（hddolby 模式：<a href="details.php?id=N"><b>...</b></a>）
            a = tr.find("a", href=_DETAILS_ID_RE)
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

            # ---------------- title: 优先 <a title="...">，其次 <b> 标签
            title: str = str(a.get("title") or "").strip()
            if not title:
                bm = a.find("b")
                if bm is not None:
                    title = bm.get_text(strip=True)
            if not title:
                # 最后 fallback：a 标签全文本
                title = a.get_text(strip=True)

            # ---------------- category: <img class="c_xxx" title="...">
            category: str | None = None
            cat_img = tr.find("img", class_=re.compile(r"^c\d?_"))
            if cat_img is not None:
                cat_title = str(cat_img.get("title") or cat_img.get("alt") or "")
                category = cat_title.strip() or None

            # ---------------- size: 扫 td 匹配 <td>X.XX<br />UNIT</td>
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

            # ---------------- published_at: 发布时间单元格
            published_at = parse_publish_time(tr)

            # ---------------- 免费标记：img class="pro_XXX" → 标准 class
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
            # font class="free|twoupfree|..." 标记
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

        流程：
        1. GET /details.php?id=X 提取 ``downhash=...`` 完整 URL
        2. GET 该完整 URL 拿 bencoded bytes
        """
        if not self._cookies:
            raise _TorrentDownloadError("缺少 cookies")
        try:
            resp = await self._client.get(
                self._detail_url(item_id),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise _TorrentDownloadError(f"详情页拉取失败：{exc}") from exc
        if resp.status_code >= 400:
            raise _TorrentDownloadError(f"详情页 HTTP {resp.status_code}")
        m = _DOWNHASH_DL_RE.search(resp.text)
        if not m:
            raise _TorrentDownloadError(f"未找到 downhash 签名 download URL（id={item_id}）")
        signed_url = (
            f"{self._base_url()}/download.php"
            f"?id={m.group(1)}&downhash={m.group(2)}"
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

        NexusPHP 不暴露删除/违规标记 → 仅基于 HTTP 状态判断：
        - 200 → EXISTS
        - 404 → DELETED
        - 302/401/403 → UNKNOWN（cookie 过期重定向到 login.php）
        - 其他 → UNKNOWN
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

        解析：title（h1 锚点）+ size（保留在 raw_html 中）+ download_url（downhash 签名 URL）。
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
        title_text = (
            sel.css("h1::text").get()
            or sel.css("h1 font::text").get()
            or sel.css('a[title]::attr(title)').get()
        )
        m = _DOWNHASH_DL_RE.search(html)
        download_url = None
        if m:
            download_url = (
                f"{self._base_url()}/download.php"
                f"?id={m.group(1)}&downhash={m.group(2)}"
            )
        return TorrentDetail(
            item_id=item_id,
            title=title_text.strip() if title_text else None,
            download_url=download_url,
            raw_html=html,
        )


    # ----------------------- FR-1.6 站点用户数据（精简版） -----------------------

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """hddolby 走 ``/userdetails.php``（NexusPHP 模板）。"""
        if not self._cookies:
            return None
        try:
            resp = await self._client.get(
                f"{self._base_url()}/userdetails.php",
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("hddolby get_user_snapshot HTTP 失败：%s", exc)
            return None
        if resp.status_code >= 400:
            return None
        return nexus_php.parse_user_snapshot(resp.text)



    async def close(self) -> None:
        await self._client.aclose()





class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HddolbyAdapter", "_TorrentDownloadError"]
