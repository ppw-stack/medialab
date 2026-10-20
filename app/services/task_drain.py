"""任务队列排空作业：崩溃恢复 + 退避重试消费。

每次运行：
1. 回收僵死 running 任务（updated_at 早于 30 分钟前 → 进程崩溃残留）
2. 认领到期 pending 任务（run_at <= now，指数退避已到点）
3. 逐个执行（task_executor 按 kind 分发），成功 mark_done / 失败 mark_failed

由调度作业 ``drain_task_queue``（cron ``*/5 * * * *``）驱动。
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.session import session_scope
from app.services.scheduler import JobStep
from app.services.task_executor import PushItemError, execute_task
from app.services.task_queue import STALE_RUNNING_MINUTES, TaskQueueService

if TYPE_CHECKING:
    from app.services.factory import AdapterFactory

logger = get_logger(__name__)

#: 单次排空认领上限（防御：队列积压时限流，下轮继续）
_DRAIN_LIMIT = 50

#: 终态任务保留天数（审计排障窗口）
_PURGE_DAYS = 7


async def drain_task_queue(factory: AdapterFactory) -> dict[str, object]:
    """排空到期的任务队列，返回作业结果（steps/summary 供历史弹窗展示）。"""
    # 1) 回收僵死 running
    async with session_scope() as session:
        svc = TaskQueueService(session)
        requeued = await svc.requeue_stale(
            format_bj(bj_now_dt() - timedelta(minutes=STALE_RUNNING_MINUTES))
        )

    # 2) 认领到期 pending
    async with session_scope() as session:
        svc = TaskQueueService(session)
        claimed = await svc.claim_due(bj_now(), limit=_DRAIN_LIMIT)
        # 取出执行所需字段（脱离 session 后 ORM 实例属性已加载）
        tasks = [
            (t.id, t.kind, dict(t.payload)) for t in claimed
        ]

    # 3) 清理超期终态（无条件执行——即使本轮无任务，历史也该定期清理）
    async with session_scope() as session:
        svc = TaskQueueService(session)
        purged = await svc.purge_finished(
            format_bj(bj_now_dt() - timedelta(days=_PURGE_DAYS))
        )
    if purged:
        logger.info("drain_task_queue: 清理 %d 条超 %d 天的终态任务", purged, _PURGE_DAYS)

    if not tasks:
        return {
            "requeued": requeued,
            "claimed": 0,
            "done": 0,
            "failed": 0,
            "purged": purged,
            "summary": (
                (f"回收僵死 {requeued}，无到期任务" if requeued else "无到期任务")
                + (f"，清理历史 {purged}" if purged else "")
            ),
            "steps": [
                JobStep("回收僵死任务", "ok" if requeued else "skip", requeued,
                        f"{requeued} 个" if requeued else "无僵死任务"),
                JobStep("认领到期任务", "skip", 0, "无到期任务"),
                JobStep("清理历史", "ok" if purged else "skip", purged,
                        f"{purged} 条超 7 天" if purged else "无超期历史"),
            ],
        }

    # 4) 逐个执行
    done = 0
    failed = 0
    retried = 0
    errors: list[str] = []
    for task_id, kind, payload in tasks:
        try:
            await execute_task(str(kind), payload, factory)
        except PushItemError as exc:
            async with session_scope() as session:
                svc = TaskQueueService(session)
                if exc.category == "fatal":
                    await svc.mark_failed_forever(task_id, str(exc))
                    failed += 1
                else:
                    final = await svc.mark_failed(task_id, str(exc))
                    if final == "failed":
                        failed += 1
                    else:
                        retried += 1
            errors.append(f"task={task_id} kind={kind}: {exc}")
        except Exception as exc:
            async with session_scope() as session:
                svc = TaskQueueService(session)
                final = await svc.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
                if final == "failed":
                    failed += 1
                else:
                    retried += 1
            errors.append(f"task={task_id} kind={kind}: {exc}")
        else:
            async with session_scope() as session:
                svc = TaskQueueService(session)
                await svc.mark_done(task_id)
            done += 1

    summary = (
        f"认领 {len(tasks)} 任务，成功 {done}"
        + (f"，失败 {failed}" if failed else "")
        + (f"，退避重试 {retried}" if retried else "")
    )
    if requeued:
        summary = f"回收僵死 {requeued}，" + summary
    if purged:
        summary += f"，清理历史 {purged}"

    return {
        "requeued": requeued,
        "claimed": len(tasks),
        "done": done,
        "failed": failed,
        "retried": retried,
        "purged": purged,
        "errors": tuple(errors[:10]),
        "summary": summary,
        "steps": [
            JobStep("回收僵死任务", "ok" if requeued else "skip", requeued,
                    f"{requeued} 个" if requeued else "无僵死任务"),
            JobStep("认领到期任务", "ok", len(tasks), f"{len(tasks)} 个"),
            JobStep(
                "执行任务",
                "warn" if failed else "ok",
                done,
                f"{done} 成功，{failed} 永久失败，{retried} 退避重试",
            ),
            JobStep("清理历史", "ok" if purged else "skip", purged,
                    f"{purged} 条超 7 天" if purged else "无超期历史"),
        ],
    }
