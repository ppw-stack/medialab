"""任务调度中心（§7.1）。

用 APScheduler 的 AsyncIOScheduler 驱动全部周期性作业。设计要点：

- **作业级异常隔离**（R8）：单任务失败不中断整轮。
- **cron 周期**：作业周期由 cron 表达式配置（5 字段：分 时 日 月 星期）。
- **错峰调度**：通过 cron 的"分钟"位错开不同作业（§1.3 资源红线）。
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
from apscheduler.triggers.cron import CronTrigger

from app.core.logging import get_logger
from app.core.metrics import stage_duration

logger = get_logger(__name__)

JobFn = Callable[[], Awaitable[object | None]]


@dataclass(slots=True)
class JobSpec:
    """作业定义。"""

    name: str
    func: JobFn
    #: cron 表达式（5 字段：分 时 日 月 星期）；错峰通过分钟位表达
    cron: str
    enabled: bool = True


@dataclass(slots=True)
class RunResult:
    """手动触发作业的执行结果。"""

    ok: bool
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    error: str = ""
    #: 作业函数返回值（dict / dataclass.toDict / str / None），供前端展示详情
    result: object = None


class SchedulerCenter:
    """调度中心门面。"""

    def __init__(self, timezone: str | None = None) -> None:
        # 默认从 settings.timezone 取（与日志/保种/窗口计算一致），
        # 单元测试可显式覆盖。
        if timezone is None:
            from app.core.config import get_settings
            timezone = get_settings().timezone
        self._tz = timezone
        self._scheduler: AsyncIOScheduler | None = None
        self._specs: dict[str, JobSpec] = {}
        self._running: set[str] = set()
        self._inflight: set[asyncio.Task[None]] = set()
        #: 停机期间置 False：拒绝新作业，只等在途作业收敛（FR-8.2）
        self.accepting_new_work = True

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

    def inflight_count(self) -> int:
        """当前正在执行的作业数。"""
        return len(self._inflight)

    async def drain(self, *, timeout: float) -> None:  # noqa: ASYNC109
        """等待在途作业收敛，超时则取消（FR-8.2）。

        调用方须先 :meth:`stop_accepting` 或手工置 ``accepting_new_work=False``。
        """
        if not self._inflight:
            return
        done, pending = await asyncio.wait(list(self._inflight), timeout=timeout)
        if pending:
            logger.warning("等待在途作业超时，取消 %d 个", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("在途作业已收敛：完成 %d，取消 %d", len(done), len(pending))

    async def shutdown(self, *, grace_seconds: int = 60) -> None:
        """停机：先停新调度，再等在途作业（FR-8.2）。"""
        self.accepting_new_work = False
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        await self.drain(timeout=grace_seconds)
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
            trigger=CronTrigger.from_crontab(spec.cron, timezone=self._tz),
            id=spec.name,
            name=spec.name,
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

    def update_cron(self, name: str, cron: str) -> bool:
        """更新作业的 cron 表达式（运行时切换，立即生效）。"""
        spec = self._specs.get(name)
        if spec is None:
            return False
        # 先校验 cron 格式（不合法时直接抛，让上层返 400）
        CronTrigger.from_crontab(cron, timezone=self._tz)
        spec.cron = cron
        if self._scheduler is not None and spec.enabled:
            # replace_existing=True 复用现有 job 槽
            self._schedule(spec)
        return True

    def list_jobs(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "cron": spec.cron,
                "enabled": spec.enabled,
                "running": name in self._running,
            }
            for name, spec in self._specs.items()
        ]

    # ------------------------------------------------------------ 执行包装
    def _wrap(self, spec: JobSpec) -> JobFn:
        async def _run() -> None:
            # R8：单作业异常隔离，绝不让调度器整体崩掉
            if not self.accepting_new_work:
                return
            if spec.name in self._running:
                logger.debug("作业 %s 上一轮未结束，跳过本轮", spec.name)
                return
            self._running.add(spec.name)
            task = asyncio.current_task()
            if task is not None:
                self._inflight.add(task)
            wall_started = datetime.now(UTC)
            started = asyncio.get_running_loop().time()
            _ok = True
            _err: str | None = None
            _result: object = None
            try:
                _result = await spec.func()
            except asyncio.CancelledError:
                logger.info("作业 %s 被取消", spec.name)
                raise
            except Exception as exc:
                logger.exception("作业 %s 执行失败：%s", spec.name, exc)
                _ok = False
                _err = str(exc)
            finally:
                stage_duration.labels(stage=spec.name).observe(
                    asyncio.get_running_loop().time() - started
                )
                self._running.discard(spec.name)
                if task is not None:
                    self._inflight.discard(task)
                _record_run(
                    job_name=spec.name,
                    trigger="cron",
                    started_at=wall_started,
                    finished_at=datetime.now(UTC),
                    ok=_ok,
                    error=_err,
                    result=_result,
                )

        return _run

    # ------------------------------------------------------------ 手动触发
    async def run_now(self, name: str) -> RunResult:
        """手动触发作业，await 执行完成后返回结果。

        与 cron 触发的区别：

        - 绕过 :meth:`_wrap` 的 R8 异常吞没——手动触发的异常必须抛回调用方，
          让用户知道失败（cron 触发走 _wrap 是为了保护调度器整体存活）
        - 仍复用 inflight 计数 + ``accepting_new_work`` 闸门
        - 不通过 APScheduler 调度（不写 job store），由 FastAPI 请求上下文驱动
        """
        spec = self._specs.get(name)
        if spec is None:
            _record_run(
                job_name=name, trigger="manual",
                started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
                ok=False, error=f"unknown job: {name}", result=None,
            )
            return RunResult(
                ok=False,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=0,
                error=f"unknown job: {name}",
            )
        if not self.accepting_new_work:
            _record_run(
                job_name=name, trigger="manual",
                started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
                ok=False, error="scheduler not accepting new work", result=None,
            )
            return RunResult(
                ok=False,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=0,
                error="scheduler not accepting new work",
            )
        if name in self._running:
            _record_run(
                job_name=name, trigger="manual",
                started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
                ok=False, error=f"job {name} is already running", result=None,
            )
            return RunResult(
                ok=False,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=0,
                error=f"job {name} is already running",
            )
        started = datetime.now(UTC)
        self._running.add(name)
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        result: object = None
        try:
            result = await spec.func()
        except asyncio.CancelledError:
            finished = datetime.now(UTC)
            logger.info("手动触发作业 %s 被取消", name)
            _record_run(
                job_name=name, trigger="manual",
                started_at=started, finished_at=finished,
                ok=False, error="cancelled", result=result,
            )
            return RunResult(
                ok=False,
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                error="cancelled",
                result=result,
            )
        except Exception as exc:
            finished = datetime.now(UTC)
            logger.exception("手动触发作业 %s 失败：%s", name, exc)
            _record_run(
                job_name=name, trigger="manual",
                started_at=started, finished_at=finished,
                ok=False, error=str(exc), result=result,
            )
            return RunResult(
                ok=False,
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                error=str(exc),
                result=result,
            )
        finally:
            self._running.discard(name)
            if task is not None:
                self._inflight.discard(task)
        finished = datetime.now(UTC)
        _record_run(
            job_name=name, trigger="manual",
            started_at=started, finished_at=finished,
            ok=True, error=None, result=result,
        )
        return RunResult(
            ok=True,
            started_at=started,
            finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000),
            result=result,
        )


#: 全局调度中心
scheduler_center = SchedulerCenter()


def _record_run(
    *,
    job_name: str,
    trigger: str,
    started_at: datetime,
    finished_at: datetime,
    ok: bool,
    error: str | None,
    result: object,
) -> None:
    """把单次作业执行结果写入 job_runs 表（失败不抛，仅 warn）。

    :param trigger: ``'cron'`` 或 ``'manual'``。
    """
    import json

    from app.db.models import JobRun
    from app.db.session import get_session_factory

    result_json: str | None = None
    try:
        if result is not None:
            result_json = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        result_json = None

    try:
        factory = get_session_factory()
        async def _persist() -> None:
            async with factory() as session:
                session.add(JobRun(
                    job_name=job_name,
                    trigger=trigger,
                    started_at=started_at.replace(tzinfo=None),
                    finished_at=finished_at.replace(tzinfo=None),
                    duration_ms=int((finished_at - started_at).total_seconds() * 1000),
                    ok=ok,
                    error=error,
                    result_json=result_json,
                ))
                await session.commit()
        # 在当前 loop 调度一次性 task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(_persist())
        else:
            # 无运行 loop（同步上下文）— 直接 asyncio.run
            asyncio.run(_persist())
    except Exception as exc:
        logger.warning("记录作业 %s 执行历史失败：%s", job_name, exc)