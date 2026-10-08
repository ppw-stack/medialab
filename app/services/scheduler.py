"""任务调度中心（§7.1）。

用 APScheduler 的 AsyncIOScheduler 驱动全部周期性作业。设计要点：

- **作业级异常隔离**（R8）：单任务失败不中断整轮。
- **错峰调度**：不同频率的作业避免同一秒集中触发（§1.3 资源红线）。
- **优雅停机**（FR-8.2）：停机时停止新调度，等待在途作业。

M1 只搭建骨架，业务作业自 M2 起通过 :meth:`SchedulerCenter.register` 注册。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.core.metrics import stage_duration

logger = get_logger(__name__)

JobFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class JobSpec:
    """作业定义。"""

    name: str
    func: JobFn
    interval_seconds: int
    #: 首次执行前的错峰偏移，避免同秒触发
    offset_seconds: int = 0
    enabled: bool = True


class SchedulerCenter:
    """调度中心门面。"""

    def __init__(self, timezone: str = "UTC") -> None:
        self._tz = timezone
        self._scheduler: AsyncIOScheduler | None = None
        self._specs: dict[str, JobSpec] = {}
        self._running: set[str] = set()
        self._inflight: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._scheduler is not None:
            return
        self._scheduler = AsyncIOScheduler(
            timezone=self._tz,
            executors={"default": AsyncIOExecutor()},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )
        self._scheduler.start()
        for spec in self._specs.values():
            if spec.enabled:
                self._schedule(spec)
        logger.info("调度中心已启动，已注册作业 %d 个", len(self._specs))

    async def shutdown(self, *, grace_seconds: int = 60) -> None:
        """停机：先停新调度，再等在途作业（FR-8.2）。"""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        if self._inflight:
            logger.info("等待在途作业 %d 个（上限 %ds）", len(self._inflight), grace_seconds)
            done, pending = await asyncio.wait(
                list(self._inflight), timeout=grace_seconds
            )
            for task in pending:
                task.cancel()
            if pending:
                logger.warning("等待超时，取消 %d 个作业", len(pending))
            _ = done
        logger.info("调度中心已停止")

    # ------------------------------------------------------------ 注册
    def register(self, spec: JobSpec) -> None:
        self._specs[spec.name] = spec
        if self._scheduler is not None and spec.enabled:
            self._schedule(spec)

    def _schedule(self, spec: JobSpec) -> None:
        assert self._scheduler is not None
        self._scheduler.add_job(
            self._wrap(spec),
            trigger=IntervalTrigger(seconds=spec.interval_seconds),
            id=spec.name,
            name=spec.name,
            next_run_time=_first_run(spec),
            replace_existing=True,
        )

    def enable(self, name: str, enabled: bool = True) -> bool:
        spec = self._specs.get(name)
        if spec is None:
            return False
        spec.enabled = enabled
        if self._scheduler is None:
            return True
        if enabled:
            self._schedule(spec)
        else:
            self._scheduler.remove_job(name)
        return True

    def list_jobs(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "interval_seconds": spec.interval_seconds,
                "enabled": spec.enabled,
                "running": name in self._running,
            }
            for name, spec in self._specs.items()
        ]

    # ------------------------------------------------------------ 执行包装
    def _wrap(self, spec: JobSpec) -> JobFn:
        async def _run() -> None:
            # R8：单作业异常隔离，绝不让调度器整体崩掉
            if spec.name in self._running:
                logger.debug("作业 %s 上一轮未结束，跳过本轮", spec.name)
                return
            self._running.add(spec.name)
            task = asyncio.current_task()
            if task is not None:
                self._inflight.add(task)
            started = asyncio.get_running_loop().time()
            try:
                await spec.func()
            except asyncio.CancelledError:
                logger.info("作业 %s 被取消", spec.name)
                raise
            except Exception as exc:
                logger.exception("作业 %s 执行失败：%s", spec.name, exc)
            finally:
                stage_duration.labels(stage=spec.name).observe(
                    asyncio.get_running_loop().time() - started
                )
                self._running.discard(spec.name)
                if task is not None:
                    self._inflight.discard(task)

        return _run


def _first_run(spec: JobSpec) -> datetime:
    now = datetime.now(UTC).replace(tzinfo=None)
    from datetime import timedelta

    return now + timedelta(seconds=spec.offset_seconds % max(spec.interval_seconds, 1))


#: 全局调度中心
scheduler_center = SchedulerCenter()