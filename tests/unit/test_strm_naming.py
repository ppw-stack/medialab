"""strm 文件命名单元测试。

固化的契约：

- strm 文件名**去掉视频扩展名**——媒体服务器按「同目录同 stem」匹配 nfo
  （``x.mkv.strm`` 的 stem 是 ``x.mkv``，网盘侧 nfo 叫 ``x.nfo`` → 对不上）
- 只剥离**属于 ``video_exts`` 的**后缀（用户配什么剥什么）
- 目录与标题沿用网盘原样（不重排、不拼装）
"""

from __future__ import annotations

import pytest
from app.services.naming import strm_filename

#: 典型配置：只认 mkv / mp4
VIDEO_EXTS = frozenset({".mkv", ".mp4"})


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("movie.mkv", "movie.strm"),
        ("movie.mp4", "movie.strm"),
        ("a.b.mkv", "a.b.strm"),
        ("S01E01.mkv", "S01E01.strm"),
        (
            "唐探1900 (2025) - 2160p.WEB-DL.mkv",
            "唐探1900 (2025) - 2160p.WEB-DL.strm",
        ),
    ],
)
def test_video_extension_stripped(src: str, expected: str) -> None:
    """视频扩展名被剥掉，只留一个扩展名（``.strm``）。"""
    assert strm_filename(src, VIDEO_EXTS) == expected


def test_ext_not_in_video_exts_kept() -> None:
    """没配进 ``video_exts`` 的后缀不剥（不硬编码后缀表）。"""
    assert strm_filename("movie.avi", VIDEO_EXTS) == "movie.avi.strm"


def test_no_video_exts_keeps_full_name() -> None:
    """没传 ``video_exts`` → 保留完整原名（向后兼容旧调用方式）。"""
    assert strm_filename("movie.mkv") == "movie.mkv.strm"


def test_long_suffix_not_treated_as_extension() -> None:
    """超过 5 字符的「后缀」不算扩展名——``movie.2020.1080p`` 不该被剥。

    沿用 :func:`app.services.naming.split_name` 的既有语义
    （``len(suffix) <= 5`` 才算扩展名）。
    """
    assert (
        strm_filename("movie.2020.1080p", VIDEO_EXTS) == "movie.2020.1080p.strm"
    )


def test_empty_source_falls_back() -> None:
    """源名清洗后为空 → ``untitled.strm``。"""
    assert strm_filename("...", VIDEO_EXTS) == "untitled.strm"


def test_illegal_chars_still_sanitized() -> None:
    """清洗仍然生效（文件名里的非法字符照旧剔除）。"""
    assert strm_filename('a:b?.mkv', VIDEO_EXTS) == "ab.strm"
