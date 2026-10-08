"""调度作业注册（M2）。

作业频率遵循文档默认值，并且**错峰**触发——所有作业同一秒启动会在
空闲时制造 CPU 尖峰，违反 §1.3 资源红线（CPU < 3%）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.free_seed_hunter import FreeSeedHunter
    from app.services.window_watcher import WindowWatcher

from app.core.logging import get_logger
from app.services.factory import AdapterFactory
from app.services.inspect_job import inspect_and_alert
from app.services.inspector import INSPECT_INTERVAL
from app.services.scheduler import JobSpec, scheduler_center
from app.services.seed_engine import SeedEngine

logger = get_logger(__name__)

#: 辅种默认间隔 60 分钟（FR-2.2），下限 15 分钟由 FeedFetcher 强制
SEED_INTERVAL = 3600
#: 队列重放间隔 5 分钟（崩溃恢复，§6）
QUEUE_INTERVAL = 300


def register_jobs(
    engine: SeedEngine, queue_replay: Callable[[], Awaitable[int]]
) -> None:
    """注册 M2 的周期作业。

    ``offset_seconds`` 让作业错峰：辅种在第 17 秒、队列在第 43 秒，
    避免与未来的巡检/清理作业撞在一起。
    """
    scheduler_center.register(
        JobSpec(
            name="cross_seed",
            func=lambda: _swallow(engine.run_all()),
            interval_seconds=SEED_INTERVAL,
            offset_seconds=17,
        )
    )
    scheduler_center.register(
        JobSpec(
            name="queue_replay",
            func=lambda: _swallow(queue_replay()),
            interval_seconds=QUEUE_INTERVAL,
            offset_seconds=43,
        )
    )
    logger.info("已注册 M2 周期作业：cross_seed / queue_replay")


def register_inspect_job(factory: AdapterFactory) -> None:
    """注册 M3 巡检作业（FR-4.1，默认 15 分钟一轮）。"""
    scheduler_center.register(
        JobSpec(
            name="inspect",
            func=lambda: _swallow(inspect_and_alert(factory)),
            interval_seconds=INSPECT_INTERVAL,
            offset_seconds=5,
        )
    )
    logger.info("已注册 M3 周期作业：inspect（%ds）", INSPECT_INTERVAL)


async def _swallow(result: Awaitable[object]) -> None:
    """作业返回值对调度器无意义；仅确保 await 完成。"""
    await result

#: FR-3.1 默认 10 分钟
FREE_SEED_INTERVAL = 600
#: FR-3.4 看护默认 60 秒（按紧迫度内部动态再调；这是上限节奏）
WATCH_INTERVAL = 60



def register_free_seed_jobs(
    engine: FreeSeedHunter, watcher: WindowWatcher
) -> None:
    """注册 M4 免费种作业（FR-3 调度：10min 抓取 + 1min 看护）。"""
    scheduler_center.register(
        JobSpec(
            name="free_seed_poll",
            func=lambda: _swallow(engine.run_all()),
            interval_seconds=FREE_SEED_INTERVAL,
            offset_seconds=29,
        )
    )
    scheduler_center.register(
        JobSpec(
            name="window_watch",
            func=lambda: _swallow(watcher.watch_active()),
            interval_seconds=WATCH_INTERVAL,
            offset_seconds=11,
        )
    )
    logger.info(
        "已注册 M4 周期作业：free_seed_poll(%ds) / window_watch(%ds)",
        FREE_SEED_INTERVAL,
        WATCH_INTERVAL,
    )
