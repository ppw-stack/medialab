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
from bs4 import BeautifulSoup

from app.adapters.sites.base import (
    BreakerTripped,
    CheckinResult,
    CheckinStatus,
    FeedItem,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
    parse_abs_datetime,
    parse_publish_time,
    parse_size_from_br,
)
from app.adapters.sites.parsers import attendance, nexus_php
from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.ai import AIClientError, get_ai_reader
from app.core.logging import get_logger
from app.core.metrics import ai_captcha_reads

logger = get_logger(__name__)

#: 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: 签到重试次数（每次重试换一张新验证码）。
#:
#: 服务端不限制重试，实测 AI 读码准确率足够高（放大后 12/12），
#: 3 次是"偶发误读"的兜底，正常一次即成。
_CHECKIN_MAX_ATTEMPTS = 3

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
        page_size: int = 200,
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

        返回的条目不带下载 URL；下载时调 :meth:`get_torrent_url` 抓详情页
        提取 ``t=...&sign=...`` 签名参数再拼 download URL（裸 ``/download.php?id=X``
        永远 404，必须带签名）。
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
            raise SiteFetchError(f"HDSky 拉取失败：{exc}") from exc
        if resp.status_code in (403, 429):
            # 限流/封禁 → 熔断信号（FR-2.2），不是普通拉取失败。
            # 403 可能是 cookie 失效而非限流，熔断时长减半（见 jobs.py）。
            raise BreakerTripped(
                f"HDSky 拉取返回 {resp.status_code}，需熔断"
            )
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"HDSky 拉取 HTTP {resp.status_code}"
                f"（第 {page_number} 页）"
            )
        if "login.php" in str(resp.url):
            # 凭据失效：站点把受保护页面 302 到登录页（跟随后是 200）——
            # 不抛错的话会解析出 0 条，被当成「站点确实没有种子」而静默停摆
            # （2026-11-08 prod：ourbits 凭据过期 10 天无人察觉）
            raise SiteFetchError(
                "hdsky 凭据已失效（被重定向到登录页），请更新 cookie"
            )
        items = HdskyAdapter._parse_spstate7_html(resp.text)
        # 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
        return items[:_MAX_ITEMS_PER_PULL]

    # ------------------------------------------------------------ 2.5 get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """返回 ``{base}/download.php?id=X&t=...&sign=...`` 绝对 URL。

        HDSky 的裸 ``/download.php?id=X`` 永远 404；签名参数只能从详情页
        ``details.php?id=X`` 的 form action 中提取。
        """
        if not item_id:
            return None
        try:
            resp = await self._client.get(self._detail_url(item_id), timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("HDSky 详情页拉取失败 %s: %s", item_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        m = _SIGNED_DL_RE.search(resp.text)
        if not m:
            return None
        signed_id, ts, sign = m.group(1), m.group(2), m.group(3)
        return (
            f"{self._base_url()}/download.php"
            f"?id={signed_id}&t={ts}&sign={sign}"
        )

    @staticmethod
    def _parse_spstate7_html(html: str) -> list[FeedItem]:
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

            # ---------------- size: 扫 td 匹配 <td>X.XX<br />UNIT</td>
            size_bytes = 0
            for td in tr.find_all("td"):
                td_html: str = td.decode()
                if size_bytes == 0:
                    # 用共享 helper（支持千分位）——内联正则会在 "1,234.56<br/>GB"
                    # 处截断成 "234.56"，写入错误的体积
                    size_bytes = parse_size_from_br(td_html)

            # ---------------- published_at: 发布时间单元格（短相对时长 td），
            # 避免误取描述栏里的"优惠剩余时间"
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

        流程与 ``get_torrent_url`` 一致：
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

        注：用 follow_redirects=False 才能识别 302 重定向
        （默认会跟到 login.php 的 200 误判为 EXISTS）。
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

        解析：title（h1/title 锚点）+ size（保留在 raw_html 中）
        + download_url（form action 中的 sign URL）。
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


    # ----------------------- FR-3.6 每日签到 -----------------------

    async def checkin(self) -> CheckinResult:
        """hdsky 用自建的 showup 三段式签到（**不是** NexusPHP 标准
        ``/attendance.php``，后者在本站 404）。

        流程（协议来自站点自己的 ``common.js``，2026-10-22 实测）：

        1. ``POST showup.php {action:check}`` —— **免验证码**查今日是否已签
        2. 未签 → ``POST image_code_ajax.php {action:new}`` 拿 32 位 code
        3. ``GET image.php?action=regimage&imagehash=<code>`` 拿 150x40 验证码图
        4. **AI 读码**（:mod:`app.core.ai`，放大 6 倍后送视觉模型）
        5. ``POST showup.php {action:showup, imagehash, imagestring}`` 提交

        读错码时**换一张新验证码重试**（服务端不限制重试，实测重试窗口内
        准确率 100%）。

        未配置 AI 读码时返回 ``UNSUPPORTED``（需人工），而不是让作业变红。
        """
        if not self._cookies:
            return CheckinResult(CheckinStatus.AUTH_EXPIRED, "缺少 cookies")

        reader = get_ai_reader()
        if not reader.available:
            logger.info("hdsky 跳过签到：未配置 AI 读码（AI_API_BASE/KEY/MODEL）")
            return CheckinResult(
                CheckinStatus.UNSUPPORTED, "未配置 AI 读码，需人工签到"
            )

        # 1) 免验证码查状态
        state = await self._fetch_sign_state()
        if state is None:
            return CheckinResult(
                CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
            )
        if state:
            return CheckinResult(CheckinStatus.ALREADY, "今日已签到")

        # 2~5) 申请验证码 → 读码 → 提交（读错则换图重试）
        last_error = "验证码识别失败"
        for _ in range(_CHECKIN_MAX_ATTEMPTS):
            code = await self._new_captcha_hash()
            if code is None:
                return CheckinResult(
                    CheckinStatus.FAILED, "获取验证码失败"
                )
            image = await self._fetch_captcha_image(code)
            if image is None:
                return CheckinResult(
                    CheckinStatus.FAILED, "获取验证码图片失败"
                )
            try:
                answer = await reader.read_captcha(image)
            except AIClientError as exc:
                ai_captcha_reads.labels(site="hdsky", result="failed").inc()
                return CheckinResult(
                    CheckinStatus.FAILED, f"AI 读码失败：{exc}"
                )
            ai_captcha_reads.labels(site="hdsky", result="ok").inc()

            result = await self._submit_checkin(code, answer)
            if result.status is not CheckinStatus.FAILED:
                return result
            last_error = result.message
            logger.info("hdsky 签到未通过（%s），换验证码重试", last_error)

        return CheckinResult(CheckinStatus.FAILED, last_error)

    async def _showup_post(
        self, data: dict[str, str]
    ) -> dict[str, object] | str:
        """POST ``showup.php``，返回解析后的 JSON（非 JSON 时返回原文本）。

        失败一律抛 :class:`SiteFetchError` / :class:`BreakerTripped`——
        与拉取路径同一契约，不吞异常。
        """
        try:
            resp = await self._client.post(
                f"{self._base_url()}/showup.php",
                data=data,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"hdsky 签到请求失败：{exc}") from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"hdsky 签到返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            raise SiteFetchError(f"hdsky 签到返回 HTTP {resp.status_code}")
        try:
            payload: dict[str, object] = resp.json()
        except ValueError:
            # 未登录时返回登录页 HTML，不是 JSON
            return resp.text
        return payload

    async def _fetch_sign_state(self) -> bool | None:
        """查今日是否已签。``None`` = 响应不可解读（含 cookie 失效）。"""
        payload = await self._showup_post({"action": "check"})
        return attendance.parse_showup_sign_state(payload)

    async def _new_captcha_hash(self) -> str | None:
        """申请一张新验证码，拿 32 位 code。"""
        try:
            resp = await self._client.post(
                f"{self._base_url()}/image_code_ajax.php",
                data={"action": "new"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"hdsky 申请验证码失败：{exc}") from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"hdsky 申请验证码返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"hdsky 申请验证码返回 HTTP {resp.status_code}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SiteFetchError(f"hdsky 验证码响应非 JSON：{exc}") from exc
        if data.get("success") is not True:
            return None
        code = data.get("code")
        return code if isinstance(code, str) and code else None

    async def _fetch_captcha_image(self, code: str) -> bytes | None:
        """取验证码图片（站点固定出 150x40，放大交给 AI 侧处理）。"""
        try:
            resp = await self._client.get(
                f"{self._base_url()}/image.php",
                params={"action": "regimage", "imagehash": code},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"hdsky 取验证码图片失败：{exc}") from exc
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"hdsky 取验证码图片返回 HTTP {resp.status_code}"
            )
        return resp.content or None

    async def _submit_checkin(
        self, code: str, answer: str
    ) -> CheckinResult:
        """提交签到（code + 识别的字符）。"""
        payload = await self._showup_post(
            {
                "action": "showup",
                "imagehash": code,
                "imagestring": answer,
            }
        )
        return attendance.parse_showup_result(payload)

    # ----------------------- FR-1.6 站点用户数据（精简版 + 时魔） -----------------------

    async def _fetch_page(self, path: str) -> str:
        """GET 一个页面并返回 HTML。失败抛 :class:`SiteFetchError`。

        拉取失败**不吞**（不返回空串 / None）：service 层据此把该站计成
        ``failed``，避免把故障伪装成「上传 0 / 做种 0」。
        """
        url = f"{self._base_url()}{path}"
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            # httpx 的 ConnectError 消息常为空（原因在 __cause__），
            # 空消息在作业 summary 里看不出所以然
            detail = str(exc) or type(exc).__name__
            raise SiteFetchError(f"hdsky {path} 拉取失败：{detail}") from exc
        if resp.status_code >= 400:
            raise SiteFetchError(f"hdsky {path} 返回 HTTP {resp.status_code}")
        if "login.php" in str(resp.url):
            raise SiteFetchError(
                "hdsky 凭据已失效（被重定向到登录页），请更新 cookie"
            )
        return resp.text

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """hdsky 走 ``/mybonus.php``（NexusPHP 模板）。

        该页 navbar（``stdhead()`` 渲染，每个登录页都有）带 4 业务字段，
        页面下半部分带时魔——**一页拿全**，不额外发请求。navbar 解析不出
        4 字段时回退 ``/userdetails.php``。

        hdsky 是老模板（无「每小时获得的合计」表），headline 即合计。
        """
        if not self._cookies:
            return None
        bonus_html = await self._fetch_page("/mybonus.php")
        snap = nexus_php.parse_user_snapshot(bonus_html)
        if snap is None:
            snap = nexus_php.parse_user_snapshot(
                await self._fetch_page("/userdetails.php")
            )
            if snap is None:
                raise SiteFetchError("hdsky 用户数据解析失败：3 个业务字段均未取到")
        bonus = nexus_php.parse_hourly_bonus(bonus_html, unit="魔力值")
        return SiteUserSnapshot(
            upload=snap.upload,
            download=snap.download,
            seeding_count=snap.seeding_count,
            hourly_bonus=bonus,
        )



    async def close(self) -> None:
        await self._client.aclose()


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败。"""


__all__ = ["HdskyAdapter", "_TorrentDownloadError"]