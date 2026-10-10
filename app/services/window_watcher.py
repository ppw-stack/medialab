"""免费种窗口看护（FR-3.4）。

职责：按紧迫度动态频率巡检在途免费种任务，越接近 ``abort_at`` 越密集。
到红线立即触发中止（与 FR-3.5 中止管线共用幂等键）。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import free_seed_outcomes
from app.core.timeutils import utcnow
from app.db.models import (
    AbortedReason,
    CooldownReason,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.task_queue import TaskQueueService
from app.services.window_service import (
    classify_completion_at,
    enqueue_abort,
    is_permanent_free,
    is_window_anchored,
    next_inspect_in,
)

logger = get_logger(__name__)

#: 巡检单次最多处理任务数（避免锁过久）
WATCH_BATCH = 200


class WindowWatcher:
    """窗口看护引擎（FR-3.4）。"""

    def __init__(
        self,
        factory: AdapterFactory,
    ) -> None:
        self._factory = factory

    async def watch_active(self) -> dict[str, int]:
        """对所有在途免费种执行一轮看护。"""
        counts = {"inspected": 0, "completed": 0, "aborted": 0}

        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(Torrent)
                        .where(
                            Torrent.source == Source.FREE_SEED,
                            Torrent.status.in_(
                                [
                                    TorrentStatus.ACTIVE,
                                    TorrentStatus.PAUSED,
                                ]
                            ),
                        )
                        .order_by(Torrent.abort_at.asc().nullslast())
                        .limit(WATCH_BATCH)
                    )
                )
                .scalars()
                .all()
            )

            for t in rows:
                counts["inspected"] += 1
                outcome = await self._watch_one(session, t)
                counts[outcome] = counts.get(outcome, 0) + 1

        if sum(v for v in counts.values() if v):
            logger.info("窗口看护完成：%s", counts)
        return counts

    async def _watch_one(
        self, session: AsyncSession, t: Torrent
    ) -> str:
        """看护单条任务。返回 :attr:`counts` 的一个键。"""
        if is_permanent_free(t.free_type):
            return "permanent"

        if t.paused_reason == "yield":
            return "yielding"

        if not is_window_anchored(t):
            return "no_anchor"

        if t.protect_until and t.protect_until > utcnow():
            return "protected"

        now = utcnow()
        # 1) 已完成：按 completed_at 归入四分类
        if t.status == TorrentStatus.COMPLETED or t.completed_at is not None:
            if t.free_end_at is None:
                return "completed"
            status, slack = classify_completion_at(
                t.completed_at or now, t.free_end_at
            )
            t.status = status
            t.window_slack = slack
            return "completed"

        # 2) 到红线：触发中止
        remaining = (
            (t.abort_at - now) if t.abort_at is not None else timedelta(0)
        )
        if remaining <= timedelta(0):
            await self._trigger_abort(
                session, t, reason=AbortedReason.WINDOW_EXPIRED
            )
            return "aborted"

        return "in_window"

    # ---------------------------------------------------------------- 中止
    async def _trigger_abort(
        self,
        session: AsyncSession,
        t: Torrent,
        *,
        reason: str,
    ) -> None:
        """触发中止：落库状态 + 入队中止指令（M5 Cleaner 真正执行）。"""
        t.status = TorrentStatus.ABORTED
        t.aborted_reason = reason
        t.aborted_at = utcnow()

        from app.services.cooldown import CooldownService

        await CooldownService(session).record(
            t.site_id, t.info_hash, CooldownReasonClass(reason)
        )

        await enqueue_abort(
            TaskQueueService(session),
            site_id=t.site_id,
            info_hash=t.info_hash,
            reason=reason,
        )

        free_seed_outcomes.labels(outcome="aborted", reason=reason).inc()
        logger.info(
            "免费种到红线：%s [%s] reason=%s", t.name, t.info_hash[:8], reason
        )


def CooldownReasonClass(reason: str) -> str:
    """把 AbortedReason 映射到 CooldownReason（按 reason 独立计数）。"""
    mapped: str = {
        AbortedReason.WINDOW_EXPIRED: CooldownReason.WINDOW_EXPIRED,
        AbortedReason.ERROR: CooldownReason.DEAD_TORRENT,
    }.get(reason, CooldownReason.DEAD_TORRENT)
    return mapped


__all__ = ["WindowWatcher", "next_inspect_in"]
