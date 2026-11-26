"""数据同步双通道分流器与溢出暂存队列。

从 :mod:`app.services.watch_manager` 拆出的事件分流与溢出回灌模块：
- 队列满不丢：先落溢出暂存字典（按路径去重），等 worker 空闲回灌；
- 去重原子性：查 ``inflight`` → ``put_nowait`` → 登记之间无 ``await``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.upload_filter import relative_to_root
from app.services.watch_gate import WatchItem

#: 溢出暂存默认上限（条）——超过才真丢（并计数）。
OVERFLOW_LIMIT = 5000


def make_lane_dispatcher(
    *,
    task_id: int,
    root: Path,
    large_queue: asyncio.Queue[WatchItem],
    small_queue: asyncio.Queue[WatchItem],
    threshold: int,
    inflight_map: dict[int, set[str]],
    on_overflow: Callable[[int, str, Path, int], None],
) -> Any:
    """构造投递回调：把稳态文件**按大小分流**进两条队列。"""

    async def _dispatch(path: Path, size: int) -> None:
        rel = relative_to_root(path, root)
        inflight = inflight_map.setdefault(task_id, set())
        if rel in inflight:
            return
        queue = small_queue if size < threshold else large_queue
        try:
            queue.put_nowait(WatchItem(path=path, size=size))
        except asyncio.QueueFull:
            on_overflow(task_id, rel, path, size)
            return
        inflight.add(rel)

    return _dispatch


def stash_overflow(
    *,
    task_id: int,
    rel: str,
    path: Path,
    size: int,
    overflow_map: dict[int, dict[str, tuple[Path, int]]],
    runtime: Any,
    limit: int = OVERFLOW_LIMIT,
) -> None:
    """队列满 → 溢出暂存（按路径去重，不阻塞监听）。"""
    box = overflow_map.setdefault(task_id, {})
    if rel in box:
        return
    if len(box) >= limit:
        runtime.dropped += 1
        runtime.log(
            f"⚠️ 溢出暂存已满（{limit}），丢弃：{path.name}"
            f"（累计丢 {runtime.dropped}；可稍后「立即全量同步」补齐）"
        )
        return
    box[rel] = (path, size)
    runtime.overflow = len(box)
    runtime.log(f"⏸ 队列已满，溢出暂存：{rel}（待空闲自动回灌，暂存 {len(box)}）")


def drain_overflow(
    *,
    task_id: int,
    overflow_map: dict[int, dict[str, tuple[Path, int]]],
    large_queues: dict[int, asyncio.Queue[WatchItem]],
    small_queues: dict[int, asyncio.Queue[WatchItem]],
    inflight_map: dict[int, set[str]],
    threshold: int,
    runtime: Any,
) -> None:
    """把溢出暂存回灌队列（纯同步，无 await）。"""
    box = overflow_map.get(task_id)
    if not box:
        return
    large_queue = large_queues.get(task_id)
    small_queue = small_queues.get(task_id)
    inflight = inflight_map.setdefault(task_id, set())
    for rel in list(box):
        if rel in inflight:
            del box[rel]
            continue
        path, size = box[rel]
        queue = small_queue if size < threshold else large_queue
        if queue is None:
            continue
        try:
            queue.put_nowait(WatchItem(path=path, size=size))
        except asyncio.QueueFull:
            break
        del box[rel]
        inflight.add(rel)
    runtime.overflow = len(box)
