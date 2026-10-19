"""调度作业 API.

端点（prefix 挂在 ``/api/v1`` 下，路径无独立前缀）：

- ``GET /scheduler``：查询调度作业状态（自动从注册的 JobSpec 列表生成）
- ``POST /scheduler/{job_name}/toggle``：启停单个作业
- ``POST /scheduler/{job_name}/cron``：运行时修改 cron 表达式
- ``GET /scheduler/{job_name}/config``：作业专属配置（cron + 该作业的 runtime params）
- ``POST /scheduler/{job_name}/run``：手动触发作业（不等 cron）
- ``GET /scheduler/runs``：作业执行历史分页
- ``GET /scheduler/runs/{run_id}``：单次执行详情
- ``GET/PUT /scheduler/runtime-params``：运行时参数查询/修改

站点相关端点在 :mod:`app.api.routes.sites`（含 ``/sites/{id}/feed-test``）。

历史背景：本文件曾名 ``cross_seed.py``（辅种作业 API），辅种匹配（``pull`` /
``pull-all``）与 ``feeds`` 列表已删除——见 commit
``refactor: 统一站点拉取作业 + 删除辅种匹配 + 删除 free_candidates``。
现更名为 ``scheduler.py`` 以反映真实职责（辅种相关端点在 ``cross_seed.py``）。
"""
from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.auth import require_session
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import JobRun
from app.db.session import session_scope
from app.services.runtime_params import (
    PARAM_SPECS,
    get_runtime_params,
    specs_for_job,
    validate,
)
from app.services.scheduler import RunResult, scheduler_center

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_session)], tags=["scheduler"])


class RunJobResult(BaseModel):
    """手动触发作业响应（对齐 :class:`RunResult`）。"""

    ok: bool
    started_at: str
    finished_at: str
    duration_ms: int
    error: str = ""
    #: 作业函数返回值（dict / None），供前端展示详情
    result: dict[str, object] | None = None


@router.get("/scheduler", summary="调度作业状态")
async def scheduler_status() -> dict[str, Any]:
    """返回调度中心当前注册的作业列表。"""
    from app.core.config import get_settings

    settings = get_settings()
    return {
        "enabled": settings.scheduler_enabled,
        "jobs": scheduler_center.list_jobs(),
        "inflight": scheduler_center.inflight_count(),
        "accepting_new_work": scheduler_center.accepting_new_work,
    }


@router.post("/scheduler/{job_name}/toggle", summary="启停调度作业")
async def toggle_job(job_name: str, enabled: bool) -> dict[str, Any]:
    """切换单个作业的启用状态。"""
    if not scheduler_center.enable(job_name, enabled):
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_name}")
    return {"job": job_name, "enabled": enabled}


class UpdateCronIn(BaseModel):
    cron: str = Field(..., min_length=1, max_length=64,
                      description="标准 5 字段 cron 表达式")


@router.post("/scheduler/{job_name}/cron", summary="修改作业的 cron 表达式")
async def update_job_cron(job_name: str, payload: UpdateCronIn) -> dict[str, Any]:
    """运行时切换 cron 表达式，立即对下一次调度生效。

    不合法 cron 返 400（由 :func:`CronTrigger.from_crontab` 抛 ValueError）。
    """
    try:
        CronTrigger.from_crontab(payload.cron, timezone=get_settings().timezone)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"cron 表达式不合法：{exc}") from exc
    if not scheduler_center.update_cron(job_name, payload.cron):
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_name}")
    return {"job": job_name, "cron": payload.cron}


class RuntimeParamsOut(BaseModel):
    """运行时参数响应。"""

    params: dict[str, int]
    specs: list[dict[str, object]]


class RuntimeParamsUpdateIn(BaseModel):
    """运行时参数更新请求（部分更新）。"""

    push_min_remaining_hours: int | None = None
    push_max_per_run: int | None = None
    monitor_pre_pause_minutes: int | None = None


@router.get("/scheduler/runtime-params", summary="查询作业运行参数")
async def get_runtime_params_view() -> RuntimeParamsOut:
    """返回当前所有可调运行参数 + 元数据（label/min/max）供前端渲染表单。"""
    mgr = get_runtime_params()
    return RuntimeParamsOut(
        params=mgr.all(),
        specs=[
            {
                "key": spec.key,
                "label": spec.label,
                "min": spec.min,
                "max": spec.max,
                "default": spec.default,
            }
            for spec in PARAM_SPECS
        ],
    )


