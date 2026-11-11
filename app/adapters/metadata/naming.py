"""媒体库目录约定：产物文件名与落盘位置。

**这是 Kodi / Jellyfin / Emby 的公开约定**，按约定重新实现：

- 电影：``<片名>.nfo``（与视频同级）
- 电视剧：剧集根目录 ``tvshow.nfo``，季目录 ``season.nfo``
- 单集：``<集名>.nfo``（与视频同级）
- 图片：``poster.jpg`` / ``backdrop.jpg`` / ``logo.png``（目录内）

**为什么文件名写死**：MoviePilot 允许用 Jinja 模板自定义命名，但模板一旦
写错（拼出媒体服务器认不出的名字），排查成本极高——现象是「媒体服务器
不认这个目录」，但不知道是模板哪一段出了问题。本项目固定约定，
真需要自定义时再开（二期）。

**季图与根图**：季图片写季目录，剧集级图片写剧集根目录——两个目录的
产物互不重叠，媒体服务器按目录层级自动匹配。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.scrape_types import ScrapeKind

__all__ = [
    "IMAGE_TYPES",
    "image_filename",
    "image_path",
    "nfo_filename",
    "nfo_path",
]


#: 图片类型 → 文件名
_IMAGE_FILENAMES: dict[str, str] = {
    "poster": "poster.jpg",
    "backdrop": "backdrop.jpg",
    "logo": "logo.png",
    "thumb": "thumb.jpg",
}

#: 支持的图片类型
IMAGE_TYPES: tuple[str, ...] = tuple(_IMAGE_FILENAMES)

#: 固定文件名的 NFO（电视剧层级）
_NFO_FILENAMES: dict[str, str] = {
    ScrapeKind.TVSHOW: "tvshow.nfo",
    ScrapeKind.SEASON: "season.nfo",
}


def nfo_filename(kind: str, stem: str | None = None) -> str:
    """NFO 文件名。

    :param kind: :class:`~app.domain.scrape_types.ScrapeKind` 取值
    :param stem: 视频文件名主干（电影 / 单集用）
    :return: 文件名
    :raises ValueError: 电影 / 单集缺少 stem
    """
    if kind in _NFO_FILENAMES:
        return _NFO_FILENAMES[kind]
    if not stem:
        raise ValueError(f"{kind} 的 NFO 需要视频文件名主干")
    return f"{stem}.nfo"


def image_filename(kind: str, image_type: str, stem: str | None = None) -> str:
    """图片文件名。

    集缩略图特殊：``<集名>-thumb.jpg``——多集同目录时若都叫 ``thumb.jpg``
    会互相覆盖。

    :param kind: :class:`~app.domain.scrape_types.ScrapeKind` 取值
    :param image_type: 图片类型（``poster`` / ``backdrop`` / ``logo`` / ``thumb``）
    :param stem: 视频文件名主干（单集缩略图用）
    :return: 文件名
    :raises ValueError: 未知图片类型，或单集缩略图缺少 stem
    """
    if image_type not in _IMAGE_FILENAMES:
        raise ValueError(f"未知图片类型：{image_type}")
    if kind == ScrapeKind.EPISODE and image_type == "thumb":
        if not stem:
            raise ValueError("单集缩略图需要视频文件名主干")
        return f"{stem}-thumb.jpg"
    return _IMAGE_FILENAMES[image_type]


def nfo_path(directory: Path, kind: str, stem: str | None = None) -> Path:
    """NFO 的完整落盘路径。

    :param directory: 目标目录
    :param kind: 刮削层级
    :param stem: 视频文件名主干（电影 / 单集用）
    :return: 完整路径
    """
    return directory / nfo_filename(kind, stem)


def image_path(
    directory: Path,
    kind: str,
    image_type: str,
    stem: str | None = None,
) -> Path:
    """图片的完整落盘路径。

    :param directory: 目标目录
    :param kind: 刮削层级
    :param image_type: 图片类型
    :param stem: 视频文件名主干（单集缩略图用）
    :return: 完整路径
    """
    return directory / image_filename(kind, image_type, stem)
