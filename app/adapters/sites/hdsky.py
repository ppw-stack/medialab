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
        """2. 抓取免费种子列表：GET /torrents.php?spstate=0，parsel CSS 解析。

        HDSky 使用标准 NexusPHP 嵌套表格，spstate=0 聚合所有促销种子
        （永久 + 限时 + 折扣）。**注意**：旧版本用 spstate=7（仅30%折扣页），
        错误地把所有种子视为无 end_at 的 30% off——已修复为 spstate=0 抓全量。

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
        """parsel CSS 解析 HDSky spstate=7 页面。

        每个 torrent 跨多个嵌套 ``<tr>``，所以**整页一次性 parse**（不切片）。
        对每个 ``a[href*="details.php?id="][title]`` 锚点，向上溯最近 ``<tr>`` 找促销信号。
        """
        sel = Selector(text=html)
        items: list[FeedItem] = []
        seen: set[str] = set()

        for a in sel.css('a[href^="details.php?id="][title]'):
            href = a.attrib.get("href", "")
            m = _DETAILS_ID_RE.search(href)
            if not m or "userdetails.php" in href:
                continue
            item_id = m.group(1)
            if item_id in seen:
                continue
            seen.add(item_id)
            title = a.attrib.get("title", "").strip()

            # 向上溯最近 <tr>（HDSky torrent 行通常在 <tr> 内）
            tr = a.xpath("./ancestor::tr[1]").get("")
            if not tr:
                continue

            tr_sel = Selector(text=tr)

            # 免费标记：img class="pro_XXX" → 标准 class
            pro_imgs = tr_sel.css('img[class*="pro_"]::attr(class)').getall()
            mapped_pro = []
            for cls in pro_imgs:
                # cls 形如 "pro_free"，可能有多个 class，取首个匹配
                for c in cls.split():
                    if c.startswith("pro_"):
                        mapped_pro.append(_PRO_IMG_MAP.get(c, c))
                        break

            # 限时/2X 标记：<font class="free|twoupfree|...">
            font_classes = tr_sel.css("font::attr(class)").getall()
            mapped_font = [c for c in font_classes if c in _PROMO_FONT_CLASSES]

            free_classes = mapped_pro + mapped_font

            # 限时绝对时刻：<span title="2026-10-11 12:41:28">
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

            # spstate=0 是全站列表，包含大量无免费标志的普通 torrent。
            # 只入候选池带免费标记的条目（避免噪声）。
            if free_payload is None:
                continue

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
                    torrent_url=None,  # 由 _fill_download_signs 后填（含 sign）
                    free_payload=free_payload,
                )
            )
            if len(items) >= limit:
                break

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