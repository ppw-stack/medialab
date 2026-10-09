"""免费窗口解析契约（FR-1.5.3）与截止时间计算（FR-3.0.1 / FR-3.4）。

这是全系统时效性核心，改动前请先读 FR-3.0 / FR-3.4。

三条不可协商的规则：

1. **适配层只解析、不判定**（FR-1.5.3）。:class:`SiteAdapter.parse_window` 返回
   :class:`FreeWindow`，是否中止由 :mod:`app.services.free_seed` 决定。
2. **``abort_at`` 是无例外绝对红线**（FR-3.4）。一旦算出，永不豁免。
3. **收益归属按 ``completed_at`` 而非巡检时刻**（FR-3.4.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from app.core.timeutils import ClockOffset, utcnow
from app.db.models import (
    NON_ABORTABLE_FREE_TYPES,
    AbortedReason,
    FreeSource,
    FreeType,
    TorrentStatus,
)

#: 置信度低于此值按 unknown 保守处理（FR-1.5.3）
LOW_CONFIDENCE_THRESHOLD: Final = 0.6
#: 可行性"临界"带宽：预计耗时在窗口 1.2 倍以内视为临界
CRITICAL_TOLERANCE: Final = 1.2


@dataclass(frozen=True, slots=True)
class FreeWindow:
    """站点适配层解析出的窗口信息（FR-1.5.3 契约）。"""

    free_type: str = FreeType.UNKNOWN
    free_end_at: datetime | None = None
    free_source: str = FreeSource.SITE_DEFAULT
    raw_text: str = ""
    confidence: float = 0.0

    @property
    def is_permanent(self) -> bool:
        """永久免费/中性种：不参与超时中止（FR-3.0 表、R10）。"""
        return self.free_type in NON_ABORTABLE_FREE_TYPES

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD

    @property
    def is_actionable(self) -> bool:
        """是否需要挂看护。永久免费与低置信度均不挂。"""
        return (
            self.free_end_at is not None
            and not self.is_permanent
            and not self.is_low_confidence
        )

    def __post_init__(self) -> None:
        # 永久免费不得带结束时刻，避免误入看护链路
        if self.is_permanent and self.free_end_at is not None:
            raise ValueError(
                f"free_type={self.free_type} 为非限时类型，free_end_at 必须为 None（R10）"
            )


@dataclass(frozen=True, slots=True)
class WindowDeadlines:
    """任务创建时一次算清的截止时间三元组（FR-3.0.1）。"""

    free_end_at: datetime
    deadline_at: datetime
    abort_at: datetime
    safety_gap: timedelta


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    """站点级窗口策略（FR-3.0.1 / FR-3.0.2 / FR-3.2.1）。"""

    min_buffer: int = 1800  # 30 分钟
    buffer_ratio: float = 0.10
    default_free_duration: int = 72 * 3600
    clock_offset: ClockOffset = field(default_factory=ClockOffset.zero)
    grab_short_window: bool = False

    def safety_gap(self, duration: timedelta) -> timedelta:
        """``max(窗口时长 × buffer_ratio, min_buffer)``。"""
        return max(
            timedelta(seconds=duration.total_seconds() * self.buffer_ratio),
            timedelta(seconds=self.min_buffer),
        )

    def deadlines_for(
        self, free_end_at: datetime, *, now: datetime | None = None
    ) -> WindowDeadlines:
        """核心公式。``abort_at`` 即 ``free_end_at``，无任何调整。"""
        ref = now or utcnow()
        duration = max(free_end_at - ref, timedelta(0))
        gap = self.safety_gap(duration)
        return WindowDeadlines(
            free_end_at=free_end_at,
            deadline_at=free_end_at - gap,
            abort_at=free_end_at,
            safety_gap=gap,
        )

    def skip_threshold(self) -> timedelta:
        """剩余窗口不足此值的候选直接跳过（FR-3.2）。"""
        return timedelta(seconds=self.min_buffer * 3)

    def should_skip_candidate(
        self, window: FreeWindow, *, now: datetime | None = None
    ) -> tuple[bool, str]:
        """候选阶段过滤（FR-3.2 + FR-3.0.1）。

        同一条件只有一种动作：跳过。不存在"仍抓取但高优先级看护"的分支。
        """
        ref = now or utcnow()
        if window.is_permanent:
            return False, ""
        if window.free_end_at is None:
            return False, ""  # 走 source 4/5 兜底后再判
        remaining = window.free_end_at - ref
        if remaining <= timedelta(0):
            return True, "window_expired"
        if remaining < self.skip_threshold():
            if not self.grab_short_window:
                return True, "remaining_window_too_short"
            # 显式开启后仍需确认体积可快速完成，否则无意义占用
            return False, ""
        return False, ""

    def deadline_from_remaining(
        self, remaining: timedelta, *, now: datetime | None = None
    ) -> datetime:
        """从"站点声明的剩余时长"求绝对结束时刻（含时钟校正，FR-3.0.2）。"""
        return self.clock_offset.remaining_to_absolute(remaining, local_now=now)


#: 完成状态集合：已完成的任务不再走中止判定（FR-3.4 表）
COMPLETED_STATUSES: Final = frozenset(
    {
        TorrentStatus.COMPLETED,
        TorrentStatus.FREE_COMPLETED,
        TorrentStatus.FREE_COMPLETED_LATE,
        TorrentStatus.FREE_MISSED,
    }
)


def classify_completion(
    completed_at: datetime,
    free_end_at: datetime,
    *,
    tolerance_seconds: int = 120,
    now: datetime | None = None,
) -> tuple[str, int | None]:
    """收益归属四分类（FR-3.4.3）。

    返回 ``(status, window_slack_seconds)``。判定基准是 ``completed_at``，
    不是巡检时刻 ``now``。

    ``tolerance_seconds`` 用于吸收巡检滞后造成的完成时刻误差。

    **重要**（FR-3.4.3 + FR-3.0.1）：``abort_at`` 恒等于 ``free_end_at``，
    因此"边界超期完成"这一档只可能由**巡检滞后**观测到——只有
    ``completed_at`` 落在 ``free_end_at`` 之后一个容差内才归入该档，更大的
    超期属于看护失效（``free_missed``），须触发 🔴 自检告警。
    """
    slack = (free_end_at - completed_at).total_seconds()
    tol = float(tolerance_seconds)
    if slack >= -tol:
        return TorrentStatus.FREE_COMPLETED, int(slack)
    if slack >= -2 * tol:
        return TorrentStatus.FREE_COMPLETED_LATE, int(slack)
    return TorrentStatus.FREE_MISSED, int(slack)


def urgency_level(
    remaining: timedelta | None, *, emergency_threshold: int = 1800
) -> str:
    """剩余时间紧迫度分级（FR-3.4.1），驱动配色与巡检频率。

    返回 ``normal`` / ``notice`` / ``approaching`` / ``urgent`` / ``critical``。
    永久免费（``remaining is None``）返回 ``permanent``。
    """
    if remaining is None:
        return "permanent"
    total = remaining.total_seconds()
    if total <= 0:
        return "expired"
    if total < emergency_threshold:
        return "critical"
    if total < 3600:
        return "urgent"
    if total < 6 * 3600:
        return "approaching"
    if total < 24 * 3600:
        return "notice"
    return "normal"


def inspect_interval_seconds(level: str) -> int:
    """紧迫度 → 巡检间隔（FR-3.4.1 行为列）。"""
    return {
        "critical": 60,
        "urgent": 300,
        "approaching": 900,
        "notice": 900,
        "normal": 900,
        "expired": 60,
        "permanent": 0,
    }.get(level, 900)


def feasibility(
    remaining_bytes: int, speed_bps: float, *, window: timedelta | None
) -> str:
    """完成可行性预测（FR-6.3 卡片字段）。

    返回 ``ok`` / ``critical`` / ``impossible``。
    """
    if window is None:
        return "ok"
    if remaining_bytes <= 0:
        return "ok"
    if speed_bps <= 0:
        return "impossible"
    needed = timedelta(seconds=remaining_bytes / speed_bps)
    if needed <= window:
        return "ok"
    if needed <= window * CRITICAL_TOLERANCE:
        return "critical"
    return "impossible"


def abort_reason_for_state(state: str) -> str:
    """下载器状态 → 中止原因（FR-3.4 表）。

    ``checking`` 与其他未完成状态统一为 ``window_expired``（红线无例外）。
    """
    if state in {"error", "missingFiles"}:
        return AbortedReason.ERROR
    if state == "pausedDL":
        return AbortedReason.PAUSED
    return AbortedReason.WINDOW_EXPIRED