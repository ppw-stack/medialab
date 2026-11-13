"""迁移任务的后台执行（内存态）。

与 :mod:`app.services.scan_jobs` / :mod:`app.services.incremental_jobs`
同款结构：后台 ``asyncio.Task`` + 内存进度表 + 环形日志。

**为什么不需要数据库表**：光鸭目标目录就是唯一真相源——重跑时「同名 +
同大小」会被判定为已存在并跳过。所以参数随请求传入，进度丢了重跑即可。
（代价：容器重启会丢掉本轮进度，但不会丢已迁移的文件。）
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
from app.services import migration
from app.services.migration import MigrationError

logger = get_logger(__name__)

#: 每个任务保留的日志行数（环形缓冲）
MAX_LOGS = 500
#: 内存里最多保留多少个历史任务
MAX_JOBS = 20
#: 失败 / 冲突明细上限（迁移可能涉及几万个文件）
MAX_DETAILS = 100
STATE_RUNNING = "running"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
#: 额度耗尽暂停（云添加额度当日不恢复，次日重跑即可续传）
STATE_PAUSED = "paused"


@dataclass(frozen=True)
class MigrationParams:
    """一次迁移的入参（前端表单原样传过来）。"""

    source_driver_id: int
    source_cid: str
    target_driver_id: int
    target_file_id: str
    source_path: str = ""
    target_path: str = ""
    #: 迁移阈值：小于它的文件不进队列（MB），0 = 不过滤
    min_size_mb: int = 0
    #: 体积上限（MB），0 = 不限
    max_size_mb: int = 0
    #: 光鸭上同时进行的离线任务数
    concurrency: int = 2
    #: 两次提交给光鸭之间的最小间隔（秒）——错开提交，避开 115 下载频控
    submit_interval_sec: float = 60.0
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #: 「搬完清理源」——迁移成功后删除源端文件（**默认关**）
    #:
    #: 只删**目标侧已确认落盘同名同大小**的文件；删除走回收站（可恢复）；
    #: **仅光鸭源支持**（115 驱动无删除能力，会被忽略并在日志里说明）。
    delete_source: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_driver_id": self.source_driver_id,
            "source_cid": self.source_cid,
            "source_path": self.source_path,
            "target_driver_id": self.target_driver_id,
            "target_file_id": self.target_file_id,
            "target_path": self.target_path,
            "min_size_mb": self.min_size_mb,
            "max_size_mb": self.max_size_mb,
            "concurrency": self.concurrency,
            "submit_interval_sec": self.submit_interval_sec,
            "exclude_disc": self.exclude_disc,
            "delete_source": self.delete_source,
        }


@dataclass
class MigrationJob:
    """一次迁移任务的运行状态。"""

    id: str
    params: MigrationParams
    #: 单调递增序号——``started_at`` 只到秒，同秒内排序会不稳定
    seq: int = 0
    state: str = STATE_RUNNING
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None

    #: 后端反解出来的源目录绝对路径（展示用）
    source_path: str = ""
    #: 当前处理的文件
    current_path: str = ""
    #: 已遍历目录数 / 已发现待迁移文件数
    dirs_walked: int = 0
    discovered: int = 0
    #: 已提交、仍在光鸭上跑的任务数
    in_flight: int = 0
    #: 在途任务明细：``taskId → {完整路径, 文件名, 大小}``——前端展示在途列表
    #:
    #: **不带进度百分比**：光鸭的 ``progress`` 字段语义不可靠（实测非媒体源
    #: 甚至拿不到该字段），展示只会误导。完成判定走「活跃列表消失 + 同名
    #: 同大小校验」，与该字段无关。
    in_flight_items: dict[str, dict[str, Any]] = field(default_factory=dict)

    success: int = 0
    failed: int = 0
    skipped_existing: int = 0
    skipped_inflight: int = 0
    conflicts: int = 0
    #: 被过滤掉的（低于阈值 / 超上限 / 非视频 / 原盘）
    filtered_small: int = 0
    filtered_large: int = 0
    filtered_ext: int = 0
    #: 已成功迁移的字节数
    migrated_bytes: int = 0
    #: 已清理的源文件数（「搬完清理源」开启时）
    deleted_source: int = 0
    #: 启动时检测到的上一轮在途任务数
    legacy_inflight: int = 0

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

    def pause(self, reason: str) -> None:
        """暂停作业（云添加额度耗尽）。

        **不是失败**——已迁成功的文件不受影响，幂等设计保证次日重跑会自动
        从断点续上（目标目录即真相源）。
        """
        self.state = STATE_PAUSED
        self.error = reason
        self.finished_at = int(time.time())
        self.log(f"⏸️ 已暂停：{reason}")

    def count_filtered(self, reason: str) -> None:
        """累计被过滤器丢弃的文件数。"""
        if reason == migration.FILTER_SMALL:
            self.filtered_small += 1
        elif reason == migration.FILTER_LARGE:
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
                    "local_gb": round(entry.size / 1024**3, 2),
                    "remote_gb": round(remote_size / 1024**3, 2),
                }
            )

    # -- 在途任务 --

    def track_task(self, task_id: str, path: str, size: int) -> None:
        """登记一个在途任务。

        ``path`` 是**完整路径**（含目录）——日志里只看文件名分不清是哪部剧，
        在途列表尤其需要（同一集可能有多个版本在不同目录）。
        """
        self.in_flight_items[task_id] = {
            "path": path,
            #: 文件名（从完整路径截取，用于紧凑展示）
            "name": path.rsplit("/", 1)[-1],
            "size": size,
        }

    def untrack_task(self, task_id: str) -> None:
        """移除一个已结束的任务。"""
        self.in_flight_items.pop(task_id, None)

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
            "source_path": self.source_path,
            "current_path": self.current_path,
            "dirs_walked": self.dirs_walked,
            "discovered": self.discovered,
            "in_flight": self.in_flight,
            "in_flight_items": self.in_flight_items,
            "success": self.success,
            "failed": self.failed,
            "skipped_existing": self.skipped_existing,
            "skipped_inflight": self.skipped_inflight,
            "conflicts": self.conflicts,
            "filtered_small": self.filtered_small,
            "filtered_large": self.filtered_large,
            "filtered_ext": self.filtered_ext,
            "migrated_bytes": self.migrated_bytes,
            "deleted_source": self.deleted_source,
            "legacy_inflight": self.legacy_inflight,
            "failed_items": self.failed_items,
            "conflict_items": self.conflict_items,
            "logs": self.logs[-MAX_LOGS:],
            "error": self.error,
        }

    @property
    def done(self) -> bool:
        return self.state != STATE_RUNNING


#: 任务表（job_id → MigrationJob）
_JOBS: dict[str, MigrationJob] = {}
_LOCK = asyncio.Lock()
_SEQ = 0


async def create_job(params: MigrationParams) -> MigrationJob:
    """创建一个迁移任务（同步，不启 task）。"""
    global _SEQ
    _SEQ += 1
    job = MigrationJob(id=uuid.uuid4().hex[:12], params=params, seq=_SEQ)
    async with _LOCK:
        _JOBS[job.id] = job
        _evict_old()
    return job


async def get_job(job_id: str) -> MigrationJob | None:
    """按 id 取任务。"""
    async with _LOCK:
        return _JOBS.get(job_id)


async def list_jobs() -> list[MigrationJob]:
    """列出任务（最新在前）。"""
    async with _LOCK:
        jobs = list(_JOBS.values())
    return sorted(jobs, key=lambda j: j.seq, reverse=True)


async def find_running() -> MigrationJob | None:
    """有没有正在跑的迁移（全局只允许一个，避免互相抢限速配额）。"""
    async with _LOCK:
        for job in _JOBS.values():
            if job.state == STATE_RUNNING:
                return job
    return None


async def start_migration_job(params: MigrationParams) -> MigrationJob:
    """创建并在后台启动迁移任务。"""
    job = await create_job(params)
    job.log("开始迁移")
    job.task = asyncio.create_task(_run_job(job))
    return job


async def cancel_job(job_id: str) -> bool:
    """取消任务（停掉后台 task；已提交的离线任务不撤回）。"""
    job = await get_job(job_id)
    if job is None or job.done:
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
    finished = [j for j in _JOBS.values() if j.done]
    finished.sort(key=lambda j: j.seq)
    for job in finished[: len(_JOBS) - MAX_JOBS]:
        _JOBS.pop(job.id, None)


async def _run_job(job: MigrationJob) -> None:
    """后台执行迁移。"""
    try:
        # 校验存储源（顺手把名字写进日志）
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
            raise MigrationError("存储源不存在")
        job.log(f"源：{source.name}　目标：{target.name}")

        await migration.run_migration(job)

        # 额度耗尽时 worker 已把状态置为 paused——**别覆盖**成 success
        if job.state == STATE_RUNNING:
            job.state = STATE_SUCCESS
            job.log(
                f"完成：成功 {job.success} 个（{job.migrated_bytes / 1024**3:.2f} GB），"
                f"已存在跳过 {job.skipped_existing}，在途跳过 {job.skipped_inflight}，"
                f"冲突 {job.conflicts}，失败 {job.failed}"
                + (f"，清理源 {job.deleted_source}" if job.deleted_source else "")
            )
            logger.info("迁移任务 %s 完成", job.id)
        else:
            logger.info("迁移任务 %s 结束（状态=%s）", job.id, job.state)
    except asyncio.CancelledError:
        job.state = STATE_CANCELLED
        job.log("任务被取消")
        raise
    except MigrationError as exc:
        job.state = STATE_FAILED
        job.error = str(exc)
        job.log(f"迁移失败：{exc}")
        logger.warning("迁移任务 %s 失败：%s", job.id, exc)
    except Exception as exc:
        job.state = STATE_FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.log(f"迁移异常：{job.error}")
        logger.error("迁移任务 %s 异常：%s", job.id, traceback.format_exc())
    finally:
        job.finished_at = int(time.time())


def clear_all() -> None:
    """清空任务表（测试用）。"""
    _JOBS.clear()
