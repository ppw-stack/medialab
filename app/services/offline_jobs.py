"""离线下载任务的后台执行（内存态）。

与 :mod:`app.services.migration_jobs` / :mod:`app.services.scan_jobs`
同款结构：后台 ``asyncio.Task`` + 内存进度表 + 环形日志。

**为什么不需要数据库表**：光鸭目标目录就是唯一真相源——重跑时「同名 +
同大小」会被判定为已存在并跳过。所以参数随请求传入，进度丢了重跑即可。
（代价：容器重启会丢掉本轮进度，但不会丢已下载的文件。）
"""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid

from app.core.logging import get_logger
from app.services.offline_download import (
    OfflineJob,
    OfflineParams,
    run_offline_download,
)

logger = get_logger(__name__)

#: 每个任务保留的日志行数（环形缓冲）
MAX_LOGS = 500
#: 内存里最多保留多少个历史任务
MAX_JOBS = 20
#: 失败 / 冲突明细上限
MAX_DETAILS = 100

STATE_RUNNING = "running"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

#: 任务表（job_id → OfflineJob）
_JOBS: dict[str, OfflineJob] = {}
_LOCK = asyncio.Lock()
_SEQ = 0


async def create_job(params: OfflineParams) -> OfflineJob:
    """创建一个离线下载任务（同步，不启 task）。"""
    global _SEQ
    _SEQ += 1
    job = OfflineJob(id=uuid.uuid4().hex[:12], params=params, seq=_SEQ)
    async with _LOCK:
        _JOBS[job.id] = job
        _evict_old()
    return job


async def get_job(job_id: str) -> OfflineJob | None:
    """按 id 取任务。"""
    async with _LOCK:
        return _JOBS.get(job_id)


async def list_jobs() -> list[OfflineJob]:
    """列出任务（最新在前）。"""
    async with _LOCK:
        jobs = list(_JOBS.values())
    return sorted(jobs, key=lambda j: j.seq, reverse=True)


async def find_running() -> OfflineJob | None:
    """有没有正在跑的离线下载（全局只允许一个——并发只会互相抢光鸭的下载配额）。"""
    async with _LOCK:
        for job in _JOBS.values():
            if job.state == STATE_RUNNING:
                return job
    return None


async def start_offline_job(params: OfflineParams) -> OfflineJob:
    """创建并在后台启动离线下载任务。"""
    job = await create_job(params)
    job.log("开始离线下载")
    job.task = asyncio.create_task(_run_job(job))
    return job


async def cancel_job(job_id: str) -> bool:
    """取消任务（停掉后台 task；已提交的离线任务不撤回）。"""
    job = await get_job(job_id)
    if job is None or job.state != STATE_RUNNING:
        return False
    if job.task is not None:
        job.task.cancel()
    job.state = STATE_CANCELLED
    job.finished_at = int(time.time())
    job.log("已取消（已提交的离线任务留在光鸭继续跑）")
    return True


def _evict_old() -> None:
    """超出上限时清掉最旧的**已完成**任务。"""
    if len(_JOBS) <= MAX_JOBS:
        return
    finished = [j for j in _JOBS.values() if j.state != STATE_RUNNING]
    finished.sort(key=lambda j: j.seq)
    for job in finished[: len(_JOBS) - MAX_JOBS]:
        _JOBS.pop(job.id, None)


async def _run_job(job: OfflineJob) -> None:
    """后台执行离线下载。"""
    try:
        await run_offline_download(job)
        if job.state == STATE_RUNNING:
            job.state = STATE_SUCCESS
        logger.info("离线下载任务 %s 结束（成功 %d）", job.id, job.success)
    except asyncio.CancelledError:
        job.state = STATE_CANCELLED
        job.log("任务被取消")
        raise
    except Exception as exc:
        job.state = STATE_FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.log(f"离线下载异常：{job.error}")
        logger.error("离线下载任务 %s 异常：%s", job.id, traceback.format_exc())
    finally:
        job.finished_at = int(time.time())


def clear_all() -> None:
    """清空所有任务（测试用）。"""
    _JOBS.clear()


__all__ = [
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_RUNNING",
    "STATE_SUCCESS",
    "cancel_job",
    "clear_all",
    "create_job",
    "find_running",
    "get_job",
    "list_jobs",
    "start_offline_job",
]
