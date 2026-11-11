"""Kodi 风格 NFO 文档生成。

**格式是公开约定**（Kodi / Jellyfin / Emby 都读它），本模块按约定**重新实现**，
不复制任何第三方实现代码。

四种文档层级：

===============  ==========================  ==========================
层级             文件名                        根节点
===============  ==========================  ==========================
电影             ``<片名>.nfo``                ``<movie>``
电视剧（整剧）   ``tvshow.nfo``                ``<tvshow>``
季               ``season.nfo``                ``<season>``
单集             ``<集名>.nfo``                ``<episodedetails>``
===============  ==========================  ==========================

简介类字段用 CDATA 包裹——简介里几乎必然含 ``&`` ``<`` ``>``，转义后
Kodi 侧显示会带 ``&amp;``。
"""

from __future__ import annotations

from xml.dom import minidom

from app.domain.scrape_types import EpisodeMeta, MediaMeta, SeasonMeta

__all__ = [
    "build_episode_nfo",
    "build_movie_nfo",
    "build_season_nfo",
    "build_tvshow_nfo",
]


#: XML 声明（Kodi 期望 ``standalone="yes"``）
_XML_DECLARATION = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'


def _add_node(
    doc: minidom.Document,
    parent: minidom.Element | minidom.Document,
    tag: str,
    text: str | None = None,
    *,
    cdata: bool = False,
) -> minidom.Element:
    """往父节点下加一个子节点。

    :param doc: 文档
    :param parent: 父节点（根节点创建时传 ``doc`` 本身）
    :param tag: 标签名
    :param text: 文本内容；为空字符串时**不建节点**（避免出现空标签）
    :param cdata: 是否用 CDATA 包裹
    :return: 新建的节点
    """
    node = doc.createElement(tag)
    if text:
        if cdata:
            node.appendChild(doc.createCDATASection(text))
        else:
            node.appendChild(doc.createTextNode(text))
    parent.appendChild(node)
    return node


def _serialize(doc: minidom.Document) -> str:
    """序列化为带声明的 XML 文本。"""
    xml = doc.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    # toprettyxml 的声明缺 ``standalone="yes"``，Kodi 认这个属性
    return xml.replace(
        '<?xml version="1.0" encoding="utf-8"?>',
        _XML_DECLARATION,
        1,
    )


def _add_unique_ids(doc: minidom.Document, parent: minidom.Element, tmdb_id: int) -> None:
    """写 TMDB 标识。

    **写两个 ``uniqueid``**（``tmdb`` 与 ``themoviedb``）：Jellyfin / Emby
    认 ``tmdb``，Kodi 生态与 MoviePilot 用 ``themoviedb``。两个都写，
    各媒体服务器都能对上号——成本是几十字节。
    """
    for type_name in ("tmdb", "themoviedb"):
        node = _add_node(doc, parent, "uniqueid", str(tmdb_id))
        node.setAttribute("type", type_name)
        if type_name == "tmdb":
            node.setAttribute("default", "true")


def _add_common_nodes(
    doc: minidom.Document,
    parent: minidom.Element,
    meta: MediaMeta,
) -> None:
    """写各媒体类型共享的字段。"""
    _add_node(doc, parent, "title", meta.title)
    _add_node(doc, parent, "originaltitle", meta.original_title or meta.title)
    if meta.year:
        _add_node(doc, parent, "year", str(meta.year))
    if meta.release_date:
        _add_node(doc, parent, "premiered", meta.release_date)
    if meta.vote_average is not None:
        _add_node(doc, parent, "rating", f"{meta.vote_average:.1f}")
    _add_node(doc, parent, "plot", meta.overview or "", cdata=True)
    _add_node(doc, parent, "outline", meta.overview or "", cdata=True)
    _add_unique_ids(doc, parent, meta.tmdb_id)


def _add_genres(doc: minidom.Document, parent: minidom.Element, meta: MediaMeta) -> None:
    """写风格标签。"""
    for genre in meta.genres:
        _add_node(doc, parent, "genre", genre)


def _add_people(doc: minidom.Document, parent: minidom.Element, meta: MediaMeta) -> None:
    """写导演与演员。

    演员只写名字与类型——TMDB 详情里的头像 URL 不进 NFO（媒体服务器自己
    会去抓演员头像，多写反而可能指向失效链接）。
    """
    for director in meta.directors:
        _add_node(doc, parent, "director", director)
    for actor in meta.actors:
        node = _add_node(doc, parent, "actor")
        _add_node(doc, node, "name", actor)
        _add_node(doc, node, "type", "Actor")


def build_movie_nfo(meta: MediaMeta) -> str:
    """生成电影 NFO。

    :param meta: 电影元数据
    :return: XML 文本
    """
    doc = minidom.Document()
    root = _add_node(doc, doc, "movie")
    _add_common_nodes(doc, root, meta)
    _add_genres(doc, root, meta)
    _add_people(doc, root, meta)
    if meta.runtime:
        _add_node(doc, root, "runtime", str(meta.runtime))
    return _serialize(doc)


def build_tvshow_nfo(meta: MediaMeta) -> str:
    """生成电视剧（整剧）NFO。

    :param meta: 剧集元数据
    :return: XML 文本
    """
    doc = minidom.Document()
    root = _add_node(doc, doc, "tvshow")
    _add_common_nodes(doc, root, meta)
    _add_genres(doc, root, meta)
    _add_people(doc, root, meta)
    if meta.status:
        _add_node(doc, root, "status", meta.status)
    if meta.season_count:
        _add_node(doc, root, "totalseasons", str(meta.season_count))
    if meta.episode_count:
        _add_node(doc, root, "totalepisodes", str(meta.episode_count))
    if meta.seasons:
        seasons_node = _add_node(doc, root, "season")
        for season in meta.seasons:
            _add_node(doc, seasons_node, "season", str(season.season_number))
    return _serialize(doc)


def build_season_nfo(meta: MediaMeta, season: SeasonMeta) -> str:
    """生成季 NFO。

    :param meta: 剧集元数据
    :param season: 季元数据
    :return: XML 文本
    """
    doc = minidom.Document()
    root = _add_node(doc, doc, "season")
    _add_node(doc, root, "title", season.name or meta.title)
    _add_node(doc, root, "seasonnumber", str(season.season_number))
    if meta.year:
        _add_node(doc, root, "year", str(meta.year))
    if season.air_date:
        _add_node(doc, root, "premiered", season.air_date)
    _add_node(doc, root, "plot", season.overview or meta.overview or "", cdata=True)
    _add_node(doc, root, "outline", season.overview or meta.overview or "", cdata=True)
    _add_unique_ids(doc, root, meta.tmdb_id)
    return _serialize(doc)


def build_episode_nfo(
    meta: MediaMeta,
    season: int,
    episode: EpisodeMeta,
) -> str:
    """生成单集 NFO。

    :param meta: 剧集元数据
    :param season: 季号
    :param episode: 单集元数据
    :return: XML 文本
    """
    doc = minidom.Document()
    root = _add_node(doc, doc, "episodedetails")
    _add_node(doc, root, "title", episode.name or f"第 {episode.episode_number} 集")
    _add_node(doc, root, "season", str(season))
    _add_node(doc, root, "episode", str(episode.episode_number))
    if episode.air_date:
        _add_node(doc, root, "aired", episode.air_date)
    _add_node(doc, root, "plot", episode.overview or "", cdata=True)
    _add_node(doc, root, "outline", episode.overview or "", cdata=True)
    _add_unique_ids(doc, root, meta.tmdb_id)
    return _serialize(doc)
