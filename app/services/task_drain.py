"""任务队列排空作业：崩溃恢复 + 退避重试消费。

每次运行：
1. 回收僵死 running 任务（updated_at 早于 30 分钟前 → 进程崩溃残留）
2. 认领到期 pending 任务（run_at <= now，指数退避已到点）
3. 逐个执行（task_executor 按 kind 分发），成功 mark_done / 失败 mark_failed
4. 清理超期的 ``job_runs`` 历史（作业执行历史，见 :data:`_JOB_RUN_PURGE_DAYS`）

由调度作业 ``drain_task_queue``（cron ``*/5 * * * *``）驱动。
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import JobRun, ProbeResult
from app.db.session import session_scope
from app.services.scheduler import JobStep, JobStepItem, build_items
from app.services.task_executor import PushItemError, execute_task
from app.services.task_queue import STALE_RUNNING_MINUTES, TaskQueueService

if TYPE_CHECKING:
    from app.services.factory import AdapterFactory

logger = get_logger(__name__)

#: 单次排空认领上限（防御：队列积压时限流，下轮继续）
_DRAIN_LIMIT = 50

#: 终态任务保留天数（审计排障窗口）
_PURGE_DAYS = 7

#: ``job_runs``（作业执行历史）保留天数。
#:
#: 作业历史**只增不删**会无限增长——现在每次运行还额外存了执行日志
#: 与逐项明细，单条 ``result_json`` 已有 10–30KB。故必须定期清理。
#:
#: 为什么挂在 drain 作业里：它已经承担 ``task_queue`` 的定期清理语义
#: （每 5 分钟一次），为一个 DELETE 单开第 8 个作业属过度设计。
_JOB_RUN_PURGE_DAYS = 30

#: ``probe_results``（存储源探测历史）保留天数。
#:
#: 启动自动测连 + 手动测连 + 登录后自动测连，一天能写几十行。
#: 探测历史是**排障用**的（看「昨天还正常，今天突然 401」），30 天够用。
_PROBE_RESULT_PURGE_DAYS = 30


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
        # 深拷贝 payload：payload 是 JSON 列，可能含嵌套 dict/list，
        # 浅拷贝（dict(t.payload)）的内层仍是 ORM 实例引用的同一对象，
        # session 关闭后访问可能触发 DetachedInstanceError。
        tasks = [
            (t.id, t.kind, copy.deepcopy(t.payload)) for t in claimed
        ]

    # 3) 清理超期终态（无条件执行——即使本轮无任务，历史也该定期清理）
    async with session_scope() as session:
        svc = TaskQueueService(session)
        purged = await svc.purge_finished(
            format_bj(bj_now_dt() - timedelta(days=_PURGE_DAYS))
        )
    if purged:
        logger.info("drain_task_queue: 清理 %d 条超 %d 天的终态任务", purged, _PURGE_DAYS)

    # 3.1) 清理超期的作业执行历史（job_runs）
    #
    # 作业历史只增不删会无限增长——现在每次运行还额外存了执行日志与
    # 逐项明细，单条 result_json 已有 10–30KB。
    async with session_scope() as session:
        job_runs_purged = await _purge_old_job_runs(session)
    if job_runs_purged:
        logger.info(
            "drain_task_queue: 清理 %d 条超 %d 天的作业历史",
            job_runs_purged, _JOB_RUN_PURGE_DAYS,
        )

    # 3.2) 清理超期的存储源探测历史（probe_results）
    #
    # 启动自动测连 + 手动测连 + 登录后自动测连，一天能写几十行，
    # **只增不删会无限增长**。同样挂在 drain 作业里（每 5 分钟一次）。
    async with session_scope() as session:
        probes_purged = await _purge_old_probe_results(session)
    if probes_purged:
        logger.info(
            "drain_task_queue: 清理 %d 条超 %d 天的探测历史",
            probes_purged, _PROBE_RESULT_PURGE_DAYS,
        )

    if not tasks:
        return {
            "requeued": requeued,
            "claimed": 0,
            "done": 0,
            "failed": 0,
            "purged": purged,
            "job_runs_purged": job_runs_purged,
            "probes_purged": probes_purged,
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
                JobStep("清理作业历史", "ok" if job_runs_purged else "skip",
                        job_runs_purged,
                        f"{job_runs_purged} 条超 {_JOB_RUN_PURGE_DAYS} 天"
                        if job_runs_purged else "无超期作业历史"),
                JobStep("清理探测历史", "ok" if probes_purged else "skip",
                        probes_purged,
                        f"{probes_purged} 条超 {_PROBE_RESULT_PURGE_DAYS} 天"
                        if probes_purged else "无超期探测历史"),
            ],
        }

    # 4) 逐个执行
    done = 0
    failed = 0
    retried = 0
    errors: list[str] = []
    # 逐任务明细（前端「步骤卡片」展开后可见）
    task_items: list[JobStepItem] = []
    for task_id, kind, payload in tasks:
        try:
            await execute_task(str(kind), payload, factory)
        except PushItemError as exc:
            async with session_scope() as session:
                svc = TaskQueueService(session)
                if exc.category == "fatal":
                    await svc.mark_failed_forever(task_id, str(exc))
                    failed += 1
                    task_items.append(
                        JobStepItem(f"task #{task_id}", "fail", None, str(exc))
                    )
                else:
                    final = await svc.mark_failed(task_id, str(exc))
                    if final == "failed":
                        failed += 1
                        task_items.append(
                            JobStepItem(f"task #{task_id}", "fail", None, str(exc))
                        )
                    else:
                        retried += 1
                        task_items.append(
                            JobStepItem(
                                f"task #{task_id}", "warn", None,
                                f"退避重试：{exc}",
                            )
                        )
            errors.append(f"task={task_id} kind={kind}: {exc}")
        except Exception as exc:
            async with session_scope() as session:
                svc = TaskQueueService(session)
                final = await svc.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
                if final == "failed":
                    failed += 1
                else:
                    retried += 1
            task_items.append(
                JobStepItem(
                    f"task #{task_id}", "fail", None, f"{type(exc).__name__}: {exc}",
                )
            )
            errors.append(f"task={task_id} kind={kind}: {exc}")
        else:
            async with session_scope() as session:
                svc = TaskQueueService(session)
                await svc.mark_done(task_id)
            done += 1
            task_items.append(JobStepItem(f"task #{task_id}", "ok", None, str(kind)))

    items, items_total = build_items(task_items)

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
        "job_runs_purged": job_runs_purged,
        "probes_purged": probes_purged,
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
                items=items,
                items_total=items_total,
            ),
            JobStep("清理历史", "ok" if purged else "skip", purged,
                    f"{purged} 条超 7 天" if purged else "无超期历史"),
            JobStep("清理作业历史", "ok" if job_runs_purged else "skip",
                    job_runs_purged,
                    f"{job_runs_purged} 条超 {_JOB_RUN_PURGE_DAYS} 天"
                    if job_runs_purged else "无超期作业历史"),
            JobStep("清理探测历史", "ok" if probes_purged else "skip",
                    probes_purged,
                    f"{probes_purged} 条超 {_PROBE_RESULT_PURGE_DAYS} 天"
                    if probes_purged else "无超期探测历史"),
        ],
    }


async def _purge_old_job_runs(session: AsyncSession) -> int:
    """删除超期的作业执行历史，返回删除条数。

    ``started_at`` 是**北京时间字符串**（``yyyy-MM-dd HH:mm:ss``），
    字典序 == 时间序，故可直接字符串比较（与 ``task_queue`` 的
    ``purge_finished`` 同款口径）。
    """
    cutoff = format_bj(bj_now_dt() - timedelta(days=_JOB_RUN_PURGE_DAYS))
    result = cast(
        CursorResult[object],
        await session.execute(
            delete(JobRun).where(JobRun.started_at < cutoff)
        ),
    )
    return int(result.rowcount or 0)


async def _purge_old_probe_results(session: AsyncSession) -> int:
    """删除超期的存储源探测历史，返回删除条数。

    ``probed_at`` 是**北京时间字符串**（``yyyy-MM-dd HH:mm:ss``），
    字典序 == 时间序，故可直接字符串比较（与 ``_purge_old_job_runs``
    同款口径）。
    """
    cutoff = format_bj(bj_now_dt() - timedelta(days=_PROBE_RESULT_PURGE_DAYS))
    result = cast(
        CursorResult[object],
        await session.execute(
            delete(ProbeResult).where(ProbeResult.probed_at < cutoff)
        ),
    )
    return int(result.rowcount or 0)
