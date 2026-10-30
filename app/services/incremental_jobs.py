"""增量同步任务的后台执行。

与 :mod:`app.services.scan_runner` 同款结构（后台 task + 内存进度表），
但跑的是 :func:`app.services.incremental.run_incremental`。

**独立于扫描任务**：两者的统计字段不同（增量有事件数、游标、冲突数），
混在一起会让前端两套 UI 都别扭。
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import session_scope
from app.services import incremental
from app.services.incremental import IncrementalError, IncrementalStats

logger = get_logger(__name__)


class IncrementalJob:
    """一个增量任务的运行状态。"""

    def __init__(self, job_id: str, driver_id: int, driver_name: str) -> None:
        self.id = job_id
        self.driver_id = driver_id
        self.driver_name = driver_name
        self.state = "running"
        self.started_at = int(time.time())
        self.finished_at: int | None = None
        self.current_path: str = ""
        self.stats = IncrementalStats()
        self.logs: list[str] = []
        self.error: str | None = None
        self.task: asyncio.Task[None] | None = None

    def log(self, message: str) -> None:
        """追加一行日志（带时间戳）。"""
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > 200:
            del self.logs[: len(self.logs) - 200]

    def to_dict(self) -> dict[str, Any]:
        """序列化给 API。"""
        return {
            "id": self.id,
            "driver_id": self.driver_id,
            "driver_name": self.driver_name,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": (self.finished_at or int(time.time())) - self.started_at,
            "current_path": self.current_path,
            "logs": self.logs,
            "error": self.error,
            **self.stats.to_dict(),
        }


#: 任务表（job_id → IncrementalJob）
_JOBS: dict[str, IncrementalJob] = {}
_LOCK = asyncio.Lock()


async def create_job(driver_id: int, driver_name: str) -> IncrementalJob:
    """创建任务。"""
    import uuid

    job = IncrementalJob(uuid.uuid4().hex[:12], driver_id, driver_name)
    async with _LOCK:
        _JOBS[job.id] = job
        # 上限：只留最近 20 个
        if len(_JOBS) > 20:
            oldest = min(_JOBS.values(), key=lambda j: j.started_at)
            _JOBS.pop(oldest.id, None)
    return job


async def get_job(job_id: str) -> IncrementalJob | None:
    """按 id 取任务。"""
    async with _LOCK:
        return _JOBS.get(job_id)


async def find_running(driver_id: int) -> IncrementalJob | None:
    """找该存储源正在跑的任务。"""
    async with _LOCK:
        for job in _JOBS.values():
            if job.driver_id == driver_id and job.state == "running":
                return job
    return None


async def start_incremental_job(driver_id: int, driver_name: str) -> IncrementalJob:
    """创建并在后台启动增量任务。"""
    job = await create_job(driver_id, driver_name)
    job.log(f"开始增量同步「{driver_name}」")
    job.task = asyncio.create_task(_run_job(job))
    return job


async def _run_job(job: IncrementalJob) -> None:
    """后台执行增量同步。"""
    try:
        async with session_scope() as db:
            row = (
                await db.execute(select(Driver).where(Driver.id == job.driver_id))
            ).scalar_one_or_none()
            if row is None:
                job.state = "failed"
                job.error = f"存储源不存在：{job.driver_id}"
                job.log(job.error)
                return

            async def on_progress(path: str, stats: IncrementalStats) -> None:
                job.current_path = path
                job.stats = stats

            stats = await incremental.run_incremental(
                db, get_settings(), row, on_progress=on_progress
            )
            job.stats = stats

        job.state = "success"
        job.log(
            f"增量完成：事件 {stats.events_fetched} 条（媒体库内 {stats.events_in_scope}），"
            f"strm 新增 {stats.created} / 更新 {stats.updated} / 未变 {stats.unchanged}，"
            f"删除 {stats.deleted}，附件新增 {stats.attachment_synced}，"
            f"游标 {stats.cursor_old} → {stats.cursor_new}"
        )
        if stats.conflicts:
            job.log(
                f"⚠️ 有 {stats.conflicts} 处路径冲突（疑似目录改名），"
                "已跳过——建议跑一次全量扫描修正"
            )
        logger.info("增量任务 %s 完成", job.id)
    except IncrementalError as exc:
        job.state = "failed"
        job.error = str(exc)
        job.log(f"增量失败：{exc}")
        logger.warning("增量任务 %s 失败：%s", job.id, exc)
    except asyncio.CancelledError:
        job.state = "cancelled"
        job.log("任务被取消")
        raise
    except Exception as exc:
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.log(f"增量异常：{job.error}")
        logger.error("增量任务 %s 异常：%s", job.id, traceback.format_exc())
    finally:
        job.finished_at = int(time.time())


def clear_all() -> None:
    """清空任务表（测试用）。"""
    _JOBS.clear()
