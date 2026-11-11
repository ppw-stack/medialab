"""刮削编排：识别 → 生成 NFO / 图片 → 落盘 → 落库。

**分层**：服务层不 import fastapi（抛 :class:`ServiceError` 由 API 翻译）。

**为什么不移植 MoviePilot 的刮削链**：那条链是「同步 + FileItem 抽象 +
事件驱动」的，绑在它的存储抽象与作业模型上。本项目只需要「本地目录 → 本地
目录」这一段，故按本项目约定重写：本地路径进、本地路径出，识别与生成
复用 :mod:`app.adapters.metadata` 下的三个纯逻辑模块。

**失败语义**（项目铁律）：识别不到 / 接口故障 / 写盘失败，一律**记 failed
并点名**，不写「空结果兜底」——把故障伪装成「没内容」会让用户看到一堆
莫名其妙的「识别失败」，而真正的原因（Key 失效、网络故障）被吞掉。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.adapters.metadata import naming
from app.adapters.metadata.nfo_builder import (
    build_episode_nfo,
    build_movie_nfo,
    build_season_nfo,
    build_tvshow_nfo,
)
from app.adapters.metadata.tmdb import TmdbClient, TmdbError, normalize_title
from app.core.logging import get_logger
from app.db.models import MediaScrape, ScrapeAlias, ScrapeCache
from app.db.session import session_scope
from app.domain.scrape_types import (
    EpisodeMeta,
    MediaKind,
    MediaMeta,
    ScrapeKind,
    ScrapePolicy,
    ScrapeStatus,
    SeasonMeta,
)
from app.services.env_settings import get_env_setting
from app.services.errors import ServiceError
from app.services.meta_parser import parse_meta, season_number_from_dir
from app.services.runtime_params import get_runtime_params

logger = get_logger(__name__)

__all__ = [
    "ScrapeOutcome",
    "ScrapeService",
    "ScrapeTarget",
    "build_scope_key",
]


#: 识别缓存有效期（秒）——7 天
CACHE_TTL_SECONDS = 7 * 24 * 3600

#: 单张图片大小上限（字节）——20MB。TMDB 原图不会超过这个量级。
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: 图片下载超时（秒）
IMAGE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ScrapeTarget:
    """刮削目标（产物落点）。

    :param directory: 产物所在目录（视频同级 / 季目录 / 剧集根目录）
    :param kind: 刮削层级（movie / tvshow / season / episode）
    :param stem: 视频文件名主干（电影 / 单集用）
    :param season: 季号
    :param episode: 集号
    """

    directory: Path
    kind: str
    stem: str | None = None
    season: int | None = None
    episode: int | None = None


@dataclass
class ScrapeOutcome:
    """刮削结果。

    :param status: scraped / failed / skipped
    :param scope_key: 刮削单元键
    :param nfo_path: NFO 落盘路径
    :param images: 已写出的图片清单
    :param error: 失败原因（失败必须点名）
    """

    status: str
    scope_key: str
    nfo_path: Path | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """是否成功（含「按策略跳过」——那不是失败）。"""
        return self.status in (ScrapeStatus.SCRAPED, ScrapeStatus.SKIPPED)


def build_scope_key(target: ScrapeTarget) -> str:
    """构造刮削单元唯一键。

    ==========================  ==========================================
    scope_key                   含义
    ==========================  ==========================================
    ``movie|<目录>``            电影
    ``tvshow|<剧集根目录>``      电视剧整剧
    ``season|<季目录>|S1``       季
    ``episode|<目录>|S1E2``     单集
    ==========================  ==========================================

    :param target: 刮削目标
    :return: 唯一键
    """
    directory = str(target.directory)
    if target.kind == ScrapeKind.SEASON:
        return f"season|{directory}|S{target.season}"
    if target.kind == ScrapeKind.EPISODE:
        return f"episode|{directory}|S{target.season}E{target.episode}"
    return f"{target.kind}|{directory}"


class ScrapeService:
    """刮削服务。

    :param tmdb: TMDB 客户端；不传则按配置构建（无 Key 时刮削不可用）
    :param http: 图片下载用的 HTTP 客户端；不传则自建
    """

    def __init__(
        self,
        *,
        tmdb: TmdbClient | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._tmdb = tmdb
        self._http = http
        self._owns_http = http is None

    # ---------------------------------------------------------------- 配置

    @staticmethod
    def api_key() -> str:
        """当前配置的 TMDB API Key（空 = 未启用刮削）。"""
        return get_env_setting("tmdb_api_key").strip()

    @staticmethod
    def policy() -> str:
        """当前刮削策略（默认仅补缺）。"""
        value = get_runtime_params().get_int("scrape_policy")
        return ScrapePolicy.OVERWRITE if value == 1 else ScrapePolicy.MISSING_ONLY

    def _client(self) -> TmdbClient:
        """取 TMDB 客户端。

        :raises ServiceError: 未配置 API Key
        """
        if self._tmdb is not None:
            return self._tmdb
        key = self.api_key()
        if not key:
            raise ServiceError("未配置 TMDB API Key，刮削功能不可用", status=400)
        self._tmdb = TmdbClient(key)
        return self._tmdb

    def _url_client(self) -> TmdbClient:
        """取用于拼图片地址的客户端（无 Key 也能用）。"""
        if self._tmdb is not None:
            return self._tmdb
        return TmdbClient(self.api_key())

    def _http_client(self) -> httpx.AsyncClient:
        """取图片下载客户端（自建时跟随重定向）。"""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(IMAGE_TIMEOUT_SECONDS),
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        """关闭自建的 HTTP 客户端。"""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    # ---------------------------------------------------------------- 识别

    async def recognize(
        self,
        title: str,
        *,
        year: int | None = None,
        media_type: str | None = None,
        alt_titles: Sequence[str] = (),
    ) -> MediaMeta | None:
        """识别媒体（别名 → 缓存 → 搜索）。

        **别名优先**：用户在 :class:`~app.db.models.ScrapeAlias` 里固化过
        「这个标题 = 那个 TMDB 条目」时，直接按 ID 取详情——**跳过搜索**，
        既省一次请求，也不再受 popularity 猜错的影响。

        **多候选标题**：中文译名候选优先——它往往就是 TMDB 上的正式名，
        搜索它能唯一命中；英文名只作兜底（见
        :meth:`app.adapters.metadata.tmdb.TmdbClient.search_any`）。

        **电影 / 电视剧互试**：文件名通常看不出是电影还是剧集（没有集号时
        尤其如此）——先按指定类型搜（默认 movie），搜不到再试另一种。

        :param title: 标题
        :param year: 年份（可选，用于消歧）
        :param media_type: 限定媒体类型；``None`` = 自动
        :param alt_titles: 备用标题候选（如方括号里的中文译名）
        :return: 识别结果；查不到返回 None
        :raises ServiceError: TMDB 故障（鉴权失败 / 网络错误）
        """
        if not title.strip():
            return None

        titles = _title_candidates(title, alt_titles)

        # 别名表优先（用户固化过的映射最可信）
        for candidate in titles:
            alias = await self._alias_lookup(candidate)
            if alias is not None:
                detail = await self._fetch_detail(alias["media_type"], alias["tmdb_id"])
                if detail is not None:
                    return detail

        cache_key = "|".join(
            [media_type or "any", *(t.strip() for t in titles), str(year or "")]
        )
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        client = self._client()
        try:
            found = await client.search_any(
                titles, year=year, media_type=media_type
            )
        except TmdbError as exc:
            raise ServiceError(f"TMDB 识别失败：{exc}", status=502) from exc
        if found is None:
            return None

        await self._cache_put(cache_key, found)
        return found

    async def _alias_lookup(self, title: str) -> dict[str, Any] | None:
        """查别名表（按归一化标题）。

        :param title: 标题
        :return: ``{"tmdb_id": int, "media_type": str}``；未命中返回 None
        """
        key = normalize_title(title)
        if not key:
            return None
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ScrapeAlias).where(ScrapeAlias.alias_key == key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type or MediaKind.MOVIE,
            }

    async def _cache_get(self, cache_key: str) -> MediaMeta | None:
        """读识别缓存（未过期才返回）。"""
        now = int(time.time())
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ScrapeCache).where(ScrapeCache.cache_key == cache_key)
                )
            ).scalar_one_or_none()
            if row is None or row.expires_at <= now:
                return None
            payload = dict(row.payload or {})
            tmdb_id = payload.get("tmdb_id")
            if not isinstance(tmdb_id, int):
                return None
            return _meta_from_payload(payload)

    async def _cache_put(self, cache_key: str, meta: MediaMeta) -> None:
        """写识别缓存。

        缓存的是**搜索命中**的轻量结果（不含演职员与图片）；详情在刮削时
        现拉——演职员变动不频繁，但图片 URL 里的尺寸/语言可能随配置变化。
        """
        payload = {
            "tmdb_id": meta.tmdb_id,
            "media_type": meta.media_type,
            "title": meta.title,
            "year": meta.year,
            "original_title": meta.original_title,
        }
        expires_at = int(time.time()) + CACHE_TTL_SECONDS
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ScrapeCache).where(ScrapeCache.cache_key == cache_key)
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    ScrapeCache(
                        cache_key=cache_key,
                        payload=payload,
                        expires_at=expires_at,
                    )
                )
            else:
                row.payload = payload
                row.expires_at = expires_at

    # ---------------------------------------------------------------- 刮削

    async def scrape(
        self,
        target: ScrapeTarget,
        mediainfo: MediaMeta,
        *,
        policy: str | None = None,
        season_meta: SeasonMeta | None = None,
        episode_meta: EpisodeMeta | None = None,
        tmdb_id: int | None = None,
        media_path: str | None = None,
        extra_image_root: Path | None = None,
        extra_prefix: str | None = None,
    ) -> ScrapeOutcome:
        """刮削一个目标。

        :param target: 刮削目标
        :param mediainfo: 媒体元数据
        :param policy: 刮削策略；不传则用当前配置
        :param season_meta: 季元数据（季层级用）
        :param episode_meta: 单集元数据（单集层级用）
        :param tmdb_id: 指定 TMDB ID（手动刮削用——识别错了要能纠正）
        :param media_path: 被刮削的媒体文件路径（记进记录，供「一键修正」定位）
        :param extra_image_root: 额外图片根目录（季图写剧集根用）
        :param extra_prefix: 额外图片文件名前缀（如 ``season01``）
        :return: 刮削结果
        """
        effective_policy = policy or self.policy()
        scope_key = build_scope_key(target)

        if tmdb_id and tmdb_id != mediainfo.tmdb_id:
            refreshed = await self._fetch_detail(mediainfo.media_type, tmdb_id)
            if refreshed is None:
                outcome = ScrapeOutcome(
                    status=ScrapeStatus.FAILED,
                    scope_key=scope_key,
                    error=f"TMDB ID {tmdb_id} 不存在",
                )
                await self._save_outcome(
                    target, outcome, mediainfo, media_path=media_path
                )
                return outcome
            mediainfo = refreshed

        # 原语言图片：中文海报常缺图 / 画质差 → 改用原语言的图片
        mediainfo = await self._apply_original_language_images(mediainfo)

        nfo_path = naming.nfo_path(target.directory, target.kind, target.stem)
        nfo_content = self._build_nfo(
            target, mediainfo, season_meta=season_meta, episode_meta=episode_meta
        )
        if nfo_content is None:
            outcome = ScrapeOutcome(
                status=ScrapeStatus.FAILED,
                scope_key=scope_key,
                error=f"无法生成 {target.kind} 的 NFO",
            )
            await self._save_outcome(target, outcome, mediainfo)
            return outcome

        exists = await asyncio.to_thread(nfo_path.exists)
        if exists and effective_policy == ScrapePolicy.MISSING_ONLY:
            outcome = ScrapeOutcome(
                status=ScrapeStatus.SKIPPED,
                scope_key=scope_key,
                nfo_path=nfo_path,
            )
            await self._save_outcome(
                target, outcome, mediainfo, media_path=media_path
            )
            return outcome

        try:
            await asyncio.to_thread(
                self._write_text, nfo_path, nfo_content
            )
        except OSError as exc:
            outcome = ScrapeOutcome(
                status=ScrapeStatus.FAILED,
                scope_key=scope_key,
                error=f"写入 NFO 失败：{exc}",
            )
            await self._save_outcome(
                target, outcome, mediainfo, media_path=media_path
            )
            return outcome

        images, image_errors = await self._scrape_images(
            target,
            mediainfo,
            season_meta=season_meta,
            episode_meta=episode_meta,
            extra_root=extra_image_root,
            extra_prefix=extra_prefix,
        )

        error = "；".join(image_errors) if image_errors else None
        outcome = ScrapeOutcome(
            status=ScrapeStatus.SCRAPED,
            scope_key=scope_key,
            nfo_path=nfo_path,
            images=images,
            error=error,
        )
        await self._save_outcome(
            target, outcome, mediainfo, media_path=media_path
        )
        return outcome

    async def _fetch_detail(
        self, media_type: str | None, tmdb_id: int
    ) -> MediaMeta | None:
        """按 ID 取详情。

        :param media_type: ``movie`` / ``tv``；``None`` = 两种都试（电影优先）
        :param tmdb_id: TMDB ID
        :return: 详情；不存在返回 None
        :raises ServiceError: TMDB 故障
        """
        client = self._client()
        order = (
            [media_type]
            if media_type
            else [MediaKind.MOVIE, MediaKind.TV]
        )
        for current in order:
            try:
                if current == MediaKind.TV:
                    result = await client.get_tv(tmdb_id)
                else:
                    result = await client.get_movie(tmdb_id)
            except TmdbError as exc:
                raise ServiceError(f"TMDB 取详情失败：{exc}", status=502) from exc
            if result is not None:
                return result
        return None

    async def recognize_full(
        self,
        title: str,
        *,
        year: int | None = None,
        media_type: str | None = None,
        tmdb_id: int | None = None,
        alt_titles: Sequence[str] = (),
    ) -> MediaMeta | None:
        """识别并取**详情**（含演职员与图片路径）。

        搜索命中只给标题与 ID——写 NFO 还需要演职员、简介、图片路径，
        这些只有详情接口才有。故刮削链路用本方法（两次请求）。

        指定 :param tmdb_id: 时跳过搜索直接按 ID 取详情（纠正识别错误）。

        :param title: 标题
        :param year: 年份
        :param media_type: 限定媒体类型；``None`` = 自动
        :param tmdb_id: 直接指定 TMDB ID（跳过搜索）
        :param alt_titles: 备用标题候选（如方括号里的中文译名）
        :return: 完整元数据；查不到返回 None
        :raises ServiceError: TMDB 故障
        """
        if tmdb_id:
            guess = media_type or MediaKind.MOVIE
            return await self._fetch_detail(guess, tmdb_id)
        light = await self.recognize(
            title, year=year, media_type=media_type, alt_titles=alt_titles
        )
        if light is None:
            return None
        return await self._fetch_detail(light.media_type, light.tmdb_id)

    async def get_season(
        self, tmdb_id: int, season: int
    ) -> SeasonMeta | None:
        """取季详情（含分集清单与季海报）。

        :param tmdb_id: TMDB 剧集 ID
        :param season: 季号
        :return: 季详情；不存在返回 None
        :raises ServiceError: TMDB 故障
        """
        client = self._client()
        try:
            return await client.get_season(tmdb_id, season)
        except TmdbError as exc:
            raise ServiceError(f"TMDB 取季详情失败：{exc}", status=502) from exc

    # ---------------------------------------------------------------- 单文件

    async def scrape_video(
        self,
        path: Path,
        *,
        policy: str | None = None,
        tmdb_id: int | None = None,
        alt_titles: Sequence[str] = (),
        remember_alias: bool = False,
    ) -> tuple[str, str, bool]:
        """解析、识别并刮削**单个视频文件**（含电视剧的季 / 整剧产物）。

        作业（:mod:`app.services.scrape_jobs`）与手动刮削 API 共用本方法。

        :param path: 视频文件路径
        :param policy: 刮削策略；不传用当前配置
        :param tmdb_id: 指定 TMDB ID（纠正识别错误）
        :param alt_titles: 备用标题候选（如方括号里的中文译名）
        :param remember_alias: 把「文件名解析出的标题 → 该 TMDB ID」记进别名表
        :return: ``(状态, 说明, 是否已记别名)``；状态 ``ok`` / ``skip`` / ``fail``
        :raises ServiceError: 配置缺失（无 Key / 无目录）或 TMDB 故障
        """
        if not path.is_absolute():
            raise ServiceError(f"刮削路径必须是绝对路径：{path}", status=400)

        meta = parse_meta(path.name)
        if meta is None:
            return "fail", f"文件名解析失败：{path.name}", False
        if meta.is_episode and meta.season is None:
            return "fail", f"识别出集号但缺季号：{path.name}", False

        media_type = MediaKind.TV if meta.is_episode else None
        titles = tuple(meta.alt_titles) + tuple(alt_titles)
        info = await self.recognize_full(
            meta.title,
            year=meta.year,
            media_type=media_type,
            tmdb_id=tmdb_id,
            alt_titles=titles,
        )
        if info is None:
            return "fail", f"未识别到媒体：{meta.title}", False

        # 别名固化：key 必须是**文件名解析出的标题**（识别时用的就是它），
        # 媒体类型取识别结果——否则命中别名时会按 movie 取 TV 条目的详情。
        remembered = False
        if remember_alias and tmdb_id:
            await self.save_alias(
                meta.title,
                tmdb_id,
                media_type=info.media_type,
                note="手动刮削修正",
            )
            remembered = True

        if info.media_type == MediaKind.TV and meta.is_episode:
            assert meta.episode is not None  # is_episode 为真时必有集号
            status, detail = await self._scrape_episode_file(
                path, info, meta.season or 1, meta.episode, policy
            )
        else:
            status, detail = await self._scrape_movie_file(path, info, policy)
        return status, detail, remembered

    async def _scrape_movie_file(
        self, path: Path, info: MediaMeta, policy: str | None
    ) -> tuple[str, str]:
        """刮削电影文件。"""
        target = ScrapeTarget(
            directory=path.parent, kind=ScrapeKind.MOVIE, stem=path.stem
        )
        outcome = await self.scrape(
            target, info, policy=policy, media_path=str(path)
        )
        return _outcome_to_result(outcome, info.title)

    async def _scrape_episode_file(
        self,
        path: Path,
        info: MediaMeta,
        season_number: int,
        episode_number: int,
        policy: str | None,
    ) -> tuple[str, str]:
        """刮削单集（含季 / 整剧层级的产物）。

        目录层级判定：父目录名能解析出季号 → 季目录，剧集根是它的上一级；
        否则父目录就是剧集根。解析不出季号的目录一律当剧集根——**不猜**，
        猜错会让 NFO 落到错误位置，媒体服务器反而认不出。
        """
        season_meta = None
        try:
            season_meta = await self.get_season(info.tmdb_id, season_number)
        except ServiceError as exc:
            logger.warning("取季详情失败（%s）：%s", season_number, exc)

        episode_meta: EpisodeMeta | None = None
        if season_meta is not None:
            for item in season_meta.episodes:
                if item.episode_number == episode_number:
                    episode_meta = item
                    break

        target = ScrapeTarget(
            directory=path.parent,
            kind=ScrapeKind.EPISODE,
            stem=path.stem,
            season=season_number,
            episode=episode_number,
        )
        outcome = await self.scrape(
            target,
            info,
            policy=policy,
            season_meta=season_meta,
            episode_meta=episode_meta,
            tmdb_id=info.tmdb_id if info.tmdb_id else None,
            media_path=str(path),
        )
        if outcome.status == ScrapeStatus.FAILED:
            return "fail", outcome.error or "刮削失败"

        parent = path.parent
        parent_season = season_number_from_dir(parent.name)
        if parent_season is not None:
            season_dir = parent
            show_dir = parent.parent
        else:
            season_dir = None
            show_dir = parent

        season_root_info = self._season_root_image_root(show_dir, season_number)
        season_root = season_root_info[0] if season_root_info else None
        season_prefix = season_root_info[1] if season_root_info else None
        if season_dir is not None and season_meta is not None:
            await self.scrape(
                ScrapeTarget(
                    directory=season_dir,
                    kind=ScrapeKind.SEASON,
                    season=season_number,
                ),
                info,
                policy=policy,
                season_meta=season_meta,
                extra_image_root=season_root,
                extra_prefix=season_prefix,
            )
        await self.scrape(
            ScrapeTarget(directory=show_dir, kind=ScrapeKind.TVSHOW),
            info,
            policy=policy,
            season_meta=season_meta,
        )

        if outcome.status == ScrapeStatus.SKIPPED:
            return "skip", "产物已存在"
        return "ok", f"已刮削：{info.title} S{season_number}E{episode_number}"

    async def list_records(
        self, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """刮削记录列表（前端「整理历史 / 刮削记录」用）。

        :param limit: 每页条数（上限 200）
        :param offset: 偏移量
        :return: ``{"total": int, "items": [dict]}``
        """
        limit = min(max(limit, 1), 200)
        from sqlalchemy import func

        async with session_scope() as session:
            total = (
                await session.execute(select(func.count(MediaScrape.id)))
            ).scalar_one()
            rows = (
                await session.execute(
                    select(MediaScrape)
                    .order_by(MediaScrape.updated_at.desc(), MediaScrape.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars()
            items = [
                {
                    "scope_key": row.scope_key,
                    "kind": row.kind,
                    "status": row.status,
                    "media_type": row.media_type,
                    "tmdb_id": row.tmdb_id,
                    "title": row.title,
                    "year": row.year,
                    "season": row.season,
                    "episode": row.episode,
                    "nfo_path": row.nfo_path,
                    "media_path": row.media_path,
                    "images": row.images,
                    "error": row.error,
                    "scraped_at": row.scraped_at,
                }
                for row in rows
            ]
        return {"total": total, "items": items}

    def _build_nfo(
        self,
        target: ScrapeTarget,
        mediainfo: MediaMeta,
        *,
        season_meta: SeasonMeta | None,
        episode_meta: EpisodeMeta | None,
    ) -> str | None:
        """按层级生成 NFO 文本。"""
        if target.kind == ScrapeKind.MOVIE:
            return build_movie_nfo(mediainfo)
        if target.kind == ScrapeKind.TVSHOW:
            return build_tvshow_nfo(mediainfo)
        if target.kind == ScrapeKind.SEASON:
            if season_meta is None:
                return None
            return build_season_nfo(mediainfo, season_meta)
        if target.kind == ScrapeKind.EPISODE:
            if episode_meta is None or target.episode is None:
                return None
            return build_episode_nfo(
                mediainfo, target.season or 0, episode_meta
            )
        return None

    async def _apply_original_language_images(
        self, mediainfo: MediaMeta
    ) -> MediaMeta:
        """启用「原语言图片」时，换成原语言详情的图片路径。

        中文条目常缺图或画质差——TMDB 的原语言资源通常更全。

        :param mediainfo: 当前元数据
        :return: 换过图片路径的元数据（未启用 / 无原语言 → 原样返回）
        """
        if not self._flag("scrape_original_image"):
            return mediainfo
        language = mediainfo.original_language
        if not language or language == "zh":
            return mediainfo
        client = TmdbClient(self.api_key(), locale=language)
        try:
            if mediainfo.media_type == MediaKind.TV:
                original = await client.get_tv(mediainfo.tmdb_id)
            else:
                original = await client.get_movie(mediainfo.tmdb_id)
        except TmdbError as exc:
            logger.warning("取原语言图片失败（%s）：%s", mediainfo.tmdb_id, exc)
            return mediainfo
        if original is None:
            return mediainfo
        return replace(
            mediainfo,
            poster_path=original.poster_path or mediainfo.poster_path,
            backdrop_path=original.backdrop_path or mediainfo.backdrop_path,
            logo_path=original.logo_path or mediainfo.logo_path,
        )

    @staticmethod
    def _flag(key: str) -> bool:
        """读运行参数开关（取不到时按关闭处理）。"""
        from app.services.runtime_params import get_runtime_params, lookup_spec

        try:
            return get_runtime_params().get_int(key) == 1
        except Exception:
            spec = lookup_spec(key)
            return bool(spec and spec.default == 1)

    def _season_root_image_root(
        self, show_dir: Path, season_number: int
    ) -> tuple[Path, str] | None:
        """季图写剧集根的目标根与前缀。

        部分媒体服务器在**剧集根目录**找季海报，故开启时额外写一份
        （``season01-poster.jpg``）——与 MoviePilot 的「季图片写根目录」同款。

        :param show_dir: 剧集根目录
        :param season_number: 季号
        :return: ``(根目录, 文件名前缀)``；未开启返回 None
        """
        if not self._flag("scrape_season_image_to_root"):
            return None
        return show_dir, f"season{season_number:02d}"

    async def _scrape_images(
        self,
        target: ScrapeTarget,
        mediainfo: MediaMeta,
        *,
        season_meta: SeasonMeta | None,
        episode_meta: EpisodeMeta | None,
        extra_root: Path | None = None,
        extra_prefix: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """下载并写入图片。

        :param extra_root: 额外图片根目录（季图写剧集根用）
        :param extra_prefix: 额外图片文件名前缀（如 ``season01``）
        :return: ``(已写出的图片清单, 失败原因列表)``
        """
        client = self._url_client()
        planned: list[tuple[str, str, Path]] = []

        if target.kind == ScrapeKind.EPISODE:
            if episode_meta is not None and episode_meta.still_path:
                url = client.image_url(episode_meta.still_path)
                if url:
                    planned.append(
                        (
                            "thumb",
                            url,
                            naming.image_path(
                                target.directory,
                                target.kind,
                                "thumb",
                                target.stem,
                            ),
                        )
                    )
        else:
            poster_path = mediainfo.poster_path
            if target.kind == ScrapeKind.SEASON and season_meta is not None:
                poster_path = season_meta.poster_path or mediainfo.poster_path
            if poster_path:
                url = client.image_url(poster_path)
                if url:
                    planned.append(
                        (
                            "poster",
                            url,
                            naming.image_path(
                                target.directory, target.kind, "poster"
                            ),
                        )
                    )
            if mediainfo.backdrop_path:
                url = client.image_url(mediainfo.backdrop_path)
                if url:
                    planned.append(
                        (
                            "backdrop",
                            url,
                            naming.image_path(
                                target.directory, target.kind, "backdrop"
                            ),
                        )
                    )
            if mediainfo.logo_path:
                url = client.image_url(mediainfo.logo_path)
                if url:
                    planned.append(
                        (
                            "logo",
                            url,
                            naming.image_path(
                                target.directory, target.kind, "logo"
                            ),
                        )
                    )

        written: list[dict[str, Any]] = []
        errors: list[str] = []
        for image_type, url, path in planned:
            ok = await self._download_image(url, path)
            if ok:
                written.append({"type": image_type, "path": str(path)})
            else:
                errors.append(f"图片下载失败：{image_type}")

        # 额外目标（季图写剧集根）：复用已下载的文件，**不重复请求远端**。
        # 文件名沿用源图片名（``season01-logo.png`` 而非 .jpg——内容是 PNG，
        # 扩展名必须跟着内容走）。
        if extra_root is not None and extra_prefix:
            for image_type, _url, source in planned:
                extra_path = extra_root / f"{extra_prefix}-{source.name}"
                if await self._copy_image(source, extra_path):
                    written.append(
                        {"type": image_type, "path": str(extra_path)}
                    )
        return written, errors

    async def _copy_image(self, source: Path, target: Path) -> bool:
        """复制一份已下载的图片到另一位置（不重复请求远端）。"""
        def _copy() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

        try:
            await asyncio.to_thread(_copy)
            return True
        except OSError as exc:
            logger.warning("复制图片失败：%s（%s）", target, exc)
            return False

    async def _download_image(self, url: str, target: Path) -> bool:
        """下载一张图片并落盘。

        :param url: 图片地址
        :param target: 落盘路径
        :return: 是否成功
        """
        try:
            response = await self._http_client().get(url)
            if response.status_code != 200:
                logger.warning(
                    "图片下载失败（HTTP %d）：%s", response.status_code, url
                )
                return False
            content = response.content
            if len(content) > MAX_IMAGE_BYTES:
                logger.warning("图片过大（%d 字节），跳过：%s", len(content), url)
                return False
            await asyncio.to_thread(self._write_bytes, target, content)
            return True
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("图片下载失败：%s（%s）", url, exc)
            return False

    # ---------------------------------------------------------------- 重刮

    async def rescrape(
        self,
        scope_key: str,
        *,
        tmdb_id: int | None = None,
        remember: bool = False,
    ) -> tuple[str, str, bool]:
        """按**刮削记录**重刮（修正识别结果）。

        定位媒体文件：优先用记录里的 ``media_path``；旧记录没有该列时
        从 ``nfo_path`` 同目录按主干名找视频文件。

        :param scope_key: 刮削单元键（记录里的 scope_key）
        :param tmdb_id: 指定 TMDB ID（纠正识别错误）
        :param remember: 把修正记进别名表
        :return: ``(状态, 说明, 是否已记别名)``；状态 ``ok`` / ``skip`` / ``fail``
        :raises ServiceError: 记录不存在 / 找不到媒体文件
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(MediaScrape).where(MediaScrape.scope_key == scope_key)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ServiceError("刮削记录不存在", status=404)
            media_path = row.media_path
            nfo_path = row.nfo_path

        if not media_path:
            media_path = self._guess_media_path(nfo_path)
        if not media_path:
            raise ServiceError(
                "找不到对应的媒体文件（记录里没有媒体路径）", status=404
            )

        status, detail, remembered = await self.scrape_video(
            Path(media_path),
            policy=ScrapePolicy.OVERWRITE,
            tmdb_id=tmdb_id,
            remember_alias=remember,
        )
        return status, detail, remembered

    @staticmethod
    def _guess_media_path(nfo_path: str | None) -> str | None:
        """从 NFO 路径反推媒体文件路径（同目录同主干）。

        :param nfo_path: NFO 的绝对路径
        :return: 媒体文件路径；找不到返回 None
        """
        if not nfo_path:
            return None
        nfo = Path(nfo_path)
        directory = nfo.parent
        stem = nfo.stem
        from app.domain.media_exts import DEFAULT_VIDEO_EXTS

        exts = {
            ext.strip().lower().lstrip(".")
            for ext in DEFAULT_VIDEO_EXTS.split(",")
            if ext.strip()
        }
        try:
            candidates = sorted(directory.iterdir())
        except OSError:
            return None
        for candidate in candidates:
            if (
                candidate.is_file()
                and candidate.stem == stem
                and candidate.suffix.lower().lstrip(".") in exts
            ):
                return str(candidate)
        return None

    # ---------------------------------------------------------------- 别名

    async def list_aliases(self) -> list[dict[str, Any]]:
        """别名列表（展示用）。

        :return: 别名条目列表（按更新时间倒序）
        """
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(ScrapeAlias).order_by(
                        ScrapeAlias.updated_at.desc(), ScrapeAlias.id.desc()
                    )
                )
            ).scalars()
            return [
                {
                    "id": row.id,
                    "alias_text": row.alias_text,
                    "alias_key": row.alias_key,
                    "tmdb_id": row.tmdb_id,
                    "media_type": row.media_type,
                    "note": row.note,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]

    async def save_alias(
        self,
        alias_text: str,
        tmdb_id: int,
        *,
        media_type: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """新增或更新一条别名（同标题覆盖）。

        :param alias_text: 标题（原始文本，展示用）
        :param tmdb_id: 目标 TMDB ID
        :param media_type: 媒体类型；``None`` = 不限
        :param note: 备注（为什么改）
        :return: 保存后的别名条目
        :raises ServiceError: 标题为空 / 媒体类型非法
        """
        text = alias_text.strip()
        if not text:
            raise ServiceError("别名标题不能为空", status=400)
        if media_type is not None and media_type not in (MediaKind.MOVIE, MediaKind.TV):
            raise ServiceError(f"媒体类型非法：{media_type}", status=400)
        key = normalize_title(text)
        if not key:
            raise ServiceError("别名标题归一化后为空", status=400)

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ScrapeAlias).where(ScrapeAlias.alias_key == key)
                )
            ).scalar_one_or_none()
            if row is None:
                row = ScrapeAlias(alias_key=key, alias_text=text)
                session.add(row)
            row.alias_text = text
            row.tmdb_id = tmdb_id
            row.media_type = media_type
            row.note = note
            await session.flush()
            return {
                "id": row.id,
                "alias_text": row.alias_text,
                "alias_key": row.alias_key,
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type,
                "note": row.note,
                "updated_at": row.updated_at,
            }

    async def delete_alias(self, alias_id: int) -> bool:
        """删除一条别名。

        :param alias_id: 别名 ID
        :return: 是否删掉了（不存在返回 False）
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ScrapeAlias).where(ScrapeAlias.id == alias_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            return True

    # ---------------------------------------------------------------- 落盘

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        """写文本文件（同步实现，由调用方包 to_thread）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        """写二进制文件（同步实现，由调用方包 to_thread）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # ---------------------------------------------------------------- 落库

    async def _save_outcome(
        self,
        target: ScrapeTarget,
        outcome: ScrapeOutcome,
        mediainfo: MediaMeta,
        *,
        media_path: str | None = None,
    ) -> None:
        """把结果写入 :class:`MediaScrape`（按 scope_key upsert）。"""
        from app.core.timeutils import bj_now

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(MediaScrape).where(
                        MediaScrape.scope_key == outcome.scope_key
                    )
                )
            ).scalar_one_or_none()
            values = {
                "kind": target.kind,
                "status": outcome.status,
                "media_type": mediainfo.media_type,
                "tmdb_id": mediainfo.tmdb_id,
                "title": mediainfo.title,
                "year": mediainfo.year,
                "season": target.season,
                "episode": target.episode,
                "nfo_path": str(outcome.nfo_path) if outcome.nfo_path else None,
                "media_path": media_path,
                "images": outcome.images,
                "error": outcome.error,
                "scraped_at": bj_now(),
            }
            if row is None:
                session.add(MediaScrape(scope_key=outcome.scope_key, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)


def _title_candidates(title: str, alt_titles: Sequence[str]) -> tuple[str, ...]:
    """构造搜索用的标题候选序列（**中文译名优先**）。

    为什么中文优先：TMDB 的中文条目 ``name`` 就是中文名，用中文名搜能
    exact 命中；英文名可能命中同名的**另一部剧**（实测 ``Light the Dark``
    会命中《黑夜告白》而不是《把黑夜点燃》）。

    :param title: 主标题（来自文件名）
    :param alt_titles: 备用候选（方括号里的中文译名等）
    :return: 候选序列（去重、保序）
    """
    ordered: list[str] = []
    for candidate in alt_titles:
        text = candidate.strip()
        if text and text not in ordered:
            ordered.append(text)
    main = title.strip()
    if main and main not in ordered:
        ordered.append(main)
    return tuple(ordered)


def _meta_from_payload(payload: dict[str, Any]) -> MediaMeta:
    """缓存 payload → :class:`MediaMeta`（轻量：只有搜索命中的字段）。"""
    return MediaMeta(
        tmdb_id=int(payload.get("tmdb_id") or 0),
        media_type=str(payload.get("media_type") or MediaKind.MOVIE),
        title=str(payload.get("title") or ""),
        original_title=payload.get("original_title"),
        year=payload.get("year"),
    )


def _outcome_to_result(outcome: ScrapeOutcome, title: str) -> tuple[str, str]:
    """把 :class:`ScrapeOutcome` 折叠成 ``(状态, 说明)`` 作业/接口统一口径。"""
    if outcome.status == ScrapeStatus.SCRAPED:
        suffix = f"（{outcome.error}）" if outcome.error else ""
        return "ok", f"已刮削：{title}{suffix}"
    if outcome.status == ScrapeStatus.SKIPPED:
        return "skip", "产物已存在"
    return "fail", outcome.error or "刮削失败"
