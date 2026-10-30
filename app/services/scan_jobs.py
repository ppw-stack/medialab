"""扫描任务管理（内存态）。

**为什么需要**：加了 1s/请求的限速后，扫一棵大树要几分钟。同步 HTTP 请求
让用户在这几分钟里只能看转圈——不知道扫到哪、成功多少、哪个目录出错；
反代/网关若有 60s 超时还会把请求掐断。

故改成**后台任务 + 轮询**：

    前端 POST /scan        → 立即拿到 job_id
    后端 asyncio.create_task 跑扫描，进度写进本模块的内存表
    前端 GET /scan-jobs/{id} → 每 2s 拉一次，画进度条 + 实时日志

**内存态**：重启即丢。单用户自用足够——扫描是幂等的，丢了重跑即可。

**日志环形缓冲**：只留最近 :data:`MAX_LOGS` 条，避免扫几万个文件时把
内存吃满。
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
class ScanJob:
    """一次扫描任务的运行状态。"""

    id: str
    driver_id: int
    driver_name: str
    #: 单调递增序号——``started_at`` 只到秒，同一秒内多个任务排序会不稳定
    seq: int = 0
    state: str = STATE_RUNNING
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None

    #: 当前正在处理的文件路径
    current_path: str = ""
    #: 已处理的文件数（含跳过）
    processed: int = 0
    #: 已生成的 strm 数
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    #: 附件（图片 / nfo / 字幕）同步结果
    attachment_synced: int = 0
    attachment_unchanged: int = 0
    attachment_failed: int = 0
    skipped_unknown: int = 0
    skipped_small: int = 0
    failed: int = 0
    #: 已遍历的目录数
    dirs: int = 0

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
            "driver_id": self.driver_id,
            "driver_name": self.driver_name,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": (self.finished_at or int(time.time())) - self.started_at,
            "current_path": self.current_path,
            "processed": self.processed,
            "dirs": self.dirs,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "attachment_synced": self.attachment_synced,
            "attachment_unchanged": self.attachment_unchanged,
            "attachment_failed": self.attachment_failed,
            "skipped_unknown": self.skipped_unknown,
            "skipped_small": self.skipped_small,
            "failed": self.failed,
            "logs": self.logs[-MAX_LOGS:],
            "error": self.error,
        }

    @property
    def done(self) -> bool:
        return self.state != STATE_RUNNING


#: 任务表（job_id → ScanJob）
_JOBS: dict[str, ScanJob] = {}
_LOCK = asyncio.Lock()
#: 单调递增序号（同一秒内多个任务也能稳定排序）
_SEQ = 0


def create_job(driver_id: int, driver_name: str) -> ScanJob:
    """创建一个扫描任务（同步，不启 task）。"""
    global _SEQ
    _SEQ += 1
    job = ScanJob(
        id=uuid.uuid4().hex[:12],
        driver_id=driver_id,
        driver_name=driver_name,
        seq=_SEQ,
    )
    _JOBS[job.id] = job
    _evict_old()
    return job


async def get_job(job_id: str) -> ScanJob | None:
    """按 id 取任务。"""
    async with _LOCK:
        return _JOBS.get(job_id)


async def list_jobs(driver_id: int | None = None) -> list[ScanJob]:
    """列出任务（最新在前）。"""
    async with _LOCK:
        jobs = list(_JOBS.values())
    if driver_id is not None:
        jobs = [j for j in jobs if j.driver_id == driver_id]
    return sorted(jobs, key=lambda j: j.seq, reverse=True)


async def find_running(driver_id: int) -> ScanJob | None:
    """找该存储源正在跑的任务（避免重复触发）。"""
    async with _LOCK:
        for job in _JOBS.values():
            if job.driver_id == driver_id and job.state == STATE_RUNNING:
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
