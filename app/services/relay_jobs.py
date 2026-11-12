"""转存任务的后台执行（内存态）。

与 :mod:`app.services.migration_jobs` 同款结构：后台 ``asyncio.Task`` +
内存进度表 + 环形日志。

**为什么不需要数据库表**：目标目录就是唯一真相源——重跑时「同名 + 同大小」
会被判定为已存在并跳过。所以参数随请求传入，进度丢了重跑即可。
（代价：容器重启会丢掉本轮进度，但不会丢已转存的文件。）
"""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import session_scope
from app.services import cloud_relay
from app.services.cloud_relay import RelayError
from app.services.migration import FILTER_LARGE, FILTER_SMALL

logger = get_logger(__name__)

#: 每个任务保留的日志行数（环形缓冲）
MAX_LOGS = 500
#: 内存里最多保留多少个历史任务
MAX_JOBS = 20
#: 失败 / 冲突明细上限（转存可能涉及几万个文件）
MAX_DETAILS = 100
STATE_RUNNING = "running"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


@dataclass(frozen=True)
class RelayParams:
    """一次转存的入参（前端表单原样传过来）。"""

    #: 源光鸭存储源 id
    source_driver_id: int
    #: 源目录 fileId（光鸭按 id 定位目录）
    source_file_id: str
    #: 目标光鸭存储源 id
    target_driver_id: int
    #: 目标目录 fileId
    target_file_id: str
    #: 体积阈值（MB）：小于它不转存，0 = 不过滤
    min_size_mb: int = 0
    #: 体积上限（MB），0 = 不限
    max_size_mb: int = 0
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #: **搬完清理源端**（默认关）——删数据不可逆，故显式开关
    delete_source: bool = False
    #: 建分享的最小间隔（秒）——防「同账号连续建分享」被限流
    submit_interval_sec: float = cloud_relay.DEFAULT_SUBMIT_INTERVAL_SEC

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_driver_id": self.source_driver_id,
            "source_file_id": self.source_file_id,
            "target_driver_id": self.target_driver_id,
            "target_file_id": self.target_file_id,
            "min_size_mb": self.min_size_mb,
            "max_size_mb": self.max_size_mb,
            "exclude_disc": self.exclude_disc,
            "delete_source": self.delete_source,
            "submit_interval_sec": self.submit_interval_sec,
        }


@dataclass
class RelayJob:
    """一次转存任务的运行状态。"""

    id: str
    params: RelayParams
    #: 单调递增序号——``started_at`` 只到秒，同秒内排序会不稳定
    seq: int = 0
    state: str = STATE_RUNNING
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None

    #: 源 / 目标存储源名（展示用）
    source_name: str = ""
    target_name: str = ""
    #: 当前处理的目录（相对源根）
    current_path: str = ""
    #: 已遍历目录数 / 已发现文件数
    dirs_walked: int = 0
    discovered: int = 0

    success: int = 0
    failed: int = 0
    #: 目标已有同名同大小 → 跳过
    skipped_existing: int = 0
    #: 同名但大小不同 → 冲突（不覆盖、不改名）
    conflicts: int = 0
    #: 被过滤掉的（低于阈值 / 超上限 / 非视频 / 原盘）
    filtered_small: int = 0
    filtered_large: int = 0
    filtered_ext: int = 0
    #: 已成功转存的字节数
    transferred_bytes: int = 0

    failed_items: list[dict[str, Any]] = field(default_factory=list)
    conflict_items: list[dict[str, Any]] = field(default_factory=list)

    logs: list[str] = field(default_factory=list)
    error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    # -- 记录 --

    def log(self, message: str) -> None:
        """追加一行日志（带时间戳，环形裁剪）。"""
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > MAX_LOGS:
            del self.logs[: len(self.logs) - MAX_LOGS]

    def count_filtered(self, reason: str) -> None:
        """累计被过滤器丢弃的文件数。"""
        if reason == FILTER_SMALL:
            self.filtered_small += 1
        elif reason == FILTER_LARGE:
            self.filtered_large += 1
        else:
            self.filtered_ext += 1

    def record_failed(self, entry: Any, reason: str) -> None:
        """记一个失败文件（明细有上限）。"""
        self.failed += 1
        if len(self.failed_items) < MAX_DETAILS:
            self.failed_items.append(
                {
                    "name": entry.name,
                    "path": entry.path,
                    "size_gb": round(entry.size / 1024**3, 2),
                    "reason": reason,
                }
            )

    def record_conflict(self, entry: Any, remote_size: int) -> None:
        """记一个「同名不同大小」的冲突（不覆盖、不重传）。"""
        self.conflicts += 1
        if len(self.conflict_items) < MAX_DETAILS:
            self.conflict_items.append(
                {
                    "name": entry.name,
                    "path": entry.path,
                    "source_gb": round(entry.size / 1024**3, 2),
                    "target_gb": round(remote_size / 1024**3, 2),
                }
            )

    # -- 状态 --

    def to_dict(self) -> dict[str, Any]:
        """序列化给 API。"""
        return {
            "id": self.id,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": (self.finished_at or int(time.time())) - self.started_at,
            "params": self.params.to_dict(),
            "source_name": self.source_name,
            "target_name": self.target_name,
            "current_path": self.current_path,
            "dirs_walked": self.dirs_walked,
            "discovered": self.discovered,
            "success": self.success,
            "failed": self.failed,
            "skipped_existing": self.skipped_existing,
            "conflicts": self.conflicts,
            "filtered_small": self.filtered_small,
            "filtered_large": self.filtered_large,
            "filtered_ext": self.filtered_ext,
            "transferred_bytes": self.transferred_bytes,
            "failed_items": self.failed_items,
            "conflict_items": self.conflict_items,
            "logs": self.logs[-MAX_LOGS:],
            "error": self.error,
        }

    @property
    def done(self) -> bool:
        return self.state != STATE_RUNNING


