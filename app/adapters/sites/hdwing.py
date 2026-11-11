"""HDWinG 站点适配器（FR-1.5）。

hdwing.org 是 NexusPHP 框架（Version 700efff）+ X2015 自定义主题的 PT 站，
cookie 鉴权，简体中文界面（账号语言设定）。

实测 DOM 与标准 NexusPHP 模板差异大（2026-11-07，dev 真实 cookie）：

- **列表页**：``GET /torrents.php?spstate=2&page=N``（``page`` 从 0 起）。
  ``table.torrent_list`` 下每行 ``<tr>`` 的列 class 依次：
  ``t_cat``（``img.catchs[title]``）/ ``t_name``（嵌套 ``table.tbname``，
  标题在 ``h3 > a``，**无 title 属性**，副标题在 ``h4``）/ ``t_comment`` /
  ``t_time``（``span[title=绝对时刻]``）/ ``t_size`` / ``t_torrents``（做种）/
  ``t_leech`` / ``t_completed`` / ``t_uploader``。

  **本适配器只拉 spstate=2（仅免费）**：业务目标是免费种推送，每轮
  ``max_pages=3`` 页 × 每页 100 条 = 最新 300 个免费种（官种永久免费 +
  限时免费都含），与 soulvoice「默认页 = 仅 free 种」同一语义。
  拉全量（spstate=0）的话 3 页全是普通种，免费种会被淹没
  （该站免费种 2 万+）。

- **优惠状态是 AJAX 的**：列表/详情页 HTML 里只有空占位
  ``<span class="sp_state_placeholder" id="<tid>"></span>``，真实优惠标记由
  ``POST /ajax_promotion.php``（form：``csrf`` + ``ids[]``，csrf 取自页面
  ``<meta name="x-csrf">``）返回 JSON::

      {"status": 200, "message": {"<tid>": {
          "sp_state": " <img class=\"pro_free\" ... title=\"免费\" />",
          "timeout": "..."}}}

  ``sp_state`` 里的 ``pro_*`` class 与标准 NexusPHP 相同；
  ``timeout`` 是限时种的剩余时间 HTML（当前免费种池全为永久免费，
  无真实样本，按标准 ``span[title=绝对时刻]`` 宽容解析）。

- **下载 URL 必须带 ``&uid=<自己>``**：``download.php?hash=<base64+签名>``
  不带 ``uid`` 会返回「下载提示」确认页（HTML，分享率门槛），带上才返回
  bencoded 字节。hash 本身内嵌 uid（base64 解出 ``"1\\n<tid>\\n<uid>"``），
  自身 uid 从 navbar ``invite.php?id=N`` 提取。签名**不可合成**（必须从
  页面解析，不伪造——见 AGENTS.md 合规边界）。

- **签到**：**不是** NexusPHP 标准的 ``/attendance.php``（实测 2026-11-07 返回
  404），而是 X2015 主题自带的插件端点 ``plugin_sign-in.php``
  （协议来自站点自己的 ``/plugin/js/base.js`` 的 ``do_signin()``，
  2026-11-11 生产 cookie 实测）：

  1. ``GET /usercp.php`` —— 取 ``<meta name="x-csrf">`` 的 csrf + 看 navbar
     是「签 到」还是「已签到」（**两个特征都不在 = 站点改版**，不能盲签）
  2. ``POST /plugin_sign-in.php?cmd=signin {csrf}`` —— 执行签到。
     **不需要图形验证码**（站点另有一个带验证码的表单页，AJAX 路径不用它）

  响应：``{"state":"success","signindays":N,"integral":"10"}`` /
  ``{"state":false}``（今日已签，**无 msg**）/ ``{"state":false,"msg":...}``。

- **用户数据**：``/mybonus.php`` navbar（纯文本 ``上传量：X GB``，无
  ``color_uploaded`` font class、无「当前活动」）→ 共享
  :func:`nexus_php.parse_user_snapshot` 的**文本回退**直接可用（做种数由
  ``title="当前做种"`` 后的数字匹配）；时魔 headline 在
  ``div.bonus_bar_box > h3``，无合计表 → headline 口径（同 hdsky）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin

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
    parse_size_text,
)
from app.adapters.sites.parsers import attendance, nexus_php
from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.logging import get_logger

logger = get_logger(__name__)


class _TorrentDownloadError(RuntimeError):
    """下载 .torrent 失败（缺 cookies / HTTP 错误 / 非法响应）。

    与其他 NexusPHP 站一致：feed_fetcher 不调 download_torrent，本错误
    由 task_executor 包成 PushItemError 上报，不触发站点熔断。
    """


#: ``pro_*`` img class → 标准 free class（sp_state HTML 里出现）。
#: 覆盖站点筛选器全部档位：免费 / 2X / 2X免费 / 50% / 2X 50% / 30%
_PRO_IMG_MAP: dict[str, str] = {
    "pro_free": "free",
    "pro_free2up": "twoupfree",
    "pro_2upfree": "twoupfree",
    "pro_2up": "2up",
    "pro_halfdown": "halfdown",
    "pro_50pctdown": "halfdown",
    "pro_2uphalfdown": "twouphalfdown",
    "pro_50pctdown2up": "twouphalfdown",
    "pro_30pctdown": "thirtypercent",
}

#: 列表页详情链接（``details.php?id=836813&hit=1``）
_DETAILS_ID_RE = re.compile(r"details\.php\?id=(\d+)")

#: sp_state HTML 里的优惠 class（``<img class="pro_free" ...>``）
_PROMO_CLASS_RE = re.compile(r'class="(pro_[\w]+)"')

#: timeout HTML 里的剩余时刻（标准 NexusPHP ``span[title]`` 形态；无真实
#: 样本，按标准格式宽容解析——站点改版后用真实页面修正）
_PROMO_TIMEOUT_RE = re.compile(
    r'title="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"'
)

#: 页面 csrf（``<meta name="x-csrf" content="<42hex>">``）
_CSRF_RE = re.compile(r'<meta\s+name="x-csrf"\s+content="([0-9a-fA-F]+)"')

#: 自身 uid（navbar ``invite.php?id=328600``）
_OWN_UID_RE = re.compile(r"invite\.php\?id=(\d+)")

#: 下载链接（相对或绝对形态；hash = base64 + "." + 签名）
_DOWNLOAD_HREF_RE = re.compile(
    r'href="((?:https?://[^"]+)?download\.php\?hash=[A-Za-z0-9+/=]+'
    r"\.[A-Za-z0-9_-]+(?:&amp;uid=\d+)?)\""
)

#: 详情页 <title> 里的种子名（``HDWinG :: 种子详情 "X"``，实体已解码）
_DETAIL_TITLE_RE = re.compile(r'种子详情\s*"([^"]*)"')

#: 详情页体积（``大小： 27.10 GB``）
_DETAIL_SIZE_RE = re.compile(r"大小[：:]\s*([\d,.]+\s*(?:TB|GB|MB|KB))")

#: 详情页「种子不存在」标记（实测文案：``错误 没有该ID的种子``）
_DELETED_MARKER = "没有该ID的种子"

#: 页面 ``<title>``（判登录页用）
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)

#: 每次优惠查询的最大批量（一页 100 条，一次 POST 足够）
_PROMO_BATCH = 100


def _is_login_page(url: str, html: str) -> bool:
    """是不是登录页（未登录 / cookie 失效）。

    判据有二（任一命中即可）：

    1. 最终 URL 落到 ``login.php``（跟随重定向后）
    2. ``<title>`` 里含「登录」（实测登录页标题是 ``HDWinG :: 登录``）

    只看 URL 不够——某些路径未登录时**不重定向**，直接回登录页 HTML。
    """
    if "login.php" in url.lower():
        return True
    m = _TITLE_RE.search(html)
    if m is None:
        return False
    return "登录" in m.group(1)


def _parse_cookies(raw: str | None) -> dict[str, str]:
    """``cf_clearance=...; PHPSESSID=...; hdchina=...`` → dict。"""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass(frozen=True, slots=True)
class _ListRow:
    """列表页单行解析结果（free_payload 由优惠接口补齐）。"""

    item_id: str
    title: str
    category: str | None
    size: int
    published_at: str | None


def _parse_free_state(sp_state_html: str, timeout_html: str) -> tuple[list[str], str | None]:
    """优惠接口的单条返回 → ``(free_classes, remaining_abs)``。"""
    classes: list[str] = []
    for cls in _PROMO_CLASS_RE.findall(sp_state_html):
        mapped = _PRO_IMG_MAP.get(cls)
        if mapped is not None and mapped not in classes:
            classes.append(mapped)
    remaining: str | None = None
    m = _PROMO_TIMEOUT_RE.search(timeout_html)
    if m is not None:
        remaining = parse_abs_datetime(m.group(1))
    return classes, remaining


class HdwingAdapter(SiteAdapter):
    """HDWinG (hdwing.org) NexusPHP cookie 适配器。"""

    adapter_type = "hdwing"

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
        """统一 base URL：从 profile.domain 拼装，保留 scheme。"""
        domain = (self.profile.domain or "").strip()
        if not domain:
            return ""
        if not domain.startswith("http://") and not domain.startswith("https://"):
            domain = "https://" + domain
        return domain.rstrip("/")

    @staticmethod
    def _parse_list_rows(html: str) -> list[_ListRow]:
        """BS4 解析列表页（``table.torrent_list``，X2015 自定义结构）。"""
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="torrent_list")
        if table is None:
            return []
        rows: list[_ListRow] = []
        seen: set[str] = set()
        for tr in table.find_all("tr", recursive=False):
            name_td = tr.find("td", class_="t_name")
            if name_td is None:
                continue  # 表头 / 非数据行
            a = name_td.find("a", href=_DETAILS_ID_RE)
            if a is None:
                continue
            m = _DETAILS_ID_RE.search(str(a.get("href") or ""))
            if m is None:
                continue
            item_id = m.group(1)
            if item_id in seen:
                continue  # 置顶种每页重复出现，跨页去重
            seen.add(item_id)
            title = a.get_text(strip=True)
            cat_img = tr.find("img", class_="catchs")
            category: str | None = None
            if cat_img is not None:
                cat_title = str(cat_img.get("title") or cat_img.get("alt") or "")
                category = cat_title.strip() or None
            size_td = tr.find("td", class_="t_size")
            size = parse_size_text(size_td.get_text(strip=True)) if size_td else 0
            published_at: str | None = None
            time_span = tr.find("td", class_="t_time")
            if time_span is not None:
                span = time_span.find("span", attrs={"title": True})
                if span is not None:
                    published_at = parse_abs_datetime(str(span.get("title") or ""))
            rows.append(
                _ListRow(
                    item_id=item_id,
                    title=title,
                    category=category,
                    size=size,
                    published_at=published_at,
                )
            )
        return rows

    async def _fetch_promotions(
        self, ids: list[str], csrf: str
    ) -> dict[str, tuple[list[str], str | None]]:
        """批量查优惠状态（``POST /ajax_promotion.php``）。

        失败抛 :class:`SiteFetchError`：spstate=2 列表的价值就在免费标记，
        没有优惠状态的数据对业务无用——如实报故障让 feed_fetcher 重试。
        """
        out: dict[str, tuple[list[str], str | None]] = {}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url()}/torrents.php",
        }
        for start in range(0, len(ids), _PROMO_BATCH):
            batch = ids[start : start + _PROMO_BATCH]
            pairs: list[tuple[str, str]] = [("csrf", csrf)]
            pairs.extend(("ids[]", tid) for tid in batch)
            try:
                resp = await self._client.post(
                    f"{self._base_url()}/ajax_promotion.php",
                    content=urlencode(pairs),
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise SiteFetchError(f"HDWinG 优惠状态查询失败：{exc}") from exc
            if resp.status_code in (403, 429):
                raise BreakerTripped(
                    f"HDWinG 优惠状态查询返回 {resp.status_code}，需熔断"
                )
            if resp.status_code >= 400:
                raise SiteFetchError(
                    f"HDWinG 优惠状态查询返回 HTTP {resp.status_code}"
                )
            try:
                payload = json.loads(resp.text)
            except ValueError as exc:
                raise SiteFetchError(
                    f"HDWinG 优惠状态响应不是 JSON：{exc}"
                ) from exc
            if payload.get("status") != 200:
                raise SiteFetchError(
                    f"HDWinG 优惠状态接口返回 status={payload.get('status')!r}"
                )
            message = payload.get("message")
            if not isinstance(message, dict):
                raise SiteFetchError("HDWinG 优惠状态响应缺少 message")
            for tid, val in message.items():
                if not isinstance(val, dict):
                    continue
                sp_state = str(val.get("sp_state") or "")
                timeout = str(val.get("timeout") or "")
                out[str(tid)] = _parse_free_state(sp_state, timeout)
        return out

    def _extract_download_url(self, html: str, base_url: str) -> str | None:
        """从页面 HTML 提取下载 URL（绝对化 + 追加自身 uid）。

        - hash 签名不可合成，必须从页面解析
        - 不带 ``&uid=`` 的 URL 返回「下载提示」HTML 而非种子字节
          （实测 2026-11-07）→ 从 navbar ``invite.php?id=N`` 取自身 uid 追加
        """
        m = _DOWNLOAD_HREF_RE.search(html)
        if m is None:
            return None
        href = m.group(1).replace("&amp;", "&")
        url: str = urljoin(base_url + "/", href)
        if "uid=" in url:
            return url
        uid_m = _OWN_UID_RE.search(html)
        if uid_m is None:
            logger.warning("HDWinG 页面未找到自身 uid（invite.php 链接）")
            return None
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}uid={uid_m.group(1)}"

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
            # hdwing navbar 是纯文本（无 color_uploaded class），
            # 「上传量」/「logout.php」是登录态的可靠标记
            logged_in = "上传量" in html or "logout.php" in html
        return SiteHealth(
            reachable=True,
            logged_in=logged_in,
            detail=("已登录" if logged_in else "未登录（被重定向到登录页），请更新 cookie"),
        )

    # ------------------------------------------------------------ 2. search_torrents
    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 100,
        page_number: int = 1,
    ) -> list[FeedItem]:
        """拉免费种列表（``spstate=2``，page 从 0 起）+ 批量查优惠状态。"""
        _ = query, page_size  # HTML 站每页固定 100 条，page_size 不生效
        url = (
            f"{self._base_url()}/torrents.php"
            f"?spstate=2&page={max(0, page_number - 1)}"
        )
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            # 拉取失败必须抛出：返回 [] 表示"站点确实没有种子"，
            # 与"站点挂了"混淆会让作业统计看不出故障站
            raise SiteFetchError(f"HDWinG 拉取失败：{exc}") from exc
        if resp.status_code in (403, 429):
            # 限流/封禁 → 熔断信号（FR-2.2），不是普通拉取失败。
            # 403 可能是 cookie 失效而非限流，熔断时长减半（见 jobs.py）。
            raise BreakerTripped(f"HDWinG 拉取返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"HDWinG 拉取 HTTP {resp.status_code}（第 {page_number} 页）"
            )
        if "login.php" in str(resp.url):
            # 凭据失效：站点把受保护页面 302 到登录页（跟随后是 200）——
            # 不抛错的话会解析出 0 条，被当成「站点确实没有种子」而静默停摆
            # （2026-11-08 prod：ourbits 凭据过期 10 天无人察觉）
            raise SiteFetchError(
                "hdwing 凭据已失效（被重定向到登录页），请更新 cookie"
            )

        html = resp.text
        rows = self._parse_list_rows(html)
        if not rows:
            return []

        csrf_m = _CSRF_RE.search(html)
        if csrf_m is None:
            raise SiteFetchError("HDWinG 列表页缺少 x-csrf（页面改版？）")
        promotions = await self._fetch_promotions(
            [r.item_id for r in rows], csrf_m.group(1)
        )

        items: list[FeedItem] = []
        for row in rows:
            free_payload: dict[str, object] | None = None
            state = promotions.get(row.item_id)
            if state is not None:
                free_classes, remaining_abs = state
                if free_classes or remaining_abs:
                    free_payload = {
                        "free_classes": free_classes,
                        "remaining_abs": remaining_abs,
                    }
            items.append(
                FeedItem(
                    title=row.title,
                    item_id=row.item_id,
                    info_hash=None,  # 列表页不直接给 hash
                    size=row.size,
                    category=row.category,
                    published_at=row.published_at,
                    description="",  # 副标题可能含「免费/剩余」字样，
                    # 会污染 window_parser 的 str 兜底路径，不放
                    free_payload=free_payload,
                )
            )
        return items

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """从详情页解析 hash URL（+ 自身 uid）后 GET 拿 bencoded bytes。"""
        if not item_id:
            raise ValueError("item_id 不能为空")
        if not self._cookies:
            # 与其他 NexusPHP 站一致：未配置 cookie 时直接报下载错误，
            # 不要让流程走到 BreakerTripped——后者会被 feed_fetcher 当熔断处理，
            # 把"未配置 cookie"误升级为站点熔断。
            raise _TorrentDownloadError("缺少 cookies")
        url = await self.get_torrent_url(item_id)
        if not url:
            raise BreakerTripped(f"无法从详情页提取下载 URL id={item_id}")
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise BreakerTripped(f"HDWinG 下载失败 id={item_id}: {exc}") from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"HDWinG HTTP {resp.status_code} id={item_id}")
        resp.raise_for_status()
        if not resp.content or not resp.content.startswith(b"d"):
            raise BreakerTripped(f"HDWinG 响应不是 bencoded torrent id={item_id}")
        return resp.content

    # ------------------------------------------------------------ 4. check_status
    async def check_status(self, item_id: str) -> TorrentBrief:
        url = f"{self._base_url()}/details.php?id={item_id}"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("HDWinG 状态检查失败 id=%s: %s", item_id, exc)
            return TorrentBrief.UNKNOWN
        deleted_markers = (
            resp.status_code == 404
            or _DELETED_MARKER in resp.text
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

        title: str | None = None
        title_tag = soup.find("title")
        if title_tag is not None:
            m = _DETAIL_TITLE_RE.search(title_tag.get_text())
            title = m.group(1).strip() if m else title_tag.get_text(strip=True)

        size = 0
        size_m = _DETAIL_SIZE_RE.search(html)
        if size_m is not None:
            size = parse_size_text(size_m.group(1))

        download_url = self._extract_download_url(html, self._base_url())

        return TorrentDetail(
            item_id=item_id,
            title=title,
            size=size,
            download_url=download_url,
            free_payload=None,  # 详情页优惠状态同样是 AJAX 占位符，见类注释
            raw_html=html,
        )

    # ------------------------------------------------------------ 6. get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """拉详情页解析 hash URL（追加自身 uid 后可直接被 qB/TR 抓取）。"""
        url = f"{self._base_url()}/details.php?id={item_id}"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("HDWinG 解析下载 URL 失败 id=%s: %s", item_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        return self._extract_download_url(resp.text, self._base_url())

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
            raise SiteFetchError(f"hdwing {path} 拉取失败：{detail}") from exc
        if resp.status_code >= 400:
            raise SiteFetchError(f"hdwing {path} 返回 HTTP {resp.status_code}")
        if "login.php" in str(resp.url):
            raise SiteFetchError(
                "hdwing 凭据已失效（被重定向到登录页），请更新 cookie"
            )
        return resp.text

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """hdwing 走 ``/mybonus.php``。

        该页 navbar（``stdhead()`` 渲染）带 3 业务字段（纯文本
        ``上传量：X GB``，共享解析器的文本回退可解析），页面下部的
        ``div.bonus_bar_box > h3`` 带时魔 headline——**一页拿全**。
        navbar 解析不出 3 字段时回退 ``/userdetails.php``。
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
                raise SiteFetchError("hdwing 用户数据解析失败：3 个业务字段均未取到")
        bonus = nexus_php.parse_hourly_bonus(bonus_html, unit="魔力值")
        return SiteUserSnapshot(
            upload=snap.upload,
            download=snap.download,
            seeding_count=snap.seeding_count,
            hourly_bonus=bonus,
        )

    # ----------------------- FR-3.6 站点每日签到 -----------------------

    async def checkin(self) -> CheckinResult:
        """hdwing 走 X2015 主题自带的 ``plugin_sign-in.php``。

        **不是** NexusPHP 标准的 ``/attendance.php``（本站 404）。

        流程（协议来自站点自己的 ``/plugin/js/base.js`` 的 ``do_signin()``，
        2026-11-11 生产 cookie 实测）：

        1. ``GET /usercp.php`` —— 取 ``<meta name="x-csrf">`` 的 csrf
           + 看 navbar 是「签 到」还是「已签到」
        2. ``POST /plugin_sign-in.php?cmd=signin {csrf}`` —— 执行签到，
           **不需要图形验证码**

        「今日已签」时**不发 POST**（省一次请求，也不打扰站点）——
        navbar 上的状态是权威判据。响应 ``{"state":false}`` 既是「已签」
        也是「失败无原因」，故失败时再查一次状态定性（宁可多一次请求，
        也不把幂等的「已签」误报成失败，或把真失败静默吞掉）。
        """
        if not self._cookies:
            return CheckinResult(CheckinStatus.AUTH_EXPIRED, "缺少 cookies")

        html = await self._fetch_signin_page()
        if html is None:
            return CheckinResult(
                CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
            )

        csrf_m = _CSRF_RE.search(html)
        if csrf_m is None:
            return CheckinResult(
                CheckinStatus.FAILED, "页面缺少 x-csrf（站点改版？）"
            )

        state = attendance.parse_signin_state(html)
        if state is True:
            return CheckinResult(CheckinStatus.ALREADY, "今日已签到")
        if state is None:
            # 两个特征都不在 → 页面结构变了。**不能**当成「未签到」盲签
            return CheckinResult(
                CheckinStatus.FAILED, "无法识别签到状态（站点改版？）"
            )

        result = await self._submit_signin(csrf_m.group(1))
        if result.message != attendance.UNREASONED_FAILURE:
            return result

        # 「失败但站点没给原因」—— 极可能是今日已签到，再查一次状态定性
        confirm = await self._fetch_signin_page()
        if confirm is None:
            return CheckinResult(
                CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
            )
        if attendance.parse_signin_state(confirm) is True:
            return CheckinResult(CheckinStatus.ALREADY, "今日已签到")
        return result

    async def _fetch_signin_page(self) -> str | None:
        """GET ``/usercp.php``（取 csrf + 签到状态）。

        :returns: 页面 HTML；``None`` = cookie 失效（被重定向到登录页）。
        网络 / HTTP 故障抛 :class:`SiteFetchError` / :class:`BreakerTripped`
        ——与拉取路径同一契约，不吞异常。
        """
        url = f"{self._base_url()}/usercp.php"
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"hdwing 签到页拉取失败：{exc}") from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"hdwing 签到返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            raise SiteFetchError(f"hdwing 签到页返回 HTTP {resp.status_code}")
        if _is_login_page(str(resp.url), resp.text):
            return None
        return resp.text

    async def _submit_signin(self, csrf: str) -> CheckinResult:
        """POST ``plugin_sign-in.php?cmd=signin`` 执行签到。"""
        base = self._base_url()
        try:
            resp = await self._client.post(
                f"{base}/plugin_sign-in.php?cmd=signin",
                data={"csrf": csrf},
                headers={
                    "Content-Type": (
                        "application/x-www-form-urlencoded; charset=UTF-8"
                    ),
                    "Referer": f"{base}/usercp.php",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"hdwing 签到提交失败：{exc}") from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"hdwing 签到返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            raise SiteFetchError(f"hdwing 签到提交返回 HTTP {resp.status_code}")

        try:
            payload: object = resp.json()
        except ValueError:
            # 未登录时返回登录页 HTML，不是 JSON
            if _is_login_page(str(resp.url), resp.text):
                return CheckinResult(
                    CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
                )
            return CheckinResult(
                CheckinStatus.FAILED, "签到响应不是 JSON（站点改版？）"
            )
        return attendance.parse_plugin_signin_result(payload)

    async def close(self) -> None:
        await self._client.aclose()
