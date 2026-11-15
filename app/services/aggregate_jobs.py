"""聚合归拢任务管理（内存态，照 :mod:`app.services.scan_jobs` 的模式）。

**为什么需要**：手动「立即归拢」若同步执行，单文件传输要几分钟，反代 /
网关的 60s 超时会**掐断请求**（``drivers.py`` 的 scan 路由注释里记过同款
坑），且用户只能看转圈、没有进度。故改**后台任务 + 轮询**：

    前端 POST /aggregate/groups/{id}/sync   → 立即拿到 job_id
    后端 asyncio.create_task 跑归拢，进度写进本模块的内存表
    前端 GET  /aggregate/sync-jobs/{id}     → 每 2s 拉一次，看状态与明细

**内存态**：重启即丢。单用户自用足够——归拢幂等（同名同大小跳过、
认领残留），丢了重跑即可。

**取消**：撤销后台 task（正在传输的文件会被打断——云下载任务仍在服务端
跑，落盘后由「认领残留」接住，不浪费额度）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 每个任务保留的最大日志行数（环形缓冲）
MAX_LOGS = 500
#: 内存里最多保留多少个历史任务
MAX_JOBS = 50

#: 任务状态
STATE_RUNNING = "running"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


@dataclass
class AggregateSyncJob:
    """一次手动归拢的运行状态。"""

    id: str
    group_id: int
    group_name: str
    #: 单调递增序号——``started_at`` 只到秒，同秒内排序会不稳定
    seq: int = 0
    state: str = STATE_RUNNING
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None

    # -- 归拢结果（与 AggregateSyncResult 同构）--
    synced: int = 0
    cleaned: int = 0
    paused: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)

    #: 实时日志（环形，最多 MAX_LOGS 条）
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    #: 后台 task（不参与序列化）
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def log(self, message: str) -> None:
        """追加一行日志（带时间戳，环形裁剪）。"""
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > MAX_LOGS:
            del self.logs[: len(self.logs) - MAX_LOGS]

    def to_dict(self) -> dict[str, Any]:
        """序列化给 API。"""
        return {
            "id": self.id,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": (self.finished_at or int(time.time())) - self.started_at,
            "synced": self.synced,
            "cleaned": self.cleaned,
            "paused": self.paused,
            "failed": self.failed,
            "skipped": self.skipped,
            "details": list(self.details),
            "logs": list(self.logs),
            "error": self.error,
        }

    @property
    def done(self) -> bool:
        return self.state != STATE_RUNNING


#: 任务表（job_id → AggregateSyncJob）
_JOBS: dict[str, AggregateSyncJob] = {}
_LOCK = asyncio.Lock()
#: 单调递增序号（同一秒内多个任务也能稳定排序）
_SEQ = 0


def create_job(group_id: int, group_name: str) -> AggregateSyncJob:
    """创建一个归拢任务（同步，不启 task）。"""
    global _SEQ
    _SEQ += 1
    job = AggregateSyncJob(
        id=uuid.uuid4().hex[:12],
        group_id=group_id,
        group_name=group_name,
        seq=_SEQ,
    )
    _JOBS[job.id] = job
    _evict_old()
    return job


async def get_job(job_id: str) -> AggregateSyncJob | None:
    """按 id 取任务。"""
    async with _LOCK:
        return _JOBS.get(job_id)


async def list_jobs(group_id: int | None = None) -> list[AggregateSyncJob]:
    """列出任务（最新在前）。"""
    async with _LOCK:
        jobs = list(_JOBS.values())
    if group_id is not None:
        jobs = [j for j in jobs if j.group_id == group_id]
    return sorted(jobs, key=lambda j: j.seq, reverse=True)


async def find_running(group_id: int) -> AggregateSyncJob | None:
    """找该组正在跑的任务（避免重复触发）。"""
    async with _LOCK:
        for job in _JOBS.values():
            if job.group_id == group_id and job.state == STATE_RUNNING:
                return job
    return None


def _evict_old() -> None:
    """超出上限时清掉最旧的**已完成**任务（调用方无需持锁，纯内存操作）。"""
    if len(_JOBS) <= MAX_JOBS:
        return
    finished = [j for j in _JOBS.values() if j.done]
    finished.sort(key=lambda j: j.seq)
    for job in finished[: len(_JOBS) - MAX_JOBS]:
        _JOBS.pop(job.id, None)


async def cancel_job(job_id: str) -> bool:
    """取消任务（停掉后台 task）。"""
    job = await get_job(job_id)
    if job is None or job.done:
        return False
    if job.task is not None:
        job.task.cancel()
    job.state = STATE_CANCELLED
    job.finished_at = int(time.time())
    job.log("已取消")
    return True


def clear_all() -> None:
    """清空任务表（测试用）。"""
    _JOBS.clear()


# --------------------------------------------------------------------------
# 启动入口
# --------------------------------------------------------------------------


async def start_sync_job(group_id: int, group_name: str) -> AggregateSyncJob:
    """创建并启动一个归拢任务（**立即返回**，归拢在后台跑）。

    调用方（路由层）负责先校验组存在（404）与无同组任务在跑（409）。
    """
    from app.services.aggregate_sync import sync_group_now

    job = create_job(group_id, group_name)

    async def _run() -> None:
        try:
            job.log(f"开始归拢组「{group_name}」")
            result = await sync_group_now(group_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.state = STATE_FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = int(time.time())
            job.log(f"❌ 归拢失败：{exc}")
            logger.exception("聚合归拢任务失败 group=%s", group_name)
            return
        job.synced = result.synced
        job.cleaned = result.cleaned
        job.paused = result.paused
        job.failed = result.failed
        job.skipped = result.skipped
        job.details = list(result.details)
        for line in result.details:
            job.log(line)
        job.state = STATE_SUCCESS
        job.finished_at = int(time.time())
        job.log(
            f"✅ 归拢完成：成功 {result.synced} / 清理 {result.cleaned} / "
            f"暂停 {result.paused} / 失败 {result.failed} / 跳过 {result.skipped}"
        )

    job.task = asyncio.create_task(_run())
    return job

