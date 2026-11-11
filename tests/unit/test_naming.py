"""媒体库命名规则单元测试。

固化的契约：
- 电影 / 单集 NFO 与视频同名，电视剧 / 季用固定名（tvshow.nfo / season.nfo）
- 单集缩略图带集名（多集同目录不会互相覆盖）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.metadata.naming import (
    image_filename,
    image_path,
    nfo_filename,
    nfo_path,
)
from app.domain.scrape_types import ScrapeKind


@pytest.mark.parametrize(
    ("kind", "stem", "expected"),
    [
        (ScrapeKind.MOVIE, "Movie.Name.2025", "Movie.Name.2025.nfo"),
        (ScrapeKind.TVSHOW, None, "tvshow.nfo"),
        (ScrapeKind.SEASON, None, "season.nfo"),
        (ScrapeKind.EPISODE, "Show.S01E02", "Show.S01E02.nfo"),
    ],
)
def test_nfo_filename(kind: str, stem: str | None, expected: str) -> None:
    """NFO 文件名按层级固定。"""
    assert nfo_filename(kind, stem) == expected


def test_movie_nfo_requires_stem() -> None:
    """电影 NFO 缺文件名主干 → 报错（不静默生成无名文件）。"""
    with pytest.raises(ValueError):
        nfo_filename(ScrapeKind.MOVIE, None)


@pytest.mark.parametrize(
    ("kind", "image_type", "stem", "expected"),
    [
        (ScrapeKind.MOVIE, "poster", None, "poster.jpg"),
        (ScrapeKind.MOVIE, "backdrop", None, "backdrop.jpg"),
        (ScrapeKind.TVSHOW, "logo", None, "logo.png"),
        (ScrapeKind.SEASON, "poster", None, "poster.jpg"),
        (ScrapeKind.EPISODE, "thumb", "Show.S01E02", "Show.S01E02-thumb.jpg"),
    ],
)
def test_image_filename(
    kind: str, image_type: str, stem: str | None, expected: str
) -> None:
    """图片文件名按类型固定；单集缩略图带集名。"""
    assert image_filename(kind, image_type, stem) == expected


def test_unknown_image_type_raises() -> None:
    """未知图片类型 → 报错。"""
    with pytest.raises(ValueError):
        image_filename(ScrapeKind.MOVIE, "unknown")


def test_episode_thumb_requires_stem() -> None:
    """单集缩略图缺文件名主干 → 报错。"""
    with pytest.raises(ValueError):
        image_filename(ScrapeKind.EPISODE, "thumb", None)


def test_paths_are_joined_with_directory() -> None:
    """落盘路径 = 目录 + 文件名。"""
    directory = Path("/media/Movies/Movie Name (2025)")
    assert nfo_path(directory, ScrapeKind.MOVIE, "Movie.Name.2025") == (
        directory / "Movie.Name.2025.nfo"
    )
    assert image_path(directory, ScrapeKind.MOVIE, "poster") == directory / "poster.jpg"
