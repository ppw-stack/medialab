"""整理服务集成测试（临时源目录 + 假 TMDB，真实 DB）。

**输入是「源目录」不是「下载器任务」**——文件从哪来无所谓，把视频放进
源目录就会被扫到、识别、按标准结构整理到媒体库。

固化的契约：
- copy：源文件保留，媒体库出现标准结构目录
- move：源文件删除（字幕跟随，**copy 模式也复制字幕**）
- 目标冲突（已存在且大小不同）→ fail，**绝不覆盖**
- 目标已存在且大小一致 → skip（幂等）
- 历史判重：按 source_path，已整理的源文件下轮跳过
- 源目录不存在 → fail 点名（不静默跳过）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from app.db.session import session_scope
from app.services.env_settings import EnvSettingsManager, set_env_settings

API = "https://api.themoviedb.org/3"


@pytest.fixture
def runtime_params() -> AsyncIterator[None]:
    """运行参数：整理开启 + copy + 刮削关闭（刮削钩子走兜底日志）。"""
    from app.services.runtime_params import RuntimeParamManager, set_runtime_params

    manager = RuntimeParamManager()
    manager.set_memory("organize_enabled", 1)
    manager.set_memory("organize_action", 0)
    manager.set_memory("scrape_enabled", 0)
    set_runtime_params(manager)
    yield
    set_runtime_params(None)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """返回 ``(源目录, 媒体库目录)``。"""
    return tmp_path / "source", tmp_path / "library"


def setup_env(dirs: tuple[Path, Path]) -> Path:
    """环境配置：媒体库目录 + 源目录 + Key。返回源目录。"""
    source, library = dirs
    source.mkdir()
    set_env_settings(
        EnvSettingsManager(
            {
                "media_library_dir": str(library),
                "tmdb_api_key": "test-key",
                "organize_source_dirs": str(source),
            }
        )
    )
    return source


def _mock_movie_search() -> None:
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 100,
                        "title": "Movie Name",
                        "popularity": 50.0,
                        "release_date": "2025-05-01",
                    }
                ]
            },
        )
    )
    respx.get(f"{API}/movie/100").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 100,
                "title": "Movie Name",
                "release_date": "2025-05-01",
                "overview": "简介",
            },
        )
    )


def _mock_tv() -> None:
    respx.get(f"{API}/search/tv").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 200,
                        "name": "Show Name",
                        "popularity": 50.0,
                        "first_air_date": "2024-01-01",
                    }
                ]
            },
        )
    )
    respx.get(f"{API}/tv/200").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 200,
                "name": "Show Name",
                "first_air_date": "2024-01-01",
                "overview": "简介",
                "seasons": [{"season_number": 1, "name": "第一季"}],
            },
        )
    )
    respx.get(f"{API}/tv/200/season/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "season_number": 1,
                "name": "第一季",
                "episodes": [
                    {"episode_number": 2, "name": "第二集"},
                ],
            },
        )
    )


# ------------------------------------------------------------------ 电影


@respx.mock
async def test_organize_movie_copy(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """copy：媒体库出现标准结构，源文件保留。"""
    source, library = dirs
    setup_env(dirs)
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"x" * 100)
    _mock_movie_search()

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert result["summary"] == "1 整理 / 0 跳过 / 0 失败", result

    target = library / "Movie Name (2025)" / "Movie Name (2025).mkv"
    assert target.exists()
    assert target.read_bytes() == b"x" * 100
    assert video.exists(), "copy 模式源文件必须保留"

    from app.db.models import OrganizeHistory
    from sqlalchemy import select

    async with session_scope() as session:
        rows = (await session.execute(select(OrganizeHistory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].source_path == str(video)
    assert rows[0].tmdb_id == 100
    assert rows[0].title == "Movie Name"


@respx.mock
async def test_organize_movie_move_deletes_source(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """move：复制成功后删源文件。"""
    source, library = dirs
    setup_env(dirs)
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"x" * 100)
    _mock_movie_search()

    from app.services.runtime_params import get_runtime_params

    get_runtime_params().set_memory("organize_action", 1)

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert result["summary"] == "1 整理 / 0 跳过 / 0 失败", result

    target = library / "Movie Name (2025)" / "Movie Name (2025).mkv"
    assert target.exists()
    assert not video.exists(), "move 模式源文件应被删除"


@respx.mock
async def test_organize_subtitle_follows_copy_and_move(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """同 stem 字幕随视频整理：copy 复制、move 搬移。"""
    source, library = dirs
    setup_env(dirs)
    (source / "Movie.Name.2025.1080p.mkv").write_bytes(b"v")
    (source / "Movie.Name.2025.1080p.chs.ass").write_text("字幕", encoding="utf-8")
    _mock_movie_search()

    from app.services.organize_service import run_organize

    await run_organize()

    target_dir = library / "Movie Name (2025)"
    assert (target_dir / "Movie Name (2025).chs.ass").read_text(
        encoding="utf-8"
    ) == "字幕"

    # move 模式：再放一对字幕，验证搬移
    (source / "Movie2.2025.1080p.srt").write_text("字幕2", encoding="utf-8")
    (source / "Movie2.2025.1080p.mkv").write_bytes(b"v2")
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 101,
                        "title": "Movie2",
                        "popularity": 10.0,
                        "release_date": "2025-01-01",
                    }
                ]
            },
        )
    )
    respx.get(f"{API}/movie/101").mock(
        return_value=httpx.Response(
            200,
            json={"id": 101, "title": "Movie2", "release_date": "2025-01-01"},
        )
    )
    from app.services.runtime_params import get_runtime_params

    get_runtime_params().set_memory("organize_action", 1)
    await run_organize()

    assert (library / "Movie2 (2025)" / "Movie2 (2025).srt").read_text(
        encoding="utf-8"
    ) == "字幕2"
    assert not (source / "Movie2.2025.1080p.srt").exists()


# ------------------------------------------------------------------ 冲突与幂等


@respx.mock
async def test_organize_conflict_never_overwrites(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """目标已存在且大小不同 → fail，**绝不覆盖**（不误删底线）。"""
    source, library = dirs
    setup_env(dirs)
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"x" * 100)

    target = library / "Movie Name (2025)" / "Movie Name (2025).mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different-content")
    _mock_movie_search()

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert "1 失败" in result["summary"]
    assert target.read_bytes() == b"different-content", "冲突时目标不能被覆盖"


@respx.mock
async def test_organize_idempotent_skips_same_size(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """目标已存在且大小一致 → skip（幂等）。"""
    source, library = dirs
    setup_env(dirs)
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"x" * 100)

    target_dir = library / "Movie Name (2025)"
    target_dir.mkdir(parents=True)
    (target_dir / "Movie Name (2025).mkv").write_bytes(b"x" * 100)
    _mock_movie_search()

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert result["summary"] == "0 整理 / 1 跳过 / 0 失败", result
    assert video.exists()


# ------------------------------------------------------------------ 判重


@respx.mock
async def test_organize_history_dedupes_by_source_path(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """已整理成功的源文件下轮跳过（按 source_path 判重）。"""
    source, _ = dirs
    setup_env(dirs)
    video = source / "Movie.Name.2025.1080p.mkv"
    video.write_bytes(b"x" * 100)
    _mock_movie_search()

    from app.services.organize_service import run_organize

    first = await run_organize()
    assert first["summary"] == "1 整理 / 0 跳过 / 0 失败"

    second = await run_organize()
    assert second["summary"] == "0 整理 / 1 跳过 / 0 失败", "判重后不再整理"


@respx.mock
async def test_organize_failed_source_is_retried(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """失败的源文件**允许重试**（status=failed 不算已整理）。

    ``1080p.mkv`` 是纯噪声文件名——:func:`parse_meta` 返回 None，根本
    不打 TMDB（不依赖 mock），失败原因点名「文件名解析失败」。
    """
    source, _ = dirs
    setup_env(dirs)
    bad = source / "1080p.mkv"
    bad.write_bytes(b"x")

    from app.services.organize_service import run_organize

    first = await run_organize()
    assert "1 失败" in first["summary"], first

    # 下一轮：失败文件仍会被重试
    second = await run_organize()
    assert "1 失败" in second["summary"], "失败的源文件必须重试"


# ------------------------------------------------------------------ 源目录


@respx.mock
async def test_organize_missing_source_dir_is_named(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """源目录不存在 → fail **点名**（不静默跳过）。"""
    source, library = dirs
    set_env_settings(
        EnvSettingsManager(
            {
                "media_library_dir": str(library),
                "tmdb_api_key": "test-key",
                "organize_source_dirs": str(source / "不存在的目录"),
            }
        )
    )

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert "1 失败" in result["summary"]
    items = result["steps"][0].items
    assert items is not None
    assert "不存在" in items[0].detail


@respx.mock
async def test_organize_multiple_source_dirs(
    db, runtime_params, dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    """多个源目录（逗号分隔）都被扫描。"""
    source, library = dirs
    second_source = tmp_path / "source2"
    source.mkdir()
    second_source.mkdir()
    set_env_settings(
        EnvSettingsManager(
            {
                "media_library_dir": str(library),
                "tmdb_api_key": "test-key",
                "organize_source_dirs": f"{source},{second_source}",
            }
        )
    )
    (source / "Movie.Name.2025.1080p.mkv").write_bytes(b"a")
    (second_source / "Movie.Two.2025.1080p.mkv").write_bytes(b"b")
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 100,
                        "title": "Movie Name",
                        "popularity": 50.0,
                        "release_date": "2025-05-01",
                    },
                    {
                        "id": 101,
                        "title": "Movie Two",
                        "popularity": 40.0,
                        "release_date": "2025-06-01",
                    },
                ]
            },
        )
    )
    def _detail(request: httpx.Request) -> httpx.Response:
        """详情按 URL 里的 ID 返回（两个源目录的片子不同）。"""
        movie_id = int(request.url.path.rsplit("/", 1)[-1])
        titles = {100: "Movie Name", 101: "Movie Two"}
        dates = {100: "2025-05-01", 101: "2025-06-01"}
        return httpx.Response(
            200,
            json={"id": movie_id, "title": titles.get(movie_id, "?"),
                  "release_date": dates.get(movie_id)},
        )

    respx.route(method="GET", path__regex=r"/movie/\d+").mock(side_effect=_detail)

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert result["summary"] == "2 整理 / 0 跳过 / 0 失败", result


# ------------------------------------------------------------------ 剧集


@respx.mock
async def test_organize_episode_structure(
    db, runtime_params, dirs: tuple[Path, Path]
) -> None:
    """剧集 → 剧名 (年份)/Season 01/剧名 - S01E02。"""
    source, library = dirs
    setup_env(dirs)
    show_dir = source / "Show.S01E02.1080p"
    show_dir.mkdir()
    video = show_dir / "Show.S01E02.1080p.mkv"
    video.write_bytes(b"x" * 50)
    _mock_tv()

    from app.services.organize_service import run_organize

    result = await run_organize()
    assert result["summary"] == "1 整理 / 0 跳过 / 0 失败", result

    target = (
        library
        / "Show Name (2024)"
        / "Season 01"
        / "Show Name - S01E02.mkv"
    )
    assert target.exists()
