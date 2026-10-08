"""窗口服务测试（FR-3.0.1 / FR-3.0.2 / FR-3.2 / FR-3.4.3）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.timeutils import utcnow
from app.core.windows import FreeWindow, WindowPolicy, classify_completion
from app.db.models import CooldownReason, FreeType, TorrentStatus
from app.db.session import session_scope
from app.services.window_service import (
    DeadlineCalculator,
    SiteWindowService,
    enqueue_abort,
    is_permanent_free,
    is_window_anchored,
    next_inspect_in,
    urgency_for_remaining,
)


def svc_with(policy: WindowPolicy) -> SiteWindowService:
    """无 session 构造 SiteWindowService（仅测纯逻辑方法）。"""
    s = SiteWindowService.__new__(SiteWindowService)
    s._policy = policy
    return s


# ------------------------------------------------------------ 永久免费
def test_permanent_free_recognized() -> None:
    assert is_permanent_free(FreeType.PERMANENT) is True
    assert is_permanent_free(FreeType.NEUTRAL) is True
    assert is_permanent_free(FreeType.TIMED) is False
    assert is_permanent_free(FreeType.TWOU_TIMED) is False
    assert is_permanent_free(None) is False


def test_free_window_factory_disallows_permanent_with_end() -> None:
    """构造器铁律：永久免费不能带 end_at。"""
    with pytest.raises(ValueError, match="free_end_at"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=utcnow() + timedelta(hours=5),
            free_source="api",
        )


# ------------------------------------------------------------ safety_gap
def test_safety_gap_uses_max() -> None:
    p = WindowPolicy(min_buffer=1800, buffer_ratio=0.10)
    assert p.safety_gap(timedelta(hours=3)) == timedelta(minutes=30)
    assert p.safety_gap(timedelta(hours=24)) == timedelta(minutes=144)


# ------------------------------------------------------------ FR-3.0.1 截止时间
def test_deadline_abort_equals_end() -> None:
    """FR-3.4：abort_at 恒等于 free_end_at（无豁免）。"""
    end = utcnow() + timedelta(hours=6)
    calc = DeadlineCalculator(WindowPolicy())
    fe, _dl, ab = calc.compute(end)
    assert fe == end
    assert ab == end


def test_deadline_rejects_none() -> None:
    with pytest.raises(ValueError, match="free_end_at"):
        DeadlineCalculator(WindowPolicy()).compute(None)  # type: ignore[arg-type]


# ------------------------------------------------------------ FR-3.2 跳过阈值
def test_skip_short_window_under_90_min() -> None:
    p = WindowPolicy(min_buffer=1800)
    short_end = utcnow() + timedelta(minutes=45)
    skip, reason = svc_with(p).should_skip_short_candidate(
        FreeType.TIMED, short_end
    )
    assert skip is True
    assert reason == "remaining_window_too_short"


def test_skip_expired_window() -> None:
    p = WindowPolicy()
    past = utcnow() - timedelta(minutes=1)
    skip, reason = svc_with(p).should_skip_short_candidate(FreeType.TIMED, past)
    assert skip is True
    assert reason == "window_expired"


def test_skip_allows_long_window() -> None:
    p = WindowPolicy()
    long_end = utcnow() + timedelta(hours=5)
    skip, _ = svc_with(p).should_skip_short_candidate(FreeType.TIMED, long_end)
    assert skip is False


def test_skip_does_not_filter_permanent() -> None:
    p = WindowPolicy()
    short_end = utcnow() + timedelta(minutes=10)
    skip, _ = svc_with(p).should_skip_short_candidate(
        FreeType.PERMANENT, short_end
    )
    assert skip is False


def test_skip_respects_grab_short_window_opt_in() -> None:
    p = WindowPolicy(min_buffer=1800, grab_short_window=True)
    short_end = utcnow() + timedelta(minutes=30)
    skip, _ = svc_with(p).should_skip_short_candidate(
        FreeType.TIMED, short_end
    )
    assert skip is False


# ------------------------------------------------------------ FR-3.4.3 收益四分类
def test_completion_inside_window() -> None:
    end = utcnow() + timedelta(hours=2)
    status, slack = classify_completion(utcnow(), end)
    assert status == TorrentStatus.FREE_COMPLETED
    assert slack and slack > 0


def test_completion_late_within_tolerance() -> None:
    end = utcnow() + timedelta(hours=1)
    late = end + timedelta(seconds=180)  # 1.5x tolerance
    status, _ = classify_completion(late, end, tolerance_seconds=120)
    assert status == TorrentStatus.FREE_COMPLETED_LATE


def test_completion_way_after_is_missed() -> None:
    end = utcnow() + timedelta(minutes=1)
    far_late = end + timedelta(hours=2)
    status, _ = classify_completion(far_late, end, tolerance_seconds=120)
    assert status == TorrentStatus.FREE_MISSED


# ------------------------------------------------------------ 紧迫度 → 巡检频率
@pytest.mark.parametrize(
    ("remaining", "expected_label", "expected_interval"),
    [
        (timedelta(hours=12), "notice", 900),
        (timedelta(hours=2), "approaching", 900),
        (timedelta(minutes=45), "urgent", 300),
        (timedelta(minutes=15), "critical", 60),
        (None, "permanent", 0),
    ],
)
def test_urgency_maps_to_inspect_interval(
    remaining: timedelta | None, expected_label: str, expected_interval: int
) -> None:
    assert urgency_for_remaining(remaining) == expected_label
    assert next_inspect_in(expected_label) == expected_interval


# ------------------------------------------------------------ 锚点检测
def test_is_window_anchored_true_and_false() -> None:
    class T:
        abort_at = utcnow() + timedelta(hours=5)
        deadline_at = utcnow() + timedelta(hours=4)

    class U:
        abort_at = None
        deadline_at = None

    assert is_window_anchored(T) is True  # type: ignore[arg-type]
    assert is_window_anchored(U) is False  # type: ignore[arg-type]


# ------------------------------------------------------------ 任务队列幂等
async def test_enqueue_abort_idempotent(db) -> None:
    from app.services.task_queue import TaskQueueService

    async with session_scope() as s:
        queue = TaskQueueService(s)
        first = await enqueue_abort(
            queue,
            site_id=1,
            info_hash="a" * 40,
            reason=CooldownReason.WINDOW_EXPIRED,
        )
        second = await enqueue_abort(
            queue,
            site_id=1,
            info_hash="a" * 40,
            reason=CooldownReason.WINDOW_EXPIRED,
        )
    assert first is True
    assert second is False


async def test_enqueue_abort_distinct_reasons(db) -> None:
    """同 hash 不同 reason 应各入一条（独立阶梯）。"""
    from app.services.task_queue import TaskQueueService

    async with session_scope() as s:
        queue = TaskQueueService(s)
        a = await enqueue_abort(
            queue,
            site_id=1,
            info_hash="a" * 40,
            reason=CooldownReason.WINDOW_EXPIRED,
        )
        b = await enqueue_abort(
            queue, site_id=1, info_hash="a" * 40, reason="tracker_fail"
        )
    assert a is True
    assert b is True
