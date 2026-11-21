"""通用站点适配器：用站点规则包驱动搜索与用户数据解析。

设计要点：

- 规则来自 :mod:`app.adapters.sites.indexer.rules`（MoviePilot-Resources 的
  解密规则包），**不依赖** MoviePilot 的 Cython 扩展与站点认证逻辑。
- cookie / apikey 由本项目的 ``sites`` 表提供，沿用现有适配器契约。
- 失败语义与其它适配器一致：拉取失败抛 :class:`SiteFetchError`，
  403/429 抛 :class:`BreakerTripped`——绝不返回空列表掩盖故障。
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from parsel import Selector

from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    SubtitleItem,
    TorrentBrief,
    TorrentDetail,
    parse_abs_datetime,
    parse_size_text,
)
from app.adapters.sites.indexer.compat.settings import get_runtime_setting
from app.adapters.sites.indexer.engine import SPIDER_PARSER_CLASSES, IndexerEngine
from app.adapters.sites.indexer.rules import SiteRules, load_site_rules
from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import format_bj

logger = get_logger(__name__)

#: NexusPHP 详情页默认路径（规则包里没有独立字段，按主流实现兜底）
_DEFAULT_DETAIL_PATH = "details.php?id={item_id}"

#: 列表页/详情页的促销 class（用于把倍率翻译成现有的免费窗口输入）
_PROMO_CLASS_BY_FACTOR = {0: "free", 0.5: "halfdown", 0.3: "thirtypercent"}

_LOGIN_MARKERS_RE = re.compile(r"(login\.php|登录|請登入|请登录)", re.IGNORECASE)
_LOGGED_IN_RE = re.compile(
    r"(userdetails\.php|logout\.php|takelogout\.php|usercp\.php)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _rules() -> SiteRules:
    return load_site_rules()


@lru_cache(maxsize=1)
def _engine() -> IndexerEngine:
    return IndexerEngine()


def resolve_rule(domain: str) -> dict[str, Any] | None:
    """按站点域名取规则（缓存）。"""
    return _rules().find_by_domain(domain)


class GenericAdapter(SiteAdapter):
    """规则包驱动的通用适配器。"""

    adapter_type = "generic"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        cookies: str | None = None,
        apikey: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(profile, cookies=cookies, apikey=apikey, timeout=timeout)
        settings = get_settings()
        self._user_agent = get_runtime_setting("USER_AGENT")
        self._rule = resolve_rule(profile.domain)
        self._base_url_value = str((self._rule or {}).get("domain") or profile.domain).rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self._user_agent},
            cookies=_cookie_dict(cookies),
            timeout=timeout,
            follow_redirects=True,
            proxy=(settings.proxy_url or None),
        )

    # ------------------------------------------------------------------ 站点配置
    def _site_config(self) -> dict[str, Any]:
        """规则 + 用户凭据合并成引擎认识的站点配置。"""
        if not self._rule:
            raise SiteFetchError(f"未找到站点规则：{self.profile.domain}")
        return {
            **self._rule,
            "name": self.profile.name,
            "domain": f"{self._base_url_value}/",
            "cookie": self._cookies,
            "apikey": self._apikey,
            "ua": self._user_agent,
            "proxy": False,
        }

    def _detail_url(self, item_id: str) -> str:
        rule = self._rule or {}
        path = str((rule.get("torrents") or {}).get("details_path") or _DEFAULT_DETAIL_PATH)
        return urljoin(f"{self._base_url_value}/", path.format(item_id=quote(str(item_id))))

    # ------------------------------------------------------------------ 1. 连通性
    async def check_reachability(self) -> SiteHealth:
        if not self._cookies and not self._apikey:
            return SiteHealth(reachable=False, detail="缺少 cookies/apikey")
        # API 站：交给专站 spider 的 test_connection（用同一认证头探用户接口，
        # 不依赖首页登录标记——SPA 站首页看不出登录态）
        api_health = await self._api_reachability()
        if api_health is not None:
            return api_health
        try:
            resp = await self._client.get(f"{self._base_url_value}/", timeout=self._timeout)
        except httpx.HTTPError as exc:
            return SiteHealth(reachable=False, detail=str(exc))
        if resp.status_code >= 500:
            return SiteHealth(reachable=False, detail=f"HTTP {resp.status_code}")
        text = resp.text
        if _LOGGED_IN_RE.search(text):
            return SiteHealth(reachable=True, logged_in=True, detail="ok")
        if _LOGIN_MARKERS_RE.search(text) or "login.php" in str(resp.url):
            return SiteHealth(reachable=True, logged_in=False, detail="cookie 已过期，需重新获取")
        return SiteHealth(reachable=True, logged_in=False, detail="无法确认登录态")

    async def _api_reachability(self) -> SiteHealth | None:
        """专站 spider 有 ``test_connection`` 时走 API 探测，否则返回 None。"""
        parser_name = str((self._rule or {}).get("parser") or "")
        cls = SPIDER_PARSER_CLASSES.get(parser_name)
        if cls is None:
            return None
        tester: Any = getattr(cls, "test_connection", None)
        if tester is None:
            return None
        try:
            spider = cls(self._site_config())
            ok, detail = await asyncio.to_thread(tester, spider)
        except Exception as exc:
            return SiteHealth(reachable=False, detail=f"连通性检测失败：{exc}")
        detail = str(detail or "")
        if ok:
            return SiteHealth(reachable=True, logged_in=True, detail=detail or "连接成功")
        # test_connection 只看布尔；从文案区分「网络不通」与「鉴权失效」
        network_markers = ("无法连接", "无法打开", "HTTP", "错误：")
        reachable = not any(marker in detail for marker in network_markers)
        return SiteHealth(reachable=reachable, logged_in=False, detail=detail)

    # ------------------------------------------------------------------ 2. 搜索
    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 200,
        page_number: int = 1,
    ) -> list[FeedItem]:
        keyword = str(query.get("keyword") or "").strip()
        page = max(0, page_number - 1)
        site = self._site_config()
        try:
            rows = await _engine().async_search_torrents(site, keyword=keyword or None, page=page)
        except SiteFetchError:
            raise
        except Exception as exc:
            raise SiteFetchError(f"{self.profile.name} 搜索失败：{exc}") from exc
        return [_to_feed_item(row, self._rule or {}) for row in rows]

    # ------------------------------------------------------------------ 3. 下载
    async def download_torrent(self, item_id: str) -> bytes:
        url = await self.get_torrent_url(item_id)
        if not url:
            raise SiteFetchError(f"{self.profile.name} 无法解析下载地址（id={item_id}）")
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"{self.profile.name} 下载失败：{exc}") from exc
        self._check_status_code(resp)
        return resp.content

    # ------------------------------------------------------------------ 3.5 下载地址
    async def get_torrent_url(self, item_id: str) -> str | None:
        if not item_id:
            return None
        # API 站（m-team / hdarea / piggo）由专站 spider 生成下载配方
        api_url = await self._parser_download_url(item_id)
        if api_url:
            return api_url
        detail_url = self._detail_url(item_id)
        try:
            resp = await self._client.get(detail_url, timeout=self._timeout)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400 or _looks_like_login(resp.text, resp.url):
            return None
        return _extract_download_url(resp.text, self._base_url_value, self._rule or {})

    async def _parser_download_url(self, item_id: str) -> str | None:
        """专站 spider 自带 ``get_download_url`` 时优先使用（API 站必需）。"""
        parser_name = str((self._rule or {}).get("parser") or "")
        cls = SPIDER_PARSER_CLASSES.get(parser_name)
        if cls is None:
            return None
        getter: Any = getattr(cls, "get_download_url", None)
        if getter is None:
            return None
        try:
            spider = cls(self._site_config())
            return await asyncio.to_thread(getter, spider, item_id)
        except Exception as exc:
            logger.debug("站点 %s 专站下载配方失败：%s", self.profile.name, exc)
            return None

    # ------------------------------------------------------------------ 4. 状态
    async def check_status(self, item_id: str) -> TorrentBrief:
        detail_url = self._detail_url(item_id)
        try:
            resp = await self._client.get(detail_url, timeout=self._timeout)
        except httpx.HTTPError:
            return TorrentBrief.UNKNOWN
        if resp.status_code == 404:
            return TorrentBrief.DELETED
        if resp.status_code >= 400:
            return TorrentBrief.UNKNOWN
        if _looks_like_login(resp.text, resp.url):
            return TorrentBrief.UNKNOWN
        return TorrentBrief.EXISTS

    # ------------------------------------------------------------------ 5. 详情
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        detail_url = self._detail_url(item_id)
        try:
            resp = await self._client.get(detail_url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            return TorrentDetail(item_id=item_id, error=str(exc))
        if resp.status_code >= 400:
            return TorrentDetail(item_id=item_id, error=f"HTTP {resp.status_code}")
        html = resp.text
        doc = Selector(text=html)
        title = (doc.css("title::text").get() or "").strip() or None
        fields = (self._rule or {}).get("torrents", {}).get("fields", {})
        size_text = _query_field(doc, fields.get("size"))
        return TorrentDetail(
            item_id=item_id,
            title=title,
            size=parse_size_text(size_text),
            download_url=_extract_download_url(html, self._base_url_value, self._rule or {}),
            free_payload=_promo_payload({}, self._rule or {}),
            raw_html=html,
        )

    # ------------------------------------------------------------------ 6. 字幕
    async def search_subtitles(
        self,
        query: dict[str, object],
        *,
        page_number: int = 1,
    ) -> list[SubtitleItem]:
        """按规则包的 ``subtitles`` 配置搜索字幕（无配置的站点返回 ``[]``）。"""
        site = self._site_config()
        if not site.get("subtitles"):
            return []
        keyword = str(query.get("keyword") or "").strip() or None
        page = max(0, page_number - 1)
        try:
            rows = await asyncio.to_thread(
                _engine().search_subtitles, site, keyword, page
            )
        except SiteFetchError:
            raise
        except Exception as exc:
            raise SiteFetchError(f"{self.profile.name} 字幕搜索失败：{exc}") from exc
        return [_to_subtitle_item(row) for row in rows]

    # ------------------------------------------------------------------ 7. 用户数据
    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        site = self._site_config()
        if not site.get("schema"):
            return None
        try:
            data = await asyncio.to_thread(_engine().refresh_userdata, site)
        except Exception as exc:
            raise SiteFetchError(f"{self.profile.name} 用户数据解析失败：{exc}") from exc
        if data is None:
            return None
        if data.get("err_msg") and not data.get("userid"):
            raise SiteFetchError(f"{self.profile.name} 用户数据解析失败：{data['err_msg']}")
        return SiteUserSnapshot(
            upload=int(data.get("upload") or 0),
            download=int(data.get("download") or 0),
            seeding_count=int(data.get("seeding") or 0),
        )

    # ------------------------------------------------------------------ 工具
    def _check_status_code(self, resp: httpx.Response) -> None:
        if resp.status_code in (403, 429):
            raise BreakerTripped(f"{self.profile.name} 返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            raise SiteFetchError(f"{self.profile.name} 返回 HTTP {resp.status_code}")

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- 纯函数

def _cookie_dict(cookies: str | None) -> dict[str, str] | None:
    if not cookies:
        return None
    result: dict[str, str] = {}
    for part in cookies.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        result[key.strip()] = value.strip()
    return result or None



def _row_item_id(row: dict[str, Any]) -> str | None:
    """取站内 ID。

    页面规则里通常有 ``id`` 字段；API 站（m-team 等）的 spider 只给
    ``page_url`` / ``enclosure``，没有 ``id`` 键——从 URL 的 ``id=`` 参数
    回退提取，否则下载/去重/幂等键全都拿不到 ID。
    """
    raw = row.get("id")
    if raw not in (None, ""):
        return str(raw)
    for key in ("page_url", "enclosure", "download"):
        url = row.get(key)
        if not url:
            continue
        text = str(url)
        # 页面型站点 `details.php?id=123`；API 站（m-team）`/detail/123`
        for pattern in (r"[?&]id=(\d+)", r"/detail/(\d+)", r"/(\d+)/?$"):
            match = re.search(pattern, text)
            if match:
                return match.group(1)
    return None


def _published_at(value: Any) -> str | None:
    """把协议侧发布时间转成 DB 记录时刻（北京时间字符串）。

    协议侧的值有两种形态：unix 秒（整数，与 ``time.time()`` 同口径）或站点
    自带的时间文本。**只有在这一层（落库边界）才转成北京时间字符串**——
    协议层保持原值，避免两套时间口径在中间层混用。

    :param value: 协议侧发布时间
    :return: 北京时间字符串；无法解释时返回原文本或 None
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return format_bj(datetime.fromtimestamp(float(value), UTC))
        except (OSError, OverflowError, ValueError):
            return None
    text = parse_abs_datetime(str(value))
    if text:
        return text
    return str(value)


