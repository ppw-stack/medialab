"""调度作业注册——仅保留 RSS 拉取两个作业。

辅种拉取（FR-2.2，每小时）和免费种抓取（FR-3.1，每 10 分钟）。
其余三条作业（queue_replay / window_watch / cleanup_aborts）随清理模块一并移除。
"""
from __future__ import annotations

from collections.abc import Awaitable

from app.core.logging import get_logger
from app.services.free_seed_hunter import FreeSeedHunter
from app.services.scheduler import JobSpec, scheduler_center
from app.services.seed_engine import SeedEngine

logger = get_logger(__name__)

#: 辅种默认间隔 60 分钟（FR-2.2），下限 15 分钟由 FeedFetcher 强制
SEED_INTERVAL = 3600
#: FR-3.1 默认 10 分钟
FREE_SEED_INTERVAL = 600


def register_seed_jobs(
    engine: SeedEngine, hunter: FreeSeedHunter
) -> None:
    """注册仅有的 RSS 拉取作业。

    ``offset_seconds`` 让两条作业错峰：辅种在第 17 秒、免费种在第 29 秒。
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
            name="free_seed_poll",
            func=lambda: _swallow(hunter.pull_all()),
            interval_seconds=FREE_SEED_INTERVAL,
            offset_seconds=29,
        )
    )
    logger.info(
        "已注册 RSS 拉取作业：cross_seed(%ds) / free_seed_poll(%ds)",
        SEED_INTERVAL,
        FREE_SEED_INTERVAL,
    )


async def _swallow(result: Awaitable[object]) -> None:
    """作业返回值对调度器无意义；仅确保 await 完成。"""
    await result