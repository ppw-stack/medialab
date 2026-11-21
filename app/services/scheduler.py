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

from app.core.job_log import JobLogCollector, capture_job_logs
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
    #: 中文名（如「辅种巡查」）——前端展示用。
    #:
    #: **后端是唯一真源**：前端曾硬编码一份 job → 中文名的映射表，
    #: 每加一个作业就要手改前端，漏一处就在调度页显示原始 job 名
    #: （如 "scan_orphans"）和 "—" 描述。改为由后端下发后，新增
    #: 作业只需改 :mod:`app.services.job_registry` 一处。
    title: str = ""
    #: 一句话说明这个作业做什么（前端展示在标题下方）
    description: str = ""


@dataclass(slots=True)
class JobStepItem:
    """步骤内的一条**明细**（逐站 / 逐种 / 逐任务）。

    为什么需要：步骤的 ``detail`` 是**聚合**描述（如「20 成功，0 失败」），
    光看它**无法回答「是哪些出了问题」**。而作业执行时这些数据本来就在
    内存里（逐站结果、逐种推送结果…），只是没被带出来。

    例：``fetch_all_sites`` 内部维护着 ``(site_id, name, pulled, ok, ko,
    pull_error)`` 的列表，聚合后只剩一句「8 站失败」——哪个站失败、
    什么原因，全部丢失。本字段把这份明细还给用户。

    存储在 :attr:`JobStep.items`，随 ``result_json.steps`` 落库。
    """

    #: 主体标识（站名 / 种子名 / hash 前缀 / task id）
    name: str
    #: 状态：ok / warn / skip / fail
    status: str
    #: 涉及数量；None 表示不适用
    count: int | None = None
    #: 简要描述（如失败原因「DNS 解析失败」）
    detail: str | None = None


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
    #: 逐项明细（默认 None = 该步骤无明细）
    #:
    #: 前端**默认折叠**，点击「明细（N）」才展开——``monitor`` 这类作业
    #: 可能有几百条明细，默认展开会把详情面板撑爆。
    items: list[JobStepItem] | None = None
    #: 明细的**完整**条数。
    #:
    #: ``len(items) < items_total`` 说明明细被截断了（前端据此显示
    #: 「显示 50 / 共 200」）。None = 未截断或无明细。
    items_total: int | None = None
    #: **子步骤**（2026-11-21 加）——本身还是 :class:`JobStep`。
    #:
    #: 用途：编排型作业（如 ``cross_seed_maintenance``）把子作业的步骤
    #: 拍平展示。以前子作业自带 5 条步骤，被编排层折成 1 条后用户在页面
    #: 上只能看到「辅种巡查」一行，看不到「写入快照 / 检测异常 / 自动暂停」。
    #:
    #: 默认 None → 既有 60+ 处 ``JobStep(...)`` 调用零改动；``asdict``
    #: 会递归序列化，落库无需额外处理。
    children: list[JobStep] | None = None


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


#: 单个步骤最多保留的明细条数（超出丢最旧的）。
#:
#: 必须限流：``monitor`` 可能命中几百个种子，明细全存会让
#: ``job_runs.result_json`` 失控（辅种快照本身就 590KB）。
MAX_STEP_ITEMS = 50


def build_items(
    items: list[JobStepItem], *, limit: int = MAX_STEP_ITEMS
) -> tuple[list[JobStepItem], int | None]:
    """截断明细列表，返回 ``(items, items_total)``。

    :param items: 完整明细列表。
    :param limit: 最多保留多少条。
    :return: ``(截断后的明细, 完整条数)``。未截断时第二项为 None
        （前端据此判断要不要显示「显示 N / 共 M」）。

    **丢最旧的还是最晚的**：明细是**按执行顺序**产生的，故丢**末尾**
    的（保留最早发生的）——排查时「先发生了什么」比「最后发生了什么」
    更能定位问题。
    """
    total = len(items)
    if total <= limit:
        return items, None
    return items[:limit], total


