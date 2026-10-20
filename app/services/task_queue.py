"""幂等任务队列（§6）。

职责：
- ``enqueue`` 用唯一 ``idem_key`` 记录"某任务曾被尝试"，冲突即幂等命中。
- ``claim_one`` / ``claim_due`` 把 pending → running（attempts + 1）。
- ``mark_done`` / ``mark_failed`` 推进终态；瞬态失败回 pending 带指数退避。
- ``requeue_stale`` 回收僵死 running 任务（进程崩溃后由 drain 作业兜底）。

崩溃恢复语义：claim 落库**先于**网络执行——进程在 claim 后崩溃，任务停在
running，超过 :data:`STALE_RUNNING_MINUTES` 后被回收为 pending 重试。
执行器自身幂等（已 pushed 直接成功 / duplicate 报错视为成功），重复执行安全。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import bj_now, bj_now_dt, format_bj
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


#: running 超过该时长视为僵死（进程崩溃），回收为 pending 重试
STALE_RUNNING_MINUTES = 30

#: 退避基数（分钟）：第 N 次失败后 run_at = now + min(2**N, 30) 分钟
_BACKOFF_CAP_MINUTES = 30


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """入队结果。"""

    task_id: int
    #: False = 幂等命中，队列中已存在等价任务
    created: bool
    #: 幂等命中时既有任务的状态；created=True 时为 pending
    existing_status: str | None = None


def make_idem_key(kind: str, *, site_id: int | None, info_hash: str, extra: str = "") -> str:
    """构造幂等键。

    形式：``kind|site_id|info_hash|extra``。同一站点同一 hash 的同一操作
    只会入队一次。

    ``|`` 是字段分隔符，info_hash/extra 中的 ``|`` 会被转义为 ``_``
    以免破坏字段语义（item_id 是站点任意字符串，可能含特殊字符）。
    info_hash 调用方应保证 40 位 hex，但本函数仍做 `.lower()` 归一化。
    """
    safe_hash = (info_hash or "").lower().replace("|", "_")
    parts = [kind, str(site_id or 0), safe_hash]
    if extra:
        parts.append(extra.replace("|", "_"))
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
            return EnqueueResult(
                task_id=existing.id, created=False, existing_status=existing.status
            )

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
            return EnqueueResult(
                task_id=row.id, created=False, existing_status=row.status
            )
        return EnqueueResult(task_id=task.id, created=True, existing_status=None)

    # ------------------------------------------------------------ 状态推进
    async def claim_one(self, task_id: int) -> TaskQueue | None:
        """把单个 pending 任务认领为 running（attempts + 1）。

        任务不存在或不在 pending 态（已被其他执行者认领）→ 返回 None。
        """
        row = await self._session.get(TaskQueue, task_id)
        if row is None or row.status != TaskStatus.PENDING:
            return None
        row.status = TaskStatus.RUNNING
        row.attempts += 1
        await self._session.flush()
        return row

    async def claim_due(self, now: str, *, limit: int = 50) -> list[TaskQueue]:
        """认领到期任务：pending 且 run_at <= now → running。

        run_at 是北京时区字符串（``yyyy-MM-dd HH:mm:ss``），字典序 == 时间序。
        """
        rows = (
            await self._session.execute(
                select(TaskQueue)
                .where(
                    TaskQueue.status == TaskStatus.PENDING,
                    TaskQueue.run_at <= now,
                )
                .order_by(TaskQueue.run_at)
                .limit(limit)
            )
        ).scalars().all()
        for row in rows:
            row.status = TaskStatus.RUNNING
            row.attempts += 1
        await self._session.flush()
        return list(rows)

    async def mark_done(self, task_id: int) -> None:
        """任务成功 → done。"""
        await self._session.execute(
            update(TaskQueue)
            .where(TaskQueue.id == task_id)
            .values(status=TaskStatus.DONE)
        )

    async def mark_failed(self, task_id: int, error: str) -> str:
        """任务失败 → pending（退避重试）或 failed（永久）。

        重试策略：attempts 已达 :data:`MAX_ATTEMPTS` → 永久 ``failed``；
        否则回 ``pending``，run_at 指数退避。

        退避语义：claim 时已 `attempts += 1`，故 mark_failed 收到的
        attempts 表示"本次失败是第 N 次尝试"（N 从 1 起）。退避基数为
        ``2 ** (attempts - 1)``，即第 1 次失败退避 1 分钟、第 2 次 2 分钟、
        第 3 次 4 分钟... 上限 30 分钟。

        :return: 终态（``pending`` / ``failed``），供调用方区分"待重试"与"死信"。
        """
        row = await self._session.get(TaskQueue, task_id)
        if row is None:
            return TaskStatus.FAILED
        row.last_error = error[:500]
        if row.attempts >= MAX_ATTEMPTS:
            row.status = TaskStatus.FAILED
            return TaskStatus.FAILED
        # attempts 从 1 起（claim 时已自增），故退避基数减 1 让首次失败退避 1 分钟
        delay_min = min(2 ** (row.attempts - 1), _BACKOFF_CAP_MINUTES)
        row.status = TaskStatus.PENDING
        row.run_at = format_bj(bj_now_dt() + timedelta(minutes=delay_min))
        return TaskStatus.PENDING

    async def mark_failed_forever(self, task_id: int, error: str) -> None:
        """致命错误（站点/下载器/索引行已删除等）→ 直接永久 failed，不重试。"""
        await self._session.execute(
            update(TaskQueue)
            .where(TaskQueue.id == task_id)
            .values(status=TaskStatus.FAILED, last_error=error[:500])
        )

    async def requeue_stale(self, stale_before: str) -> int:
        """回收僵死 running 任务（updated_at < stale_before）为 pending。

        进程在 claim 后崩溃时任务停在 running——本方法把它重新排回队列，
        由 drain 作业在下次运行时认领执行。

        回收时把 run_at 推后一个退避窗口（基于当前 attempts），避免
        僵死任务（attempts 已被前次 claim 自增）立即被下轮 claim_due
        再认领并再次 attempts+1，导致重试次数快速耗尽。
        """
        rows = (
            await self._session.execute(
                select(TaskQueue.id, TaskQueue.attempts).where(
                    TaskQueue.status == TaskStatus.RUNNING,
                    TaskQueue.updated_at < stale_before,
                )
            )
        ).all()
        if not rows:
            return 0
        # 按 attempts 分组，给每组算一个退避 run_at（避免逐行更新）
        from collections import defaultdict

        by_attempts: dict[int, list[int]] = defaultdict(list)
        for row_id, attempts in rows:
            by_attempts[int(attempts or 0)].append(int(row_id))
        now_dt = bj_now_dt()
        for attempts, ids in by_attempts.items():
            delay_min = min(
                2 ** max(attempts - 1, 0), _BACKOFF_CAP_MINUTES
            )
            new_run_at = format_bj(now_dt + timedelta(minutes=delay_min))
            await self._session.execute(
                update(TaskQueue)
                .where(TaskQueue.id.in_(ids))
                .values(status=TaskStatus.PENDING, run_at=new_run_at)
            )
        return sum(len(ids) for ids in by_attempts.values())

    async def purge_finished(self, older_than: str) -> int:
        """清理终态任务（done / failed）中早于 ``older_than`` 的行。

        task_queue 只增不删会无限堆积（push_free 每轮最多 50 行）——
        审计价值随时间衰减，保留近 7 天足够排障。
        """
        from sqlalchemy import delete, func

        count = (
            await self._session.execute(
                select(func.count(TaskQueue.id)).where(
                    TaskQueue.status.in_((TaskStatus.DONE, TaskStatus.FAILED)),
                    TaskQueue.updated_at < older_than,
                )
            )
        ).scalar_one()
        if count == 0:
            return 0
        await self._session.execute(
            delete(TaskQueue).where(
                TaskQueue.status.in_((TaskStatus.DONE, TaskStatus.FAILED)),
                TaskQueue.updated_at < older_than,
            )
        )
        return int(count)


#: 任务最大尝试次数（claim 时 attempts + 1；超过则 mark_failed 转永久 failed）
MAX_ATTEMPTS = 5


__all__ = [
    "MAX_ATTEMPTS",
    "STALE_RUNNING_MINUTES",
    "EnqueueResult",
    "TaskKind",
    "TaskQueueService",
    "TaskStatus",
    "make_idem_key",
]
