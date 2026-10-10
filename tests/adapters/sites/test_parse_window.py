"""``SiteAdapter.parse_window`` 单元测试（FR-1.5.3 / AC-16）。

测试四个适配器的 str 路径（HTML 详情页或 RSS description）："剩余 X 天" 文案兜底。

- MteamAdapter（m-team JSON API 站点）
- HhanclubAdapter（NexusPHP cookie 鉴权站点）
- HdskyAdapter（NexusPHP v1.5 cookie 鉴权站点）
- OurbitsAdapter（NexusPHP 1.10.x cookie 鉴权站点）

str 路径逻辑在四个适配器间共享相同正则（简体/繁体/英文），确保新适配器不破坏现有契约。

dict 路径的覆盖已迁到各适配器的专属测试文件（test_mteam.py / test_hhanclub.py /
test_hdsky.py / test_ourbits.py），本文件只保留 str 路径 + 契约保护测试。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.hdsky import HdskyAdapter
from app.adapters.sites.hhanclub import HhanclubAdapter
from app.adapters.sites.mteam import MteamAdapter
from app.adapters.sites.ourbits import OurbitsAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType


@pytest.fixture
def mteam_adapter() -> MteamAdapter:
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    return MteamAdapter(profile, clock_offset=ClockOffset.zero())


@pytest.fixture
def hhanclub_adapter() -> HhanclubAdapter:
    profile = SiteProfile(name="hhanclub", domain="hhanclub.net")
    return HhanclubAdapter(profile, clock_offset=ClockOffset.zero())


@pytest.fixture
def hdsky_adapter() -> HdskyAdapter:
    profile = SiteProfile(name="hdsky", domain="hdsky.me")
    return HdskyAdapter(profile, clock_offset=ClockOffset.zero())


@pytest.fixture
def ourbits_adapter() -> OurbitsAdapter:
    profile = SiteProfile(name="ourbits", domain="ourbits.club")
    return OurbitsAdapter(profile, clock_offset=ClockOffset.zero())


#: 所有需要测试 str 路径的适配器（参数化）
ALL_ADAPTERS = ["mteam_adapter", "hhanclub_adapter", "hdsky_adapter", "ourbits_adapter"]


def _parse(adapter, text: str):
    """同步包装异步 parse_window。"""
    return asyncio.run(adapter.parse_window(text))


# ---- str 路径（文案兜底）---- 参数化测试两个适配器


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_permanent_marker_overrides_remaining_text(request, adapter_name: str) -> None:
    """永久免费标记 > 任何剩余文案。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.LIST_TEXT
    assert win.confidence == 0.9


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_remaining_chinese(request, adapter_name: str) -> None:
    """简体剩余文案。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "剩余 2 天 6 小时")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_remaining_traditional_chinese(request, adapter_name: str) -> None:
    """繁体剩余文案（小時）。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "剩餘 2 天 6 小時")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_remaining_english(request, adapter_name: str) -> None:
    """英文剩余文案。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "left 3 days 5 hours")
    assert win.free_type == FreeType.TIMED


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_remaining_short_duration(request, adapter_name: str) -> None:
    """短窗口剩余文案。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "剩余 30 分钟")
    assert win.free_type == FreeType.TIMED


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_negative_remaining_returns_unknown(request, adapter_name: str) -> None:
    """已结束文案 → UNKNOWN。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "已结束 1 天")
    assert win.free_type == FreeType.UNKNOWN
    assert win.confidence == 0.0


@pytest.mark.parametrize("adapter_name", ALL_ADAPTERS)
def test_empty_text_returns_unknown(request, adapter_name: str) -> None:
    """空文本 → UNKNOWN。"""
    adapter = request.getfixturevalue(adapter_name)
    win = _parse(adapter, "")
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 契约保护：FreeWindow 构造期校验 ----


def test_free_window_rejects_permanent_with_deadline(mteam_adapter: MteamAdapter) -> None:
    """契约：永久免费不允许带结束时刻。"""
    from app.core.windows import FreeWindow
    with pytest.raises(ValueError, match="非限时"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2030, 1, 1),
            free_source=FreeSource.API,
            confidence=1.0,
        )
