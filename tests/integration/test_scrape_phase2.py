"""第二阶段增补功能的集成测试。

覆盖四项：
1. **同名冲突消歧**（``标题 (2025) - 2.ext``）——多版本合法同名
2. **季图写剧集根**（``season01-poster.jpg``）
3. **原语言图片**（改用原语言的 TMDB 图片）
4. **一键修正**（按刮削记录重刮 + 记别名）
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from app.db.models import MediaScrape, ScrapeAlias
from app.db.session import session_scope
from app.domain.scrape_types import MediaKind, ScrapeKind, ScrapePolicy
from app.services.scrape_service import ScrapeService, ScrapeTarget
from sqlalchemy import select

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/original"


@pytest.fixture
def runtime_params() -> AsyncIterator[None]:
    """运行参数 manager（各用例按需改开关）。"""
    from app.services.runtime_params import RuntimeParamManager, set_runtime_params

    manager = RuntimeParamManager()
    manager.set_memory("scrape_policy", 0)
    manager.set_memory("scrape_enabled", 1)
    manager.set_memory("scrape_season_image_to_root", 1)
    manager.set_memory("scrape_original_image", 0)
    manager.set_memory("organize_conflict_policy", 0)
    set_runtime_params(manager)
    yield
    set_runtime_params(None)


def _client():
    from app.adapters.metadata.tmdb import TmdbClient

    return TmdbClient("test-key")


# ==================================================================== 1. 冲突消歧


@respx.mock
async def test_conflict_disambiguation_creates_numbered_copy(
    db, runtime_params, tmp_path: Path
) -> None:
    """同名不同大小 + 消歧策略 → 落 ``标题 - 2.ext``，不覆盖原文件。"""
    from app.services.organize_service import _execute_plan_sync, _FilePlan

    library = tmp_path / "library"
    source = tmp_path / "src"
    source.mkdir()
    library.mkdir()
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"new-content")
    # 已有同名但大小不同的文件
    target_dir = library / "Movie Name (2025)"
    target_dir.mkdir()
    (target_dir / "Movie Name (2025).mkv").write_bytes(b"old-content-different")

    plan = _FilePlan(
        source=video, target=target_dir / "Movie Name (2025).mkv"
    )
    result = await asyncio.to_thread(
        _execute_plan_sync, plan, "copy", "disambiguate"
    )

    assert result == "copied"
    original = target_dir / "Movie Name (2025).mkv"
    disambiguated = target_dir / "Movie Name (2025) - 2.mkv"
    assert original.read_bytes() == b"old-content-different", "原文件不能被覆盖"
    assert disambiguated.read_bytes() == b"new-content", "消歧副本应落盘"


async def test_conflict_default_policy_fails(db, runtime_params, tmp_path: Path) -> None:
    """默认策略（记冲突失败）→ 不产出副本。"""
    from app.services.organize_service import _execute_plan_sync, _FilePlan

    library = tmp_path / "library"
    source = tmp_path / "src"
    source.mkdir()
    library.mkdir()
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"new-content")
    target_dir = library / "Movie Name (2025)"
    target_dir.mkdir()
    (target_dir / "Movie Name (2025).mkv").write_bytes(b"old-content-different")

    plan = _FilePlan(
        source=video, target=target_dir / "Movie Name (2025).mkv"
    )
    result = await asyncio.to_thread(_execute_plan_sync, plan, "copy", "fail")

    assert result == "conflict"
    assert not (target_dir / "Movie Name (2025) - 2.mkv").exists()
    assert (target_dir / "Movie Name (2025).mkv").read_bytes() == (
        b"old-content-different"
    )


# ==================================================================== 2. 季图写剧集根


@respx.mock
async def test_season_image_copied_to_show_root(
    db, runtime_params, tmp_path: Path
) -> None:
    """季海报额外写一份到剧集根（``season01-poster.jpg``）。"""
    respx.get(f"{IMG}/season.jpg").mock(
        return_value=httpx.Response(200, content=b"season-poster")
    )
    respx.get(f"{IMG}/logo.png").mock(
        return_value=httpx.Response(200, content=b"logo-bytes")
    )

    service = ScrapeService(tmdb=_client())
    show_dir = tmp_path / "Show (2024)"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)

    from app.domain.scrape_types import MediaMeta, SeasonMeta

    meta = MediaMeta(
        tmdb_id=1399,
        media_type=MediaKind.TV,
        title="Show",
        year=2024,
        logo_path="/logo.png",
    )
    season = SeasonMeta(season_number=1, name="第一季", poster_path="/season.jpg")

    outcome = await service.scrape(
        ScrapeTarget(directory=season_dir, kind=ScrapeKind.SEASON, season=1),
        meta,
        policy=ScrapePolicy.OVERWRITE,
        season_meta=season,
        extra_image_root=show_dir,
        extra_prefix="season01",
    )

    assert outcome.status == "scraped"
    assert (season_dir / "poster.jpg").read_bytes() == b"season-poster"
    root_copy = show_dir / "season01-poster.jpg"
    assert root_copy.exists(), "季海报应复制到剧集根"
    assert root_copy.read_bytes() == b"season-poster"


@respx.mock
async def test_season_image_root_copy_disabled(
    db, runtime_params, tmp_path: Path
) -> None:
    """开关关闭 → 不写剧集根副本。"""
    from app.services.runtime_params import get_runtime_params

    get_runtime_params().set_memory("scrape_season_image_to_root", 0)

    respx.get(f"{IMG}/season.jpg").mock(
        return_value=httpx.Response(200, content=b"season-poster")
    )
    service = ScrapeService(tmdb=_client())
    show_dir = tmp_path / "Show (2024)"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)

    from app.domain.scrape_types import MediaMeta, SeasonMeta

    meta = MediaMeta(tmdb_id=1399, media_type=MediaKind.TV, title="Show", year=2024)
    season = SeasonMeta(season_number=1, name="第一季", poster_path="/season.jpg")

    await service.scrape(
        ScrapeTarget(directory=season_dir, kind=ScrapeKind.SEASON, season=1),
        meta,
        policy=ScrapePolicy.OVERWRITE,
        season_meta=season,
    )
    assert not (show_dir / "season01-poster.jpg").exists()


# ==================================================================== 3. 原语言图片


@respx.mock
async def test_original_language_image_used(
    db, runtime_params, tmp_path: Path
) -> None:
    """开启原语言图片 → 用原语言详情里的图片路径。"""
    from app.services.runtime_params import get_runtime_params

    get_runtime_params().set_memory("scrape_original_image", 1)

    zh_detail = respx.get(
        f"{API}/movie/100", params={"language": "zh-CN"}
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 100,
                "title": "中文片名",
                "original_language": "en",
                "release_date": "2025-05-01",
                "poster_path": "/zh-poster.jpg",
            },
        )
    )
    en_detail = respx.get(
        f"{API}/movie/100", params={"language": "en"}
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 100,
                "title": "Original Title",
                "original_language": "en",
                "release_date": "2025-05-01",
                "poster_path": "/en-poster.jpg",
            },
        )
    )
    respx.get(f"{IMG}/en-poster.jpg").mock(
        return_value=httpx.Response(200, content=b"original-poster")
    )

    service = ScrapeService(tmdb=_client())
    from app.domain.scrape_types import MediaMeta

    meta = MediaMeta(
        tmdb_id=100,
        media_type=MediaKind.MOVIE,
        title="中文片名",
        year=2025,
        original_language="en",
        poster_path="/zh-poster.jpg",
    )
    outcome = await service.scrape(
        ScrapeTarget(directory=tmp_path, kind=ScrapeKind.MOVIE, stem="movie"),
        meta,
        policy=ScrapePolicy.OVERWRITE,
    )

    assert outcome.status == "scraped"
    assert en_detail.called, "应当取原语言详情"
    assert (tmp_path / "poster.jpg").read_bytes() == b"original-poster"
    assert zh_detail.call_count <= 1


@respx.mock
async def test_original_language_image_disabled(
    db, runtime_params, tmp_path: Path
) -> None:
    """关闭 → 沿用当前语言的图片，不额外请求原语言详情。"""
    respx.get(f"{IMG}/zh-poster.jpg").mock(
        return_value=httpx.Response(200, content=b"zh-poster")
    )
    service = ScrapeService(tmdb=_client())
    from app.domain.scrape_types import MediaMeta

    meta = MediaMeta(
        tmdb_id=100,
        media_type=MediaKind.MOVIE,
        title="中文片名",
        year=2025,
        original_language="en",
        poster_path="/zh-poster.jpg",
    )
    outcome = await service.scrape(
        ScrapeTarget(directory=tmp_path, kind=ScrapeKind.MOVIE, stem="movie"),
        meta,
        policy=ScrapePolicy.OVERWRITE,
    )
    assert outcome.status == "scraped"
    assert (tmp_path / "poster.jpg").read_bytes() == b"zh-poster"


# ==================================================================== 4. 一键修正


@respx.mock
async def test_rescrape_by_record(db, runtime_params, tmp_path: Path) -> None:
    """按记录重刮：定位媒体文件 → 覆盖刮削 → 记别名。"""
    respx.get(f"{API}/tv/288328").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 288328,
                "name": "把黑夜点燃",
                "first_air_date": "2026-10-15",
                "overview": "简介",
            },
        )
    )
    respx.get(f"{API}/tv/288328/season/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "season_number": 1,
                "name": "第一季",
                "episodes": [{"episode_number": 1, "name": "第一集"}],
            },
        )
    )

    service = ScrapeService(tmdb=_client())
    video = tmp_path / "Show.S01E01.2026.mkv"
    video.write_bytes(b"fake")
    # 先刮一次，产生记录（直接指定 ID，跳过搜索）
    status, _detail, _ = await service.scrape_video(
        video, policy=ScrapePolicy.OVERWRITE, tmdb_id=288328
    )
    assert status == "ok"

    async with session_scope() as session:
        row = (
            await session.execute(select(MediaScrape).where(MediaScrape.nfo_path.is_not(None)))
        ).scalars().first()
    assert row is not None
    assert row.media_path == str(video), "记录里必须存媒体文件路径（供一键修正定位）"

    # 按记录重刮 + 记别名
    status, detail, remembered = await service.rescrape(
        row.scope_key, tmdb_id=288328, remember=True
    )
    assert status == "ok", detail
    assert remembered is True

    async with session_scope() as session:
        alias = (
            await session.execute(select(ScrapeAlias))
        ).scalars().first()
    assert alias is not None
    assert alias.tmdb_id == 288328


async def test_rescrape_missing_record_raises(db, runtime_params) -> None:
    """记录不存在 → ServiceError(404)。"""
    from app.services.errors import ServiceError

    service = ScrapeService(tmdb=_client())
    with pytest.raises(ServiceError) as excinfo:
        await service.rescrape("episode|/nope|S1E1")
    assert excinfo.value.status == 404
