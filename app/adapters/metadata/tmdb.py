"""TMDB 官方 API 客户端（只做影视；音乐不在本项目范围）。

**为什么自己封而不直接用 ``tmdbv3api``**：那个库是同步 requests 风格，
且本项目只需要 5 个端点（search / detail / season / images / 换图基址）——
引入整库要连带处理同步阻塞、线程池与全局 session，与「事件循环零同步 IO」
的铁律冲突。这里用 httpx async 重新封装，客户端由调用方注入（复用连接池、
便于 respx 打桩）。

**失败语义**（与项目「拉取失败必须抛错」一致）：

- **查不到**（404 / 空结果）→ 返回 ``None``：这是正常业务结果
- **查挂了**（网络错误 / 5xx / 401）→ 抛 :class:`TmdbError`：**绝不能**折叠成
  ``None``——那会把「TMDB 挂了」伪装成「这部片子不存在」，用户看到的是
  一堆莫名其妙的「识别失败」，而真正的原因（Key 失效 / 网络故障）被吞掉
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.domain.scrape_types import EpisodeMeta, MediaKind, MediaMeta, SeasonMeta

logger = get_logger(__name__)

__all__ = ["TmdbClient", "TmdbError", "normalize_title"]


#: TMDB API 基址
TMDB_API_BASE: Final = "https://api.themoviedb.org/3"

#: TMDB 图片基址（``{base}/{size}{file_path}``）
TMDB_IMAGE_BASE: Final = "https://image.tmdb.org/t/p"

#: 默认语言（简体中文）
DEFAULT_LOCALE: Final = "zh-CN"

#: 429 / 5xx 的最大重试次数
MAX_RETRIES: Final = 3

#: 重试基础退避秒数（指数增长）
RETRY_BACKOFF_SEC: Final = 1.5

#: 图片尺寸档位
IMAGE_SIZE_ORIGINAL: Final = "original"
IMAGE_SIZE_POSTER: Final = "w500"
IMAGE_SIZE_BACKDROP: Final = "w780"
IMAGE_SIZE_LOGO: Final = "w500"


class TmdbError(RuntimeError):
    """TMDB 交互失败（网络错误 / 鉴权失败 / 服务端错误）。

    与「查不到」（返回 ``None``）严格区分。
    """


def normalize_title(text: str) -> str:
    """标题归一化：小写 + 去空白与标点。

    ``"The Lord of the Rings"`` → ``"thelordoftherings"``。

    **公开**（服务层的别名表要用同一套口径）——归一化规则必须处处一致，
    否则「识别时能匹配、存别名时对不上」。
    """
    return re.sub(r"[\s\.\-_·:：,，!！?？'\"()（）\[\]【】]+", "", text).lower()


def _is_containment_match(query: str, candidate: str) -> bool:
    """一方是另一方的子串且长度足够——「副标题」型标题的常见形态。

    ``"the lord of the rings"`` vs ``"the lord of the rings extended"``。

    **下限 4 字符**：太短的串（``"the"``、``"爱"``）子串命中是噪声，
    会把毫不相干的条目判成强匹配。
    """
    if len(query) < 4 or len(candidate) < 4:
        return False
    return query in candidate or candidate in query


def _pick_best_image(images: list[dict[str, Any]]) -> str | None:
    """从图片列表里挑评分最高的一张。

    排序键：``vote_average`` → ``vote_count``（与 MoviePilot 的
    ``__pick_best_image_path`` 同款口径——先看平均分，再看投票人数）。

    :param images: TMDB images 响应里的一类图片数组
    :return: 相对路径；没有可用图片时返回 None
    """
    if not images:
        return None
    ordered = sorted(
        images,
        key=lambda item: (
            item.get("vote_average") or 0.0,
            item.get("vote_count") or 0,
        ),
        reverse=True,
    )
    path = ordered[0].get("file_path")
    return str(path) if path else None


class TmdbClient:
    """TMDB 客户端。

    :param api_key: TMDB API Key（v3 的 ``api_key`` 参数形式）
    :param locale: 元数据语言，默认 ``zh-CN``
    :param http: 注入的 HTTP 客户端；不传则自建（调用方负责关闭）
    :param api_base: API 基址（测试时可指向本地 mock）
    :param image_base: 图片基址
    """

    def __init__(
        self,
        api_key: str,
        *,
        locale: str = DEFAULT_LOCALE,
        http: httpx.AsyncClient | None = None,
        api_base: str = TMDB_API_BASE,
        image_base: str = TMDB_IMAGE_BASE,
        proxy: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._locale = locale
        self._http = http
        self._owns_http = http is None
        self._api_base = api_base.rstrip("/")
        self._image_base = image_base.rstrip("/")
        #: 代理地址（``http://`` / ``socks5://``）——国内直连 TMDB 经常不通，
        #: 走代理由调用方按配置决定。仅对**自建** client 生效（注入的
        #: client 归调用方管，不在构造时改写）。
        self._proxy = proxy or None

    # ---------------------------------------------------------------- 内部

    def _client(self) -> httpx.AsyncClient:
        """取 HTTP 客户端（未注入时按需创建，带配置的代理）。"""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0), proxy=self._proxy
            )
        return self._http

    async def aclose(self) -> None:
        """关闭自建的 HTTP 客户端（注入的客户端不归本类管）。"""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一次 GET，返回 JSON。

        429 / 5xx 退避重试；404 返回空 dict（调用方按「查不到」处理）；
        其它错误抛 :class:`TmdbError`。

        :param path: API 路径（相对 :data:`TMDB_API_BASE`）
        :param params: 额外查询参数
        :return: 响应 JSON
        :raises TmdbError: 网络故障 / 鉴权失败 / 重试后仍失败
        """
        query = {"api_key": self._api_key, "language": self._locale}
        if params:
            query.update(params)

        url = f"{self._api_base}{path}"
        last_error = ""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self._client().get(url, params=query)
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{exc}"
            else:
                if response.status_code == 404:
                    # 查不到 —— 正常业务结果，不是故障
                    return {}
                if response.status_code == 401:
                    raise TmdbError("TMDB 鉴权失败（API Key 无效或已过期）")
                if response.status_code == 429:
                    last_error = "TMDB 触发限流（429）"
                elif response.status_code >= 500:
                    last_error = f"TMDB 服务端错误（{response.status_code}）"
                elif response.status_code >= 400:
                    raise TmdbError(
                        f"TMDB 请求失败（{response.status_code}）：{response.text[:200]}"
                    )
                else:
                    try:
                        return dict(response.json())
                    except ValueError as exc:
                        raise TmdbError(f"TMDB 响应不是合法 JSON：{exc}") from exc

            if attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "TMDB 请求失败（第 %d/%d 次），%.1fs 后重试：%s",
                    attempt,
                    MAX_RETRIES,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)

        raise TmdbError(f"TMDB 请求失败（重试 {MAX_RETRIES} 次）：{last_error}")

    # ---------------------------------------------------------------- 搜索

    async def search(
        self,
        title: str,
        *,
        year: int | None = None,
        media_type: str = MediaKind.MOVIE,
    ) -> MediaMeta | None:
        """按标题搜索媒体（单标题）。

        :param title: 标题（不带年份）
        :param year: 年份（可选，用于消歧）
        :param media_type: ``movie`` 或 ``tv``
        :return: 最佳匹配；查不到返回 None
        :raises TmdbError: 接口故障
        """
        return await self.search_any([title], year=year, media_type=media_type)

    async def search_any(
        self,
        titles: Sequence[str],
        *,
        year: int | None = None,
        media_type: str | None = None,
    ) -> MediaMeta | None:
        """按**多个候选标题**搜索（exact 命中优先）。

        为什么需要多候选：资源文件名里的英文名未必是 TMDB 上的名字。
        实测 ``Light the Dark``（2026 港剧）在 TMDB 上会搜到《黑夜告白》
        ——因为该剧的 TMDB ``name`` 是中文，英文查询无法「完全相等」，
        只能按 popularity 决胜，于是热度更高的**另一部剧**赢了。而方括号
        里 publishers 给的中文译名（``[把黑夜点燃]``）恰是 TMDB 上的正式
        名，用它搜能**唯一命中**。

        策略：

        1. 依次用每个候选标题、每种媒体类型搜索
        2. 任一结果**标题完全相等**（归一化后）→ 立即采用（强证据）
        3. 都不相等 → 取第一个候选的模糊命中（子串命中优先于 popularity）
        """
        candidates = [t.strip() for t in titles if t and t.strip()]
        if not candidates:
            return None

        media_types = [media_type] if media_type else [MediaKind.MOVIE, MediaKind.TV]
        fallback: MediaMeta | None = None

        for title in candidates:
            for current_type in media_types:
                data = await self._search_raw(title, year=year, media_type=current_type)
                results = data.get("results") or []
                exact = self._pick_exact(results, title)
                if exact is not None:
                    return self._to_media_meta(exact, media_type=current_type)
                if fallback is None:
                    picked = self._pick_search_result(
                        results, title=title, year=year
                    )
                    if picked is not None:
                        fallback = self._to_media_meta(picked, media_type=current_type)
        return fallback

    async def _search_raw(
        self, title: str, *, year: int | None, media_type: str
    ) -> dict[str, Any]:
        """发一次搜索请求，返回原始 JSON。"""
        params: dict[str, Any] = {"query": title, "include_adult": "false"}
        if media_type == MediaKind.TV:
            if year:
                params["first_air_date_year"] = year
            return await self._get("/search/tv", params)
        if year:
            params["year"] = year
        return await self._get("/search/movie", params)

    @staticmethod
    def _pick_exact(
        results: list[dict[str, Any]], title: str
    ) -> dict[str, Any] | None:
        """挑出标题**完全相等**的结果（强证据）。

        比对 ``name`` / ``title`` / ``original_name`` / ``original_title``
        四个字段——中文条目在 zh-CN locale 下 ``name`` 就是中文名。
        """
        query = normalize_title(title)
        if not query:
            return None
        for item in results:
            for key in ("name", "title", "original_name", "original_title"):
                value = item.get(key)
                if value and normalize_title(str(value)) == query:
                    return item
        return None

    @staticmethod
    def _pick_search_result(
        results: list[dict[str, Any]],
        *,
        title: str,
        year: int | None,
    ) -> dict[str, Any] | None:
        """从搜索结果里挑最佳匹配（模糊层）。

        优先级：
        1. 标题归一化后**完全等于**查询词（TMDB 按热度排序，首条未必最准）
        2. 标题**互相包含**（副标题型标题，如 ``X`` vs ``X Extended``）
        3. 年份一致
        4. 热度（popularity）

        ⚠️ 热度是**最弱**的证据：实测两个 2026 年的剧会靠 popularity 分出
        高下，而热度与「是不是用户要找的那部」毫无关系。**中文译名候选
        （exact 命中）才是可靠路径**——见 :meth:`search_any`。

        :param results: TMDB 搜索结果数组
        :param title: 查询标题
        :param year: 查询年份
        :return: 选中的结果；无结果返回 None
        """
        if not results:
            return None
        query = normalize_title(title)
        exact: list[dict[str, Any]] = []
        contained: list[dict[str, Any]] = []
        year_match: list[dict[str, Any]] = []
        for item in results:
            names = [
                item.get("title"),
                item.get("name"),
                item.get("original_title"),
                item.get("original_name"),
            ]
            normalized = [normalize_title(str(n)) for n in names if n]
            if query in normalized:
                exact.append(item)
                continue
            if any(_is_containment_match(query, n) for n in normalized):
                contained.append(item)
                continue
            item_year = _extract_year(item.get("release_date") or item.get("first_air_date"))
            if year and item_year == year:
                year_match.append(item)
        if exact:
            return max(exact, key=lambda item: item.get("popularity") or 0)
        if contained:
            return max(contained, key=lambda item: item.get("popularity") or 0)
        if year_match:
            return max(year_match, key=lambda item: item.get("popularity") or 0)
        return max(results, key=lambda item: item.get("popularity") or 0)

    # ---------------------------------------------------------------- 详情

    async def get_movie(self, tmdb_id: int) -> MediaMeta | None:
        """取电影详情（含演职员与图片）。

        :param tmdb_id: TMDB 电影 ID
        :return: 详情；不存在返回 None
        :raises TmdbError: 接口故障
        """
        data = await self._get(f"/movie/{tmdb_id}", {"append_to_response": "credits,images"})
        if not data:
            return None
        return self._to_media_meta(data, media_type=MediaKind.MOVIE, detail=True)

    async def get_tv(self, tmdb_id: int) -> MediaMeta | None:
        """取剧集详情（含分季摘要、演职员与图片）。

        :param tmdb_id: TMDB 剧集 ID
        :return: 详情；不存在返回 None
        :raises TmdbError: 接口故障
        """
        data = await self._get(f"/tv/{tmdb_id}", {"append_to_response": "credits,images"})
        if not data:
            return None
        return self._to_media_meta(data, media_type=MediaKind.TV, detail=True)

    async def get_season(self, tmdb_id: int, season: int) -> SeasonMeta | None:
        """取季详情（含分集清单）。

        :param tmdb_id: TMDB 剧集 ID
        :param season: 季号（0 = 特别篇）
        :return: 季详情；不存在返回 None
        :raises TmdbError: 接口故障
        """
        data = await self._get(f"/tv/{tmdb_id}/season/{season}")
        if not data:
            return None
        episodes = tuple(
            EpisodeMeta(
                episode_number=int(item.get("episode_number") or 0),
                name=item.get("name") or None,
                overview=item.get("overview") or None,
                still_path=item.get("still_path") or None,
                air_date=item.get("air_date") or None,
            )
            for item in (data.get("episodes") or [])
            if item.get("episode_number") is not None
        )
        return SeasonMeta(
            season_number=int(data.get("season_number") or season),
            name=data.get("name") or None,
            overview=data.get("overview") or None,
            poster_path=data.get("poster_path") or None,
            air_date=data.get("air_date") or None,
            episodes=episodes,
        )

    # ---------------------------------------------------------------- 转换

    def _to_media_meta(
        self,
        data: dict[str, Any],
        *,
        media_type: str,
        detail: bool = False,
    ) -> MediaMeta:
        """把 TMDB 响应转成 :class:`MediaMeta`。

        :param data: TMDB 响应
        :param media_type: ``movie`` / ``tv``
        :param detail: 是否是详情响应（详情才带演职员与 images）
        """
        images = data.get("images") or {}
        poster_path = data.get("poster_path") or None
        backdrop_path = data.get("backdrop_path") or None
        logo_path = None

        if detail:
            # 当前语言没有图时，用 images 接口按评分回填（MoviePilot 同款做法）
            if not poster_path:
                poster_path = _pick_best_image(images.get("posters") or [])
            if not backdrop_path:
                backdrop_path = _pick_best_image(images.get("backdrops") or [])
            logo_path = _pick_best_image(images.get("logos") or [])

        credits = data.get("credits") or {}
        directors = tuple(
            str(item.get("name"))
            for item in (credits.get("crew") or [])
            if item.get("job") == "Director" and item.get("name")
        )
        actors = tuple(
            str(item.get("name"))
            for item in (credits.get("cast") or [])[:10]
            if item.get("name")
        )
        genres = tuple(
            str(item.get("name"))
            for item in (data.get("genres") or [])
            if item.get("name")
        )

        seasons: tuple[SeasonMeta, ...] = ()
        if media_type == MediaKind.TV:
            seasons = tuple(
                SeasonMeta(
                    season_number=int(item.get("season_number") or 0),
                    name=item.get("name") or None,
                    overview=item.get("overview") or None,
                    poster_path=item.get("poster_path") or None,
                    air_date=item.get("air_date") or None,
                )
                for item in (data.get("seasons") or [])
                if item.get("season_number") is not None
            )

        release_date = data.get("release_date") or data.get("first_air_date") or None
        title = data.get("title") or data.get("name") or ""
        original_title = data.get("original_title") or data.get("original_name") or None

        return MediaMeta(
            tmdb_id=int(data.get("id") or 0),
            media_type=media_type,
            title=title,
            original_title=original_title,
            original_language=data.get("original_language") or None,
            year=_extract_year(release_date),
            release_date=release_date,
            overview=data.get("overview") or None,
            vote_average=_as_float(data.get("vote_average")),
            poster_path=poster_path,
            backdrop_path=backdrop_path,
            logo_path=logo_path,
            genres=genres,
            directors=directors,
            actors=actors,
            runtime=data.get("runtime") or None,
            status=data.get("status") or None,
            season_count=data.get("number_of_seasons") or None,
            episode_count=data.get("number_of_episodes") or None,
            seasons=seasons,
        )

    # ---------------------------------------------------------------- 图片

    def image_url(self, path: str | None, size: str = IMAGE_SIZE_ORIGINAL) -> str | None:
        """相对路径 → 完整图片地址。

        :param path: TMDB 相对路径（如 ``/abc.jpg``）
        :param size: 尺寸档位
        :return: 完整地址；``path`` 为空返回 None
        """
        if not path:
            return None
        return f"{self._image_base}/{size}{path}"


def _extract_year(date_text: str | None) -> int | None:
    """从日期字符串取年份（``2025-01-01`` → 2025）。"""
    if not date_text or len(date_text) < 4:
        return None
    try:
        return int(date_text[:4])
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    """宽松转 float。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