def _to_feed_item(row: dict[str, Any], rule: dict[str, Any]) -> FeedItem:
    published = _published_at(row.get("pubdate"))
    factor = row.get("downloadvolumefactor")
    return FeedItem(
        title=str(row.get("title") or ""),
        item_id=_row_item_id(row),
        info_hash=None,
        size=int(row.get("size") or 0),
        category=str(row.get("category") or "") or None,
        description=str(row.get("description") or ""),
        published_at=str(published) if published else None,
        download_factor=float(factor) if factor is not None else None,
        seeders=int(row.get("seeders") or 0),
        peers=int(row.get("peers") or 0),
        free_payload=_promo_payload(row, rule),
    )


def _size_bytes(value: Any) -> int:
    """规则解析器已把体积转成字节；兼容字符串形态。"""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return parse_size_text(str(value))


def _to_subtitle_item(row: dict[str, Any]) -> SubtitleItem:
    """规则解析结果 → 字幕契约对象。"""
    published = _published_at(row.get("pubdate"))
    return SubtitleItem(
        title=str(row.get("title") or ""),
        language=str(row["language"]) if row.get("language") else None,
        download_url=str(row.get("enclosure")) if row.get("enclosure") else None,
        page_url=str(row.get("page_url")) if row.get("page_url") else None,
        size=_size_bytes(row.get("size")),
        published_at=str(published) if published else None,
        uploader=str(row["uploader"]) if row.get("uploader") else None,
        report_url=str(row["report_url"]) if row.get("report_url") else None,
        language_icon=str(row["language_icon"]) if row.get("language_icon") else None,
        torrent_id=str(row["torrent_id"]) if row.get("torrent_id") else None,
        subtitle_id=str(row["subtitle_id"]) if row.get("subtitle_id") else None,
        file_name=str(row["file_name"]) if row.get("file_name") else None,
    )