@router.put("/scheduler/runtime-params", summary="修改作业运行参数")
async def update_runtime_params(payload: RuntimeParamsUpdateIn) -> RuntimeParamsOut:
    """运行时修改可调参数（DB 持久化）。下次作业立即生效。

    仅更新 body 中显式给出的字段（partial update）。
    """
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="未提供任何要修改的参数")
    try:
        validate(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    mgr = get_runtime_params()
    async with session_scope() as session:
        await mgr.apply(session, updates)
    # 事务已提交成功，再更新内存（顺序：DB 先、内存后）
    for key, value in updates.items():
        mgr.set_memory(key, value)
    return RuntimeParamsOut(
        params=mgr.all(),
        specs=[
            {
                "key": spec.key,
                "label": spec.label,
                "min": spec.min,
                "max": spec.max,
                "default": spec.default,
            }
            for spec in PARAM_SPECS
        ],
    )


@router.post(
    "/scheduler/{job_name}/run",
    summary="手动触发作业",
    response_model=RunJobResult,
)
async def run_job(job_name: str) -> RunJobResult:
    """立即触发作业并 await 完成，返回结果。

    与 cron 触发的区别：

    - 跳过 cron 表达式，立即触发
    - 复用 :meth:`SchedulerCenter.run_now` 的异常隔离 + inflight 计数
    - 停机（``accepting_new_work=False``）时返回 ok=False + error
    """
    r: RunResult = await scheduler_center.run_now(job_name)
    return RunJobResult(
        ok=r.ok,
        started_at=r.started_at,
        finished_at=r.finished_at,
        duration_ms=r.duration_ms,
        error=r.error,
        # r.result 是 dataclass 序列化的 dict（PushResult.asdict）或 None
        result=r.result if isinstance(r.result, dict) else None,
    )


class JobRunItem(BaseModel):
    """单条作业执行历史。"""

    id: int
    job_name: str
    trigger: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    ok: bool
    error: str | None = None
    result: Any | None = None

    @classmethod
    def from_row(cls, r: JobRun, *, parsed_result: Any = None) -> JobRunItem:
        """从 ORM 行构建（DB 存北京时间字符串，直接使用）。"""
        return cls(
            id=r.id,
            job_name=r.job_name,
            trigger=r.trigger,
            started_at=r.started_at,
            finished_at=r.finished_at,
            duration_ms=r.duration_ms,
            ok=r.ok,
            error=r.error,
            result=parsed_result,
        )


class JobRunPage(BaseModel):
    """作业历史分页响应。"""

    total: int
    items: list[JobRunItem]


@router.get("/scheduler/runs", summary="作业执行历史")
async def list_job_runs(
    job_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JobRunPage:
    """查询作业执行历史（按 started_at desc）。

    :param job_name: 可选过滤单个作业。
    :param limit: 上限 200。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    async with session_scope() as session:
        base = select(JobRun).order_by(JobRun.started_at.desc())
        if job_name:
            base = base.where(JobRun.job_name == job_name)

        total = (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()

        rows = (
            await session.execute(base.limit(limit).offset(offset))
        ).scalars().all()

    items: list[JobRunItem] = []
    for r in rows:
        parsed: Any = None
        if r.result_json:
            with suppress(Exception):
                parsed = json.loads(r.result_json)
        items.append(JobRunItem.from_row(r, parsed_result=parsed))

    return JobRunPage(total=total, items=items)


@router.get("/scheduler/runs/{run_id}", summary="作业执行详情")
async def get_job_run(run_id: int) -> JobRunItem:
    """单条作业执行详情（含 result JSON 解析）。"""
    async with session_scope() as session:
        r = (
            await session.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail="记录不存在")

    parsed: Any = None
    if r.result_json:
        with suppress(Exception):
            parsed = json.loads(r.result_json)
    return JobRunItem.from_row(r, parsed_result=parsed)


@router.get(
    "/scheduler/{job_name}/config",
    summary="作业专属配置 (cron + 该作业的 runtime params)",
)
async def get_job_config(job_name: str) -> dict[str, Any]:
    """按 job 查 cron + 该 job 专属的 runtime param spec + 当前值.

    前端 ConfigModal 一次拿到所有需要编辑的字段, 不必额外调 /scheduler
    或 /runtime-params.
    """
    spec = scheduler_center._specs.get(job_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_name}")
    job_specs = specs_for_job(job_name)
    rp = get_runtime_params()
    return {
        "job_name": spec.name,
        "cron": spec.cron,
        "enabled": spec.enabled,
        "params": {s.key: rp.get_int(s.key) for s in job_specs},
        "specs": [
            {
                "key": s.key,
                "label": s.label,
                "min": s.min,
                "max": s.max,
                "default": s.default,
            }
            for s in job_specs
        ],
    }