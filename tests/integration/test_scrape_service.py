"""刮削服务集成测试（假 TMDB + 临时目录，真实 DB）。

固化的契约：
- 识别结果落缓存，二次识别**不再打 TMDB**
- 刮削产出 NFO + 图片，路径遵循 Kodi 约定
- 策略「仅补缺」跳过已存在产物；「覆盖」重写
- 结果落 :class:`MediaScrape`（按 scope_key upsert）
- TMDB 故障抛 :class:`ServiceError`（**不**折叠成「识别不到」）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from app.adapters.metadata.tmdb import TmdbClient
from app.db.models import MediaScrape, ScrapeCache
from app.db.session import session_scope
from app.domain.scrape_types import (
    EpisodeMeta,
    MediaKind,
    MediaMeta,
    ScrapeKind,
    ScrapePolicy,
    SeasonMeta,
)
from app.services.errors import ServiceError
from app.services.scrape_service import ScrapeService, ScrapeTarget, build_scope_key

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/original"


@pytest.fixture
def runtime_params() -> AsyncIterator[None]:
    """注入运行时参数 manager（服务层读策略用）。"""
    from app.services.runtime_params import RuntimeParamManager, set_runtime_params

    set_runtime_params(RuntimeParamManager())
    yield
    set_runtime_params(None)


def _movie_meta() -> MediaMeta:
    """电影元数据样本。"""
    return MediaMeta(
        tmdb_id=603,
        media_type=MediaKind.MOVIE,
        title="黑客帝国",
        year=1999,
        overview="简介",
        poster_path="/poster.jpg",
        backdrop_path="/backdrop.jpg",
    )


def _mock_search(tmdb_id: int = 603, title: str = "黑客帝国") -> None:
    """打桩搜索接口。"""
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": tmdb_id,
                        "title": title,
                        "popularity": 50.0,
                        "release_date": "1999-03-30",
                    }
                ]
            },
        )
    )


def _mock_image(name: str) -> None:
    """打桩图片下载。"""
    respx.get(f"{IMG}/{name}").mock(
        return_value=httpx.Response(200, content=b"image-bytes")
    )


# ------------------------------------------------------------------ 识别


@respx.mock
async def test_recognize_caches_result(db, runtime_params) -> None:
    """识别结果落缓存，二次识别不再打 TMDB。"""
    route = respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 603,
                        "title": "黑客帝国",
                        "popularity": 50.0,
                        "release_date": "1999-03-30",
                    }
                ]
            },
        )
    )
    service = ScrapeService(tmdb=TmdbClient("test-key"))

    first = await service.recognize("黑客帝国")
    assert first is not None
    assert first.tmdb_id == 603

    second = await service.recognize("黑客帝国")
    assert second is not None
    assert second.tmdb_id == 603
    assert route.call_count == 1, "命中缓存后不该再打 TMDB"

    async with session_scope() as session:
        cache_rows = (
            await session.execute(select_cache())
        ).scalars().all()
    assert len(cache_rows) == 1
    assert cache_rows[0].payload["tmdb_id"] == 603


def select_cache():
    """构造 ScrapeCache 查询（避免顶层 import 噪声）。"""
    from sqlalchemy import select

    return select(ScrapeCache)


@respx.mock
async def test_recognize_tmdb_failure_raises_service_error(db, runtime_params) -> None:
    """TMDB 故障 → ServiceError（不折叠成「识别不到」）。"""
    respx.get(f"{API}/search/movie").mock(side_effect=httpx.ConnectError("boom"))
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    with pytest.raises(ServiceError) as excinfo:
        await service.recognize("黑客帝国")
    assert excinfo.value.status == 502


@respx.mock
async def test_recognize_not_found_returns_none(db, runtime_params) -> None:
    """查不到 → None（不是故障）。"""
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(f"{API}/search/tv").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    assert await service.recognize("不存在的片子") is None


# ------------------------------------------------------------------ 刮削


@respx.mock
async def test_scrape_movie_writes_nfo_and_images(
    db, runtime_params, tmp_path: Path
) -> None:
    """刮削电影 → NFO + 海报 + 背景图。"""
    _mock_image("poster.jpg")
    _mock_image("backdrop.jpg")
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    target = ScrapeTarget(
        directory=tmp_path,
        kind=ScrapeKind.MOVIE,
        stem="Movie.Name.1999",
    )

    outcome = await service.scrape(
        target, _movie_meta(), policy=ScrapePolicy.MISSING_ONLY
    )

    assert outcome.status == "scraped"
    nfo = tmp_path / "Movie.Name.1999.nfo"
    assert nfo.exists()
    assert "黑客帝国" in nfo.read_text(encoding="utf-8")
    assert (tmp_path / "poster.jpg").read_bytes() == b"image-bytes"
    assert (tmp_path / "backdrop.jpg").exists()
    assert {item["type"] for item in outcome.images} == {"poster", "backdrop"}


@respx.mock
async def test_scrape_movie_records_in_db(db, runtime_params, tmp_path: Path) -> None:
    """结果落 MediaScrape（按 scope_key upsert）。"""
    _mock_image("poster.jpg")
    _mock_image("backdrop.jpg")
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    target = ScrapeTarget(
        directory=tmp_path, kind=ScrapeKind.MOVIE, stem="Movie.Name.1999"
    )

    await service.scrape(target, _movie_meta(), policy=ScrapePolicy.MISSING_ONLY)

    async with session_scope() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(MediaScrape).where(
                    MediaScrape.scope_key == build_scope_key(target)
                )
            )
        ).scalar_one()
    assert row.status == "scraped"
    assert row.tmdb_id == 603
    assert row.title == "黑客帝国"
    assert row.nfo_path == str(tmp_path / "Movie.Name.1999.nfo")
    assert row.scraped_at


@respx.mock
async def test_policy_missing_only_skips_existing(
    db, runtime_params, tmp_path: Path
) -> None:
    """「仅补缺」：产物已存在就跳过（不重写）。"""
    nfo = tmp_path / "Movie.Name.1999.nfo"
    nfo.write_text("旧内容", encoding="utf-8")
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    target = ScrapeTarget(
        directory=tmp_path, kind=ScrapeKind.MOVIE, stem="Movie.Name.1999"
    )

    outcome = await service.scrape(
        target, _movie_meta(), policy=ScrapePolicy.MISSING_ONLY
    )

    assert outcome.status == "skipped"
    assert nfo.read_text(encoding="utf-8") == "旧内容"


@respx.mock
async def test_policy_overwrite_rewrites_existing(
    db, runtime_params, tmp_path: Path
) -> None:
    """「覆盖」：产物已存在也重写。"""
    nfo = tmp_path / "Movie.Name.1999.nfo"
    nfo.write_text("旧内容", encoding="utf-8")
    _mock_image("poster.jpg")
    _mock_image("backdrop.jpg")
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    target = ScrapeTarget(
        directory=tmp_path, kind=ScrapeKind.MOVIE, stem="Movie.Name.1999"
    )

    outcome = await service.scrape(
        target, _movie_meta(), policy=ScrapePolicy.OVERWRITE
    )

    assert outcome.status == "scraped"
    assert "黑客帝国" in nfo.read_text(encoding="utf-8")


@respx.mock
async def test_scrape_episode_writes_episode_nfo(
    db, runtime_params, tmp_path: Path
) -> None:
    """单集 → episodicedetails NFO + 集缩略图。"""
    _mock_image("still.jpg")
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    meta = MediaMeta(
        tmdb_id=1399, media_type=MediaKind.TV, title="权力的游戏", year=2011
    )
    episode = EpisodeMeta(
        episode_number=2, name="让王座坐实", still_path="/still.jpg"
    )
    target = ScrapeTarget(
        directory=tmp_path,
        kind=ScrapeKind.EPISODE,
        stem="Show.S01E02",
        season=1,
        episode=2,
    )

    outcome = await service.scrape(
        target, meta, policy=ScrapePolicy.MISSING_ONLY, episode_meta=episode
    )

    assert outcome.status == "scraped"
    nfo_text = (tmp_path / "Show.S01E02.nfo").read_text(encoding="utf-8")
    assert "<episodedetails>" in nfo_text
    assert "<episode>2</episode>" in nfo_text
    assert (tmp_path / "Show.S01E02-thumb.jpg").exists()


@respx.mock
async def test_scrape_season_writes_season_nfo(
    db, runtime_params, tmp_path: Path
) -> None:
    """季 → season.nfo + 季海报。"""
    _mock_image("season.jpg")
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    meta = MediaMeta(
        tmdb_id=1399, media_type=MediaKind.TV, title="权力的游戏", year=2011
    )
    season = SeasonMeta(season_number=1, name="第一季", poster_path="/season.jpg")
    target = ScrapeTarget(
        directory=tmp_path / "Season 01", kind=ScrapeKind.SEASON, season=1
    )

    outcome = await service.scrape(
        target, meta, policy=ScrapePolicy.MISSING_ONLY, season_meta=season
    )

    assert outcome.status == "scraped"
    assert "<season>" in (tmp_path / "Season 01" / "season.nfo").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "Season 01" / "poster.jpg").exists()


@respx.mock
async def test_image_failure_does_not_fail_whole_scrape(
    db, runtime_params, tmp_path: Path
) -> None:
    """单张图片下载失败不影响 NFO——但**必须点名**。"""
    _mock_image("poster.jpg")
    respx.get(f"{IMG}/backdrop.jpg").mock(return_value=httpx.Response(500))
    service = ScrapeService(tmdb=TmdbClient("test-key"))
    target = ScrapeTarget(
        directory=tmp_path, kind=ScrapeKind.MOVIE, stem="Movie.Name.1999"
    )

    outcome = await service.scrape(
        target, _movie_meta(), policy=ScrapePolicy.MISSING_ONLY
    )

    assert outcome.status == "scraped"
    assert outcome.error is not None
    assert "backdrop" in outcome.error
    assert (tmp_path / "poster.jpg").exists()
