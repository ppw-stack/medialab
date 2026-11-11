"""文件名解析器单元测试（表驱动）。

固化的契约：
- 标题取「年份之前的连续非噪声片段」
- 年份只挖最后一个匹配（``1917`` 是片名不是年份）
- 季集优先于标题，且支持中文数字（第X季 / 第X集）
- 画质 / 来源 / 版本 / 语种标记不进标题
"""

from __future__ import annotations

import pytest
from app.services.meta_parser import (
    ParsedMeta,
    parse_meta,
    parse_meta_from_path,
    season_number_from_dir,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # —— 常规电影 ——
        ("Movie.Name.2025.1080p.WEB-DL.x264-GRP", ParsedMeta("Movie Name", 2025)),
        ("Movie.2025.HDTS.x264", ParsedMeta("Movie", 2025)),
        ("Movie.Name.1080p", ParsedMeta("Movie Name")),
        ("The.Lord.of.the.Rings.2001.1080p", ParsedMeta("The Lord of the Rings", 2001)),
        ("3.Body.Problem.2024.1080p", ParsedMeta("3 Body Problem", 2024)),
        ("Spider-Man.No.Way.Home.2021.1080p", ParsedMeta("Spider Man No Way Home", 2021)),
        ("Movie.Title.2019.1080p.BluRay.x265.10bit.HDR", ParsedMeta("Movie Title", 2019)),
        ("A.Movie.2020.PROPER.1080p.BluRay", ParsedMeta("A Movie", 2020)),
        ("Movie.Name.2025.EAC3.5.1.HDR.x265-GRP", ParsedMeta("Movie Name", 2025)),
        # —— 中文 ——
        ("东极岛 (2025) - 1080p.mp4", ParsedMeta("东极岛", 2025)),
        ("三体 (2023)", ParsedMeta("三体", 2023)),
        ("三体（电视剧）", ParsedMeta("三体")),
        # —— 片名形似年份 ——
        ("1917.2019.1080p.BluRay", ParsedMeta("1917", 2019)),
        ("1917.1080p", ParsedMeta("1917", 1917)),
        # —— 括号内的画质标记不进标题 ——
        ("Movie Title (2015) [1080p] [BluRay].mkv", ParsedMeta("Movie Title", 2015)),
    ],
)
def test_parse_movie(filename: str, expected: ParsedMeta) -> None:
    """电影文件名 → 标题 + 年份。"""
    assert parse_meta(filename) == expected


@pytest.mark.parametrize(
    ("filename", "season", "episode"),
    [
        ("Show.Name.S01E02.1080p.H264-GROUP", 1, 2),
        ("剧名.S01E02.1080p.mkv", 1, 2),
        ("剧名 第3季 第2集.mkv", 3, 2),
        ("剧名.第二季.第05集.国语.中字", 2, 5),
        ("剧名.第十季.第一集.mkv", 10, 1),
        ("Show.S01.1080p", 1, None),
        ("Show.Name.Season.2.Episode.5.mkv", 2, 5),
    ],
)
def test_parse_episode(filename: str, season: int | None, episode: int | None) -> None:
    """电视剧文件名 → 季号 / 集号。"""
    result = parse_meta(filename)
    assert result is not None
    assert result.season == season
    assert result.episode == episode
    assert result.is_episode is (episode is not None)


def test_episode_title_excludes_season_marker() -> None:
    """集号不进标题。"""
    result = parse_meta("Show.Name.S01E02.1080p.H264-GROUP")
    assert result is not None
    assert result.title == "Show Name"


@pytest.mark.parametrize("filename", ["", "1080p", "   ", ".mkv"])
def test_unparsable_returns_none(filename: str) -> None:
    """识别不出标题 → 返回 None（调用方据此记 failed，不写空标题兜底）。"""
    assert parse_meta(filename) is None


def test_parse_meta_from_path_uses_file_name() -> None:
    """从路径解析时只取文件名部分（目录名不参与解析）。"""
    result = parse_meta_from_path("/downloads/Movie.Name.2025.1080p/movie.mkv")
    assert result == ParsedMeta("movie")


def test_extension_is_stripped() -> None:
    """扩展名不参与解析。"""
    assert parse_meta("Movie.Name.2025.mkv") == ParsedMeta("Movie Name", 2025)


def test_chinese_title_joins_without_space() -> None:
    """中文片段之间不插空格，英文之间插空格。"""
    assert parse_meta("三体 电视剧").title == "三体电视剧"
    assert parse_meta("The Matrix").title == "The Matrix"


@pytest.mark.parametrize(
    ("dirname", "expected"),
    [
        ("Season 01", 1),
        ("season 2", 2),
        ("S02", 2),
        ("第3季", 3),
        ("第二季", 2),
        ("Season 10", 10),
        # 剧名目录不是季目录
        ("凡人修仙传 (2020)", None),
        ("Show Name (2024)", None),
        ("", None),
    ],
)
def test_season_number_from_dir(dirname: str, expected: int | None) -> None:
    """目录名季号判定：季目录识别得出，剧名目录返回 None。"""
    assert season_number_from_dir(dirname) == expected


def test_season_dir_without_title_still_detected() -> None:
    """``Season 01`` 是纯季标记（无标题）——parse_meta 返回 None，
    但季号判定必须仍然识别出它是季目录（回归：整剧产物曾因此落进季目录）。
    """
    assert parse_meta("Season 01") is None
    assert season_number_from_dir("Season 01") == 1


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # 方括号里的中文译名是**候选标题**（回归：曾被当噪声剥掉，
        # 导致英文名搜到同名的另一部剧）
        (
            "[把黑夜点燃].Light.the.Dark.2026.S01E01.2160p.IQ.WEB-DL.mkv",
            ("把黑夜点燃",),
        ),
        (
            "[凡人修仙传].A.Mortal's.Journey.S01E196.2020.2160p.WEB-DL.mkv",
            ("凡人修仙传",),
        ),
        # 纯 ASCII 的方括号内容是发布组标记，不是标题
        ("[OurTV].Movie.Name.2025.1080p.mkv", ()),
        ("[GroupName].Movie.Name.2025.1080p.mkv", ()),
        # 没有方括号
        ("Movie.Name.2025.1080p.mkv", ()),
    ],
)
def test_alt_titles_extracted_from_brackets(filename: str, expected: tuple[str, ...]) -> None:
    """方括号内容：中文 → 候选标题；纯 ASCII（发布组）→ 忽略。"""
    result = parse_meta(filename)
    assert result is not None
    assert result.alt_titles == expected
