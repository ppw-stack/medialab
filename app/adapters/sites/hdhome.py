"""HDHome 站点适配器（FR-1.5）。

hdhome.org 是 NexusPHP 框架 + BlueGene 主题的标准 PT 站，基于 cookie 鉴权。
实测 DOM 结构：
- 列表页：GET /torrents.php?spstate=0，SSR 标准 NexusPHP 表格
- 详情页：GET /details.php?id=X，"种子链接" 节点含含 downhash 的完整下载
  URL（不可合成）
- 下载：downhash 签名（不可合成），必须从详情页 HTML 解析完整 URL 后 GET

契约方法（5 个）：
1. check_reachability: GET /index.php，检查登录态
2. search_torrents: GET /torrents.php?spstate=0，BS4 解析
3. download_torrent: GET /details.php?id=X → 解析 downhash URL → 拿 bencoded bytes
4. check_status: GET /details.php?id=X，看 HTTP 200/404
5. get_torrent_detail: GET /details.php?id=X

免费标记格式（标准 NexusPHP pro_* class）：
- ``<img class="pro_free">`` 限时免费
- ``<img class="pro_free2up">`` 限时免费 + 2X 上传
- ``<img class="pro_50pctdown">`` 50% 下载折扣（HDHome 自定义）

限时剩余时间：``<span title="YYYY-MM-DD HH:MM:SS">剩余文案</span>``

spstate 含义（实测）：
- 0 = 全量（永久 + 限时 + 折扣 + 普通）
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from app.adapters.sites.base import (
    BreakerTripped,
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


#: HDHome 的 pro_* img class → 标准 free class
#: HDHome 用 ``pro_50pctdown`` 替代标准 ``pro_halfdown``
_PRO_IMG_MAP: dict[str, str] = {
    "pro_free": "free",
    "pro_free2up": "free2up",
    "pro_2upfree": "2upfree",
    "pro_2up": "2up",
    "pro_halfdown": "halfdown",
    "pro_50pctdown": "halfdown",  # HDHome 自定义别名
    "pro_2uphalfdown": "2uphalfdown",
    "pro_30pctdown": "thirtypercent",
}

#: 列表页详情链接正则
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)")

#: 详情页 downhash 完整下载 URL 正则
_DOWNLOAD_URL_RE = re.compile(
    r'href="(https?://hdhome\.org/download\.php\?id=\d+&downhash=[^"]+)"'
)

#: 列表页"种子链接"行的剩余时间标题正则（Y/M/D H:M:S）
_ABS_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$")

#: 类名 ID 正则（c_xxx）
_CAT_CLASS_RE = re.compile(r"^c\d?_")


def _parse_cookies(raw: str | None) -> dict[str, str]:
    """c_secure_uid=...; c_secure_pass=...; ... → dict。"""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class HdhomeAdapter(SiteAdapter):
    """HDHome (hdhome.org) NexusPHP cookie 适配器。"""

    adapter_type = "hdhome"

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
            cookies=_parse_cookies(cookies),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    # ------------------------------------------------------------ 内部
    def _base_url(self) -> str:
        """统一 base URL：从 profile.domain 拼装，保留 scheme（domain 可含 https://）。"""
        domain = (self.profile.domain or "").strip()
        if not domain:
            return ""
        if not domain.startswith("http://") and not domain.startswith("https://"):
            domain = "https://" + domain
        return domain.rstrip("/")

    @staticmethod
    def _find_details_id(href: str) -> str | None:
        m = _DETAILS_ID_RE.search(href)
        return m.group(1) if m else None

    @staticmethod
    def _extract_download_url_from_detail(html: str) -> str | None:
        """从详情页 HTML 提取含 downhash 的下载 URL。

        锚点位置："种子链接" 行的 <a href="...downhash=...">。
        downhash 不可合成（32-char 内部 hash），必须从 HTML 解析。
        """
        m = _DOWNLOAD_URL_RE.search(html)
        return m.group(1) if m else None

    # ------------------------------------------------------------ 1. check_reachability
    async def check_reachability(self) -> SiteHealth:
        url = f"{self._base_url()}/index.php"
        try:
            resp = await self._client.get(url)
        except (httpx.HTTPError, OSError) as exc:
            return SiteHealth(reachable=False, logged_in=False, detail=str(exc))
        if resp.status_code >= 400:
            return SiteHealth(
                reachable=False, logged_in=False,
                detail=f"HTTP {resp.status_code}",
            )
        # 未登录：follow_redirects=True 后 final URL 落到 login.php
        final_url = str(resp.url)
        if final_url.endswith("/login.php") or "/login.php?" in final_url:
            logged_in = False
        else:
            html = resp.text
            logged_in = (
                "userdetails.php" in html or "logout.php" in html
                or 'id="info_block"' in html
            )
        return SiteHealth(
            reachable=True,
            logged_in=logged_in,
            detail=("logged in" if logged_in else "redirected to login"),
        )

    # ------------------------------------------------------------ 2. search_torrents
    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 200,
        page_number: int = 1,
    ) -> list[FeedItem]:
        """spstate=0 拉全量种子列表（BS4 解析标准 NexusPHP 表格）。"""
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=0&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("HDHome 拉取失败：%s", exc)
            return []
        if resp.status_code >= 400:
            logger.warning("HDHome 拉取 HTTP %d", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", class_="torrents")
        if table is None:
            table = soup

        items: list[FeedItem] = []
        seen: set[str] = set()

        for tr in table.find_all("tr"):
            # 找 details 锚点（必须带 title 属性，避免导航条干扰）
            a = tr.find("a", href=_DETAILS_ID_RE, attrs={"title": True})
            if a is None:
                continue
            href: str = str(a.get("href") or "")
            item_id = self._find_details_id(href)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            title: str = str(a.get("title") or "").strip()
            if not title:
                title = a.get_text(strip=True)

            # category: <img class="c_xxx" title="...">
            category: str | None = None
            cat_img = tr.find("img", class_=_CAT_CLASS_RE)
            if cat_img is not None:
                cat_title = str(cat_img.get("title") or cat_img.get("alt") or "")
                category = cat_title.strip() or None

            # size: <td>X.XX<br />GB</td> 或 MB/KB/TB
            size_bytes: int = 0
            for td in tr.find_all("td"):
                text = td.get_text(" ", strip=True)
                m = re.match(r"^([\d.]+)\s*(GB|MB|KB|TB)$", text)
                if m:
                    val = float(m.group(1))
                    unit = m.group(2)
                    mult = {
                        "KB": 1024, "MB": 1024**2,
                        "GB": 1024**3, "TB": 1024**4,
                    }[unit]
                    size_bytes = int(val * mult)
                    break

            # published_at: 最后一个 span title 含绝对时刻（hdhome DOM 顺序：remaining_abs 在前，published_at 在后）
            published_at: str | None = None
            for span in reversed(tr.find_all("span", attrs={"title": True})):
                tval = str(span.get("title") or "").strip()
                if _ABS_DATETIME_RE.match(tval):
                    published_at = parse_abs_datetime(tval)
                    break

            # free class: <img class="pro_XXX"> 或 <font class="pro_XXX">
            free_classes: list[str] = []
            for img in tr.find_all("img", class_=re.compile(r"^pro_")):
                classes: list[str] = list(img.get("class") or [])
                for c in classes:
                    if c.startswith("pro_") and c in _PRO_IMG_MAP:
                        mapped = _PRO_IMG_MAP[c]
                        if mapped not in free_classes:
                            free_classes.append(mapped)
                        break
            for font in tr.find_all("font", class_=re.compile(r"^pro_")):
                classes = list(font.get("class") or [])
                for c in classes:
                    if c.startswith("pro_") and c in _PRO_IMG_MAP:
                        mapped = _PRO_IMG_MAP[c]
                        if mapped not in free_classes:
                            free_classes.append(mapped)
                        break

            # remaining_abs: 限时种 <span title="绝对时刻"> 排除发布时间
            remaining_abs: str | None = None
            for span in tr.find_all("span", attrs={"title": True}):
                tval = str(span.get("title") or "").strip()
                if not _ABS_DATETIME_RE.match(tval):
                    continue
                if (published_at is not None
                        and parse_abs_datetime(tval) == published_at):
                    continue  # 这是发布时间
                remaining_abs = tval
                break

            # promo_texts: 文本里"X%"提取（用于 NEUTRAL 折扣精确定级）
            promo_texts: list[str] = []
            for span in tr.find_all("span", class_=re.compile(r"^pro_")):
                ttxt = span.get_text(strip=True)
                if ttxt and "%" in ttxt:
                    promo_texts.append(ttxt)

            free_payload: dict[str, object] | None = None
            if free_classes or remaining_abs:
                free_payload = {
                    "free_classes": free_classes,
                    "remaining_abs": remaining_abs,
                }
                if promo_texts:
                    free_payload["promo_texts"] = promo_texts

            items.append(
                FeedItem(
                    title=title,
                    link=f"details.php?id={item_id}",
                    item_id=item_id,
                    info_hash=None,  # NexusPHP 列表页不直接给 hash
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
        """先拉详情页解析 downhash 完整 URL，再 GET 拿 bencoded bytes。"""
        if not item_id:
            raise ValueError("item_id 不能为空")
        url = await self.get_torrent_url(item_id)
        if not url:
            raise BreakerTripped(f"无法从详情页提取下载 URL id={item_id}")
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise BreakerTripped(f"HDHome 下载失败 id={item_id}: {exc}") from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"HDHome HTTP {resp.status_code} id={item_id}")
        resp.raise_for_status()
        if not resp.content or not resp.content.startswith(b"d"):
            raise BreakerTripped(f"HDHome 响应不是 bencoded torrent id={item_id}")
        return resp.content

    # ------------------------------------------------------------ 4. check_status
    async def check_status(self, item_id: str) -> TorrentBrief:
        url = f"{self._base_url()}/details.php?id={item_id}"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("HDHome 状态检查失败 id=%s: %s", item_id, exc)
            return TorrentBrief.UNKNOWN
        deleted_markers = (
            resp.status_code == 404
            or "没有找到种子" in resp.text
            or "种子不存在" in resp.text
        )
        if deleted_markers:
            return TorrentBrief.DELETED
        if resp.status_code >= 400:
            return TorrentBrief.UNKNOWN
        return TorrentBrief.EXISTS

    # ------------------------------------------------------------ 5. get_torrent_detail
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        url = f"{self._base_url()}/details.php?id={item_id}"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            return TorrentDetail(item_id=item_id, error=str(exc))
        if resp.status_code >= 400:
            return TorrentDetail(
                item_id=item_id, error=f"HTTP {resp.status_code}",
            )
        html = resp.text
        soup = BeautifulSoup(html, "lxml")

        # title: <title>HDHome :: 种子详情 "X" 高清家园 - Powered by NexusPHP</title>
        title_tag = soup.find("title")
        title: str | None = None
        if title_tag:
            ttxt = title_tag.get_text()
            m = re.search(r'种子详情\s*[&"]?([^"&]+?)[&"]?\s*高清家园', ttxt)
            title = m.group(1).strip() if m else ttxt.strip()

        # size: 找 X.XX GB/MB 文本
        size_bytes: int = 0
        size_text = soup.find(string=re.compile(r"^[\d.]+\s*(GB|MB|KB|TB)$"))
        if size_text:
            m = re.match(r"^([\d.]+)\s*(GB|MB|KB|TB)$", size_text.strip())
            if m:
                val = float(m.group(1))
                unit = m.group(2)
                mult = {
                    "KB": 1024, "MB": 1024**2,
                    "GB": 1024**3, "TB": 1024**4,
                }[unit]
                size_bytes = int(val * mult)

        # free_payload: 复用列表页的 pro_* class 提取
        free_classes: list[str] = []
        for img in soup.find_all("img", class_=re.compile(r"^pro_")):
            classes = list(img.get("class") or [])
            for c in classes:
                if c.startswith("pro_") and c in _PRO_IMG_MAP:
                    mapped = _PRO_IMG_MAP[c]
                    if mapped not in free_classes:
                        free_classes.append(mapped)
                    break

        # remaining_abs / published_at
        remaining_abs: str | None = None
        published_at: str | None = None
        for span in soup.find_all("span", attrs={"title": True}):
            tval = str(span.get("title") or "").strip()
            if not _ABS_DATETIME_RE.match(tval):
                continue
            if published_at is None and remaining_abs is None:
                published_at = parse_abs_datetime(tval)
            elif remaining_abs is None:
                remaining_abs = tval

        free_payload: dict[str, object] | None = None
        if free_classes or remaining_abs:
            free_payload = {
                "free_classes": free_classes,
                "remaining_abs": remaining_abs,
            }

        # download_url: 解析"种子链接" <a href="...downhash=...">
        download_url = self._extract_download_url_from_detail(html)

        return TorrentDetail(
            item_id=item_id,
            title=title,
            size=size_bytes,
            download_url=download_url,
            free_payload=free_payload,
            raw_html=html,
        )

    # ------------------------------------------------------------ 6. get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """拉详情页解析含 downhash 的完整下载 URL。"""
        url = f"{self._base_url()}/details.php?id={item_id}"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("HDHome 解析下载 URL 失败 id=%s: %s", item_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        return self._extract_download_url_from_detail(resp.text)

    # ------------------------------------------------------------ 7. close
    async def close(self) -> None:
        await self._client.aclose()
