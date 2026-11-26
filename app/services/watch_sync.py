"""数据同步全量扫描补偿执行器。

从 :mod:`app.services.watch_manager` 拆出的全量同步路径：
- 边扫描边上传，避免 CIFS 目录遍历耗时导致长时间无反馈；
- 与事件流通道共享 ``inflight`` 集合防同文件双传；
- 与大文件通道共享 ``sync_lock`` 串行访问 ``CloudDirIndex``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.upload_filter import aiter_local_files
from app.services.uploader import (
    RESULT_CONFLICT,
    RESULT_FAILED,
    RESULT_OVERWRITTEN,
    RESULT_UNCHANGED,
    RESULT_UPLOADED,
    SyncContext,
    SyncOutcome,
)

UploadFn = Callable[..., Awaitable[SyncOutcome]]
ProgressFactory = Callable[[Any, str, int], Any]


async def run_full_sync(
    *,
    task_id: int,
    runtime: Any,
    watcher: Any,
    ctx: SyncContext,
    lock: asyncio.Lock | None,
    inflight: set[str],
    overflow_box: dict[str, tuple[Path, int]],
    upload_fn: UploadFn,
    make_progress_fn: ProgressFactory,
) -> dict[str, Any]:
    """执行一轮边扫描边同步的全量补偿。"""
    runtime.syncing = True
    runtime.log("开始全量同步…")
    try:
        uploaded = overwritten = unchanged = failed = conflicts = 0
        total_bytes = 0
        deleted_local = 0
        skipped_inflight = 0
        index = 0
        async for path in aiter_local_files(
            watcher.root, include_subdirs=watcher.include_subdirs
        ):
            index += 1
            rel = watcher.relative(path)
            if rel in inflight:
                skipped_inflight += 1
                runtime.log(f"⏭️ [{index}] 跳过（事件队列处理中）：{rel}")
                continue
            inflight.add(rel)
            overflow_box.pop(rel, None)
            runtime.current = rel
            runtime.log(f"⏳ [{index}] 处理中：{rel}")
            try:
                try:
                    stat = await asyncio.to_thread(path.stat)
                    file_size = stat.st_size
                except OSError as exc:
                    # CIFS 上 stat 失败通常是权限 / 挂载断开 / 并发删除，与
                    # 「文件确实是 0 字节」是两回事——静默降级会掩盖真实原因，
                    # 且后续上传与限速计算都会失真。显式降级 + 留痕。
                    runtime.log(f"⚠️ [{index}] 读取文件大小失败：{rel}（{exc}）")
                    file_size = 0
                if lock is not None:
                    async with lock:
                        outcome = await upload_fn(
                            task_id,
                            ctx,
                            path,
                            rel,
                            file_size,
                            on_progress=make_progress_fn(runtime, rel, file_size),
                        )
                else:
                    outcome = await upload_fn(
                        task_id,
                        ctx,
                        path,
                        rel,
                        file_size,
                        on_progress=make_progress_fn(runtime, rel, file_size),
                    )
            finally:
                inflight.discard(rel)
            if outcome.local_deleted:
                deleted_local += 1
            if outcome.status == RESULT_UPLOADED:
                uploaded += 1
                total_bytes += outcome.size
                runtime.total_uploaded += 1
                runtime.total_bytes += outcome.size
                runtime.log(
                    f"✅ [{index}] 已上传：{rel}"
                    f"（{outcome.size / 1024**2:.1f} MB）"
                    + ("（本地删除中）" if outcome.local_deleted else "")
                )
            elif outcome.status == RESULT_OVERWRITTEN:
                overwritten += 1
                total_bytes += outcome.size
                runtime.total_uploaded += 1
                runtime.total_bytes += outcome.size
                runtime.log(
                    f"♻️ [{index}] 已覆盖：{rel}"
                    f"（{outcome.size / 1024**2:.1f} MB）"
                    + ("（本地删除中）" if outcome.local_deleted else "")
                )
            elif outcome.status == RESULT_UNCHANGED:
                unchanged += 1
                if outcome.local_deleted:
                    deleted_local += 1
                note = (
                    "（本地删除中）"
                    if outcome.local_deleted
                    else (f"（{outcome.reason}）" if outcome.reason else "")
                )
                runtime.log(f"⏭️ [{index}] 已存在：{rel}{note}")
            elif outcome.status == RESULT_CONFLICT:
                conflicts += 1
                runtime.total_failed += 1
                runtime.log(f"⚠️ [{index}] 冲突：{rel}（{outcome.reason}）")
            elif outcome.status == RESULT_FAILED:
                failed += 1
                runtime.total_failed += 1
                runtime.log(f"❌ [{index}] 失败：{rel}（{outcome.reason}）")
        runtime.current = ""

        runtime.log(
            f"全量同步完成：共 {index} 个文件，上传 {uploaded}，覆盖 {overwritten}，"
            f"已存在 {unchanged}，失败 {failed}，冲突 {conflicts}"
            + (f"，本地已安排删除 {deleted_local}" if deleted_local else "")
            + (f"，在传跳过 {skipped_inflight}" if skipped_inflight else "")
        )
        return {
            "uploaded": uploaded,
            "overwritten": overwritten,
            "unchanged": unchanged,
            "failed": failed,
            "conflicts": conflicts,
            "deleted_local": deleted_local,
            "uploaded_bytes": total_bytes,
            "skipped_inflight": skipped_inflight,
        }
    finally:
        runtime.syncing = False
