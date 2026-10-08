"""调度作业注册（M2）。

作业频率遵循文档默认值，并且**错峰**触发——所有作业同一秒启动会在
空闲时制造 CPU 尖峰，违反 §1.3 资源红线（CPU < 3%）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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