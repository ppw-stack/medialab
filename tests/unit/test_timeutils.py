"""时间工具测试（Phase 0）。

全系统的内部时间表示统一为**北京时间字符串**（``yyyy-MM-dd HH:mm:ss``），
任何隐式时区转换都是事故来源，故这里把契约钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from app.core.timeutils import BJ_TZ, bj_now, bj_now_dt, format_bj, parse_bj, to_bj


def test_bj_now_format() -> None:
    now = bj_now()
    assert len(now) == 19
    assert now[4] == "-" and now[7] == "-" and now[10] == " "
    assert now[13] == ":" and now[16] == ":"
    # 能被 format 解析回 datetime
    assert parse_bj(now) is not None


def test_bj_now_dt_has_beijing_tz() -> None:
    dt = bj_now_dt()
    assert dt.tzinfo is not None
    assert dt.tzinfo == BJ_TZ
    assert dt.utcoffset() == timedelta(hours=8)


def test_parse_bj_valid() -> None:
    dt = parse_bj("2026-10-21 14:51:34")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 21
    assert dt.hour == 14
    assert dt.minute == 51
    assert dt.second == 34
    assert dt.tzinfo == BJ_TZ


def test_parse_bj_invalid_returns_none() -> None:
    assert parse_bj(None) is None
    assert parse_bj("") is None
    assert parse_bj("   ") is None
    assert parse_bj("不是时间") is None
    assert parse_bj("2026-13-45 99:99:99") is None
    # 缺秒
    assert parse_bj("2026-10-21 14:51") is None


def test_parse_bj_tolerates_whitespace() -> None:
    dt = parse_bj("  2026-10-21 14:51:34  ")
    assert dt is not None
    assert dt.hour == 14


def test_format_bj_to_string() -> None:
    aware = datetime(2026, 10, 21, 14, 51, 34, tzinfo=BJ_TZ)
    assert format_bj(aware) == "2026-10-21 14:51:34"


def test_format_bj_converts_utc_input() -> None:
    aware = datetime(2026, 10, 21, 6, 51, 34, tzinfo=UTC)
    # UTC 06:51:34 == 北京 14:51:34
    assert format_bj(aware) == "2026-10-21 14:51:34"


def test_to_bj_naive_treated_as_beijing() -> None:
    naive = datetime(2026, 10, 21, 14, 51, 34)
    assert to_bj(naive) == "2026-10-21 14:51:34"


def test_to_bj_aware_converted_by_real_tz() -> None:
    aware = datetime(2026, 10, 21, 6, 51, 34, tzinfo=UTC)
    assert to_bj(aware) == "2026-10-21 14:51:34"


def test_parse_format_roundtrip() -> None:
    original = "2026-10-21 14:51:34"
    dt = parse_bj(original)
    assert dt is not None
    assert format_bj(dt) == original


def test_bj_string_lexicographic_order_matches_chronology() -> None:
    """北京时间字符串的字典序与时序一致。

    ``media_files.mtime`` 等列从 unix 秒改为字符串后，增量扫描的比较
    从 int 大小比较变为字符串字典序比较。``%Y-%m-%d %H:%M:%S`` 格式
    保证字典序与时序一致——本测试守住这条不变量（格式漂移会立刻失败）。
    """
    # 跨年 / 跨月 / 跨日 / 跨时 / 同分不同秒
    samples = [
        "2025-12-31 23:59:58",
        "2025-12-31 23:59:59",
        "2026-01-01 00:00:00",
        "2026-10-30 09:00:00",
        "2026-10-30 18:03:33",
        "2026-10-30 18:03:34",
    ]
    sorted_lex = sorted(samples)
    sorted_chrono = sorted(samples, key=lambda s: parse_bj(s))  # type: ignore[arg-type]
    assert sorted_lex == sorted_chrono == samples
    # 字符串比较与 datetime 比较逐对一致
    for earlier, later in pairwise(samples):
        assert earlier < later
        assert parse_bj(earlier) < parse_bj(later)  # type: ignore[operator]
