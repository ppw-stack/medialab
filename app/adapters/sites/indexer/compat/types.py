"""移植代码使用的最小媒体类型枚举。

MoviePilot 的 ``MediaType`` 还带大量业务方法，解析路径只用到
``MOVIE`` / ``TV`` / ``MUSIC`` / ``UNKNOWN`` 与 ``from_agent``。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class MediaType(str, Enum):
    """站点分类使用的媒体类型。"""

    MOVIE = "电影"
    TV = "电视剧"
    MUSIC = "音乐"
    COLLECTION = "系列"
    UNKNOWN = "未知"

    @classmethod
    def from_agent(cls, value: Optional[str]) -> "MediaType":
        """把配置里的字符串（含英文别名）映射成枚举。"""
        if not value:
            return cls.UNKNOWN
        text = str(value).strip()
        aliases = {
            "movie": cls.MOVIE, "电影": cls.MOVIE,
            "tv": cls.TV, "电视剧": cls.TV,
            "music": cls.MUSIC, "音乐": cls.MUSIC,
            "collection": cls.COLLECTION, "系列": cls.COLLECTION,
        }
        return aliases.get(text.lower(), cls.UNKNOWN)


class MediaSource(str, Enum):
    """种子页面声明的媒体身份来源（移植代码只做透传）。"""

    IMDb = "imdb"
    TMDB = "tmdb"
    Douban = "douban"
    TVDB = "tvdb"