def _promo_payload(row: dict[str, Any], rule: dict[str, Any]) -> dict[str, object] | None:
    """把规则解析出的倍率/截止时间翻译成 ``window_parser`` 认识的输入。"""
    factor = row.get("downloadvolumefactor")
    upload_factor = row.get("uploadvolumefactor")
    free_classes: list[str] = []
    try:
        if factor is not None and float(factor) == 0:
            free_classes.append("free")
        elif factor is not None and float(factor) == 0.5:
            free_classes.append("halfdown")
    except (TypeError, ValueError):
        pass
    try:
        if upload_factor is not None and float(upload_factor) >= 2:
            free_classes.append("twoup")
    except (TypeError, ValueError):
        pass
    freedate = row.get("freedate")
    if not free_classes and not freedate:
        return None
    return {
        "free_classes": free_classes,
        "remaining_text": str(freedate) if freedate else None,
        "remaining_abs": parse_abs_datetime(str(freedate)) if freedate else None,
        "promo_texts": row.get("labels") or [],
    }


def _query_field(node: Selector, config: Any) -> str | None:
    if not config or not isinstance(config, dict):
        return None
    selector = config.get("selector")
    if not selector:
        return None
    selector = _normalize_selector(selector)
    hits = node.css(selector)
    if not hits:
        return None
    attribute = config.get("attribute")
    if attribute:
        return hits[0].attrib.get(attribute)
    return hits[0].xpath("string(.)").get()


def _normalize_selector(selector: str) -> str:
    """把 MoviePilot 规则里的 ``:has("x")`` 规范成 cssselect 支持的写法。"""
    return re.sub(r':has\("([^"]+)"\)', r":has(\1)", selector)


def _extract_download_url(html: str, base_url: str, rule: dict[str, Any]) -> str | None:
    doc = Selector(text=html)
    fields = (rule.get("torrents") or {}).get("fields") or {}
    link = _query_field(doc, fields.get("download"))
    if not link:
        return None
    link = link.strip()
    if link.startswith("magnet:"):
        return link
    if link.startswith("//"):
        scheme = urlsplit(base_url).scheme or "https"
        return f"{scheme}:{link}"
    return urljoin(f"{base_url}/", link)


def _looks_like_login(html: str, url: httpx.URL) -> bool:
    if "login.php" in str(url):
        return True
    doc = Selector(text=html)
    title = (doc.css("title::text").get() or "").strip()
    if title in {"登录", "登錄"}:
        return True
    return "login.php" in html[:2000].lower() and "logout" not in html[:2000].lower()
