"""整理与刮削的领域常量（不依赖任何层）。

这些是**业务概念**：刮削产物是什么、整理动作为什么，与它们存在哪张表、
存在哪个文件无关。住在 :mod:`app.domain.enums` 同层，可被 db / services /
api 任意层安全导入。

与 :mod:`app.domain.enums` 的分工：那边是**辅种域**的枚举（免费性质 /
推送状态），这边是**媒体库域**的整理刮削枚举。两边互不混用，避免媒体库
域的概念渗进辅种域。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "MEDIA_KINDS",
    "EpisodeMeta",
    "MediaMeta",
    "OrganizeAction",
    "OrganizeStatus",
    "ScrapeKind",
    "ScrapePolicy",
    "ScrapeStatus",
    "SeasonMeta",
]


class ScrapeKind:
    """刮削产物的层级（决定文件名与落盘位置）。

    对齐 Kodi / Jellyfin 的媒体库约定——**不同层级的产物文件名不同**，
    写错名字媒体服务器就认不出来：

    - ``MOVIE``：电影 → ``<片名>.nfo``（与视频同级）
    - ``TVSHOW``：电视剧根目录 → ``tvshow.nfo``
    - ``SEASON``：季目录 → ``season.nfo``
    - ``EPISODE``：单集 → ``<集名>.nfo``（与视频同级）
    """

    MOVIE = "movie"
    TVSHOW = "tvshow"
    SEASON = "season"
    EPISODE = "episode"


class MediaKind:
    """媒体类型（电影 / 电视剧）。"""

    MOVIE = "movie"
    TV = "tv"


#: 合法媒体类型取值
MEDIA_KINDS: tuple[str, ...] = (MediaKind.MOVIE, MediaKind.TV)


class ScrapePolicy:
    """刮削策略（目标文件已存在时怎么办）。

    - ``MISSING_ONLY``：仅补缺——目标已存在就跳过（**默认**）
    - ``OVERWRITE``：覆盖——每次都重写

    语义与 MoviePilot 的刮削策略一致，但收敛成两档：MoviePilot 还有
    ``UPGRADE``（歌词专用），本项目不做音乐刮削，用不到。
    """

    MISSING_ONLY = "missing_only"
    OVERWRITE = "overwrite"


class ScrapeStatus:
    """刮削结果状态。

    **没有 PENDING**：刮削是同步动作，每次执行当场出结果，不需要
    「排队中」这个中间态（与 ``PushStatus`` 不设 PENDING 同理）。
    """

    #: 已刮削成功
    SCRAPED = "scraped"
    #: 失败（识别不到 / 网络故障 / 写盘失败）——失败必须点名，不写 0 兜底
    FAILED = "failed"
    #: 按策略跳过（目标已存在且策略为仅补缺）
    SKIPPED = "skipped"


class OrganizeAction:
    """整理动作（源文件怎么处理）。

    - ``COPY``：复制到媒体库，源文件保留（**默认**）
    - ``MOVE``：复制完成后删除源文件

    **为什么没有硬链接**：硬链接要求源与目标同分区，跨设备不可用；
    且 NAS / CIFS 挂载下的硬链接语义不可靠——与其给用户一个「有时能链、
    有时退化成复制」的行为，不如显式两种语义。

    **MOVE 的风险**：移动下载器里的文件会弄死做种（tracker 找不到数据）。
    因此 move 必须显式开启，且默认只用于**非辅种**下载器。
    """

    COPY = "copy"
    MOVE = "move"


class OrganizeStatus:
    """整理结果状态。"""

    #: 整理成功
    SUCCESS = "success"
    #: 失败（识别不到 / 目标冲突 / 拷贝出错）——失败必须点名
    FAILED = "failed"
    #: 跳过（已整理过 / 目标已存在且大小一致 / 种子状态不允许整理）
    SKIPPED = "skipped"


# ------------------------------------------------------------------ 数据类


@dataclass(frozen=True, slots=True)
class EpisodeMeta:
    """单集元数据。

    :param episode_number: 集号
    :param name: 集名
    :param overview: 单集简介
    :param still_path: 截图相对路径（TMDB 路径，不含图片基址）
    :param air_date: 播出日期
    """

    episode_number: int
    name: str | None = None
    overview: str | None = None
    still_path: str | None = None
    air_date: str | None = None


@dataclass(frozen=True, slots=True)
class SeasonMeta:
    """季元数据。

    :param season_number: 季号（0 = 特别篇）
    :param name: 季名
    :param overview: 季简介
    :param poster_path: 季海报相对路径
    :param air_date: 首播日期
    :param episodes: 分集清单
    """

    season_number: int
    name: str | None = None
    overview: str | None = None
    poster_path: str | None = None
    air_date: str | None = None
    episodes: tuple[EpisodeMeta, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MediaMeta:
    """媒体元数据（TMDB 精简模型）。

    **刻意做小**：只保留「写 NFO + 选图片」真正用得到的字段。MoviePilot 的
    ``MediaInfo`` 有 100+ 字段，那是它的搜索 / 订阅 / 过滤全链共用的结果对象；
    本项目只做识别后的产物生成，字段越少越好维护。

    图片字段存的是 **TMDB 相对路径**（如 ``/abc.jpg``），拼基址由
    :class:`app.adapters.metadata.tmdb.TmdbClient` 负责——领域对象不关心
    基址配置（可换 CDN / 代理）。
    """

    tmdb_id: int
    media_type: str
    title: str
    original_title: str | None = None
    #: 原语言代码（如 ``en`` / ``ja`` / ``zh``）——原语言图片靠它取
    original_language: str | None = None
    year: int | None = None
    release_date: str | None = None
    overview: str | None = None
    vote_average: float | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    logo_path: str | None = None
    genres: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()
    runtime: int | None = None
    status: str | None = None
    season_count: int | None = None
    episode_count: int | None = None
    #: 电视剧分季摘要（仅电视剧有）
    seasons: tuple[SeasonMeta, ...] = field(default_factory=tuple)
