"""TMDB 客户端单元测试（respx 打桩，不发真实请求）。

固化的契约：
- **查不到**（404 / 空 results）→ 返回 None
- **查挂了**（网络错误 / 5xx / 401）→ 抛 :class:`TmdbError`（不折叠成 None）
- 搜索结果挑选：标题完全匹配 > 年份匹配 > 热度
- 图片挑选：vote_average → vote_count
"""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.metadata.tmdb import TmdbClient, TmdbError

API = "https://api.themoviedb.org/3"


def _client() -> TmdbClient:
    """构造测试客户端。"""
    return TmdbClient("test-key", locale="zh-CN")


# ------------------------------------------------------------------ 搜索


@respx.mock
async def test_search_movie_returns_first_exact_match() -> None:
    """标题完全匹配的候选优先于热度更高的其它结果。"""
    route = respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 2,
                        "title": "三体",
                        "popularity": 99.0,
                        "release_date": "2023-01-01",
                    },
                    {
                        "id": 1,
                        "title": "三体",
                        "popularity": 50.0,
                        "release_date": "2023-01-01",
                    },
                ]
            },
        )
    )
    result = await _client().search("三体")
    assert result is not None
    assert result.tmdb_id == 2
    assert result.media_type == "movie"
    assert route.called


@respx.mock
async def test_search_prefers_year_match() -> None:
    """标题不同但年份吻合的候选优先于无年份候选。"""
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 1, "title": "别的片", "popularity": 99.0},
                    {
                        "id": 2,
                        "title": "某片",
                        "popularity": 10.0,
                        "release_date": "2025-05-01",
                    },
                ]
            },
        )
    )
    result = await _client().search("某片", year=2025)
    assert result is not None
    assert result.tmdb_id == 2
    assert result.year == 2025


@respx.mock
async def test_search_empty_returns_none() -> None:
    """空结果 → None（正常业务结果，不是故障）。"""
    respx.get(f"{API}/search/movie").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    assert await _client().search("不存在的片子") is None


async def test_search_blank_title_skips_request() -> None:
    """空标题直接返回 None，不发请求。"""
    assert await _client().search("  ") is None


# ------------------------------------------------------------------ 详情


@respx.mock
async def test_get_movie_parses_credits_and_images() -> None:
    """详情响应 → 演职员 + 最佳图片。"""
    respx.get(f"{API}/movie/603").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 603,
                "title": "黑客帝国",
                "original_title": "The Matrix",
                "release_date": "1999-03-30",
                "overview": "描述 & 符号",
                "vote_average": 8.2,
                "runtime": 136,
                "genres": [{"name": "动作"}, {"name": "科幻"}],
                "credits": {
                    "cast": [{"name": "基努·里维斯"}],
                    "crew": [
                        {"name": "拉·华纳", "job": "Director"},
                        {"name": "某编剧", "job": "Writer"},
                    ],
                },
                "images": {
                    "posters": [
                        {"file_path": "/low.jpg", "vote_average": 5.0, "vote_count": 10},
                        {"file_path": "/best.jpg", "vote_average": 8.0, "vote_count": 5},
                    ],
                    "logos": [{"file_path": "/logo.png"}],
                },
            },
        )
    )
    meta = await _client().get_movie(603)
    assert meta is not None
    assert meta.title == "黑客帝国"
    assert meta.original_title == "The Matrix"
    assert meta.year == 1999
    assert meta.directors == ("拉·华纳",)
    assert meta.actors == ("基努·里维斯",)
    assert meta.genres == ("动作", "科幻")
    assert meta.runtime == 136
    # 当前语言没给 poster_path → 用 images 里评分最高的一张
    assert meta.poster_path == "/best.jpg"
    assert meta.logo_path == "/logo.png"


