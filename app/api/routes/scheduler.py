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

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_session
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import job_run_service
from app.services import runtime_params as runtime_params_service
from app.services.job_run_service import JobRunRow
from app.services.job_settings import save_cron, save_enabled_state
from app.services.runtime_params import (
    PARAM_SPECS,
    ParamSpec,
    get_runtime_params,
    specs_for_job,
    validate,
)
from app.services.scheduler import RunResult, scheduler_center

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_session)], tags=["scheduler"])


def _spec_options(spec: ParamSpec) -> list[list[object]] | None:
    """把 spec 的枚举取值转成 JSON 友好结构（``[[值, 显示名], ...]``）。

    连续数值参数没有枚举取值 → 返回 None（前端渲染数字输入框）。

    参数类型用 :class:`ParamSpec` 而非 ``object`` + ``getattr``——后者会
    静默吞掉「spec 没有 options 属性」的情况（拼错字段名也不会报错，
    只是把枚举参数当成连续数值渲染）。
    """
    if not spec.options:
        return None
    return [[value, text] for value, text in spec.options]


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
    settings = get_settings()
    return {
        "enabled": settings.scheduler_enabled,
        "jobs": scheduler_center.list_jobs(),
        "inflight": scheduler_center.inflight_count(),
        "accepting_new_work": scheduler_center.accepting_new_work,
    }


@router.post("/scheduler/{job_name}/toggle", summary="启停调度作业")
async def toggle_job(job_name: str, enabled: bool) -> dict[str, Any]:
    """切换单个作业的启用状态（持久化到 DB，跨重启保持）。

    DB 写失败时返 500：开关在调度中心**已经生效**，但状态没存住，
    容器重启后会恢复成启用。必须让用户知道，否则会以为停用是永久的。
    """
    if not scheduler_center.enable(job_name, enabled):
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_name}")
    try:
        await save_enabled_state(job_name, enabled)
    except Exception as exc:
        logger.exception("作业 %s 启停状态写入 DB 失败", job_name)
        raise HTTPException(
            status_code=500,
            detail="启停状态持久化失败，容器重启后会恢复原状态",
        ) from exc
    return {"job": job_name, "enabled": enabled}


class UpdateCronIn(BaseModel):
    cron: str = Field(..., min_length=1, max_length=64,
                      description="标准 5 字段 cron 表达式")


@router.post("/scheduler/{job_name}/cron", summary="修改作业的 cron 表达式")
async def update_job_cron(job_name: str, payload: UpdateCronIn) -> dict[str, Any]:
    """运行时切换 cron 表达式，**落库**（跨重启保持）。

    不合法 cron 返 400（由 :meth:`SchedulerCenter.validate_cron` 抛 ValueError
    ——**校验逻辑的单一真源在 service 层**，路由不重复实现）。

    DB 写失败时返 500：cron 在调度中心**已经生效**，但没存住，
    容器重启后会回落到代码默认值。必须让用户知道，否则会以为修改是永久的。
    """
    try:
        scheduler_center.validate_cron(payload.cron)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"cron 表达式不合法：{exc}") from exc
    if not scheduler_center.update_cron(job_name, payload.cron):
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_name}")
    try:
        await save_cron(job_name, payload.cron)
    except Exception as exc:
        logger.exception("作业 %s 的 cron 写入 DB 失败", job_name)
        raise HTTPException(
            status_code=500,
            detail="cron 已生效但持久化失败，容器重启后会回落到默认表达式",
        ) from exc
    return {"job": job_name, "cron": payload.cron}


class RuntimeParamsOut(BaseModel):
    """运行时参数响应。"""

    params: dict[str, int]
    specs: list[dict[str, object]]


