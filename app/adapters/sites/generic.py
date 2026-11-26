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
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from parsel import Selector

from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    Promotion,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SitePermanentError,
    SiteProfile,
    SubtitleItem,
    TorrentBrief,
    TorrentDetail,
    parse_abs_datetime,
    parse_size_text,
)
from app.adapters.sites.indexer.compat.settings import get_runtime_setting
from app.adapters.sites.indexer.download import (
    is_download_confirmation,
    parse_download_confirmation,
    parse_download_recipe,
    parse_download_response,
)
from app.adapters.sites.indexer.engine import SPIDER_PARSER_CLASSES, IndexerEngine
from app.adapters.sites.indexer.rules import SiteRules, load_site_rules
from app.adapters.sites.indexer.spider.ttg import TTGDownloadAdapter
from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import bj_now_dt, format_bj, parse_bj

logger = get_logger(__name__)

#: NexusPHP 详情页默认路径（规则包里没有独立字段，按主流实现兜底）
_DEFAULT_DETAIL_PATH = "details.php?id={item_id}"

#: 列表页/详情页的促销 class（用于把倍率翻译成现有的免费窗口输入）
_PROMO_CLASS_BY_FACTOR = {0: "free", 0.5: "halfdown", 0.3: "thirtypercent"}

#: 优惠标记是 AJAX 占位符、只能逐种子查 ``promotion.php`` 的规则解析器。
_PROMOTION_LOOKUP_PARSERS = frozenset({"HDWing"})

#: 合法 .torrent 的首字节（bencode 顶层必须是 dict）。
_TORRENT_MAGIC = b"d"

#: 「下载响应不是 .torrent」时的即时重试间隔（秒）。
#:
#: 实测（2026-11-21 prod）：HDWinG 偶发对 ``download.php`` 返回 2xx 的 HTML
#: （站点瞬时异常 / 会话检查页），紧接着重试即成功。故首次拿到非 torrent
#: 内容不直接判死，短暂退避后**重取一次**——把这类瞬时故障消化在适配器内，
#: 避免用户看到「推送失败」而任务其实在 5 分钟后才被队列补推成功。
_NON_TORRENT_RETRY_DELAY = 3.0

_LOGIN_MARKERS_RE = re.compile(r"(login\.php|登录|請登入|请登录)", re.IGNORECASE)
_LOGGED_IN_RE = re.compile(
    r"(userdetails\.php|logout\.php|takelogout\.php|usercp\.php)",
    re.IGNORECASE,
)

#: 疑似「登录页」判定时读取的最大响应体（字节）。
#:
#: HTML 提示页 / 登录页远小于此；超大响应（误下到二进制）不必再解 HTML。
_LOGIN_CHECK_MAX_BYTES = 256 * 1024

#: 登录页的**表单特征**（只看正文前段，避开页脚里的「登录」链接）。
#:
#: 为什么不用「请登录」这类中文文案：正常错误页的页脚也可能有「登录」
#: 链接，误判成凭据失效会把**瞬时**故障错报成永久失败（用户白等重试）。
#: ``login.php`` 作为特征是安全的——已登录用户的导航栏是 ``logout.php``。
_LOGIN_FORM_MARKERS = (
    "login.php",
    'type="password"',
    "type='password'",
    'name="passwd"',
    "name='passwd'",
)

#: NexusPHP「下载提示页」的端点名（``downloadnotice.php``）。
#:
#: 站点在某些条件下（分享率不足、首次下载等）不直接给种子，而是先弹一
#: 个确认页：勾选一项承诺 → 提交「下载种子文件」→ 服务端放行并重定向回
#: ``download.php``。**这不是故障**，而是站点的正常下载协议。
_DOWNLOAD_NOTICE_PATH = "downloadnotice.php"


def _looks_like_login_response(resp: httpx.Response) -> bool:
    """下载响应看起来是登录页吗（凭据失效的判据）。

    比 :func:`_looks_like_login` 宽松一档：多认「带密码输入框的表单」
    ——不少站点的「请登录后下载」提示页不带 login.php 链接。
    """
    if len(resp.content) > _LOGIN_CHECK_MAX_BYTES:
        return False
    text = resp.text
    if _looks_like_login(text, resp.url):
        return True
    head = text[:2000].lower()
    return any(marker in head for marker in _LOGIN_FORM_MARKERS)


