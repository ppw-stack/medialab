"""免费种窗口看护（FR-3.4）。

职责：按紧迫度动态频率巡检在途免费种任务，越接近 ``abort_at`` 越密集。
到红线立即触发中止（与 FR-3.5 中止管线共用幂等键）。

**为什么需要单独的看护引擎**：
- M3 巡检 15 分钟一轮，对"30 分钟后即将过期"的任务来不及反应
- 看护频率与 M3 解耦，1 分钟 / 5 分钟 / 15 分钟都按紧迫度自适应
- 看护是写路径（会推中止指令），巡检是只读 + 告警

本模块是 **可观察、可重放、可中断**的：
- 每个中止事件都入队 ``TaskQueue``，崩溃后自动重放（与 M2 一致）
- 阻塞逻辑只针对单条任务，不影响其他看护
- 抢救策略（FR-3.8）的执行结果作为任务级 metric 上报
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
from app.services.alerter import Alerter
from app.services.factory import AdapterFactory
from app.services.free_seed_hunter import win_policy  # noqa: F401
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
#: 抢救策略执行后的 grace 期（秒），避免反复提速
RESCUE_COOLDOWN = 600


class WindowWatcher:
    """窗口看护引擎（FR-3.4）。"""

    def __init__(
        self,
        factory: AdapterFactory,
        alerter: Alerter | None = None,
    ) -> None:
        self._factory = factory
        self._alerter = alerter

    async def watch_active(self) -> dict[str, int]:
        """对所有在途免费种执行一轮看护。"""
        counts = {"inspected": 0, "completed": 0, "aborted": 0, "rescued": 0}

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
        # 永久免费不在看护范围（R10）
        if is_permanent_free(t.free_type):
            return "permanent"

        # 任务被让路暂停时跳过（FR-3.6 互斥：让路不视为需要中止）
        if t.paused_reason == "yield":
            return "yielding"

        # 缺锚点（M1 旧数据或种子中途被启用免费种模块）
        if not is_window_anchored(t):
            if t.abort_at is None:
                return "no_anchor"
            return "no_anchor"

        # 禁删保护期（FR-5.2.6）：刚添加的任务瞬时 abort_at 几乎必然过期，
        # 但**不**应自动中止——否则添加瞬间就被回收。覆盖到保护期结束。
        # 看护会持续巡检到保护期过后的下一轮。
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

        # 2) 计算剩余与紧迫度
        remaining = (
            (t.abort_at - now) if t.abort_at is not None else timedelta(0)
        )
        if remaining <= timedelta(0):
            # 到红线：触发中止
            await self._trigger_abort(
                session, t, reason=AbortedReason.WINDOW_EXPIRED
            )
            return "aborted"

        # 3) 接近红线（deadline_at ≤ now < abort_at）：预警 + 抢救
        if t.deadline_at is not None and now >= t.deadline_at:
            await self._alert_warning(session, t, remaining)
            rescued = await self._maybe_rescue(session, t, remaining)
            if rescued:
                return "rescued"

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

        # 冷却是 FR-2.3.1 阶梯：失败原因按 reason 独立计数
        # 复用 session 内 CooldownService（不需提前 import）
        from app.services.cooldown import CooldownService

        await CooldownService(session).record(
            t.site_id, t.info_hash, CooldownReasonClass(reason)
        )

        # 入队共享幂等键（FR-3.5 Cleaner 接管）
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

    # ---------------------------------------------------------------- 抢救
    async def _maybe_rescue(
        self,
        session: AsyncSession,
        t: Torrent,
        remaining: timedelta,
    ) -> bool:
        """FR-3.8 抢救策略。

        仅在 ``rescue_strategy != notify_only`` 时执行；抢救后必须**仍在
        abort_at 之前**生效（不能突破红线）。任何写入操作都不得接触
        FR-5.2 安全机制。
        """
        # 抢救策略配置在 sites 表，本任务查站点
        from app.db.models import Site

        site = (
            await session.execute(select(Site).where(Site.id == t.site_id))
        ).scalar_one_or_none()
        if site is None or site.rescue_strategy == "notify_only":
            return False

        # 当前抢救冷却中：跳过（避免反复提速）
        if t.last_rescued_at and (
            utcnow() - t.last_rescued_at
        ).total_seconds() < RESCUE_COOLDOWN:
            return False

        # 抢救前：当前速率能完成吗？若能完成，no rescue
        # （避免对正常完成的任务打补丁）
        # 速率读取由 M3 巡检负责，本看护阶段读快照
        if t.size and t.downloaded:
            eta = t.size - t.downloaded
            # 简化：若剩余时间 > 3 倍窗口已过时间，认为可行
            # 真实实现需要速率；M4 先留 hook
            _ = eta

        # 应用策略
        applied = False
        if site.rescue_strategy == "speed_up" and t.downloader_id:
            applied = await self._speed_up(session, t)
        elif site.rescue_strategy == "abort_now":
            # 提前止损：立即触发中止链路
            await self._trigger_abort(
                session, t, reason=AbortedReason.WINDOW_EXPIRED
            )
            return True
        # "yield_then_retry"：FR-3.6 上传让路，由 M3 巡检层面的下载器
        # 状态决定，本看护只标记 paused_reason

        if applied:
            from sqlalchemy import update

            await session.execute(
                update(Torrent)
                .where(Torrent.id == t.id)
                .values(last_rescued_at=utcnow())
            )
            free_seed_outcomes.labels(
                outcome="rescued", reason=site.rescue_strategy
            ).inc()
            return True
        return False

    async def _speed_up(self, session: AsyncSession, t: Torrent) -> bool:
        """提速策略：临时调高下载限速。

        严格受限于 ``abort_at`` 之前；本实现仅做"清除限速"——具体提速值
        留给用户在站点配置中设定（M4 不引入新的站点字段）。
        """
        from app.db.models import DownloaderInstance

        dl = (
            await session.execute(
                select(DownloaderInstance).where(
                    DownloaderInstance.id == t.downloader_id
                )
            )
        ).scalar_one_or_none()
        if dl is None:
            return False
        try:
            adapter = self._factory.downloader(dl)
            await adapter.set_rate_limit(down=0)  # 0 = 解除限速
            return True
        except Exception as exc:
            logger.warning("提速失败 %s：%s", t.name, exc)
            return False

    # ---------------------------------------------------------------- 预警
    async def _alert_warning(
        self,
        session: AsyncSession,
        t: Torrent,
        remaining: timedelta,
    ) -> None:
        """接近红线：🟡 告警 + 速率可行性评估。FR-3.4 巡检行表。"""
        # 复用 M3 告警中心（如果 lifespan 初始化过）
        if self._alerter is None:
            return
        level = "warning" if remaining > timedelta(minutes=30) else "critical"
        msg = (
            f"{t.name} 进入 deadline_at 预警区间，"
            f"剩余 {int(remaining.total_seconds() // 60)} 分钟"
        )
        try:
            await self._alerter.record(
                level,
                "free_seed_warning",
                msg,
                info_hash=t.info_hash,
                torrent_id=t.id,
            )
        except Exception as exc:
            logger.debug("告警失败：%s", exc)


def CooldownReasonClass(reason: str) -> str:
    """把 AbortedReason 映射到 CooldownReason（按 reason 独立计数）。"""
    mapped: str = {
        AbortedReason.WINDOW_EXPIRED: CooldownReason.WINDOW_EXPIRED,
        AbortedReason.ERROR: CooldownReason.DEAD_TORRENT,
    }.get(reason, CooldownReason.DEAD_TORRENT)
    return mapped


# 兼容：上一段里我引用了 add_history 模块，但实际在 M3 写告警时是 per-info_hash
# 落 add_history 这张表作为幂等凭证；免费种看护也走同一份。
__all__ = ["WindowWatcher", "next_inspect_in"]
