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
存储在 sites.cookies，由 AdapterFactory.site 注入。
不自动登录（AGENTS.md：不引入验证码绕过）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import cast

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
    parse_abs_datetime,
    parse_size_text,
)
from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: 详情页锚点 href 中的 id 提取
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)", re.IGNORECASE)

#: 详情页 anchor href 里的 passkey download URL（hhanclub 无 https= 参数）
#: ``https://hhanclub.net/download.php?id=203626&passkey=9b42c1c53f661c86a250814b5e51149f``
_SIGNED_DL_RE = re.compile(
    r"download\.php\?id=(\d+)&passkey=([a-f0-9]+)",
    re.IGNORECASE,
)

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


def _parse_hhanclub_snapshot(html: str) -> SiteUserSnapshot | None:
    """hhanclub HH v2 fork navbar 解析（专用）。

    模板示例::

        <img ... alt="上传">&nbsp;89.307 TB
        <img ... alt="下载">&nbsp;15.200 TB
        <a href="userdetails.php?id=N&action=2"><img ... alt="做种数">&nbsp;150
        <a href="userdetails.php?id=N&action=3"><img ... alt="下载数">&nbsp;0

    其他字段（分享率/认领/H&R/勋章）不影响 4 业务字段，无需提取。
    """
    from app.adapters.sites.base import parse_size_text

    def _grab(icon_alt: str, parser: Callable[[str], int]) -> int:
        """抓 ``<img alt=ICON>&nbsp;VALUE`` 中 VALUE，parser 转 int。"""
        # lookahead `(?=<|$)`：数值后紧跟标签或文本尾均可（原来只允许 `<`，
        # 若 navbar 把上传量放在最后一个 <td> 无后续标签会漏抓）
        m = re.search(
            r'alt=["\']' + re.escape(icon_alt) + r'["\'][^>]*>'
            r'\s*(?:&nbsp;|\xa0)?\s*([^<\n]+?)(?=<|$)',
            html,
        )
        if not m:
            return 0
        try:
            return int(parser(m.group(1).strip()))
        except (TypeError, ValueError):
            return 0

    def _size(s: str) -> int:
        v = parse_size_text(s)
        return v if v else parse_size_text(s + " B")

    def _num(s: str) -> int:
        return int(float(s.replace(",", "")) or 0)

    upload = _grab("上传", _size)
    download = _grab("下载", _size)
    seeding_count = _grab("做种数", _num)
    leeching_count = _grab("下载数", _num)

    if not (upload or download or seeding_count or leeching_count):
        return None
    return SiteUserSnapshot(
        upload=upload,
        download=download,
        seeding_count=seeding_count,
        leeching_count=leeching_count,
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
        #: 列表页 ``<a href="...download.php?id=X&passkey=Y">`` 抽出的下载 URL
        #: （item_id → 绝对 URL），由 :meth:`search_torrents` 刷新。
        #: ``get_torrent_url`` 优先查此处 → 0 HTTP；miss 时回退详情页。
        self._download_url_cache: dict[str, str] = {}

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
    def _extract_download_urls(html: str, base_url: str) -> dict[str, str]:
        """从列表页 HTML 抽 ``download.php?id=X&passkey=Y`` anchor → 绝对 URL。

        复用 :data:`_SIGNED_DL_RE`（与详情页 anchor 同源 regex）。同 item_id
        出现多次（理论上不会，HHan 列表页 anchor 无 ``&type=zip`` 变体）
        取首条。
        """
        out: dict[str, str] = {}
        for m in _SIGNED_DL_RE.finditer(html):
            item_id, passkey = m.group(1), m.group(2)
            if item_id in out:
                continue
            out[item_id] = f"{base_url}/download.php?id={item_id}&passkey={passkey}"
        return out

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
        """2. 抓取活种列表：GET /torrents.php?spstate=0，BS4 解析。

        与其他 5 站一致用 spstate=0（所有活种）。HHan 自定义主题的
        spstate=7（所有免费）是 JS 动态加载，HTML 不含 torrent 链接。
        改用 spstate=0 拉全量活种 + BS4 解析 free_classes：
        - 永久免费：free_type=PERMANENT
        - 限免（pro_free/pro_2xfree 等）：free_type=TIMED
        - 折扣（pro_50pctdown 等）：free_type=NEUTRAL
        - 普通：free_type=UNKNOWN

        注：HHan 限免块（剩余时间）藏在 onmouseover 鼠标提示里（Tailwind
        自定义主题），BS4 解析时已提取。HDSky 的 spstate=0 行为也是
        "所有活种"，对齐为标准做法。

        返回的条目不带下载 URL；下载时调 :meth:`get_torrent_url` 抓详情页
        提取 ``passkey=...`` 拼 download URL。
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
            raise SiteFetchError(f"HHanClub 拉取失败：{exc}") from exc
        if resp.status_code in (403, 429):
            # 限流/封禁 → 熔断信号（FR-2.2），不是普通拉取失败。
            # 403 可能是 cookie 失效而非限流，熔断时长减半（见 jobs.py）。
            raise BreakerTripped(
                f"HHanClub 拉取返回 {resp.status_code}，需熔断"
            )
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"HHanClub 拉取 HTTP {resp.status_code}"
                f"（第 {page_number} 页）"
            )
        items = HhanclubAdapter._parse_spstate0_html(resp.text)
        # 同步刷新 download URL cache（供 get_torrent_url 0-HTTP 命中）
        self._download_url_cache = HhanclubAdapter._extract_download_urls(
            resp.text, self._base_url()
        )
        return items

    # ------------------------------------------------------------ 2.5 get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """返回 ``{base}/download.php?id=X&passkey=Y`` 绝对 URL。

        流程：
        1. 查 :attr:`_download_url_cache`（由 :meth:`search_torrents` 从列表页
           anchor 维护）→ 命中返回，0 HTTP
        2. miss → 回退抓详情页抽 ``passkey=...`` 拼 URL（1 HTTP）
        """
        if not item_id:
            return None
        cached = self._download_url_cache.get(item_id)
        if cached:
            return cached
        # miss: 详情页兜底（与上一版一致）
        try:
            resp = await self._client.get(self._detail_url(item_id), timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("HHanClub 详情页拉取失败 %s: %s", item_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        m = _SIGNED_DL_RE.search(resp.text)
        if not m:
            return None
        signed_id, passkey = m.group(1), m.group(2)
        return (
            f"{self._base_url()}/download.php"
            f"?id={signed_id}&passkey={passkey}"
        )

    @staticmethod
    def _parse_spstate0_html(html: str) -> list[FeedItem]:
        """BS4 解析 HHanClub spstate=0 页面（所有活种 SSR）。

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
            href: str = str(a.get("href") or "")
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
            title: str = str(a.get("title") or a.get_text(strip=True) or "").strip()

            # 向上找 torrent 卡片容器：HHanClub 为 Tailwind 自定义主题，
            # 标题锚点与 size/cat/pub 的 ``<div class="torrent-info">`` 不在同一分支，
            # 最近公共祖先为 ``<div class="... torrent-table-sub-info">``。
            # 因此以"最近的、包含 size 子 div 的祖先"为卡片容器。
            container: Tag | None = None
            for depth, ancestor in enumerate(a.parents):
                # BS4 parents 迭代到 None 时停止迭代而非 yield None，
                # 故 `ancestor is None` 永不为真——只需限制深度
                if depth > 8:
                    break
                if ancestor.find("div", class_="torrent-info-text-size") is not None:
                    container = ancestor
                    break
            if container is None:
                fallback = _find_torrent_container(a)
                if isinstance(fallback, Tag):
                    container = fallback

            # ---------------- size / category / published_at: Tailwind 主题
            # <div class="torrent-info-text torrent-info-text-size">32.23 GB</div>
            # <div class="torrent-info-text torrent-info-text-added"
            #      ><span title="2026-10-09 00:54:32">3天18时</span></div>  (发布时间)
            size_bytes = 0
            category: str | None = None
            published_at = None
            if container is not None:
                for div in container.find_all(
                    "div", class_=re.compile(r"torrent-info-text")
                ):
                    div_classes: list[str] = list(div.get("class") or [])
                    text: str = div.get_text(strip=True)
                    if "torrent-info-text-size" in div_classes:
                        size_bytes = parse_size_text(text)
                    elif "torrent-info-text-cat" in div_classes and text:
                        category = text
                    elif "torrent-info-text-added" in div_classes:
                        # added 显示相对时间（"3天18时"），内层 span 带绝对时刻 title
                        sp = div.find("span", attrs={"title": True})
                        if sp is not None:
                            published_at = parse_abs_datetime(
                                str(sp.get("title") or "")
                            )

            # ---------------- 免费标记：promotion-tag-*（HHan 主题）/ pro_* / free 等
            free_classes: list[str] = []
            promo_texts: list[str] = []
            _hh_seen: set[str] = set()
            remaining_abs: str | None = None
            if container is not None:
                for tag in container.find_all(class_=True):
                    classes: list[str] = list(tag.get("class") or [])
                    for cls in classes:
                        if cls.startswith("promotion-tag-"):
                            # promotion-tag-free / -2xfree / -50 / -30 → free/2xfree/50/30
                            # HHan v2 fork 用非标准名（2xfree/2up），
                            # 映射到 NexusPHP 标准 class 名以兼容 WindowParser
                            _HH_TAG_MAP: dict[str, str] = {
                                "2xfree": "twoupfree",
                                "2up": "twoup",
                            }
                            tag_key = cls[len("promotion-tag-"):]
                            tag_key = _HH_TAG_MAP.get(tag_key, tag_key)
                            if tag_key not in _hh_seen:
                                free_classes.append(tag_key)
                                _hh_seen.add(tag_key)
                            text = tag.get_text(strip=True)
                            if text and "%" in text:
                                promo_texts.append(text)
                        elif cls in _PROMO_CLASSES and cls not in _hh_seen:
                            free_classes.append(cls)
                            _hh_seen.add(cls)
                            text = tag.get_text(strip=True)
                            if text and "%" in text:
                                promo_texts.append(text)
                        elif cls.startswith("pro_"):
                            mapped = cls[4:]
                            if mapped in _PROMO_CLASSES and mapped not in _hh_seen:
                                free_classes.append(mapped)
                                _hh_seen.add(mapped)
                                text = tag.get_text(strip=True)
                                if text and "%" in text:
                                    promo_texts.append(text)

                # 限时剩余时刻：<span title="YYYY-MM-DD HH:MM:SS">（排除发布时间 span）
                for span in container.find_all("span", attrs={"title": True}):
                    t: str = str(span.get("title") or "")
                    if not _ABS_DATETIME_RE.match(t):
                        continue
                    if published_at is not None and parse_abs_datetime(t) == published_at:
                        continue
                    remaining_abs = t
                    break

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

        注：裸 ``/download.php?id=X`` 在 HHanClub 永远 404，必须带 passkey。
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
        m = _SIGNED_DL_RE.search(resp.text)
        if not m:
            raise _TorrentDownloadError(f"未找到 passkey download URL（id={item_id}）")
        signed_url = (
            f"{self._base_url()}/download.php"
            f"?id={m.group(1)}&passkey={m.group(2)}"
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

        HHan Tailwind 主题用 BS4 解析：title（嵌套 div/span）
        + download_url（anchor href 中的 passkey URL）。
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
        from bs4 import BeautifulSoup as _BS

        soup = _BS(html, "lxml")
        title_text: str | None = None
        # h1 标签
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title_text = h1.get_text(strip=True)
        else:
            # 找带 title 属性的 a
            a = soup.find("a", attrs={"title": True})
            if a:
                title_attr = a.get("title")
                if isinstance(title_attr, str):
                    title_text = title_attr

        m = _SIGNED_DL_RE.search(html)
        download_url = None
        if m:
            download_url = (
                f"{self._base_url()}/download.php"
                f"?id={m.group(1)}&passkey={m.group(2)}"
            )
        return TorrentDetail(
            item_id=item_id,
            title=title_text,
            download_url=download_url,
            raw_html=html,
        )


    # ----------------------- FR-1.6 站点用户数据（精简版） -----------------------

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """hhanclub 走 ``/userdetails.php``。

        hhanclub (HH v2 fork) navbar 模板跟标准 NexusPHP 1.5 不同：
        用 ``<img alt="上传">`` / ``<img alt="下载">`` / ``<img alt="做种数">``
        / ``<img alt="下载数">`` 紧跟数值；做种/下载中数字在 anchor 链接里。

        不用 nexus_php.parse_user_snapshot（标准 NexusPHP 模板匹配不上），
        这里用 :func:`_parse_hhanclub_snapshot` 单点适配。
        """
        if not self._cookies:
            return None
        try:
            resp = await self._client.get(
                f"{self._base_url()}/userdetails.php",
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("hhanclub get_user_snapshot HTTP 失败：%s", exc)
            return None
        if resp.status_code >= 400:
            return None
        return _parse_hhanclub_snapshot(resp.text)



    async def close(self) -> None:
        await self._client.aclose()





def _find_torrent_container(anchor: object) -> object | None:
    """向上查找 torrent 容器（最近的 <tr> 或最近的 <div>）。"""
    node = cast(Tag, anchor)
    # 优先：最近 <tr>（标准 NexusPHP 表格行）
    tr = node.find_parent("tr")
    if tr is not None:
        return tr
    # 兜底：最近的 <div>（HHan Tailwind 自定义主题，每个 torrent 卡片是一个 div）
    # 限制深度 8 层，避免把整个 body 当作 container
    parent = node.parent
    seen: set[int] = set()
    depth = 0
    max_depth = 8
    while parent is not None and id(parent) not in seen and depth < max_depth:
        seen.add(id(parent))
        depth += 1
        # 找到块级容器（div 或 article 或 section）
        if getattr(parent, "name", None) in {"div", "article", "section"}:
            return parent
        parent = parent.parent
    return None


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HhanclubAdapter", "_TorrentDownloadError"]