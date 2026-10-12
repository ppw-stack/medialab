"""调度作业注册——仅保留 RSS 拉取两个作业。

辅种拉取（FR-2.2）和免费种抓取（FR-3.1）的执行周期由 cron 表达式配置，
通过 :attr:`AppSettings.cross_seed_cron` / :attr:`AppSettings.free_seed_poll_cron`
注入，便于运维修改频率无需改代码。

cron 5 字段：分 时 日 月 星期。错峰通过分钟位表达。
默认：cross_seed "17 * * * *"（每小时第 17 分）、free_seed_poll "*/10 * * * *"
（每 10 分钟）。
"""
from __future__ import annotations

from collections.abc import Awaitable

from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.services.free_seed_hunter import FreeSeedHunter
from app.services.scheduler import JobSpec, scheduler_center
from app.services.seed_engine import SeedEngine

logger = get_logger(__name__)


def register_seed_jobs(
    engine: SeedEngine, hunter: FreeSeedHunter
) -> None:
    """注册仅有的 RSS 拉取作业。"""
    settings: AppSettings = get_settings()
    scheduler_center.register(
        JobSpec(
            name="cross_seed",
            func=lambda: _swallow(engine.run_all()),
            cron=settings.cross_seed_cron,
        )
    )
    scheduler_center.register(
        JobSpec(
            name="free_seed_poll",
            func=lambda: _swallow(hunter.pull_all()),
            cron=settings.free_seed_poll_cron,
        )
    )
    logger.info(
        "已注册 RSS 拉取作业：cross_seed cron=%r / free_seed_poll cron=%r",
        settings.cross_seed_cron,
        settings.free_seed_poll_cron,
    )


async def _swallow(result: Awaitable[object]) -> None:
    """作业返回值对调度器无意义；仅确保 await 完成。"""
    await result