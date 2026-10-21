"""免费窗口解析测试（Phase 0）。

窗口判定是全系统时效性核心（FR-3.0 / FR-3.4）——限时就推、到点必停、
永久免费绝不挂看护。改动这里的判定逻辑会直接影响「到点是否停种」，
故把 5 套解析策略的判定边界全部钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.adapters.sites.base import FeedItem
from app.domain.enums import FreeType
from app.services.window_parser import WindowParser, is_refund_marker

# ------------------------------------------------------------------ str 路径

def test_str_path_timed_remaining_text() -> None:
    item = FeedItem(title="t", description="剩余 2 天 6 小时")
    w = WindowParser.parse(item)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at is not None
    assert w.confidence == 0.8


def test_str_path_permanent_marker() -> None:
    item = FeedItem(title="t", description="永久免费种")
    w = WindowParser.parse(item)
    assert w.free_type == FreeType.PERMANENT
    assert w.free_end_at is None


def test_str_path_unknown() -> None:
    item = FeedItem(title="t", description="普通种子")
    w = WindowParser.parse(item)
    assert w.free_type == FreeType.UNKNOWN
    assert w.free_end_at is None
    assert w.confidence == 0.0


def test_str_path_empty_description() -> None:
    w = WindowParser.parse(FeedItem(title="t"))
    assert w.free_type == FreeType.UNKNOWN


# ----------------------------------------------------------------- dict 路径

def test_dict_path_mteam_discount_free_no_end_is_permanent() -> None:
    payload = {"status": {"discount": "FREE"}}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.PERMANENT
    assert w.free_end_at is None
    assert w.discount_pct == 100


def test_dict_path_mteam_discount_free_with_end_is_timed() -> None:
    """有结束时间的 FREE 必须判 TIMED，不能当永久免费。"""
    payload = {
        "status": {
            "discount": "FREE",
            "discountEndTime": "2026-10-25 12:00:00",
        }
    }
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at == "2026-10-25 12:00:00"


def test_dict_path_mteam_percent_30_is_neutral() -> None:
    payload = {"status": {"discount": "PERCENT_30"}}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.NEUTRAL
    assert w.discount_pct == 30
    assert w.free_end_at is None


def test_dict_path_mteam_percent_100_is_permanent() -> None:
    payload = {"status": {"discount": "PERCENT_100"}}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.PERMANENT
    assert w.discount_pct == 100


def test_dict_path_mteam_promotion_rule_discount_zero_is_not_free() -> None:
    """discount=0（0% off）不是免费——曾被误判成「全站免费」。"""
    payload = {
        "status": {
            "promotionRule": {"discount": 0, "endTime": "2026-10-25 12:00:00"},
        }
    }
    w = WindowParser.parse_payload(payload)
    assert w.free_type != FreeType.PERMANENT


def test_dict_path_mteam_promotion_free_with_end_is_timed() -> None:
    payload = {
        "status": {
            "promotionRule": {"discount": "FREE", "endTime": "2026-10-25 12:00:00"},
        }
    }
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at == "2026-10-25 12:00:00"


def test_dict_path_mteam_mall_single_free() -> None:
    payload = {
        "status": {
            "mallSingleFree": {
                "status": "ONGOING",
                "endDate": "2026-10-25 12:00:00",
            }
        }
    }
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at == "2026-10-25 12:00:00"


def test_dict_path_mteam_iso_datetime_with_tz() -> None:
    """带时区的 ISO 时间 → 转北京时间字符串。"""
    payload = {
        "status": {
            "discount": "FREE",
            "discountEndTime": "2026-10-25T04:00:00Z",
        }
    }
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at == "2026-10-25 12:00:00"


def test_dict_path_mteam_unix_seconds() -> None:
    payload = {
        "status": {
            "discount": "FREE",
            "discountEndTime": int(
                datetime(2026, 10, 25, 4, 0, 0, tzinfo=UTC).timestamp()
            ),
        }
    }
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at == "2026-10-25 12:00:00"


# --------------------------------------------------------------- NexusPHP 路径

def test_nexusphp_free_class_with_remaining_text_is_timed() -> None:
    payload = {"free_classes": ["free"], "remaining_text": "剩余 3 天"}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at is not None


def test_nexusphp_free_class_without_remaining_is_permanent() -> None:
    payload = {"free_classes": ["free"]}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.PERMANENT
    assert w.free_end_at is None


def test_nexusphp_halfdown_is_neutral() -> None:
    payload = {"free_classes": ["halfdown"]}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.NEUTRAL
    assert w.discount_pct == 50
    assert w.free_end_at is None


def test_nexusphp_thirtypercent_is_neutral() -> None:
    payload = {"free_classes": ["thirtypercent"]}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.NEUTRAL
    assert w.discount_pct == 30


def test_nexusphp_promo_text_overrides_class_pct() -> None:
    """促销标签可见文本比 class 更精确，应覆盖 class 推断的折扣。"""
    payload = {"free_classes": ["halfdown"], "promo_texts": ["30%"]}
    w = WindowParser.parse_payload(payload)
    assert w.discount_pct == 30


def test_nexusphp_permanent_marker_wins() -> None:
    payload = {"free_classes": ["free"], "remaining_text": "permanent"}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.PERMANENT


def test_nexusphp_refund_class_takes_priority() -> None:
    """退还种优先于普通免费标记。"""
    payload = {"free_classes": ["free", "refund"], "remaining_text": "1d 退还"}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.REFUND


def test_nexusphp_refund_via_remaining_text() -> None:
    payload = {"free_classes": ["free"], "remaining_text": "退还"}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.REFUND


def test_nexusphp_remaining_abs_absolute_time() -> None:
    payload = {"free_classes": ["free"], "remaining_abs": "2026-10-25 12:00:00"}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.TIMED
    assert w.free_end_at == "2026-10-25 12:00:00"


def test_is_refund_marker() -> None:
    assert is_refund_marker("1d 退还") is True
    assert is_refund_marker("refund") is True
    assert is_refund_marker("返还") is True
    assert is_refund_marker("普通种子") is False
    assert is_refund_marker(None) is False
    assert is_refund_marker("") is False


# ------------------------------------------------------------------- R10 契约

def test_permanent_window_must_not_have_end_at() -> None:
    """R10：永久免费带结束时刻是契约违规，必须抛错。"""
    from app.core.windows import FreeWindow

    with pytest.raises(ValueError, match="free_end_at 必须为 None"):
        FreeWindow(free_type=FreeType.PERMANENT, free_end_at="2026-10-25 12:00:00")


def test_neutral_window_must_not_have_end_at() -> None:
    """NEUTRAL（中性下载）无时间窗口，带 end_at 同样违规。"""
    from app.core.windows import FreeWindow

    with pytest.raises(ValueError, match="free_end_at 必须为 None"):
        FreeWindow(free_type=FreeType.NEUTRAL, free_end_at="2026-10-25 12:00:00")


def test_nexusphp_neutral_ignores_remaining_abs() -> None:
    """NEUTRAL 必须忽略 remaining_abs（中性下载不限时）。"""
    payload = {"free_classes": ["halfdown"], "remaining_abs": "2026-10-25 12:00:00"}
    w = WindowParser.parse_payload(payload)
    assert w.free_type == FreeType.NEUTRAL
    assert w.free_end_at is None


def test_window_is_permanent_property() -> None:
    """``is_permanent`` 只对永久免费为 True。

    NEUTRAL（半价/三折）**不是**永久免费——monitor 的现行行为是
    「当前不是免费种 → 立即暂停」，NEUTRAL 走这条路径。
    「不参与超时中止」≠「永不暂停」，两者曾被混进同一个集合。
    """
    from app.core.windows import FreeWindow

    assert FreeWindow(free_type=FreeType.PERMANENT).is_permanent is True
    assert FreeWindow(free_type=FreeType.NEUTRAL).is_permanent is False
    assert FreeWindow(free_type=FreeType.TIMED).is_permanent is False
    assert FreeWindow(free_type=FreeType.UNKNOWN).is_permanent is False


def test_non_abortable_free_types_only_permanent() -> None:
    """不参与超时中止的类型集合只含 PERMANENT。"""
    from app.domain.enums import NON_ABORTABLE_FREE_TYPES

    assert frozenset({FreeType.PERMANENT}) == NON_ABORTABLE_FREE_TYPES
    assert FreeType.NEUTRAL not in NON_ABORTABLE_FREE_TYPES


def test_window_is_low_confidence() -> None:
    from app.core.windows import FreeWindow

    assert FreeWindow(confidence=0.3).is_low_confidence is True
    assert FreeWindow(confidence=0.9).is_low_confidence is False
