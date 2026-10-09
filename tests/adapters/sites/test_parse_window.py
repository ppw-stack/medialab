"""``MteamAdapter.parse_window`` 单元测试（FR-1.5.3 / AC-16）。

测试 MteamAdapter 的两条解析路径：
- str 路径（HTML 详情页或 RSS description）："剩余 X 天" 文案兜底
- dict 路径（m-team search API status 块）：结构化字段精确识别

dict 路径的覆盖已迁到 test_mteam.py（用真实 API 响应快照），
本文件只保留 str 路径 + 契约保护测试。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.mteam import MteamAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType


@pytest.fixture
def adapter() -> MteamAdapter:
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    return MteamAdapter(profile, clock_offset=ClockOffset.zero())


# ---- str 路径（文案兜底） ----


def test_permanent_marker_overrides_remaining_text(adapter: MteamAdapter) -> None:
    """永久免费标记 > 任何剩余文案。"""
    win = _parse(adapter, "剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.LIST_TEXT
    assert win.confidence == 0.9


def test_remaining_chinese(adapter: MteamAdapter) -> None:
    win = _parse(adapter, "剩余 2 天 6 小时")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


def test_remaining_english(adapter: MteamAdapter) -> None:
    win = _parse(adapter, "left 3 days 5 hours")
    assert win.free_type == FreeType.TIMED


def test_remaining_short_duration(adapter: MteamAdapter) -> None:
    win = _parse(adapter, "剩余 30 分钟")
    assert win.free_type == FreeType.TIMED


def test_negative_remaining_returns_unknown(adapter: MteamAdapter) -> None:
    win = _parse(adapter, "已结束 1 天")
    assert win.free_type == FreeType.UNKNOWN
    assert win.confidence == 0.0


def test_empty_text_returns_unknown(adapter: MteamAdapter) -> None:
    win = _parse(adapter, "")
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 契约保护：FreeWindow 构造期校验 ----


def test_free_window_rejects_permanent_with_deadline(adapter: MteamAdapter) -> None:
    """契约：永久免费不允许带结束时刻。"""
    from app.core.windows import FreeWindow
    with pytest.raises(ValueError, match="非限时"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2030, 1, 1),
            free_source=FreeSource.API,
            confidence=1.0,
        )


def _parse(adapter: MteamAdapter, text: str):
    """同步包装异步 parse_window。"""
    return asyncio.run(adapter.parse_window(text))
