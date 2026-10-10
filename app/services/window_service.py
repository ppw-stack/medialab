"""免费种窗口服务（FR-3 全部主线）。

把 :mod:`app.core.windows` 的纯函数层扩展为**领域服务层**：

- :class:`DeadlineCalculator`：把 ``FreeWindow`` 落库为 ``Torrent`` 的窗口字段
- :class:`UrgencyClassifier`：把紧迫度映射到实际巡检频率（FR-3.4.1）
- :func:`classify_completion_at`：把 ``completed_at`` 归入四分类（FR-3.4.3）

与 :mod:`app.core.windows` 的边界：本模块依赖 :class:`~app.db.session.AsyncSession`
与 ORM 实体；纯函数层不依赖。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from app.core.timeutils import utcnow
from app.core.windows import (
    WindowPolicy,
    classify_completion,
    inspect_interval_seconds,
)
from app.core.windows import (
    urgency_level as _base_urgency,
)
from app.db.models import Site, Torrent
from app.services.task_queue import TaskKind, TaskQueueService, make_idem_key

#: 巡检间隔上下限（FR-3.4.1 行为列）
URGENT_INTERVAL_FLOOR: Final = 60
URGENT_INTERVAL_CEIL: Final = 900
#: 完成容差（FR-3.4.3 巡检滞后）
COMPLETION_TOLERANCE_SECONDS: Final = 120


def is_permanent_free(free_type: str | None) -> bool:
    """永久免费/中性种**不参与超时中止**（FR-3.0 表、R10）。"""
    return free_type in ("freeleech_permanent", "neutral_leech")


def urgency_for_remaining(remaining: timedelta | None) -> str:
    """紧迫度：包装核心函数以保持 API 入口稳定。"""
    return _base_urgency(remaining)


def next_inspect_in(level: str) -> int:
    """当前紧迫度下下次巡检前应等多少秒（FR-3.4.1）。"""
    secs = inspect_interval_seconds(level)
    if secs == 0:
        return 0
    return max(URGENT_INTERVAL_FLOOR, min(secs, URGENT_INTERVAL_CEIL))


class SiteWindowService:
    """站点级窗口服务（策略参数统一用默认 WindowPolicy）。"""

    def __init__(
        self,
        session: AsyncSession,
        policy: WindowPolicy | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or WindowPolicy()

    @property
    def policy(self) -> WindowPolicy:
        return self._policy

    def get_policy_for(self, site: Site) -> WindowPolicy:
        """站点级策略：当前统一返回默认 WindowPolicy。"""
        return WindowPolicy()

    def should_skip_short_candidate(
        self,
        free_type: str | None,
        free_end_at: datetime | None,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """FR-3.2：剩余窗口不足 ``min_buffer × 3``（默认 90min）直接跳过。

        永久免费/中性种永不被此路径拦截。
        """
        if is_permanent_free(free_type):
            return False, ""
        if free_end_at is None:
            return False, ""
        ref = now or utcnow()
        remaining = free_end_at - ref
        if remaining <= timedelta(0):
            return True, "window_expired"
        skip_threshold = timedelta(seconds=self._policy.min_buffer * 3)
        if remaining < skip_threshold:
            return True, "remaining_window_too_short"
        return False, ""


class DeadlineCalculator:
    """窗口锚点字段落库（FR-3.0.1 / FR-3.4）。"""

    def __init__(self, policy: WindowPolicy) -> None:
        self._policy = policy

    def compute(self, free_end_at: datetime) -> tuple[datetime, datetime, datetime]:
        """返回 ``(free_end_at, deadline_at, abort_at)``。

        ``abort_at`` 恒等于 ``free_end_at``（无任何豁免，v1.2 修订）。
        ``deadline_at = free_end_at - safety_gap``，其中
        ``safety_gap = max(窗口 × buffer_ratio, min_buffer)``。
        """
        if free_end_at is None:
            raise ValueError("free_end_at 不能为 None（永久免费不走此路径）")
        d = self._policy.deadlines_for(free_end_at)
        return d.free_end_at, d.deadline_at, d.abort_at


def classify_completion_at(
    completed_at: datetime,
    free_end_at: datetime,
    *,
    tolerance_seconds: int = COMPLETION_TOLERANCE_SECONDS,
) -> tuple[str, int | None]:
    """按 :func:`core.windows.classify_completion` 包装，对外公开。"""
    return classify_completion(
        completed_at, free_end_at, tolerance_seconds=tolerance_seconds
    )


# ---------------------------------------------------------------- 看护队列
async def enqueue_abort(
    queue: TaskQueueService,
    *,
    site_id: int,
    info_hash: str,
    reason: str,
) -> bool:
    """为 ``now ≥ abort_at`` 的任务入队"中止"指令。"""
    enq = await queue.enqueue(
        TaskKind.ABORT_WINDOW,
        {"site_id": site_id, "info_hash": info_hash, "reason": reason},
        idem_key=make_idem_key(
            TaskKind.ABORT_WINDOW, site_id=site_id, info_hash=info_hash, extra=reason
        ),
    )
    return enq.created


def is_window_anchored(t: Torrent) -> bool:
    """任务是否已经挂上窗口锚点字段（FR-3.4）。"""
    return t.abort_at is not None and t.deadline_at is not None
