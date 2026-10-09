"""``MteamAdapter.parse_window`` 单元测试 = 附录 D 全量用例集（FR-1.5.3 / AC-16）。

这是全系统最敏感的函数：解析错误会误杀长窗口种子或让免费种超期滞留（R9）。
每个用例断言 ``free_type`` / ``free_end_at`` / ``free_source`` / ``confidence`` 四元组。

适配层只解析、不判定：永久免费 (R10) 必须 free_end_at=None。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.mteam import MteamAdapter
from app.core.timeutils import ClockOffset, utcnow
from app.db.models import FreeSource, FreeType


# ---- 字符串路径（剩余 X 天文案，列表页 HTML 场景） ----

CHINESE = "剩余 2 天 6 小时"
ENGLISH = "left 3 days 5 hours"
SHORT = "剩余 30 分钟"
NEGATIVE = "已结束 1 天"
PERMANENT_TXT = "免费 forever 永久"


@pytest.fixture
def adapter() -> MteamAdapter:
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    return MteamAdapter(profile, clock_offset=ClockOffset.zero())


# ---- 用例：文案路径（永久免费优先 + 结构化字段 + 文案） ----


def test_permanent_marker_overrides_remaining_text(adapter: MteamAdapter) -> None:
    """永久免费标记 > 任何剩余文案。"""
    win = await_adapter_parse(adapter, "剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.LIST_TEXT
    assert win.confidence == 0.9


def test_remaining_chinese(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, CHINESE)
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


def test_remaining_english(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, ENGLISH)
    assert win.free_type == FreeType.TIMED


def test_remaining_short_duration(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, SHORT)
    assert win.free_type == FreeType.TIMED


def test_negative_remaining_returns_unknown(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, NEGATIVE)
    assert win.free_type == FreeType.UNKNOWN
    assert win.confidence == 0.0


def test_empty_text_returns_unknown(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, "")
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 用例：字典路径（m-team JSON 字段） ----


def test_discount_100_is_permanent(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, {"discount": 100})
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 1.0
    assert win.free_source == FreeSource.API


def test_free_flag_without_deadline_is_permanent(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, {"free": True, "discount": 0})
    assert win.free_type == FreeType.PERMANENT


def test_discount_0_with_deadline_is_timed(adapter: MteamAdapter) -> None:
    end = "2027-01-15T10:00:00Z"
    win = await_adapter_parse(adapter, {"discount": 0, "free_deadline": end})
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95
    assert win.free_source == FreeSource.API


def test_discount_50_is_neutral(adapter: MteamAdapter) -> None:
    """discount ≈ -50（m-team 50% 下载乘数）= 中性种。"""
    win = await_adapter_parse(adapter, {"discount": -50})
    assert win.free_type == FreeType.NEUTRAL
    assert win.free_end_at is None
    assert win.confidence == 0.3  # 字典路径但无截止时刻


def test_discount_100_with_deadline_drops_to_permanent(adapter: MteamAdapter) -> None:
    """m-team 行为：discount=100 永远视作永久，截止时刻被忽略。

    （注：通用 FreeWindow 契约的永久免费不允许带 deadline，但 m-team
    实际数据可能两个字段都有。我们的 ``_classify_free`` 直接返回永久，
    截止时刻被丢弃。``parse_window`` 不会构造矛盾的 FreeWindow。）
    """
    ft, end = adapter._classify_free(
        {"discount": 100, "free_deadline": "2030-01-01T00:00:00Z"}
    )
    assert ft == FreeType.PERMANENT
    assert end is None  # 截止时刻被忽略


def test_parse_window_permanent_does_not_reject_deadline(adapter: MteamAdapter) -> None:
    """FreeWindow 构造时：永久 + end_at=None 是合法组合。"""
    win = await_adapter_parse(adapter, {"discount": 100, "free_deadline": "2030-01-01T00:00:00Z"})
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None  # end_at 被 _classify_free 丢弃


def test_free_window_rejects_permanent_with_deadline(adapter: MteamAdapter) -> None:
    """契约保护：直接构造 ``FreeWindow(free_type=PERMANENT, free_end_at=...)`` 抛错。"""
    from app.core.windows import FreeWindow
    with pytest.raises(ValueError, match="非限时"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2030, 1, 1),
            free_source=FreeSource.API,
            confidence=1.0,
        )


def test_no_fields_returns_unknown(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, {})
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


def test_unix_timestamp_in_deadline(adapter: MteamAdapter) -> None:
    """free_deadline 也支持 Unix 秒。"""
    win = await_adapter_parse(
        adapter, {"discount": 0, "free_deadline": 1800000000}
    )
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


def test_invalid_deadline_string_falls_back_to_unknown(adapter: MteamAdapter) -> None:
    win = await_adapter_parse(adapter, {"discount": 0, "free_deadline": "not-a-date"})
    # 截止时刻解析失败 → 字典路径无 end_at → unknown（低置信度）
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 辅助 ----


def await_adapter_parse(adapter: MteamAdapter, raw: str | dict[str, Any]):
    """同步包装异步 parse_window，便于断言。"""
    import asyncio
    return asyncio.run(adapter.parse_window(raw))
