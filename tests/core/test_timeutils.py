"""时间与时钟偏差测试（FR-3.0.2）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core.timeutils import (
    ClockOffset,
    parse_duration_seconds,
    remaining_until,
    to_utc,
    utcnow,
)


def test_utcnow_is_naive_utc() -> None:
    """内部统一 naive UTC，避免隐式时区陷阱。"""
    assert utcnow().tzinfo is None


def test_to_utc_converts_aware() -> None:
    aware = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
    assert to_utc(aware) == aware.replace(tzinfo=None)


def test_to_utc_passes_through_naive() -> None:
    naive = datetime(2026, 10, 8, 12, 0)
    assert to_utc(naive) == naive


def test_clock_offset_from_probe() -> None:
    """站点快 5 分钟 → offset = +5min。"""
    local = datetime(2026, 10, 8, 12, 0)
    site = local + timedelta(minutes=5)
    offset = ClockOffset.from_probe(site, local)
    assert offset.seconds == 300


def test_remaining_conversion_subtracts_offset() -> None:
    """free_end_at = now + 声明剩余 - clock_offset。"""
    local = datetime(2026, 10, 8, 12, 0)
    offset = ClockOffset(offset=timedelta(minutes=5))
    end = offset.remaining_to_absolute(timedelta(hours=54), local_now=local)
    # 54h - 5min
    assert end == local + timedelta(hours=54) - timedelta(minutes=5)


def test_zero_offset_site() -> None:
    local = datetime(2026, 10, 8, 12, 0)
    end = ClockOffset.zero().remaining_to_absolute(timedelta(hours=6), local_now=local)
    assert end == local + timedelta(hours=6)


def test_manual_adjust_accumulates() -> None:
    off = ClockOffset(offset=timedelta(minutes=5)).manual_adjust(timedelta(minutes=-2))
    assert off.seconds == 180  # 5min - 2min


def test_remaining_until_none_for_permanent() -> None:
    assert remaining_until(None) is None


def test_remaining_until_negative_when_expired() -> None:
    assert remaining_until(utcnow() - timedelta(minutes=5)) < timedelta(0)


@pytest.mark.parametrize(
    ("text", "expected_seconds"),
    [
        ("72h", 72 * 3600),
        ("30m", 1800),
        ("2d", 2 * 86400),
        ("1w", 604800),
        ("45s", 45),
        ("1h30m", 5400),
        (3600, 3600),
        ("3600", 3600),
    ],
)
def test_parse_duration_seconds(text: str | int, expected_seconds: int) -> None:
    assert parse_duration_seconds(text) == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize("bad", ["abc", "h", "30x"])
def test_parse_duration_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="时长格式非法"):
        parse_duration_seconds(bad)


def test_parse_duration_empty_is_none() -> None:
    """空串视为"未配置"，返回 None 而不是抛错。"""
    assert parse_duration_seconds("") is None
    assert parse_duration_seconds("   ") is None


def test_parse_duration_none_passthrough() -> None:
    assert parse_duration_seconds(None) is None