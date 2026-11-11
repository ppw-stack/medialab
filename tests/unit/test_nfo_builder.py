"""NFO 文档生成单元测试。

固化的契约：
- 根节点随层级变化（movie / tvshow / season / episodicedetails）
- 简介用 CDATA（避免 ``&`` 被转义成 ``&amp;``）
- ``uniqueid`` 同时写 ``tmdb`` 与 ``themoviedb``（媒体服务器各认一种）
- XML 声明带 ``standalone="yes"``（Kodi 期望）
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.adapters.metadata.nfo_builder import (
    build_episode_nfo,
    build_movie_nfo,
    build_season_nfo,
    build_tvshow_nfo,
)
from app.domain.scrape_types import EpisodeMeta, MediaMeta, SeasonMeta


def _movie_meta() -> MediaMeta:
    """电影元数据样本。"""
    return MediaMeta(
        tmdb_id=603,
        media_type="movie",
        title="黑客帝国",
        original_title="The Matrix",
        year=1999,
        release_date="1999-03-30",
        overview="一个关于现实与虚拟的故事 & 细节",
        vote_average=8.2,
        genres=("动作", "科幻"),
        directors=("拉·华纳",),
        actors=("基努·里维斯",),
        runtime=136,
    )


def _tv_meta() -> MediaMeta:
    """剧集元数据样本。"""
    return MediaMeta(
        tmdb_id=1399,
        media_type="tv",
        title="权力的游戏",
        year=2011,
        release_date="2011-04-17",
        overview="狼与王座",
        vote_average=8.4,
        status="Ended",
        season_count=8,
        episode_count=73,
        seasons=(
            SeasonMeta(season_number=0, name="特别篇"),
            SeasonMeta(season_number=1, name="第一季"),
        ),
    )


def _parse(xml_text: str) -> ET.Element:
    """解析 XML 文本 → 根节点。"""
    return ET.fromstring(xml_text)


def test_movie_nfo_root_and_fields() -> None:
    """电影 NFO：根节点 movie + 关键字段。"""
    xml_text = build_movie_nfo(_movie_meta())
    assert xml_text.startswith('<?xml version="1.0" encoding="utf-8" standalone="yes"?>')
    root = _parse(xml_text)
    assert root.tag == "movie"
    assert root.findtext("title") == "黑客帝国"
    assert root.findtext("originaltitle") == "The Matrix"
    assert root.findtext("year") == "1999"
    assert root.findtext("premiered") == "1999-03-30"
    assert root.findtext("rating") == "8.2"
    assert root.findtext("runtime") == "136"
    assert root.findtext("director") == "拉·华纳"
    assert root.findtext("actor/name") == "基努·里维斯"
    assert root.findtext("actor/type") == "Actor"
    assert [g.text for g in root.findall("genre")] == ["动作", "科幻"]


def test_movie_nfo_plot_is_cdata() -> None:
    """简介用 CDATA——``&`` 不被转义。"""
    xml_text = build_movie_nfo(_movie_meta())
    assert "<![CDATA[" in xml_text
    root = _parse(xml_text)
    assert root.findtext("plot") == "一个关于现实与虚拟的故事 & 细节"


def test_movie_nfo_writes_both_uniqueid_types() -> None:
    """uniqueid 同时写 tmdb 与 themoviedb。"""
    root = _parse(build_movie_nfo(_movie_meta()))
    ids = {node.get("type"): node.text for node in root.findall("uniqueid")}
    assert ids == {"tmdb": "603", "themoviedb": "603"}
    default_ids = [n for n in root.findall("uniqueid") if n.get("default") == "true"]
    assert len(default_ids) == 1


def test_tvshow_nfo_root_and_seasons() -> None:
    """电视剧 NFO：根节点 tvshow + 季列表。"""
    root = _parse(build_tvshow_nfo(_tv_meta()))
    assert root.tag == "tvshow"
    assert root.findtext("status") == "Ended"
    assert root.findtext("totalseasons") == "8"
    assert root.findtext("totalepisodes") == "73"
    assert [s.text for s in root.findall("season/season")] == ["0", "1"]


def test_season_nfo_root() -> None:
    """季 NFO：根节点 season + 季号。"""
    season = SeasonMeta(
        season_number=1,
        name="第一季",
        overview="第一季简介",
        air_date="2011-04-17",
    )
    root = _parse(build_season_nfo(_tv_meta(), season))
    assert root.tag == "season"
    assert root.findtext("title") == "第一季"
    assert root.findtext("seasonnumber") == "1"
    assert root.findtext("premiered") == "2011-04-17"
    assert root.findtext("plot") == "第一季简介"


def test_episode_nfo_root() -> None:
    """单集 NFO：根节点 episodicedetails + 季集号。"""
    episode = EpisodeMeta(
        episode_number=2,
        name="让王座坐实",
        overview="单集简介",
        still_path="/still.jpg",
        air_date="2011-04-24",
    )
    root = _parse(build_episode_nfo(_tv_meta(), 1, episode))
    assert root.tag == "episodedetails"
    assert root.findtext("title") == "让王座坐实"
    assert root.findtext("season") == "1"
    assert root.findtext("episode") == "2"
    assert root.findtext("aired") == "2011-04-24"
    assert root.findtext("plot") == "单集简介"


def test_episode_nfo_falls_back_to_generic_title() -> None:
    """无集名时用「第 N 集」兜底。"""
    episode = EpisodeMeta(episode_number=7)
    root = _parse(build_episode_nfo(_tv_meta(), 2, episode))
    assert root.findtext("title") == "第 7 集"