#: 任务表（job_id → RelayJob）
_JOBS: dict[str, RelayJob] = {}
_LOCK = asyncio.Lock()
_SEQ = 0


async def create_job(params: RelayParams) -> RelayJob:
    """创建一个转存任务（同步，不启 task）。"""
    global _SEQ
    _SEQ += 1
    job = RelayJob(id=uuid.uuid4().hex[:12], params=params, seq=_SEQ)
    async with _LOCK:
        _JOBS[job.id] = job
        _evict_old()
    return job


async def get_job(job_id: str) -> RelayJob | None:
    """按 id 取任务。"""
    async with _LOCK:
        return _JOBS.get(job_id)


async def list_jobs() -> list[RelayJob]:
    """列出任务（最新在前）。"""
    async with _LOCK:
        jobs = list(_JOBS.values())
    return sorted(jobs, key=lambda j: j.seq, reverse=True)


async def find_running() -> RelayJob | None:
    """有没有正在跑的转存（全局只允许一个，避免互相抢分享配额）。"""
    async with _LOCK:
        for job in _JOBS.values():
            if job.state == STATE_RUNNING:
                return job
    return None


async def start_relay_job(params: RelayParams) -> RelayJob:
    """创建并在后台启动转存任务。"""
    job = await create_job(params)
    job.log("开始转存")
    job.task = asyncio.create_task(_run_job(job))
    return job


async def cancel_job(job_id: str) -> bool:
    """取消任务（停掉后台 task；已提交的服务端保存任务不撤回）。"""
    job = await get_job(job_id)
    if job is None or job.done:
        return False
    if job.task is not None:
        job.task.cancel()
    job.state = STATE_CANCELLED
    job.finished_at = int(time.time())
    job.log("已取消（服务端已开始的保存任务不撤回）")
    return True


def _evict_old() -> None:
    """超出上限时清掉最旧的**已完成**任务。"""
    if len(_JOBS) <= MAX_JOBS:
        return
    finished = [j for j in _JOBS.values() if j.done]
    finished.sort(key=lambda j: j.seq)
    for job in finished[: len(_JOBS) - MAX_JOBS]:
        _JOBS.pop(job.id, None)


async def _run_job(job: RelayJob) -> None:
    """后台执行转存。"""
    try:
        async with session_scope() as db:
            source = (
                await db.execute(
                    select(Driver).where(Driver.id == job.params.source_driver_id)
                )
            ).scalar_one_or_none()
            target = (
                await db.execute(
                    select(Driver).where(Driver.id == job.params.target_driver_id)
                )
            ).scalar_one_or_none()

        if source is None or target is None:
            raise RelayError("存储源不存在")

        await cloud_relay.run_relay(job)

        job.state = STATE_SUCCESS
        job.log(
            f"完成：成功 {job.success} 个（{job.transferred_bytes / 1024**3:.2f} GB），"
            f"已存在跳过 {job.skipped_existing}，冲突 {job.conflicts}，"
            f"失败 {job.failed}"
        )
        logger.info("转存任务 %s 完成", job.id)
    except asyncio.CancelledError:
        job.state = STATE_CANCELLED
        job.log("任务被取消")
        raise
    except RelayError as exc:
        job.state = STATE_FAILED
        job.error = str(exc)
        job.log(f"转存失败：{exc}")
        logger.warning("转存任务 %s 失败：%s", job.id, exc)
    except Exception as exc:
        job.state = STATE_FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.log(f"转存异常：{job.error}")
        logger.error("转存任务 %s 异常：%s", job.id, traceback.format_exc())
    finally:
        job.finished_at = int(time.time())


def clear_all() -> None:
    """清空任务表（测试用）。"""
    _JOBS.clear()
