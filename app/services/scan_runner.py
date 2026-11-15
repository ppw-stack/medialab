"""扫描任务的后台执行。

把 :class:`ScanJob`（内存进度）与 :func:`scan_driver`（实际扫描）接起来：

- 扫描在 ``asyncio.create_task`` 里跑，不阻塞 HTTP 请求
- ``on_progress`` 回调把文件路径与统计写进 job
- 成功 / 失败 / 取消都落状态，前端轮询时能看到终态

**独立的 DB session**：后台任务不能复用请求的 session（请求结束就关了）。
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import session_scope
from app.services import scan_jobs
from app.services.driver_factory import build_driver
from app.services.media import MediaServiceError, ScanStats, scan_driver

logger = get_logger(__name__)


def start_scan_job(driver_id: int, driver_name: str, *, prune: bool = False) -> Any:
    """创建并在后台启动扫描任务。

    Returns:
        刚创建的 :class:`ScanJob`（``id`` 立即可用）。
    """
    job = scan_jobs.create_job(driver_id, driver_name)
    job.log(f"开始扫描「{driver_name}」")
    job.task = asyncio.create_task(_run_job(job, prune=prune))
    return job


async def _run_job(job: scan_jobs.ScanJob, *, prune: bool) -> None:
    """后台执行扫描，把结果写回 job。"""
    try:
        async with session_scope() as db:
            row = (
                await db.execute(select(Driver).where(Driver.id == job.driver_id))
            ).scalar_one_or_none()
            if row is None:
                job.state = scan_jobs.STATE_FAILED
                job.error = f"存储源不存在：{job.driver_id}"
                job.log(job.error)
                job.finished_at = int(__import__("time").time())
                return

            job.log(f"媒体库路径 {row.media_path or '?'} → {row.output_dir or '?'}")

            async def _on_refresh(cred: Any) -> None:
                """Token 刷新后写回 DB（光鸭的 token 只有 1~2 小时有效）。"""
                from app.services.redirect import _persist_refreshed_credential

                await _persist_refreshed_credential(row.id, cred)

            driver_impl = build_driver(
                row.type,
                row.credential,
                row.device_type,
                on_token_refresh=_on_refresh,
            )

            async def on_progress(path: str, stats: ScanStats, processed: int) -> None:
                job.current_path = path
                job.processed = processed
                job.created = stats.created
                job.updated = stats.updated
                job.unchanged = stats.unchanged
                job.attachment_synced = stats.attachment_synced
                job.attachment_unchanged = stats.attachment_unchanged
                job.attachment_failed = stats.attachment_failed
                job.skipped_unknown = stats.skipped_unknown
                job.skipped_small = stats.skipped_small
                job.deduped = stats.deduped
                job.pruned = stats.pruned
                # 每 10 个记一行日志，避免刷屏
                if processed % 10 == 0:
                    job.log(f"已处理 {processed} 个文件：{path}")

            stats = await scan_driver(
                db,
                get_settings(),
                row,
                prune=prune,
                on_progress=on_progress,
                driver_impl=driver_impl,
            )

        # 用**最终** stats 覆盖 job 字段。
        #
        # on_progress 最后一次调用发生在处理完最后一个文件**之前**，
        # 所以 job.created 会比实际少 1（曾出现日志 7861 / API 7860 的不一致）。
        job.created = stats.created
        job.updated = stats.updated
        job.unchanged = stats.unchanged
        job.attachment_synced = stats.attachment_synced
        job.attachment_unchanged = stats.attachment_unchanged
        job.attachment_failed = stats.attachment_failed
        job.skipped_unknown = stats.skipped_unknown
        job.skipped_small = stats.skipped_small
        job.deduped = stats.deduped
        job.pruned = stats.pruned
        job.failed = stats.failed
        job.dedup_details = list(stats.dedup_details)
        job.current_path = ""

        job.state = scan_jobs.STATE_SUCCESS
        job.log(
            f"扫描完成：strm 新增 {stats.created}，更新 {stats.updated}，"
            f"未变 {stats.unchanged}；"
            f"附件新增 {stats.attachment_synced}，未变 {stats.attachment_unchanged}，"
            f"失败 {stats.attachment_failed}；"
            f"跳过 未知 {stats.skipped_unknown} / 太小 {stats.skipped_small}；"
            f"去重 {stats.deduped} / 清理 {stats.pruned}"
        )
        logger.info("扫描任务 %s 完成", job.id)

    except asyncio.CancelledError:
        job.state = scan_jobs.STATE_CANCELLED
        job.log("任务被取消")
        raise
    except MediaServiceError as exc:
        job.state = scan_jobs.STATE_FAILED
        job.error = str(exc)
        job.log(f"扫描失败：{exc}")
        logger.warning("扫描任务 %s 失败：%s", job.id, exc)
    except Exception as exc:
        job.state = scan_jobs.STATE_FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.log(f"扫描异常：{job.error}")
        logger.error("扫描任务 %s 异常：%s", job.id, traceback.format_exc())
    finally:
        import time

        job.finished_at = int(time.time())
