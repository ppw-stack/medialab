"""窗口锚定核心逻辑测试（FR-3.0.1 / FR-3.4 / FR-3.4.3）。

覆盖：双阈值公式、红线无例外、收益归属四分类、紧迫度分级、可行性预测。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core.timeutils import utcnow
from app.core.windows import (
    FreeWindow,
    WindowPolicy,
    abort_reason_for_state,
    classify_completion,
    feasibility,
    inspect_interval_seconds,
    urgency_level,
)
from app.db.models import AbortedReason, FreeSource, FreeType, TorrentStatus


@pytest.fixture
def policy() -> WindowPolicy:
    return WindowPolicy(min_buffer=1800, buffer_ratio=0.10)


def test_safety_gap_uses_max_of_ratio_and_buffer(policy: WindowPolicy) -> None:
    """safety_gap = max(窗口 × 10%, 30min)。"""
    # 3 小时窗口 → 10% = 18min < 30min → 取下限
    assert policy.safety_gap(timedelta(hours=3)) == timedelta(minutes=30)
    # 24 小时窗口 → 10% = 144min > 30min → 取比例
    assert policy.safety_gap(timedelta(hours=24)) == timedelta(minutes=144)


def test_abort_at_equals_free_end_at(policy: WindowPolicy) -> None:
    """FR-3.4：abort_at 恒等于 free_end_at，不做任何调整。"""
    end = utcnow() + timedelta(hours=5)
    d = policy.deadlines_for(end)
    assert d.abort_at == end
    assert d.free_end_at == end
    assert d.deadline_at < d.abort_at


def test_long_window_is_not_compressed(policy: WindowPolicy) -> None:
    """FR-3.0.1「不设上限兜底」：30 天窗口不被压缩。"""
    end = utcnow() + timedelta(days=30)
    assert policy.deadlines_for(end).abort_at == end


def test_permanent_free_never_gets_deadlines(policy: WindowPolicy) -> None:
    w = FreeWindow(
        free_type=FreeType.PERMANENT,
        free_end_at=None,
        free_source=FreeSource.API,
        confidence=1.0,
    )
    assert w.is_permanent
    assert not w.is_actionable
    assert urgency_level(None) == "permanent"
    assert inspect_interval_seconds("permanent") == 0


def test_short_window_candidate_is_skipped(policy: WindowPolicy) -> None:
    """FR-3.2：剩余 < min_buffer×3（90min）直接跳过，且只有跳过一种动作。"""
    w = FreeWindow(
        free_type=FreeType.TIMED,
        free_end_at=utcnow() + timedelta(minutes=45),
        free_source=FreeSource.LIST_TEXT,
        confidence=0.9,
    )
    skip, reason = policy.should_skip_candidate(w)
    assert skip is True
    assert reason == "remaining_window_too_short"


def test_grab_short_window_opt_in_allows_capture() -> None:
    """显式开启 grab_short_window 后允许抓取。"""
    policy = WindowPolicy(grab_short_window=True)
    w = FreeWindow(
        free_type=FreeType.TIMED,
        free_end_at=utcnow() + timedelta(minutes=45),
        free_source=FreeSource.LIST_TEXT,
        confidence=0.9,
    )
    assert policy.should_skip_candidate(w)[0] is False


def test_expired_candidate_is_skipped(policy: WindowPolicy) -> None:
    w = FreeWindow(
        free_type=FreeType.TIMED,
        free_end_at=utcnow() - timedelta(minutes=1),
        free_source=FreeSource.LIST_TEXT,
        confidence=0.9,
    )
    assert policy.should_skip_candidate(w) == (True, "window_expired")


def test_long_window_candidate_not_skipped(policy: WindowPolicy) -> None:
    w = FreeWindow(
        free_type=FreeType.TIMED,
        free_end_at=utcnow() + timedelta(hours=48),
        free_source=FreeSource.API,
        confidence=0.95,
    )
    assert policy.should_skip_candidate(w) == (False, "")


# ------------------------------------------------------------ 收益归属 FR-3.4.3
def test_completion_inside_window() -> None:
    end = utcnow() + timedelta(hours=2)
    status, slack = classify_completion(utcnow(), end)
    assert status == TorrentStatus.FREE_COMPLETED
    assert slack is not None and slack > 0


def test_completion_late_but_within_tolerance() -> None:
    """完成时刻落在窗口结束后 1~2 倍容差内 → late，不计入成功率分子。"""
    end = utcnow() + timedelta(minutes=10)
    # 180s > tol(120s) 且 <= 2*tol
    status, slack = classify_completion(end + timedelta(seconds=180), end)
    assert status == TorrentStatus.FREE_COMPLETED_LATE
    assert slack is not None and slack < 0


def test_completion_after_abort_is_missed() -> None:
    """超出 2 倍容差的超期完成 = 看护失效（free_missed），须触发 🔴 自检告警。"""
    end = utcnow() + timedelta(minutes=10)
    status, _ = classify_completion(end + timedelta(hours=1), end)
    assert status == TorrentStatus.FREE_MISSED


def test_tolerance_absorbs_inspection_lag() -> None:
    """巡检滞后造成的微小超差不得误判为 late 或 missed。"""
    end = utcnow() + timedelta(minutes=10)
    status, _ = classify_completion(
        end + timedelta(seconds=90), end, tolerance_seconds=120
    )
    assert status == TorrentStatus.FREE_COMPLETED


def test_late_band_requires_exceeding_one_tolerance() -> None:
    """刚过容差一点点仍算窗口内完成（测量误差）。"""
    end = utcnow() + timedelta(minutes=10)
    status, _ = classify_completion(end + timedelta(seconds=121), end)
    assert status == TorrentStatus.FREE_COMPLETED_LATE


# ------------------------------------------------------------ 紧迫度 FR-3.4.1
@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(hours=30), "normal"),
        (timedelta(hours=12), "notice"),
        (timedelta(hours=3), "approaching"),
        (timedelta(minutes=45), "urgent"),
        (timedelta(minutes=10), "critical"),
        (timedelta(minutes=-1), "expired"),
    ],
)
def test_urgency_levels(remaining: timedelta, expected: str) -> None:
    assert urgency_level(remaining) == expected


def test_critical_urgency_increases_inspect_frequency() -> None:
    """FR-3.4.1：<30min 时巡检频率提升至 1 分钟。"""
    assert inspect_interval_seconds("critical") == 60
    assert inspect_interval_seconds("normal") == 900


# ------------------------------------------------------------ 可行性 FR-6.3
def test_feasibility_ok_when_window_sufficient() -> None:
    """1GB 剩余 @ 10MB/s ≈ 100s，窗口 2h → 可完成。"""
    assert feasibility(1024**3, 10 * 1024**2, window=timedelta(hours=2)) == "ok"


def test_feasibility_impossible_when_zero_speed() -> None:
    """速率为 0 且未完成 → 无法完成。"""
    assert feasibility(1024**3, 0, window=timedelta(hours=2)) == "impossible"


def test_feasibility_ok_when_already_complete() -> None:
    assert feasibility(0, 0, window=timedelta(hours=2)) == "ok"


def test_feasibility_ok_without_window() -> None:
    """永久免费无窗口概念，恒为可完成。"""
    assert feasibility(1024**3, 0, window=None) == "ok"


def test_feasibility_critical_near_boundary() -> None:
    """预计耗时落在窗口 1~1.2 倍 → 临界。"""
    need = 1100.0  # 秒
    window = timedelta(seconds=need / 1.1)
    assert feasibility(int(need * 1024**2), 1024**2, window=window) == "critical"


def test_feasibility_impossible_beyond_boundary() -> None:
    """预计耗时超过窗口 1.2 倍 → 无法完成。"""
    need = 5000.0
    window = timedelta(seconds=need / 3)
    assert feasibility(int(need * 1024**2), 1024**2, window=window) == "impossible"


# ------------------------------------------------------------ 中止原因
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("error", AbortedReason.ERROR),
        ("missingFiles", AbortedReason.ERROR),
        ("pausedDL", AbortedReason.PAUSED),
        # FR-3.4：checking 在 v1.2 已移除 10 分钟宽限，到红线即中止
        ("checking", AbortedReason.WINDOW_EXPIRED),
        ("downloading", AbortedReason.WINDOW_EXPIRED),
        ("stalledDL", AbortedReason.WINDOW_EXPIRED),
    ],
)
def test_abort_reason_by_state(state: str, expected: str) -> None:
    """红线无例外：任何未完成状态到点即中止。"""
    assert abort_reason_for_state(state) == expected


def test_low_confidence_window_never_actionable() -> None:
    w = FreeWindow(
        free_type=FreeType.TIMED,
        free_end_at=datetime(2026, 10, 10, 18, 0),
        free_source=FreeSource.SITE_DEFAULT,
        confidence=0.4,
    )
    assert w.is_low_confidence
    assert not w.is_actionable