class RuntimeParamsUpdateIn(BaseModel):
    """运行时参数更新请求（部分更新）。

    字段必须与 :data:`app.services.runtime_params.PARAM_SPECS` 一致——
    之前手写字段列表漏了 ``push_download_interval_ms``，该参数被 pydantic
    静默丢弃（前端显示"已保存"，实际没存进去）。
    下面的 `_assert_params_model_covers_specs()` 在导入时校验两者一致，
    新增 PARAM_SPECS 条目而忘了加字段会立刻报错（而不是静默丢数据）。
    """

    push_min_remaining_hours: int | None = None
    push_max_per_run: int | None = None
    monitor_pre_pause_minutes: int | None = None
    monitor_delete_expired: int | None = None
    push_download_interval_ms: int | None = None
    disk_rotation_threshold_pct: int | None = None
    disk_rotation_action: int | None = None
    #: 磁盘流转模式（0 = 实际执行 / 1 = 仅预演）
    disk_rotation_dry_run: int | None = None
    scrape_enabled: int | None = None
    scrape_policy: int | None = None
    organize_enabled: int | None = None
    organize_action: int | None = None
    organize_conflict_policy: int | None = None
    scrape_season_image_to_root: int | None = None
    scrape_original_image: int | None = None
    #: 聚合归拢的单轮提交上限（0 = 不限）
    aggregate_max_submissions: int | None = None
    #: 小文件走「分享转存」的体积上限（0 = 关闭该通道）
    aggregate_share_threshold_bytes: int | None = None
    #: 归拢传输通道（0 = 按体积分流 / 1 = 转存优先）
    aggregate_transfer_mode: int | None = None


def _assert_params_model_covers_specs() -> None:
    """校验更新模型覆盖了全部已注册参数（导入时执行）。"""
    from app.services.runtime_params import PARAM_SPECS

    spec_keys = {s.key for s in PARAM_SPECS}
    model_keys = set(RuntimeParamsUpdateIn.model_fields)
    missing = spec_keys - model_keys
    if missing:
        raise RuntimeError(
            "RuntimeParamsUpdateIn 缺少已注册参数："
            f"{sorted(missing)}（新增 PARAM_SPECS 后必须同步加字段）"
        )


_assert_params_model_covers_specs()


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
                "note": spec.note,
                "options": _spec_options(spec),
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
    # 事务由 service 层管理（DB 先、内存后——顺序不能反）
    await runtime_params_service.apply_runtime_params(updates)
    return RuntimeParamsOut(
        params=get_runtime_params().all(),
        specs=[
            {
                "key": spec.key,
                "label": spec.label,
                "min": spec.min,
                "max": spec.max,
                "default": spec.default,
                "note": spec.note,
                "options": _spec_options(spec),
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
    def from_row(cls, r: JobRunRow) -> JobRunItem:
        """从 service 层 DTO 构建（DB 存北京时间字符串，直接使用）。"""
        return cls(
            id=r.id,
            job_name=r.job_name,
            trigger=r.trigger,
            started_at=r.started_at,
            finished_at=r.finished_at,
            duration_ms=r.duration_ms,
            ok=r.ok,
            error=r.error,
            result=r.result,
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
    include_logs: bool = False,
) -> JobRunPage:
    """查询作业执行历史（按 started_at desc）。

    :param job_name: 可选过滤单个作业。
    :param limit: 上限 200。
    :param include_logs: 是否返回执行日志。**默认 False**——日志 +
        明细让单条结果达 10–30KB，列表一次 100 条会到 MB 级。
        完整数据走 ``GET /scheduler/runs/{run_id}``。
    """
    page = await job_run_service.list_job_runs(
        job_name=job_name, limit=limit, offset=offset,
        include_logs=include_logs,
    )
    return JobRunPage(
        total=page.total,
        items=[JobRunItem.from_row(r) for r in page.items],
    )


@router.get("/scheduler/runs/{run_id}", summary="作业执行详情")
async def get_job_run(run_id: int) -> JobRunItem:
    """单条作业执行详情（含完整日志与大字段明细）。

    列表端点为了控制响应体积剥离了这些；详情端点返回**完整**数据。
    """
    return JobRunItem.from_row(await job_run_service.get_job_run(run_id))


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
                "note": s.note,
                "options": _spec_options(s),
            }
            for s in job_specs
        ],
    }