@respx.mock
async def test_search_any_prefers_exact_match_of_chinese_candidate() -> None:
    """多候选：英文候选只能模糊命中，中文候选 exact 命中 → 取中文那条。

    回归（2026-11-11 实测）：``Light the Dark`` 在 TMDB 上会搜到《黑夜告白》
    （popularity 更高），而正确答案是《把黑夜点燃》——方括号里的中文译名
    搜过去能唯一命中，必须优先采用。
    """
    def _search(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if query == "Light the Dark":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 254498,
                            "name": "黑夜告白",
                            "popularity": 5.277,
                            "first_air_date": "2026-04-26",
                        },
                        {
                            "id": 288328,
                            "name": "把黑夜点燃",
                            "popularity": 3.708,
                            "first_air_date": "2026-10-15",
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 288328,
                        "name": "把黑夜点燃",
                        "popularity": 3.708,
                        "first_air_date": "2026-10-15",
                    }
                ]
            },
        )

    respx.get(f"{API}/search/tv").mock(side_effect=_search)
    respx.get(f"{API}/search/movie").mock(side_effect=_search)

    result = await TmdbClient("k").search_any(
        ["把黑夜点燃", "Light the Dark"], year=2026, media_type="tv"
    )
    assert result is not None
    assert result.tmdb_id == 288328, "中文候选 exact 命中必须优先于英文候选的模糊命中"


@respx.mock
async def test_get_movie_404_returns_none() -> None:
    """404 → None（查不到，不是故障）。"""
    respx.get(f"{API}/movie/999999").mock(return_value=httpx.Response(404))
    assert await _client().get_movie(999999) is None


@respx.mock
async def test_get_tv_parses_seasons() -> None:
    """剧集详情 → 分季摘要。"""
    respx.get(f"{API}/tv/1399").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1399,
                "name": "权力的游戏",
                "first_air_date": "2011-04-17",
                "number_of_seasons": 8,
                "number_of_episodes": 73,
                "status": "Ended",
                "seasons": [
                    {"season_number": 0, "name": "特别篇"},
                    {"season_number": 1, "name": "第一季"},
                ],
            },
        )
    )
    meta = await _client().get_tv(1399)
    assert meta is not None
    assert meta.media_type == "tv"
    assert meta.year == 2011
    assert meta.status == "Ended"
    assert [s.season_number for s in meta.seasons] == [0, 1]


@respx.mock
async def test_get_season_parses_episodes() -> None:
    """季详情 → 分集清单。"""
    respx.get(f"{API}/tv/1399/season/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "season_number": 1,
                "name": "第一季",
                "air_date": "2011-04-17",
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "王权",
                        "overview": "简介",
                        "still_path": "/still1.jpg",
                        "air_date": "2011-04-17",
                    },
                    {"episode_number": 2, "name": "让王座坐实"},
                ],
            },
        )
    )
    season = await _client().get_season(1399, 1)
    assert season is not None
    assert season.season_number == 1
    assert len(season.episodes) == 2
    assert season.episodes[0].still_path == "/still1.jpg"
    assert season.episodes[0].overview == "简介"


# ------------------------------------------------------------------ 故障语义


@respx.mock
async def test_unauthorized_raises_tmdb_error() -> None:
    """401 → 抛 TmdbError（Key 失效必须暴露，不能伪装成「查不到」）。"""
    respx.get(f"{API}/movie/1").mock(return_value=httpx.Response(401))
    with pytest.raises(TmdbError, match="鉴权失败"):
        await _client().get_movie(1)


@respx.mock
async def test_server_error_raises_after_retries() -> None:
    """5xx 重试后仍失败 → 抛 TmdbError。"""
    respx.get(f"{API}/movie/1").mock(return_value=httpx.Response(500))
    with pytest.raises(TmdbError):
        await _client().get_movie(1)


@respx.mock
async def test_network_error_raises_tmdb_error() -> None:
    """网络错误 → 抛 TmdbError。"""
    respx.get(f"{API}/movie/1").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TmdbError):
        await _client().get_movie(1)


@respx.mock
async def test_rate_limit_retries_then_succeeds() -> None:
    """429 退避后重试成功。"""
    route = respx.get(f"{API}/movie/1")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"id": 1, "title": "片"}),
    ]
    meta = await _client().get_movie(1)
    assert meta is not None
    assert meta.title == "片"


# ------------------------------------------------------------------ 图片地址


def test_image_url_joins_base_and_path() -> None:
    """相对路径 → 完整图片地址。"""
    client = TmdbClient("k", image_base="https://img.example.com/t/p")
    assert (
        client.image_url("/abc.jpg")
        == "https://img.example.com/t/p/original/abc.jpg"
    )


def test_image_url_none_returns_none() -> None:
    """空路径 → None。"""
    assert TmdbClient("k").image_url(None) is None
