"""文件后缀分类与文件名清洗。

**strm 文件名去掉视频扩展名**：``东极岛 (2025) - 1080p.mp4`` →
``东极岛 (2025) - 1080p.strm``。

理由：媒体服务器按「同目录同 stem」匹配 nfo 与媒体文件——
``x.mkv.strm`` 的 stem 是 ``x.mkv``，而网盘侧的 nfo 多数叫 ``x.nfo``
（stem ``x``）→ 对不上，Emby / Jellyfin 认不到 nfo，元数据刮不出来。
去掉扩展名后 stem 统一成 ``x``，媒体服务器才能认到。

其余部分沿用网盘里的原文件名（原文件名通常已包含完整的发行信息：
标题 / 年份 / 画质 / 编码，自己再拼一遍反而容易出错，如把片名里的
``1900`` 误当年份）。

本模块只保留两件事：

- :func:`classify_by_ext` —— 按配置的后缀把文件分成 视频 / 图片 / 数据 / 未知
- :func:`sanitize` —— 清洗单个路径组件（防御性：源文件名理论上安全，
  但写盘前仍要做基本校验，避免非法字符导致写文件失败）
"""

from __future__ import annotations

import re

#: 文件系统敏感字符
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 连续的点（消除路径穿越残留 ``..``）
_MULTI_DOT = re.compile(r"\.{2,}")

#: Windows 保留名（不能作为文件名）
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def split_name(filename: str) -> tuple[str, str]:
    """拆文件名与扩展名（扩展名小写，不含点）。"""
    stem = filename
    ext = ""
    if "." in filename:
        stem, _, suffix = filename.rpartition(".")
        if suffix and len(suffix) <= 5:
            ext = suffix.lower()
        else:
            stem = filename
    return stem, ext


def parse_ext_list(raw: str | None) -> frozenset[str]:
    """把逗号分隔的后缀串解析成集合（统一带点、小写）。

    Args:
        raw: 如 ``"mkv, mp4"`` 或 ``".mkv,.mp4"``。

    Returns:
        形如 ``{".mkv", ".mp4"}`` 的集合。
    """
    if not raw:
        return frozenset()
    result: set[str] = set()
    for part in raw.split(","):
        item = part.strip().lower().lstrip(".")
        if item:
            result.add(f".{item}")
    return frozenset(result)


def classify_by_ext(
    filename: str,
    video_exts: frozenset[str],
    image_exts: frozenset[str] = frozenset(),
    data_exts: frozenset[str] = frozenset(),
) -> str:
    """按配置的后缀分类文件。

    Returns:
        - ``"video"`` —— 视频后缀（**应生成 strm**）
        - ``"image"`` —— 图片后缀（当前忽略）
        - ``"data"`` —— 数据后缀（当前忽略）
        - ``"unknown"`` —— 其它后缀（忽略）

    图片 / 数据 / 未知**分开计数**——便于排查「为什么这个文件没生成」。

    **隐藏备份文件一律算 unknown**：115 客户端会在目录里留
    ``.mp-backup-xxxx**..mp4`` 这类自动备份（实测 16 个 / 28 GB），
    它们后缀确实是 ``.mp4``，若按后缀分类会被当成正片生成 strm。
    """
    if is_hidden_backup(filename):
        return "unknown"
    _, ext = split_name(filename)
    if not ext:
        return "unknown"
    dotted = f".{ext}"
    if dotted in video_exts:
        return "video"
    if dotted in image_exts:
        return "image"
    if dotted in data_exts:
        return "data"
    return "unknown"


#: 隐藏备份文件的名字特征（115 客户端的自动备份残留）
HIDDEN_BACKUP_PATTERNS = (".mp-backup",)


def is_hidden_backup(filename: str) -> bool:
    """判断是不是隐藏备份文件。

    判定条件：**以 ``.`` 开头**且名字里含备份特征——两条都要，避免误伤
    正常的隐藏文件（``.config`` 之类）。
    """
    return filename.startswith(".") and any(
        p in filename for p in HIDDEN_BACKUP_PATTERNS
    )


def sanitize(name: str) -> str:
    """清洗单个路径组件。

    - 去掉文件系统敏感字符（``<>:"/\\|?*`` 与控制字符）
    - **折叠连续的点**（消除 ``..`` —— 剥掉 ``/`` 后 ``../../etc`` 会剩下
      ``....etc``，虽然已无分隔符不构成穿越，但不该留这种残留）
    - 去掉结尾的点与空格（Windows 不允许）
    - 避开 Windows 保留名（``CON`` / ``PRN`` …）

    源文件名理论上不含这些（115 的文件名是合法文件名），但写盘前仍要过一遍
    ——把「不可能」当假设，比出事后排查便宜。
    """
    cleaned = _UNSAFE_CHARS.sub("", name).strip()
    cleaned = _MULTI_DOT.sub(".", cleaned)
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return ""
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def strm_filename(
    source_name: str, video_exts: frozenset[str] | None = None
) -> str:
    """由源文件名算出 ``.strm`` 文件名。

    ``东极岛 (2025) - 1080p.mp4`` → ``东极岛 (2025) - 1080p.strm``

    **去掉视频扩展名**：媒体服务器按「同目录同 stem」匹配 nfo——
    ``x.mkv.strm`` 的 stem 是 ``x.mkv``，网盘侧的 nfo 多数叫 ``x.nfo``
    （stem ``x``）→ 对不上，刮不到元数据（见模块 docstring）。

    只剥离**属于 ``video_exts`` 的**后缀（用户配什么剥什么）：其余后缀
    一律保留，避免把 ``movie.2020.1080p`` 剥成 ``movie.2020``。
    ``video_exts`` 为 ``None`` 时不剥离（保留完整原名）。
    """
    cleaned = sanitize(source_name)
    if not cleaned:
        return "untitled.strm"
    if video_exts:
        stem, ext = split_name(cleaned)
        if stem and f".{ext}" in video_exts:
            cleaned = stem
    return f"{cleaned}.strm"
