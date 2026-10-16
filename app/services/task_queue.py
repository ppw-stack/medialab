"""幂等记录表（§6）。

当前职责：``enqueue`` 用唯一 ``idem_key`` 记录"本站曾对此 info_hash 尝试过
ADD_TORRENT"，作为幂等凭证。冲突即视为"已尝试"，不报错。

**未实现**：崩溃恢复重放（``claim_due`` / ``requeue_running`` 排空消费）、
``mark_done`` / ``mark_failed`` 状态推进。实际的投递去重由业务层
(:mod:`app.services.matcher` 的 Torrent / AddHistory / Cooldown 判定) 负责，
失败任务在冷却过期后由下一轮重试。task_queue 行目前只增不删，作为审计轨迹。
要真正实现崩溃恢复重放，需在调度循环中消费队列并推进状态——届时补回
claim/mark 系列方法即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import bj_now
from app.db.models import TaskQueue


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
    #: False = 幂等命中，队列中已存在等价任务
    created: bool


def make_idem_key(kind: str, *, site_id: int | None, info_hash: str, extra: str = "") -> str:
    """构造幂等键。

    形式：``kind|site_id|info_hash|extra``。同一站点同一 hash 的同一操作
    只会入队一次。
    """
    parts = [kind, str(site_id or 0), info_hash.lower()]
    if extra:
        parts.append(extra)
    return "|".join(parts)


class TaskQueueService:
    """持久化幂等记录表。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, object],
        *,
        idem_key: str,
        run_at: str | None = None,
    ) -> EnqueueResult:
        """入队。幂等键冲突时返回既有任务（不报错）。"""
        existing = (
            await self._session.execute(
                select(TaskQueue).where(TaskQueue.idem_key == idem_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return EnqueueResult(task_id=existing.id, created=False)

        task = TaskQueue(
            idem_key=idem_key,
            kind=str(kind),
            payload=payload,
            status=TaskStatus.PENDING,
            attempts=0,
            run_at=run_at or bj_now(),
        )
        self._session.add(task)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            # 并发插入同键：唯一约束兜底，视为幂等命中
            row = (
                await self._session.execute(
                    select(TaskQueue).where(TaskQueue.idem_key == idem_key)
                )
            ).scalar_one_or_none()
            if row is None:
                raise
            return EnqueueResult(task_id=row.id, created=False)
        return EnqueueResult(task_id=task.id, created=True)


__all__ = [
    "EnqueueResult",
    "TaskKind",
    "TaskQueueService",
    "TaskStatus",
    "make_idem_key",
]
