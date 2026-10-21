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

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.logging import get_logger
from app.core.metrics import stage_duration
from app.core.timeutils import bj_now, bj_now_dt, format_bj

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
class JobStep:
    """作业单次执行的子步骤（前端「步骤卡片」数据源）。

    存进 :class:`JobRun` 的 ``result_json.steps``，前端按
    ``ok / warn / skip / fail`` 渲染状态色。
    """

    #: 步骤名（如「查询下载器」「推送到下载器」）
    name: str
    #: 状态：ok / warn / skip / fail
    status: str
    #: 涉及数量；None 表示不适用
    count: int | None = None
    #: 简要描述（如「5 成功 / 0 失败」）
    detail: str | None = None


@dataclass(slots=True)
class RunResult:
    """手动触发作业的执行结果。"""

    ok: bool
    started_at: str
    finished_at: str
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
        # _running 防同作业并发（cron + 手动触发共享）
        self._running: set[str] = set()
        # _inflight 只追踪 cron 触发的作业 task——手动触发的 task 属于
        # HTTP 请求上下文，drain 停机时不应 cancel 它们（会取消正在
        # 返回响应的 HTTP 请求）。手动触发由调用方等待结果。
        self._inflight: set[asyncio.Task[None]] = set()
        # _manual_inflight 追踪手动触发的作业 task：drain 会**等待**它们
        # 收敛（不取消——取消会打断正在返回响应的 HTTP 请求），
        # 且 inflight_count 要把它们算进去，否则：
        # 1) 前端看到的 running 作业数会漏掉手动触发的那一个；
        # 2) 停机时 drain 会看不到在途的手动作业，进程可能在它还在
        #    写库时就退出。
        self._manual_inflight: set[asyncio.Task[None]] = set()
        #: DB 持久化的作业启停状态（作业名 → enabled）。启动期由
        #: :meth:`apply_enabled_overrides` 灌入，:meth:`register` 时应用——
        #: 被停用的作业根本不会被安排到 APScheduler。
        self._enabled_overrides: dict[str, bool] = {}
        #: 停机期间置 False：拒绝新作业，只等在途作业收敛（FR-8.2）
        self.accepting_new_work = True

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._scheduler is not None:
            return
        # 同进程重启（测试/重建 app）后恢复接收新作业
        self.accepting_new_work = True
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
        """当前正在执行的作业数（cron + 手动触发）。"""
        return len(self._inflight) + len(self._manual_inflight)

    async def drain(self, *, timeout: float) -> None:  # noqa: ASYNC109
        """等待在途作业收敛，超时则取消（FR-8.2）。

        调用方须先 :meth:`stop_accepting` 或手工置 ``accepting_new_work=False``。

        - **cron 作业**：等待超时后**取消**（作业可安全重跑）。
        - **手动触发的作业**：只**等待**不取消——它跑在 HTTP 请求上下文里，
          取消会打断正在返回响应的请求。超时后记 warning 继续。
        """
        cron = list(self._inflight)
        manual = list(self._manual_inflight)
        if not cron and not manual:
            return
        try:
            if cron:
                done, pending = await asyncio.wait(cron, timeout=timeout)
                if pending:
                    logger.warning("等待在途作业超时，取消 %d 个", len(pending))
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                logger.info(
                    "在途 cron 作业已收敛：完成 %d，取消 %d", len(done), len(pending)
                )
            if manual:
                # 只等不取消：给手动触发的作业留出收尾时间
                done_m, pending_m = await asyncio.wait(manual, timeout=timeout)
                if pending_m:
                    logger.warning(
                        "等待手动触发的作业超时（%d 个未完成，不取消以免打断 HTTP 响应）",
                        len(pending_m),
                    )
                else:
                    logger.info("在途手动作业已收敛：完成 %d", len(done_m))
        except Exception as exc:
            # drain 兜底：停机路径上再抛异常会中断 shutdown，导致在途作业
            # 状态不一致。CancelledError 之外的异常都吞掉并记录。
            logger.warning("drain 期间异常（已吞掉以保停机完成）：%s", exc)

    async def shutdown(self, *, grace_seconds: int = 60) -> None:
        """停机：先停新调度，再等在途作业（FR-8.2）。"""
        self.accepting_new_work = False
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        await self.drain(timeout=grace_seconds)
        logger.info("调度中心已停止")

    # ------------------------------------------------------------ 注册
    def apply_enabled_overrides(self, states: dict[str, bool]) -> None:
        """应用 DB 读回的作业启停状态（启动期，在 register_jobs 之前调用）。

        必须在注册作业**之前**调用：:meth:`register` 会按
        ``spec.enabled`` 决定是否把作业安排到 APScheduler，届时才知道
        该不该排。
        """
        self._enabled_overrides = dict(states)
        if self._enabled_overrides:
            disabled = sorted(
                name for name, on in self._enabled_overrides.items() if not on
            )
            logger.info(
                "已载入作业启停状态：%d 个作业（停用 %s）",
                len(self._enabled_overrides),
                ", ".join(disabled) if disabled else "无",
            )

    def register(self, spec: JobSpec) -> None:
        # DB 状态优先于 spec 自带的默认值——用户手动停用的作业重启后
        # 不该"复活"。DB 里没有该作业时用 spec 默认值。
        if spec.name in self._enabled_overrides:
            spec.enabled = self._enabled_overrides[spec.name]
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
            wall_started = bj_now_dt()
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
                finished_dt = bj_now_dt()
                stage_duration.labels(stage=spec.name).observe(
                    asyncio.get_running_loop().time() - started
                )
                self._running.discard(spec.name)
                if task is not None:
                    self._inflight.discard(task)
                _record_run(
                    job_name=spec.name,
                    trigger="cron",
                    started_at=format_bj(wall_started),
                    finished_at=format_bj(finished_dt),
                    duration_ms=int((finished_dt - wall_started).total_seconds() * 1000),
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
            now_str = bj_now()
            _record_run(
                job_name=name, trigger="manual",
                started_at=now_str, finished_at=now_str,
                duration_ms=0,
                ok=False, error=f"unknown job: {name}", result=None,
            )
            return RunResult(
                ok=False,
                started_at=now_str,
                finished_at=now_str,
                duration_ms=0,
                error=f"unknown job: {name}",
            )
        if not self.accepting_new_work:
            now_str = bj_now()
            _record_run(
                job_name=name, trigger="manual",
                started_at=now_str, finished_at=now_str,
                duration_ms=0,
                ok=False, error="scheduler not accepting new work", result=None,
            )
            return RunResult(
                ok=False,
                started_at=now_str,
                finished_at=now_str,
                duration_ms=0,
                error="scheduler not accepting new work",
            )
        if name in self._running:
            now_str = bj_now()
            _record_run(
                job_name=name, trigger="manual",
                started_at=now_str, finished_at=now_str,
                duration_ms=0,
                ok=False, error=f"job {name} is already running", result=None,
            )
            return RunResult(
                ok=False,
                started_at=now_str,
                finished_at=now_str,
                duration_ms=0,
                error=f"job {name} is already running",
            )
        started_dt = bj_now_dt()
        self._running.add(name)
        # 手动触发的 task 加入 _manual_inflight（不是 _inflight）：
        # - 计入 inflight_count()，前端能正确显示"运行中"
        # - drain 停机时只等待不取消（取消会打断正在返回响应的 HTTP 请求）
        # 调用方 await 本函数即等待结果。
        manual_task = asyncio.current_task()
        if manual_task is not None:
            self._manual_inflight.add(manual_task)
        result: object = None
        try:
            result = await spec.func()
        except asyncio.CancelledError:
            finished_dt = bj_now_dt()
            started_str = format_bj(started_dt)
            finished_str = format_bj(finished_dt)
            logger.info("手动触发作业 %s 被取消", name)
            _record_run(
                job_name=name, trigger="manual",
                started_at=started_str, finished_at=finished_str,
                duration_ms=int((finished_dt - started_dt).total_seconds() * 1000),
                ok=False, error="cancelled", result=result,
            )
            return RunResult(
                ok=False,
                started_at=started_str,
                finished_at=finished_str,
                duration_ms=int((finished_dt - started_dt).total_seconds() * 1000),
                error="cancelled",
                result=result,
            )
        except Exception as exc:
            finished_dt = bj_now_dt()
            started_str = format_bj(started_dt)
            finished_str = format_bj(finished_dt)
            logger.exception("手动触发作业 %s 失败：%s", name, exc)
            _record_run(
                job_name=name, trigger="manual",
                started_at=started_str, finished_at=finished_str,
                duration_ms=int((finished_dt - started_dt).total_seconds() * 1000),
                ok=False, error=str(exc), result=result,
            )
            return RunResult(
                ok=False,
                started_at=started_str,
                finished_at=finished_str,
                duration_ms=int((finished_dt - started_dt).total_seconds() * 1000),
                error=str(exc),
                result=result,
            )
        finally:
            self._running.discard(name)
            if manual_task is not None:
                self._manual_inflight.discard(manual_task)
        finished_dt = bj_now_dt()
        started_str = format_bj(started_dt)
        finished_str = format_bj(finished_dt)
        _record_run(
            job_name=name, trigger="manual",
            started_at=started_str, finished_at=finished_str,
            duration_ms=int((finished_dt - started_dt).total_seconds() * 1000),
            ok=True, error=None, result=result,
        )
        return RunResult(
            ok=True,
            started_at=started_str,
            finished_at=finished_str,
            duration_ms=int((finished_dt - started_dt).total_seconds() * 1000),
            result=result,
        )


#: 全局调度中心
scheduler_center = SchedulerCenter()

#: 审计落库 task 引用集合：event loop 只弱引用 Task，不持引用会被 GC，
#: 导致 job_runs 记录随机缺失。done 回调里移除。
_record_tasks: set[asyncio.Task[None]] = set()


def _keep_record_task(task: asyncio.Task[None]) -> None:
    _record_tasks.discard(task)


def _jsonable(obj: object) -> object:
    """把作业返回值转成可 JSON 序列化的结构。

    - dataclass（含 :class:`JobStep` 嵌套）→ dict
    - tuple / list → list
    - dict → 逐值递归

    必须在 ``json.dumps(default=str)`` **之前**调用——否则 dataclass 会被
    ``default=str`` 退化成 repr 字符串，前端就拿不到 ``steps`` 的字段。
    """
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _record_run(
    *,
    job_name: str,
    trigger: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    ok: bool,
    error: str | None,
    result: object,
) -> None:
    """把单次作业执行结果写入 job_runs 表（失败不抛，仅 warn）。

    :param trigger: ``'cron'`` 或 ``'manual'``。
    :param started_at: 北京时间字符串。
    :param finished_at: 北京时间字符串。
    :param duration_ms: 执行时长（毫秒），由调用方计算。
    """
    import json

    from app.db.models import JobRun
    from app.db.session import get_session_factory

    result_json: str | None = None
    try:
        if result is not None:
            # dataclass（含嵌套 JobStep）→ dict，再 JSON 序列化。
            # 不能只靠 default=str：dataclass 会被转成 repr 字符串，
            # 前端拿不到 steps 的 name/status/count 字段。
            result_json = json.dumps(
                _jsonable(result), default=str, ensure_ascii=False
            )
    except Exception:
        result_json = None

    try:
        factory = get_session_factory()
        async def _persist() -> None:
            async with factory() as session:
                session.add(JobRun(
                    job_name=job_name,
                    trigger=trigger,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
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
            task = loop.create_task(_persist())
            _record_tasks.add(task)
            task.add_done_callback(_keep_record_task)
        else:
            # 无运行 loop（同步上下文）—— 不可用 asyncio.run：
            # async engine 的 aiosqlite 连接池绑定在创建它的 loop 上，
            # 在新 loop 里用会抛跨 loop 错误（与 runtime_params 同类问题）。
            # 记录失败只影响历史统计，不该中断调用方，故记日志跳过。
            logger.warning(
                "记录作业 %s 执行历史失败：无运行中的 event loop，跳过写入", job_name
            )
    except Exception as exc:
        logger.warning("记录作业 %s 执行历史失败：%s", job_name, exc)