def _is_download_notice(resp: httpx.Response) -> bool:
    """响应是不是 NexusPHP 的「下载提示页」。

    两个判据任一命中即可：

    - **端点名**：最终地址落在 ``downloadnotice.php``（最可靠）；
    - **确认文案**：正文含「下载种子文件」（端点名被站点改过时兜底）。
    """
    if _DOWNLOAD_NOTICE_PATH in str(resp.url).lower():
        return True
    return is_download_confirmation(resp.content)


def _describe_torrent_response(resp: httpx.Response) -> str:
    """把下载响应压成一行可诊断描述（status / content-type / 长度 / 前缀）。

    ⚠️ 只取**前 120 字节**且折叠换行——足够看出是登录页 / 限频提示 /
    Cloudflare 挑战，又不会把整页 HTML 或凭据写进日志。
    """
    content_type = resp.headers.get("content-type", "-")
    prefix = (
        resp.content[:120]
        .decode("utf-8", "replace")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return (
        f"HTTP {resp.status_code}, content-type={content_type}, "
        f"{len(resp.content)} bytes, 前缀={prefix!r}"
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
        self.supports_promotion_lookup = (
            str((self._rule or {}).get("parser") or "") in _PROMOTION_LOOKUP_PARSERS
        )
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
            "proxy": bool((get_settings().proxy_url or "").strip()),
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
        """下载 .torrent 字节。

        **失败分两类**（推送链路据此决定「入队重试」还是「直接报错」）：

        - **瞬时**（:class:`SiteFetchError`）：网络抖动、站点返回非 .torrent
          的临时页（HTML 提示页 / 会话检查页 / CF 挑战）→ 重试有意义。
          非 .torrent 的 2xx 响应会**就地重试一次**（见
          :data:`_NON_TORRENT_RETRY_DELAY`），两次都失败才抛异常；异常里
          带 status / content-type / 长度 / 前 120 字节前缀，便于事后定位
          （此前只报一句「响应不是 bencoded」，无法区分登录页、限频页与
          Cloudflare 挑战，2026-11-21 prod 排查时卡在这里）。
        - **永久**（:class:`SitePermanentError`）：登录态失效（详情页 /
          下载响应是登录页）、详情页里根本没有下载地址 → **重试也不会
          成功**，交上层报错给用户（而不是骗用户「已入队自动重试」）。

        **NexusPHP 下载提示页**（``downloadnotice.php``）不是失败：站点要求
        先勾选承诺项才放行下载，按协议确认一次即可拿到种子。

        :raises SitePermanentError: 永久失败（凭据失效 / 无下载地址）
        :raises SiteFetchError: 瞬时失败（网络抖动 / 响应不是 .torrent）
        :raises BreakerTripped: 站点返回 403 / 429（触发熔断）
        """
        url, reason = await self._resolve_download_url(item_id)
        if not url:
            if reason == "auth":
                raise SitePermanentError(
                    f"{self.profile.name} 登录态失效（详情页跳登录），"
                    f"无法下载（id={item_id}）"
                )
            if reason == "no_link":
                raise SitePermanentError(
                    f"{self.profile.name} 详情页里没有下载地址（id={item_id}）"
                )
            raise SiteFetchError(
                f"{self.profile.name} 无法解析下载地址（id={item_id}）"
            )

        recipe = parse_download_recipe(url)
        if recipe is not None:
            url = await self._exchange_download_recipe(recipe, item_id)

        resp = await self._fetch_torrent_response(url)
        if resp.content.startswith(_TORRENT_MAGIC):
            return resp.content

        # NexusPHP「下载提示页」：站点要求先确认（勾选承诺项 + 提交）才给
        # 种子。这是站点的正常协议，不是故障——按协议走一遍即可拿到种子。
        # 2026-11-23 prod：HDWinG 分享率 0.985 < 1，``download.php`` 落到
        # ``downloadnotice.php?type=ratio``，此前一律报「响应不是 bencoded」，
        # 重试 5 次全败 → 用户看到「推送失败」。
        if _is_download_notice(resp):
            return await self._confirm_download_notice(resp, item_id)

        if _looks_like_login_response(resp):
            # 登录页 → 凭据失效，重试没有意义（不必浪费一次重试）
            raise SitePermanentError(
                f"{self.profile.name} 下载返回登录页，凭据已失效（id={item_id}）"
            )

        first_desc = _describe_torrent_response(resp)
        logger.warning(
            "%s 下载 id=%s 返回非 .torrent（%s），%.0fs 后重试一次",
            self.profile.name, item_id, first_desc, _NON_TORRENT_RETRY_DELAY,
        )
        if _NON_TORRENT_RETRY_DELAY > 0:
            await asyncio.sleep(_NON_TORRENT_RETRY_DELAY)

        retry = await self._fetch_torrent_response(url)
        if retry.content.startswith(_TORRENT_MAGIC):
            logger.info(
                "%s 下载 id=%s 重试成功（首次：%s）",
                self.profile.name, item_id, first_desc,
            )
            return retry.content
        if _looks_like_login_response(retry):
            raise SitePermanentError(
                f"{self.profile.name} 下载返回登录页，凭据已失效（id={item_id}）"
            )

        raise SiteFetchError(
            f"{self.profile.name} 响应不是 bencoded torrent id={item_id}"
            f"（重试后：{_describe_torrent_response(retry)}；首次：{first_desc}）"
        )

    async def _exchange_download_recipe(
        self,
        recipe: tuple[dict[str, Any], str],
        item_id: str,
    ) -> str:
        """执行 API 站的两段式下载换票配方，返回真实 ``.torrent`` 下载地址。

        配方由专站 spider 的 ``get_download_url`` 产出（``[base64_json]url``），
        含 ``method`` / ``header`` / ``params`` / ``json`` / ``cookie`` /
        ``allow_redirects`` / ``result`` 等字段。结果经
        :func:`parse_download_response` 统一提取与规范化。

        :raises BreakerTripped: 站点返回 403 / 429
        :raises SitePermanentError: 401 鉴权失败（API Key / Token 无效）
        :raises SiteFetchError: 网络异常、其他 HTTP 错误或响应未含下载地址
        """
        config, endpoint = recipe
        method = str(config.get("method") or "get").upper()
        raw_headers = config.get("header")
        headers: dict[str, str] = (
            {str(k): str(v) for k, v in raw_headers.items() if v is not None}
            if isinstance(raw_headers, dict)
            else {}
        )
        headers.setdefault("User-Agent", self._user_agent)
        params = config.get("params") if isinstance(config.get("params"), dict) else None
        json_body = config.get("json") if isinstance(config.get("json"), dict) else None
        follow_redirects = bool(config.get("allow_redirects", True))
        use_cookie = bool(config.get("cookie", False))

        try:
            if use_cookie:
                resp = await self._client.request(
                    method,
                    endpoint,
                    headers=headers,
                    params=params,
                    json=json_body,
                    follow_redirects=follow_redirects,
                    timeout=self._timeout,
                )
            else:
                proxy_url = (get_settings().proxy_url or "").strip() or None
                async with httpx.AsyncClient(
                    headers=headers,
                    follow_redirects=follow_redirects,
                    timeout=self._timeout,
                    proxy=proxy_url if config.get("proxy") else None,
                ) as api_client:
                    resp = await api_client.request(
                        method,
                        endpoint,
                        params=params,
                        json=json_body,
                    )
        except httpx.HTTPError as exc:
            raise SiteFetchError(
                f"{self.profile.name} 换取下载链接失败 id={item_id}：{exc}"
            ) from exc

        if resp.status_code == 401:
            raise SitePermanentError(
                f"{self.profile.name} 换票鉴权失败（HTTP 401），请检查 API Key/Token"
                f"（id={item_id}）"
            )
        self._check_status_code(resp)

        payload: object = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        real_url = parse_download_response(
            config, resp.status_code, payload, resp.text
        )
        if not real_url:
            raise SiteFetchError(
                f"{self.profile.name} 换票响应未包含下载地址 id={item_id}"
                f"（{_describe_torrent_response(resp)}）"
            )
        return real_url

    async def _fetch_torrent_response(self, url: str) -> httpx.Response:
        """GET 下载地址并按统一契约检查状态码。

        :raises SiteFetchError: 网络请求抛出 ``httpx.HTTPError``，
            或响应状态码 >= 400（403 / 429 由熔断分支先拦）。
        :raises BreakerTripped: 站点返回 403 / 429，需要熔断。
        """
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise SiteFetchError(f"{self.profile.name} 下载失败：{exc}") from exc
        self._check_status_code(resp)
        return resp

    async def _confirm_download_notice(
        self, resp: httpx.Response, item_id: str
    ) -> bytes:
        """走完 NexusPHP 下载提示页的确认流程，取回种子。

        流程：解析提示页里的确认表单 → POST 提交 → 站点放行并重定向回
        ``download.php``（客户端自动跟随重定向）→ 拿到真种子。

        :return: 种子字节
        :raises SiteFetchError: 提示页没有可提交的表单 / 网络异常 /
            站点返回错误状态码 / 确认后仍未给出种子（**按瞬时失败处理**：
            无法确证是站点封禁，重试仍有可能成功）
        :raises BreakerTripped: 站点返回 403 / 429
        """
        parsed = parse_download_confirmation(resp.text, str(resp.url))
        if parsed is None:
            raise SiteFetchError(
                f"{self.profile.name} 下载提示页没有可提交的确认表单 id={item_id}"
                f"（{_describe_torrent_response(resp)}）"
            )
        target, data = parsed
        logger.info(
            "%s 下载 id=%s 命中下载提示页，提交确认表单（字段：%s）",
            self.profile.name, item_id, ",".join(sorted(data)),
        )
        try:
            confirmed = await self._client.post(
                target, data=data, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise SiteFetchError(
                f"{self.profile.name} 下载确认失败：{exc}"
            ) from exc
        self._check_status_code(confirmed)
        if not confirmed.content.startswith(_TORRENT_MAGIC):
            raise SiteFetchError(
                f"{self.profile.name} 下载提示页确认后仍未给出种子 id={item_id}"
                f"（{_describe_torrent_response(confirmed)}）"
            )
        logger.info(
            "%s 下载 id=%s 经下载提示页确认后取得种子（%d bytes）",
            self.profile.name, item_id, len(confirmed.content),
        )
        return confirmed.content

    # ------------------------------------------------------------------ 3.5 下载地址
    async def get_torrent_url(self, item_id: str) -> str | None:
        """实时返回可下载 URL（契约见基类）。

        失败一律返回 ``None``（不抛）——**失败原因分类**保留在
        :meth:`_resolve_download_url` 里，供推送链路区分永久 / 瞬时失败。
        """
        url, _ = await self._resolve_download_url(item_id)
        return url

    async def _resolve_download_url(self, item_id: str) -> tuple[str | None, str]:
        """解析下载地址，并给出**失败原因分类**。

        :return: ``(url, "")`` 成功；``(None, reason)`` 失败，``reason``：

            - ``auth``：详情页跳登录页（凭据失效）→ **永久失败**
            - ``unreachable``：详情页不可达（网络 / HTTP 错误）→ 瞬时失败
            - ``no_link``：详情页正常但没有下载地址 → **永久失败**
        """
        if not item_id:
            return None, "no_link"
        # API 站（m-team / hdarea / piggo）由专站 spider 生成下载配方
        api_url = await self._parser_download_url(item_id)
        if api_url:
            return api_url, ""
        detail_url = self._detail_url(item_id)
        try:
            resp = await self._client.get(detail_url, timeout=self._timeout)
        except httpx.HTTPError:
            return None, "unreachable"
        if resp.status_code >= 400:
            return None, "unreachable"
        if _looks_like_login(resp.text, resp.url):
            return None, "auth"
        url = _extract_download_url(resp.text, self._base_url_value, self._rule or {})
        if not url:
            return None, "no_link"
        return url, ""

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

    # ------------------------------------------------------------------ 5b. 优惠
    async def get_promotion(self, item_id: str) -> Promotion | None:
        """查单个种子的优惠历史，返回**当前生效**的 Free 优惠。

        HDWinG 的列表页优惠标记是空占位符，真实数据由页面 JS 调
        ``ajax_promotion.php`` 填充；该接口被 Cloudflare Turnstile 闸保护，
        未过闸时对**任意 id**（含已过期 / 不存在的）都返回兜底的
        ``pro_free``，不可信。服务端渲染的 ``promotion.php?id={tid}`` 是
        权威源（优惠类型 / 开始时间 / 截止时间 / 操作人）。

        返回 ``None`` = 该种子当前没有生效的优惠（不是免费种；不是查失败）。
        404 / 「页面不存在」= 种子被删或 id 不存在 → 也返回 ``None``。
        403/429 → :class:`BreakerTripped`；登录页（cookie 失效）→
        :class:`SitePermanentError`；其它失败 → :class:`SiteFetchError`。
        """
        if not self.supports_promotion_lookup:
            return None
        item_id = str(item_id or "").strip()
        if not item_id:
            return None
        url = urljoin(
            f"{self._base_url_value}/",
            f"promotion.php?id={quote(item_id)}",
        )
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise SiteFetchError(
                f"{self.profile.name} 优惠历史拉取失败 id={item_id}: {exc}"
            ) from exc
        if resp.status_code in (403, 429):
            raise BreakerTripped(
                f"{self.profile.name} 优惠历史返回 {resp.status_code}，需熔断"
            )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise SiteFetchError(
                f"{self.profile.name} 优惠历史返回 HTTP {resp.status_code} id={item_id}"
            )
        if _looks_like_login_response(resp):
            raise SitePermanentError(
                f"{self.profile.name} 优惠历史返回登录页（cookie 可能已失效）"
                f" id={item_id}"
            )
        html = resp.text
        if "页面不存在" in html:
            return None
        rows, header_seen = _parse_promotion_rows(html)
        if not header_seen:
            raise SiteFetchError(
                f"{self.profile.name} 优惠历史页结构异常（缺少优惠表头）"
                f" id={item_id}"
            )
        return _current_promotion(rows, bj_now_dt())

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
        # TTG 路径形如 `/t/{id}/` 或 `/dl/{id}/{auth}`（末段是短时票据）
        ttg_id = TTGDownloadAdapter.torrent_id_from_url(text)
        if ttg_id:
            return ttg_id
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


@dataclass(frozen=True, slots=True)
class _PromotionRow:
    """``promotion.php`` 表格中的一行 Free 记录（含过期删除线标记）。"""

    start_at: str | None
    end_at: str | None
    expired: bool


def _cell_text(cell: Selector) -> str:
    """表格单元格文本（折叠空白）。"""
    return re.sub(r"\s+", " ", "".join(cell.css("::text").getall())).strip()


def _parse_promotion_rows(html: str) -> tuple[list[_PromotionRow], bool]:
    """解析 ``promotion.php`` 优惠历史表。

    Returns: ``(Free 行列表, 是否看到优惠表头)``。表头是「页面结构正常」
    的判据：缺少表头时不能把页面当成「没有优惠」——可能是登录页 / CF
    挑战 / 站点改版，调用方应抛错而不是写入错误判定。

    过期行在站点页面里带 ``style='text-decoration: line-through;'``；
    同时保留时间字段，最终由 :func:`_current_promotion` 按「开始时间 <= 现在
    <= 截止时间」再次校验（截止为空 = 永久）。
    """
    doc = Selector(text=html)
    rows: list[_PromotionRow] = []
    header_seen = False
    for tr in doc.css("tr"):
        cells = [_cell_text(cell) for cell in tr.xpath("./td|./th")]
        if not cells:
            continue
        if (
            "优惠类型" in cells
            and "开始时间" in cells
            and "截止时间" in cells
        ):
            header_seen = True
            continue
        if len(cells) < 3 or cells[0].strip().lower() != "free":
            continue
        start_raw = cells[1].strip()
        end_raw = cells[2].strip()
        rows.append(
            _PromotionRow(
                start_at=parse_abs_datetime(start_raw),
                end_at=parse_abs_datetime(end_raw) if end_raw else None,
                expired="line-through" in (tr.attrib.get("style") or "").lower(),
            )
        )
    return (rows, header_seen)


def _current_promotion(
    rows: list[_PromotionRow],
    now: datetime,
) -> Promotion | None:
    """从 Free 行里挑出**当前生效**的那条。

    判定：未被删除线标记、开始时间 <= 现在、截止时间为空或 > 现在。
    多条同时生效时取**截止最晚**的一条（永久优先）。
    """
    active: list[Promotion] = []
    for row in rows:
        if row.expired:
            continue
        start = parse_bj(row.start_at)
        if start is None or start > now:
            continue
        end = parse_bj(row.end_at)
        if end is not None and end <= now:
            continue
        active.append(
            Promotion(kind="Free", start_at=row.start_at, end_at=row.end_at)
        )
    if not active:
        return None
    return max(
        active,
        key=lambda promo: (
            promo.end_at is None,
            promo.end_at or "",
            promo.start_at or "",
        ),
    )


def _promo_payload(row: dict[str, Any], rule: dict[str, Any]) -> dict[str, object] | None:
    """把规则解析出的倍率/截止时间翻译成 ``window_parser`` 认识的输入。"""
    factor = row.get("downloadvolumefactor")
    upload_factor = row.get("uploadvolumefactor")
    free_classes: list[str] = []
    try:
        if factor is not None:
            promo_cls = _PROMO_CLASS_BY_FACTOR.get(float(factor))
            if promo_cls:
                free_classes.append(promo_cls)
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
