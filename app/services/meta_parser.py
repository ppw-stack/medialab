"""文件名解析：从下载得到的文件名里提取标题 / 年份 / 季集。

**这是精简版，不是 MoviePilot 的 ``MetaInfo``**（后者 6863 行，含发行组词表、
流媒体平台词表、拼音标题、自定义识别词引擎）。本项目整理场景只需要 4 个字段
（title / year / season / episode），那 4 个恰好是整个解析器里最稳定的部分——
用 ~200 行正则覆盖，其余靠**失败明细驱动迭代**（见
:mod:`app.services.scrape_service` 的失败记录）。

设计要点：

1. **标题靠「年份之前的片段」**——``Movie.Name.2025.1080p`` → ``Movie Name``。
   不用「取第一段」是因为中文剧名常带空格（``三体 电视剧``），第一段只是「三体」。
2. **年份只挖最后一个**——``1917.2019.1080p`` 里 ``1917`` 是**片名**不是年份，
   挖掉 ``2019`` 后标题正好是 ``1917``；反过来只挖 ``1917`` 会得到空标题。
3. **季集先挖掉再切标题**——季集号不是标题的一部分。
4. **噪声词表保守**——只收画质 / 来源 / 版本 / 语种的**明确标记**，
   全小写精确匹配。宁可漏删（标题里多几个词，TMDB 搜索仍能匹配），
   不可错删（把片名删掉就再也认不出来了）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ParsedMeta", "parse_meta", "parse_meta_from_path"]


@dataclass(frozen=True, slots=True)
class ParsedMeta:
    """文件名解析结果。

    :param title: 标题（已剥掉画质/版本等噪声片段）
    :param year: 年份；解析不到为 None
    :param season: 季号；解析不到为 None
    :param episode: 集号；解析不到为 None
    :param is_episode: 是否解析出集号（有集号 = 单集文件）
    :param alt_titles: 备用标题候选（如方括号里的中文译名）
    """

    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    is_episode: bool = False
    alt_titles: tuple[str, ...] = ()


# ------------------------------------------------------------------ 噪声词表

#: 画质 / 编码 / 音频：出现在标题里几乎不可能，删掉最安全。
_QUALITY_TOKENS: frozenset[str] = frozenset(
    {
        "1080p", "1080i", "720p", "480p", "576p", "2160p", "4k", "uhd", "fhd",
        "hd", "sd", "hq",
        "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "vp9", "mpeg2",
        "10bit", "8bit", "hdr", "hdr10", "hdr10plus", "dolbyvision", "dv",
        "sdr", "hlg",
        "ddp", "dd", "dts", "dtshd", "dtsx", "truehd", "atmos", "ac3", "eac3",
        "aac", "aac2", "flac", "opus", "mp3", "audiocore",
    }
)

#: 来源 / 发行渠道
_SOURCE_TOKENS: frozenset[str] = frozenset(
    {
        "web", "webrip", "webdl", "web-dl", "bluray", "blu-ray", "bdrip",
        "brrip", "bdremux", "remux", "hdtv", "hdts", "bdts", "hddvd", "dvdrip",
        "dvd", "dvdscr", "retail", "cam", "camrip", "ts", "tc", "r5", "vhs",
        "rip", "hdrip", "nf", "amzn", "hulu", "disney", "atvp", "max",
    }
)

#: 版本 / 压制标记
_VERSION_TOKENS: frozenset[str] = frozenset(
    {
        "extended", "uncut", "directorscut", "directors-cut", "theatrical",
        "proper", "repack", "remastered", "imax", "criterion", "unrated",
        "remaster", "rerip", "internal",
    }
)

#: 中文标记（语种 / 版本 / 压制属性）
_CN_TOKENS: frozenset[str] = frozenset(
    {
        "国语", "粤语", "英语", "日语", "韩语", "国粤", "国日", "双语",
        "中字", "中英", "中字版", "特效字幕", "特效", "简体", "繁体", "简繁",
        "完整版", "收藏版", "抢先版", "抢先", "完结", "连载", "修复", "高清",
        "最终版", "导演剪辑版", "加长版", "未删减", "剧场版", "蓝光原盘",
        "蓝光", "收藏",
    }
)

#: 全部噪声词
_NOISE_TOKENS: frozenset[str] = _QUALITY_TOKENS | _SOURCE_TOKENS | _VERSION_TOKENS | _CN_TOKENS

#: 标题分隔符（. - _ · 等，以及连续空格）
_SEP_RE = re.compile(r"[.\-_·•]+|\s+")

#: 季集号 S01E02 / s1.e2 —— 前面不能紧跟字母数字（避免吃掉 ``Disk2``）
_SE_RE = re.compile(r"(?<![a-z0-9])s(\d{1,3})[\s._-]*e(\d{1,4})(?![0-9])", re.IGNORECASE)
#: 仅集号 E02（前面无 S）
_E_ONLY_RE = re.compile(r"(?<![a-z0-9])e(\d{1,4})(?![0-9])", re.IGNORECASE)
#: 仅季号 S01（后面不能跟 E）
_S_ONLY_RE = re.compile(r"(?<![a-z0-9])s(\d{1,3})(?![0-9e])", re.IGNORECASE)
#: Season 1
_SEASON_WORD_RE = re.compile(r"(?<![a-z0-9])season[\s._-]*(\d{1,3})(?![0-9])", re.IGNORECASE)
#: Episode 5（单词形式）
_EPISODE_WORD_RE = re.compile(r"(?<![a-z0-9])episode[\s._-]*(\d{1,4})(?![0-9])", re.IGNORECASE)
#: 第 3 季（阿拉伯数字或中文数字）
_CN_SEASON_RE = re.compile(r"第\s*([0-9零〇一二三四五六七八九十百两]+)\s*季")
#: 第 2 集 / 第 2 话（阿拉伯数字或中文数字）
_CN_EPISODE_RE = re.compile(r"第\s*([0-9零〇一二三四五六七八九十百两]+)\s*[集话]")

#: 括号块（方括号 / 圆括号 / 书名号）——年份已单独挖走，剩下的都是画质标记
_BRACKET_BLOCK_RE = re.compile(r"[\[\【（(][^\]\】）)]*[\]\】）)]")

#: 括号年份（优先）：``(2025)`` / ``[2025]``
_YEAR_BRACKET_RE = re.compile(r"[\(\[\【]\s*(19\d{2}|20\d{2})\s*[\)\]\】]")
#: 裸年份：前后不能紧跟字母数字
_YEAR_BARE_RE = re.compile(r"(?<![0-9a-z])(19\d{2}|20\d{2})(?![0-9a-z])", re.IGNORECASE)

#: 中文与英文之间不加空格的分隔判断
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

#: 方括号内容（资源站给的译名前缀 / 发布组标记）
_BRACKET_CONTENT_RE = re.compile(r"[\[【]([^\]】]+)[\]】]")


def _extract_alt_titles(text: str) -> tuple[str, ...]:
    """从方括号里提取**备用标题候选**。

    资源站命名的方括号前缀通常是**中文译名**（``[把黑夜点燃]``）——那是
    精确度最高的匹配键：TMDB 里该条目的 ``name`` 就是这个名字，搜索
    中文名能唯一命中，而英文名可能命中同名的另一部剧（实测
    ``Light the Dark`` 会命中《黑夜告白》而不是《把黑夜点燃》）。

    **只收含中文的方括号内容**：纯 ASCII 的（``[OurTV]`` / ``[Group]``）
    是发布组标记，不是标题。

    :param text: 文件名
    :return: 候选标题元组（去重、保序）
    """
    titles: list[str] = []
    for match in _BRACKET_CONTENT_RE.finditer(text):
        content = match.group(1).strip()
        if not content or not _CJK_RE.search(content):
            continue
        titles.append(content)
    return tuple(dict.fromkeys(titles))


#: 中文数字与阿拉伯数字
_CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """中文数字 → 整数（支持 1-99 与逐位写法）。

    ``二`` → 2、``十`` → 10、``十二`` → 12、``二零`` → 20。
    解析不了返回 None。

    不引入 ``cn2an`` 依赖——只覆盖季集号这种小数值，几十行足够。
    """
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    result = 0
    for char in text:
        digit = _CN_DIGITS.get(char)
        if digit is None:
            return None
        result = result * 10 + digit
    return result


def _join_tokens(tokens: list[str]) -> str:
    """拼接标题片段：中文之间不插空格，其余用空格分隔。

    ``["三体", "电视剧"]`` → ``三体电视剧``；``["The", "Matrix"]`` → ``The Matrix``。
    """
    result = ""
    for token in tokens:
        if not result:
            result = token
            continue
        if _CJK_RE.search(result[-1]) and _CJK_RE.match(token):
            result += token
        else:
            result += " " + token
    return result


def _is_noise(token: str) -> bool:
    """片段是否为画质 / 来源 / 版本 / 语种标记。"""
    return token.lower() in _NOISE_TOKENS


def _split_tokens(text: str) -> list[str]:
    """按分隔符切分并丢弃空片段。"""
    return [part for part in _SEP_RE.split(text) if part]


def _extract_season_episode(text: str) -> tuple[int | None, int | None]:
    """抽取季号与集号。

    优先级：``S01E02`` > ``第N季第M集`` > ``Season N`` + ``E02`` > 单独 ``S01``。

    :param text: 已去扩展名的文件名
    :return: ``(season, episode)``；解析不到为 None
    """
    season: int | None = None
    episode: int | None = None

    if match := _SE_RE.search(text):
        season = int(match.group(1))
        episode = int(match.group(2))
        return season, episode

    if match := _CN_SEASON_RE.search(text):
        season = _cn_to_int(match.group(1))
    if match := _CN_EPISODE_RE.search(text):
        episode = _cn_to_int(match.group(1))
    if season is not None or episode is not None:
        return season, episode

    if match := _SEASON_WORD_RE.search(text):
        season = int(match.group(1))

    if (match := _EPISODE_WORD_RE.search(text)) or (match := _E_ONLY_RE.search(text)):
        episode = int(match.group(1))
    elif match := _S_ONLY_RE.search(text):
        season = int(match.group(1))

    return season, episode


def _extract_year(text: str) -> tuple[int | None, str]:
    """抽取年份，并返回「挖掉该年份后的文本」。

    **只挖最后一个年份匹配**——``1917.2019.1080p`` 里 ``1917`` 是片名，
    挖掉 ``2019`` 后标题正好是 ``1917``。

    :param text: 已去扩展名的文件名
    :return: ``(year, 挖掉年份后的文本)``
    """
    bracket = list(_YEAR_BRACKET_RE.finditer(text))
    if bracket:
        match = bracket[-1]
        year = int(match.group(1))
        cleaned = text[: match.start()] + " " + text[match.end() :]
        return year, cleaned

    bare = list(_YEAR_BARE_RE.finditer(text))
    if bare:
        match = bare[-1]
        year = int(match.group(1))
        cleaned = text[: match.start()] + " " + text[match.end() :]
        return year, cleaned

    return None, text


def _build_title(text: str, year_matched: bool) -> str:
    """从（已挖掉季集 / 年份的）文本里取标题。

    策略：取**开头连续的非噪声片段**——噪声片段之后的内容通常是画质、
    发行组等无关信息，保留会把标题弄脏。

    :param text: 已挖掉季集与年份的文本
    :param year_matched: 是否识别出年份（影响是否需要跳过「年份之前」的限制）
    :return: 标题；解析不出返回空串
    """
    tokens = _split_tokens(text)
    if not tokens:
        return ""

    leading: list[str] = []
    for token in tokens:
        if _is_noise(token):
            break
        leading.append(token)
    if leading:
        return _join_tokens(leading)

    # 前缀全是噪声（如 ``1080p`` 单独）→ 不给标题
    return ""


def parse_meta(name: str) -> ParsedMeta | None:
    """解析文件名。

    :param name: 文件名或路径（扩展名可有可无）
    :return: 解析结果；标题为空（无法识别）时返回 None
    """
    if not name:
        return None
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = stem.strip()
    if not stem:
        return None

    season, episode = _extract_season_episode(stem)

    # 挖掉季集片段（标题里不该含集号）
    without_ep = _SE_RE.sub(" ", stem)
    without_ep = _E_ONLY_RE.sub(" ", without_ep)
    without_ep = _S_ONLY_RE.sub(" ", without_ep)
    without_ep = _SEASON_WORD_RE.sub(" ", without_ep)
    without_ep = _EPISODE_WORD_RE.sub(" ", without_ep)
    without_ep = _CN_SEASON_RE.sub(" ", without_ep)
    without_ep = _CN_EPISODE_RE.sub(" ", without_ep)

    year, without_year = _extract_year(without_ep)

    # 剥掉剩余括号块（``[1080p] [BluRay]`` / ``（电视剧）``）——年份已单独挖走，
    # 剩下的括号内容都是画质或说明性标记，不属于片名。
    without_brackets = _BRACKET_BLOCK_RE.sub(" ", without_year)

    title = _build_title(without_brackets, year is not None)
    if not title:
        # 回退：年份可能是片名的一部分（``1917.1080p``）→ 用未挖年份的文本重取
        title = _build_title(_BRACKET_BLOCK_RE.sub(" ", without_ep), False)

    if not title:
        return None

    return ParsedMeta(
        title=title,
        year=year,
        season=season,
        episode=episode,
        is_episode=episode is not None,
        alt_titles=_extract_alt_titles(stem),
    )


def parse_meta_from_path(path: str) -> ParsedMeta | None:
    """解析路径（取文件名部分）。

    :param path: 文件或目录路径
    :return: 解析结果；无法识别时返回 None
    """
    return parse_meta(path)


def season_number_from_dir(name: str) -> int | None:
    """从**目录名**里判定季号（不要求标题也能解析）。

    专为「这个目录是季目录还是剧集根目录」的判定服务：``Season 01`` /
    ``S02`` / ``第3季`` / ``第二季`` → 季号；剧名目录（``凡人修仙传 (2020)``）
    → None。

    **为什么不用 :func:`parse_meta`**：``Season 01`` 是纯季标记，没有
    标题——:func:`parse_meta` 会因「标题为空」返回 None，调用方就无法
    区分「这不是季目录」和「是季目录但没标题」。两者语义不同，必须分开。

    :param name: 目录名（不含路径）
    :return: 季号；不是季目录返回 None
    """
    if not name:
        return None
    season, _episode = _extract_season_episode(name.strip())
    return season
