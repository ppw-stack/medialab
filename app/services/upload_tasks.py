"""上传任务的 CRUD 与运行态。

**运行态是内存态**（与扫描 / 迁移任务同款结构）：进度丢了重跑即可——
幂等靠云端目录（同名 + 同大小 = 已同步），不依赖本地状态。

**任务配置在 DB**（``upload_tasks`` 表），**运行态在内存**
（``_TASKS`` 字典）。DB 是真相源：重启后按 DB 重新拉起监听器。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import UploadTask as UploadTaskRow
from app.db.session import session_scope
from app.services.upload_paths import PathGuardError, resolve_watch_dir

logger = get_logger(__name__)

#: 每个任务保留的日志行数（环形缓冲）
MAX_LOGS = 300

#: 状态
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"


class UploadTaskError(Exception):
    """上传任务操作失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


@dataclass
class UploadRuntime:
    """一个任务的运行时状态。"""

    task_id: int
    #: 监听器是否在跑
    listening: bool = False
    #: 监听器协程
    watcher: Any = None
    #: 上传队列
    queue: asyncio.Queue[Path] | None = None
    #: 上传 worker 协程
    workers: list[asyncio.Task[None]] = field(default_factory=list)
    #: 正在上传的文件
    current: str = ""
    #: 最近事件时间
    last_event_at: int = 0
    #: 累计统计
    total_uploaded: int = 0
    total_failed: int = 0
    total_bytes: int = 0
    #: 日志（环形）
    logs: list[str] = field(default_factory=list)
    #: 上次错误
    error: str | None = None
    #: 是否正在跑「立即同步」
    syncing: bool = False

    def log(self, message: str) -> None:
        """追加一行日志。"""
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > MAX_LOGS:
            del self.logs[: len(self.logs) - MAX_LOGS]

    def to_dict(self) -> dict[str, Any]:
        """序列化给 API。"""
        return {
            "task_id": self.task_id,
            "listening": self.listening,
            "current": self.current,
            "last_event_at": self.last_event_at,
            "total_uploaded": self.total_uploaded,
            "total_failed": self.total_failed,
            "total_bytes": self.total_bytes,
            "logs": self.logs[-MAX_LOGS:],
            "error": self.error,
            "syncing": self.syncing,
        }


#: 运行态表（task_id → UploadRuntime）
_RUNTIMES: dict[int, UploadRuntime] = {}


def get_runtime(task_id: int) -> UploadRuntime:
    """取（或建）某个任务的运行态。"""
    runtime = _RUNTIMES.get(task_id)
    if runtime is None:
        runtime = UploadRuntime(task_id=task_id)
        _RUNTIMES[task_id] = runtime
    return runtime


def all_runtimes() -> dict[int, UploadRuntime]:
    """全部运行态。"""
    return _RUNTIMES


def clear_all() -> None:
    """清空运行态（测试用）。"""
    _RUNTIMES.clear()


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


async def list_tasks() -> list[UploadTaskRow]:
    """列出所有上传任务。"""
    async with session_scope() as db:
        rows = (
            await db.execute(select(UploadTaskRow).order_by(UploadTaskRow.id))
        ).scalars().all()
        return list(rows)


async def get_task(db: Any, task_id: int) -> UploadTaskRow:
    """取一个任务。"""
    row: UploadTaskRow | None = (
        await db.execute(select(UploadTaskRow).where(UploadTaskRow.id == task_id))
    ).scalar_one_or_none()
    if row is None:
        raise UploadTaskError(
            f"上传任务不存在：{task_id}", status_code=404, kind="not_found"
        )
    return row


async def create_task(
    db: Any,
    *,
    name: str,
    driver_id: int,
    target_file_id: str,
    watch_root: str,
    include_subdirs: bool = True,
    enabled: bool = True,
    concurrency: int = 1,
    target_path: str = "",
    delete_after_upload: bool = False,
) -> UploadTaskRow:
    """创建上传任务。

    Raises:
        UploadTaskError: 存储源不是光鸭 / 路径不在白名单 / 目录不存在。
    """
    from app.db.models import Driver

    driver = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).scalar_one_or_none()
    if driver is None:
        raise UploadTaskError(f"存储源不存在：{driver_id}", status_code=404, kind="not_found")
    if driver.type != "guangya":
        raise UploadTaskError(
            f"上传目标必须是光鸭盘（当前：{driver.type}）", status_code=400, kind="config"
        )

    watch_dir = _validate_watch_dir(watch_root)

    now = int(time.time())
    row = UploadTaskRow(
        name=name,
        driver_id=driver_id,
        target_file_id=target_file_id,
        target_path=target_path or None,
        watch_root=watch_root,
        include_subdirs=include_subdirs,
        enabled=enabled,
        delete_after_upload=delete_after_upload,
        concurrency=max(1, int(concurrency)),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "创建上传任务 id=%s 监听=%s → 光鸭 %s",
        row.id,
        watch_dir,
        target_file_id,
    )
    return row


async def update_task(db: Any, task_id: int, **fields: Any) -> UploadTaskRow:
    """修改任务。

    Raises:
        UploadTaskError: 任务不存在 / 路径非法。
    """
    row = await get_task(db, task_id)

    watch_root = fields.get("watch_root", row.watch_root)
    if "watch_root" in fields:
        _validate_watch_dir(watch_root)

    for key in (
        "name",
        "driver_id",
        "target_file_id",
        "target_path",
        "watch_root",
        "include_subdirs",
        "enabled",
        "delete_after_upload",
        "concurrency",
    ):
        if key in fields and fields[key] is not None:
            setattr(row, key, fields[key])

    row.updated_at = int(time.time())
    await db.commit()
    await db.refresh(row)
    return row


async def delete_task(db: Any, task_id: int) -> None:
    """删除任务（先停监听，再删行）。"""
    await get_task(db, task_id)
    await stop_task(task_id)
    row = await get_task(db, task_id)
    await db.delete(row)
    await db.commit()
    logger.info("删除上传任务 id=%s", task_id)


def _validate_watch_dir(watch_root: str) -> Any:
    """校验监听目录（绝对路径 + 存在）。

    Raises:
        UploadTaskError: 路径非法。
    """
    try:
        return resolve_watch_dir(watch_root)
    except PathGuardError as exc:
        raise UploadTaskError(str(exc), status_code=400, kind="config") from exc


async def stop_task(task_id: int) -> None:
    """停止某个任务的监听（不断任务本身）。"""
    from app.services.watch_manager import stop_watcher

    await stop_watcher(task_id)
    runtime = get_runtime(task_id)
    runtime.listening = False


__all__ = [
    "MAX_LOGS",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "UploadRuntime",
    "UploadTaskError",
    "all_runtimes",
    "clear_all",
    "create_task",
    "delete_task",
    "get_runtime",
    "get_task",
    "list_tasks",
    "stop_task",
    "update_task",
]
