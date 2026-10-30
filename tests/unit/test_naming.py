"""文件名处理与后缀分类的契约测试。

**命名规则已移除**：``.strm`` 文件名直接沿用网盘原文件名
（``东极岛 (2025) - 1080p.mp4`` → ``东极岛 (2025) - 1080p.mp4.strm``）。

原文件名已含完整发行信息（标题 / 年份 / 画质 / 编码），Emby / Jellyfin
对标准命名解析最准。曾经自研命名规则时出过两个真实 bug：

1. 把片名里的 ``1900``（《唐探1900》）误当成发行年份
2. 画质为空时留下 ``霸王别姬 (1993) - .mkv`` 这样的残渣

故直接沿用原文件名，不做任何重写。
"""

from __future__ import annotations

import pytest
from app.services.naming import (
    classify_by_ext,
    is_hidden_backup,
    parse_ext_list,
    sanitize,
    split_name,
    strm_filename,
)

VIDEO = frozenset({".mkv", ".mp4"})
IMAGE = frozenset({".jpg", ".png"})
DATA = frozenset({".nfo", ".srt"})


class TestSplitName:
    def test_splits_extension(self) -> None:
        assert split_name("movie.mkv") == ("movie", "mkv")

    def test_no_extension(self) -> None:
        assert split_name("movie") == ("movie", "")

    def test_multiple_dots(self) -> None:
        """只拆最后一段。"""
        assert split_name("my.movie.2020.mkv") == ("my.movie.2020", "mkv")

    def test_long_suffix_not_treated_as_extension(self) -> None:
        stem, ext = split_name("file.verylongsuffix")
        assert ext == ""
        assert stem == "file.verylongsuffix"


class TestStrmFilename:
    def test_keeps_original_name(self) -> None:
        """**核心**：原文件名原样保留 + .strm。"""
        assert (
            strm_filename("东极岛 (2025) - 1080p.mp4")
            == "东极岛 (2025) - 1080p.mp4.strm"
        )

    def test_keeps_year_and_quality(self) -> None:
        """年份与画质原样保留——不解析、不重写。"""
        name = "唐探1900 (2025) - 2160p.WEB-DL.mkv"
        assert strm_filename(name) == f"{name}.strm"

    def test_no_extension_still_works(self) -> None:
        assert strm_filename("movie") == "movie.strm"

    def test_empty_name_falls_back(self) -> None:
        assert strm_filename("") == "untitled.strm"

    def test_unsafe_chars_removed(self) -> None:
        """非法字符仍要清洗——写盘前的基本校验。"""
        result = strm_filename('a<b>c:d"e|f?g*h.mkv')
        for ch in '<>:"|?*':
            assert ch not in result

    def test_path_separator_stripped(self) -> None:
        """``/`` 会被清掉——防路径穿越。"""
        result = strm_filename("../../etc/passwd.mkv")
        assert "/" not in result
        assert ".." not in result

    def test_trailing_dots_and_spaces_stripped(self) -> None:
        result = strm_filename("movie.mkv...  ")
        assert not result.startswith(" ")
        assert result.endswith(".strm")


class TestClassifyByExt:
    def test_video(self) -> None:
        assert classify_by_ext("a.mkv", VIDEO, IMAGE, DATA) == "video"

    def test_image(self) -> None:
        assert classify_by_ext("poster.jpg", VIDEO, IMAGE, DATA) == "image"

    def test_data(self) -> None:
        assert classify_by_ext("movie.nfo", VIDEO, IMAGE, DATA) == "data"

    def test_unknown(self) -> None:
        assert classify_by_ext("readme.txt", VIDEO, IMAGE, DATA) == "unknown"

    def test_no_extension_is_unknown(self) -> None:
        assert classify_by_ext("README", VIDEO, IMAGE, DATA) == "unknown"

    def test_case_insensitive(self) -> None:
        assert classify_by_ext("A.MKV", VIDEO, IMAGE, DATA) == "video"

    def test_video_takes_precedence(self) -> None:
        """同一后缀同时配到两个类别时，视频优先。"""
        assert classify_by_ext("a.mkv", VIDEO, frozenset({".mkv"}), DATA) == "video"

    def test_empty_video_exts_means_nothing_matches(self) -> None:
        assert classify_by_ext("a.mkv", frozenset(), IMAGE, DATA) == "unknown"

    def test_hidden_backup_is_unknown(self) -> None:
        """115 客户端的隐藏备份不算正片。

        后缀确实是 ``.mp4``，若按后缀分类会被当视频生成 strm——实测
        「没关系，是爱情啊」目录里有 16 个这类文件（28 GB）。
        """
        assert (
            classify_by_ext(".mp-backup-775a9488f5a690db5e11073e**..mp4", VIDEO, IMAGE, DATA)
            == "unknown"
        )

    def test_hidden_backup_only_when_both_conditions_met(self) -> None:
        """只在「以 . 开头」且「含备份特征」时才判为备份。"""
        # 不是隐藏文件（不以 . 开头）→ 正常按后缀分类
        assert classify_by_ext("mp-backup-abc..mp4", VIDEO, IMAGE, DATA) == "video"
        # 是隐藏文件但不是备份特征 → 正常按后缀分类
        assert classify_by_ext(".config.mp4", VIDEO, IMAGE, DATA) == "video"

    def test_is_hidden_backup(self) -> None:
        assert is_hidden_backup(".mp-backup-abc**..mp4")
        assert not is_hidden_backup("mp-backup-abc**..mp4")
        assert not is_hidden_backup(".hidden.mp4")
        assert not is_hidden_backup("normal.mkv")


class TestParseExtList:
    def test_comma_separated(self) -> None:
        assert parse_ext_list("mkv,mp4") == frozenset({".mkv", ".mp4"})

    def test_with_dots_and_spaces(self) -> None:
        assert parse_ext_list(" .mkv , .mp4 ") == frozenset({".mkv", ".mp4"})

    def test_empty(self) -> None:
        assert parse_ext_list("") == frozenset()
        assert parse_ext_list(None) == frozenset()

    def test_case_normalized(self) -> None:
        assert parse_ext_list("MKV,MP4") == frozenset({".mkv", ".mp4"})

    def test_ignores_empty_segments(self) -> None:
        assert parse_ext_list("mkv,,mp4,") == frozenset({".mkv", ".mp4"})


class TestSanitize:
    def test_keeps_normal_name(self) -> None:
        assert sanitize("Movie Name (2025)") == "Movie Name (2025)"

    def test_empty_returns_empty(self) -> None:
        assert sanitize("") == ""

    def test_removes_control_chars(self) -> None:
        assert sanitize("a\x00b") == "ab"

    def test_prefixes_windows_reserved_name(self) -> None:
        assert sanitize("CON") == "_CON"

    @pytest.mark.parametrize("char", ["<", ">", ":", '"', "|", "?", "*", "\\"])
    def test_removes_unsafe_chars(self, char: str) -> None:
        assert char not in sanitize(f"a{char}b")
