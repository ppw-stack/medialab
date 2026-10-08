"""任务队列（幂等重放，非功能·可靠性/幂等性）。

需求（§6）：

- 崩溃后容器重启**从数据库恢复待执行队列**
- 重放**不产生重复任务**
- 关键写操作使用 ``info_hash + site_id + trigger_source`` 作为幂等键

因此 ``task_queue.idem_key`` 上有唯一约束：**插入时冲突即视为"已存在"**，
而不是报错。这是幂等性的真正落地点——靠业务代码记得"别重复添加"是不可靠的，
靠数据库唯一约束才是可靠的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import TaskQueue

logger = get_logger(__name__)


class TaskKind(StrEnum):
    ADD_TORRENT = "add_torrent"
    REMOVE_TORRENT = "remove_torrent"
    SET_CATEGORY = "set_category"
    SET_PATH = "set_path"
    SET_RATE_LIMIT = "set_rate_limit"
    PAUSE = "pause"
    RESUME = "resume"
    ABORT_WINDOW = "abort_window"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """入队结果。"""

    task_id: int
    #: False = 幂等命中，队列中已存在等价任务（AC-09）
    created: bool


def make_idem_key(kind: str, *, site_id: int | None, info_hash: str, extra: str = "") -> str:
    """构造幂等键。

    形式：``kind|site_id|info_hash|extra``。同一站点同一 hash 的同一操作
    只会入队一次，重放队列不会产生重复下载器任务。
    """
    parts = [kind, str(site_id or 0), info_hash.lower()]
    if extra:
        parts.append(extra)
    return "|".join(parts)


class TaskQueueService:
    """持久化任务队列。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, object],
        *,
        idem_key: str,
        run_at: datetime | None = None,
        max_attempts: int = 3,
    ) -> EnqueueResult:
        """入队。幂等键冲突时返回既有任务（不报错）。"""
        existing = (
            await self._session.execute(
                select(TaskQueue).where(TaskQueue.idem_key == idem_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # 已完成的任务也视为已处理，重复入队无意义
            return EnqueueResult(task_id=existing.id, created=False)

        task = TaskQueue(
            idem_key=idem_key,
            kind=str(kind),
            payload=payload,
            status=TaskStatus.PENDING,
            attempts=0,
            run_at=run_at or utcnow(),
        )
        self._session.add(task)
        try:
            await self._session.flush()
        except IntegrityError:
            # 并发插入同键：唯一约束兜底，视为幂等命中
            await self._session.rollback()
            row = (
                await self._session.execute(
                    select(TaskQueue).where(TaskQueue.idem_key == idem_key)
                )
            ).scalar_one_or_none()
            if row is None:
                raise
            return EnqueueResult(task_id=row.id, created=False)
        return EnqueueResult(task_id=task.id, created=True)

    async def claim_due(self, *, limit: int = 50) -> list[TaskQueue]:
        """取出到期待执行任务（先标记 running，防重复消费）。"""
        now = utcnow()
        rows = (
            (
                await self._session.execute(
                    select(TaskQueue)
                    .where(
                        TaskQueue.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
                        TaskQueue.run_at <= now,
                        TaskQueue.attempts < 3,
                    )
                    .order_by(TaskQueue.run_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = TaskStatus.RUNNING
        await self._session.flush()
        return list(rows)

    async def mark_done(self, task: TaskQueue) -> None:
        task.status = TaskStatus.DONE
        await self._session.flush()

    async def mark_failed(
        self, task: TaskQueue, error: str, *, retry_delay_seconds: int = 60
    ) -> None:
        """失败重试（§6：核心调度任务失败自动重试 3 次）。"""
        task.attempts += 1
        task.last_error = error[:2000]
        if task.attempts >= 3:
            task.status = TaskStatus.FAILED
            logger.error("任务 %s 重试耗尽：%s", task.idem_key, error)
        else:
            task.status = TaskStatus.PENDING
            task.run_at = utcnow() + timedelta(seconds=retry_delay_seconds * task.attempts)
            logger.warning(
                "任务 %s 第 %d 次失败，将重试：%s", task.idem_key, task.attempts, error
            )
        await self._session.flush()

    async def requeue_running(self) -> int:
        """崩溃恢复：把残留的 ``running`` 复位为 ``pending``。

        进程被杀时不会有 mark_done，因此启动时必须复位，否则这些任务
        会永远卡在 running 状态（非功能·可靠性）。
        """
        rows = (
            (
                await self._session.execute(
                    select(TaskQueue).where(TaskQueue.status == TaskStatus.RUNNING)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = TaskStatus.PENDING
            row.run_at = utcnow()
        await self._session.flush()
        if rows:
            logger.info("崩溃恢复：复位 %d 个中断任务", len(rows))
        return len(rows)

    async def pending_count(self) -> int:
        rows = (
            await self._session.execute(
                select(TaskQueue).where(
                    TaskQueue.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING])
                )
            )
        ).scalars().all()
        return len(rows)