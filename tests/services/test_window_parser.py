"""WindowParser 业务层测试。

parse_window 已从 SiteAdapter 契约中移除，由业务层 WindowParser 接管。
覆盖 dict 路径（m-team API status / NexusPHP free_payload）和 str 路径（文案兜底）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from app.adapters.sites.base import FeedItem
from app.core.windows import FreeWindow
from app.db.models import FreeSource, FreeType
from app.services.window_parser import WindowParser


def _parse_str(text: str) -> FreeWindow:
    return WindowParser.parse(FeedItem(title="[t]", link="", description=text))


def _parse_payload(payload: dict) -> FreeWindow:
    return WindowParser.parse(FeedItem(title="[t]", link="", free_payload=payload))


# ---- str 路径：文案兜底 ----


def test_str_permanent_marker() -> None:
    win = _parse_str("剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 0.9


def test_str_remaining_chinese() -> None:
    win = _parse_str("剩余 2 天 6 小时")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


def test_str_remaining_traditional() -> None:
    win = _parse_str("剩餘 2 天 6 小時")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


def test_str_remaining_english() -> None:
    win = _parse_str("left 3 days 5 hours")
    assert win.free_type == FreeType.TIMED


def test_str_short_duration() -> None:
    win = _parse_str("剩余 30 分钟")
    assert win.free_type == FreeType.TIMED


def test_str_no_match_returns_unknown() -> None:
    win = _parse_str("普通种子，没有免费标记")
    assert win.free_type == FreeType.UNKNOWN
    assert win.confidence == 0.0


def test_str_empty_returns_unknown() -> None:
    win = _parse_str("")
    assert win.free_type == FreeType.UNKNOWN


# ---- dict 路径：m-team API status ----


def test_mteam_discount_free_permanent() -> None:
    win = _parse_payload({"status": {"discount": "FREE"}})
    assert win.free_type == FreeType.PERMANENT
    assert win.confidence == 1.0


def test_mteam_discount_100_permanent() -> None:
    win = _parse_payload({"status": {"discount": 100}})
    assert win.free_type == FreeType.PERMANENT


def test_mteam_discount_end_time_timed() -> None:
    win = _parse_payload({"status": {"discountEndTime": 1793000000}})
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95


def test_mteam_mall_single_free_timed() -> None:
    win = _parse_payload({
        "status": {
            "mallSingleFree": {"status": "ONGOING", "endDate": "2026-12-31 00:00:00"},
        },
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


def test_mteam_promotion_rule_free() -> None:
    win = _parse_payload({
        "status": {"promotionRule": {"discount": "FREE"}},
    })
    assert win.free_type == FreeType.PERMANENT


def test_mteam_percent_50_neutral() -> None:
    win = _parse_payload({"status": {"discount": "PERCENT_50"}})
    assert win.free_type == FreeType.NEUTRAL


def test_mteam_normal_unknown() -> None:
    win = _parse_payload({"status": {"discount": "NORMAL"}})
    assert win.free_type == FreeType.UNKNOWN


# ---- dict 路径：NexusPHP free_payload ----


def test_nexushp_free_with_remaining_timed() -> None:
    win = _parse_payload({
        "free_classes": ["free"],
        "remaining_text": "剩余 2天 6小时",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95


def test_nexushp_free_no_remaining_permanent() -> None:
    win = _parse_payload({"free_classes": ["free"]})
    assert win.free_type == FreeType.PERMANENT
    assert win.confidence == 0.85


def test_nexushp_permanent_marker() -> None:
    win = _parse_payload({
        "free_classes": ["free"],
        "remaining_text": "永久免费",
    })
    assert win.free_type == FreeType.PERMANENT
    assert win.confidence == 1.0


def test_nexushp_twoupfree_timed() -> None:
    win = _parse_payload({
        "free_classes": ["twoupfree"],
        "remaining_text": "剩余 1 天",
    })
    assert win.free_type == FreeType.TWOU_TIMED


def test_nexushp_halfdown_neutral() -> None:
    win = _parse_payload({"free_classes": ["halfdown"]})
    assert win.free_type == FreeType.NEUTRAL


def test_nexushp_remaining_abs_overrides_to_timed() -> None:
    win = _parse_payload({
        "free_classes": ["free"],
        "remaining_abs": "2026-12-31 23:59:00",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


def test_nexushp_no_markers_unknown() -> None:
    win = _parse_payload({})
    assert win.free_type == FreeType.UNKNOWN


# ---- 契约保护 ----


def test_free_window_rejects_permanent_with_deadline() -> None:
    with pytest.raises(ValueError, match="非限时"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2030, 1, 1),
            free_source=FreeSource.API,
            confidence=1.0,
        )


# ---- parse_payload 便捷入口 ----


def test_parse_payload_convenience() -> None:
    win = WindowParser.parse_payload({"status": {"discount": "FREE"}})
    assert win.free_type == FreeType.PERMANENT