__all__ = [
    "MAX_STEP_ITEMS",
    "JobSpec",
    "JobStep",
    "JobStepItem",
    "RunResult",
    "SchedulerCenter",
    "build_items",
    "scheduler_center",
]


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
        #: :meth:`apply_overrides` 灌入，:meth:`register` 时应用——
        #: 被停用的作业根本不会被安排到 APScheduler。
        self._enabled_overrides: dict[str, bool] = {}
        #: DB 持久化的作业 cron 覆盖（作业名 → cron）。启动期由
        #: :meth:`apply_overrides` 灌入，:meth:`register` 时应用——
        #: 页面改的 cron 跨重启保持。
        self._cron_overrides: dict[str, str] = {}
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
    def apply_overrides(
        self, states: dict[str, bool], crons: dict[str, str] | None = None
    ) -> None:
        """应用 DB 读回的调度配置（启动期，在 register_jobs 之前调用）。

        必须在注册作业**之前**调用：:meth:`register` 会按
        ``spec.enabled`` 决定是否把作业安排到 APScheduler，并按
        ``spec.cron`` 决定触发时刻，届时才知道该用什么。

        :param states: ``{作业名: enabled}``——DB 里的启停覆盖。
        :param crons: ``{作业名: cron}``——DB 里的 cron 覆盖（**只含
            非 NULL 的行**；NULL 表示用代码里的默认值，不该覆盖）。
            省略 = 全部用代码默认值。
        """
        self._enabled_overrides = dict(states)
        self._cron_overrides = dict(crons or {})
        if self._enabled_overrides or self._cron_overrides:
            disabled = sorted(
                name for name, on in self._enabled_overrides.items() if not on
            )
            logger.info(
                "已载入作业调度配置：%d 个作业（停用 %s，cron 覆盖 %d 个）",
                len(self._enabled_overrides),
                ", ".join(disabled) if disabled else "无",
                len(self._cron_overrides),
            )

    def register(self, spec: JobSpec) -> None:
        # DB 状态优先于 spec 自带的默认值——用户手动停用的作业重启后
        # 不该"复活"；用户在页面上改的 cron 重启后不该丢。
        # DB 里没有该作业时用 spec 默认值。
        if spec.name in self._enabled_overrides:
            spec.enabled = self._enabled_overrides[spec.name]
        if spec.name in self._cron_overrides:
            spec.cron = self._cron_overrides[spec.name]
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

    def validate_cron(self, cron: str) -> None:
        """校验 cron 表达式（**单一真源**——API 层与调度中心共用）。

        用 APScheduler 的 :class:`CronTrigger` 校验——**权威**判据
        （前端只做结构检查，最终以这里为准）。

        :raises ValueError: 表达式不合法。
        """
        CronTrigger.from_crontab(cron, timezone=self._tz)

    def update_cron(self, name: str, cron: str) -> bool:
        """更新作业的 cron 表达式（运行时切换，立即生效）。

        :raises ValueError: cron 表达式不合法（:meth:`validate_cron`）。
        """
        spec = self._specs.get(name)
        if spec is None:
            return False
        # 校验 cron 格式（不合法时直接抛，让上层返 400）
        self.validate_cron(cron)
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
                # 中文名 + 描述：前端直接展示，不再硬编码映射表
                "title": spec.title,
                "description": spec.description,
            }
            for name, spec in self._specs.items()
        ]

    def list_job_cron(self, name: str) -> str | None:
        """单个作业**当前生效**的 cron（DB 覆盖后的值）。

        日志用：注册完打印的必须是生效值，不是代码里的默认值——否则用户
        改过 cron 后日志会与实际行为矛盾（"日志说 2,32 跑，实际 15,45 跑"）。
        """
        spec = self._specs.get(name)
        return spec.cron if spec is not None else None

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
            # 采集本次作业的 Python 日志（时序视角：哪步慢、中间发生了什么）。
            # 采集器由**外部**创建——作业抛异常时也要能拿到已收集的日志。
            collector = JobLogCollector()
            try:
                with capture_job_logs(collector):
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
                    logs=collector.entries(),
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
        # 采集器由外部创建——失败/取消路径也要能拿到已收集的日志
        collector = JobLogCollector()
        try:
            with capture_job_logs(collector):
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
                logs=collector.entries(),
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
                logs=collector.entries(),
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
            logs=collector.entries(),
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
    logs: list[dict[str, object]] | None = None,
) -> None:
    """把单次作业执行结果写入 job_runs 表（失败不抛，仅 warn）。

    :param trigger: ``'cron'`` 或 ``'manual'``。
    :param started_at: 北京时间字符串。
    :param finished_at: 北京时间字符串。
    :param duration_ms: 执行时长（毫秒），由调用方计算。
    :param logs: 作业期间的 Python 日志（结构化条目）。非空时**并入**
        ``result_json`` 的 ``logs`` 键——不另建列（免 DB 迁移）。
    """
    import json

    from app.db.models import JobRun
    from app.db.session import get_session_factory

    result_json: str | None = None
    try:
        payload: object = None
        if result is not None:
            # dataclass（含嵌套 JobStep）→ dict，再 JSON 序列化。
            # 不能只靠 default=str：dataclass 会被转成 repr 字符串，
            # 前端拿不到 steps 的 name/status/count 字段。
            payload = _jsonable(result)
        if logs:
            # 日志与结果**合并**成一个 JSON 对象：
            # - result 是 dict → 直接加 logs 键
            # - result 不是 dict（None / str）→ 包一层 {"result": ..., "logs": [...]}
            if isinstance(payload, dict):
                payload = {**payload, "logs": logs}
            else:
                payload = {"result": payload, "logs": logs}
        if payload is not None:
            result_json = json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        result_json = None

    try:
        factory = get_session_factory()

        async def _persist() -> None:
            """落库单条作业历史。

            **SQLite 锁竞争降级**（2026-11-12 补，与直链缓存同款场景）：
            扫描/调度批量写库期间，这里的 ``commit`` 会撞上写锁
            （``database is locked``）——退避重试一次，仍失败记 warning
            放弃。作业历史是统计性副产品（保留期 30 天），丢一条比
            刷「Task exception was never retrieved」好；其它异常同样
            降级记 warning（带类型，不静默吞真 bug）。
            """
            from sqlalchemy.exc import OperationalError

            for attempt in (1, 2):
                try:
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
                    return
                except OperationalError as exc:
                    if attempt == 2:
                        logger.warning(
                            "记录作业 %s 执行历史失败（DB 锁竞争，已重试）：%s",
                            job_name, exc,
                        )
                        return
                    await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning(
                        "记录作业 %s 执行历史失败：%s: %s",
                        job_name, type(exc).__name__, exc,
                    )
                    return

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