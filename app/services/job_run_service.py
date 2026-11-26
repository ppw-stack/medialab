"""调度作业执行历史查询服务。

此前 :mod:`app.api.routes.scheduler` 的路由层直接写 SQL 查 ``job_runs``
（4 处 ``session_scope``）+ 解析 ``result_json``。本模块把查询下沉。

作业的**启停 / cron / 运行参数**逻辑本来就住在
:mod:`app.services.scheduler` / :mod:`app.services.runtime_params` /
:mod:`app.services.job_settings`，路由只做转发——那部分保持不变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db.models import JobRun
from app.db.session import session_scope
from app.services.errors import ServiceError

logger = get_logger(__name__)

__all__ = ["JobRunPage", "JobRunRow", "get_job_run", "get_latest_job_run", "list_job_runs"]


@dataclass(frozen=True, slots=True)
class JobRunRow:
    """单条作业执行历史。"""

    id: int
    job_name: str
    trigger: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    ok: bool
    error: str | None
    result: Any | None


@dataclass(frozen=True, slots=True)
class JobRunPage:
    """作业历史分页。"""

    total: int
    items: list[JobRunRow]


def _parse_result(row: JobRun) -> Any | None:
    """解析 ``result_json``（损坏时记日志，返回 None）。"""
    if not row.result_json:
        return None
    try:
        return json.loads(row.result_json)
    except (ValueError, TypeError) as exc:
        # result_json 应是合法 JSON（内部写入），损坏时记日志便于排查
        logger.warning("JobRun id=%s result_json 解析失败：%s", row.id, exc)
        return None


#: 列表响应里要剥离的**大字段**（路径 → 说明）。
#:
#: 这些字段单条就能到几百 KB——``cross_seed_maintenance`` 的
#: ``results[].stats`` 含完整 40 位 hash 列表（辅种站快照 590KB，
#: 见 AGENTS.md）。列表一次 100 条 → 最大 ~59MB 响应，页面直接卡死。
#:
#: 列表只保留摘要（``summary`` / ``steps`` 里的聚合计数），完整数据
#: 由 ``GET /scheduler/runs/{run_id}`` 按需拉取。
#:
#: 路径语义：逐段下钻，**遇到 list 就对每个元素继续下钻**，
#: 最后一段是「要删掉的键」。
_HEAVY_PATHS: tuple[tuple[str, ...], ...] = (
    # 辅种巡查：每个下载器的完整 stats（含 40 位 hash 列表）
    ("cross_seed", "results", "stats"),
    ("cross_seed", "results", "anomalies"),
    # 磁盘流转：逐下载器的删除明细
    ("disk_rotation", "results"),
    # 孤儿扫描：逐条孤儿条目（路径 + 大小）
    ("scan_orphans", "results", "orphans"),
)


def _strip_heavy(result: Any) -> Any:
    """剥离结果里的**大字段**（原地修改）。

    只处理「路径存在」的组合——缺失的键直接跳过，旧数据（没有这些
    字段）不受影响。
    """
    if not isinstance(result, dict):
        return result
    for path in _HEAVY_PATHS:
        _strip_path(result, path)
    return result


def _strip_path(node: Any, path: tuple[str, ...]) -> None:
    """按路径递归剥离（list 元素逐个处理）。"""
    if not path:
        return
    key, rest = path[0], path[1:]
    if isinstance(node, list):
        for el in node:
            _strip_path(el, path)
        return
    if not isinstance(node, dict):
        return
    if not rest:
        # 末段：直接删掉这个键
        node.pop(key, None)
        return
    _strip_path(node.get(key), rest)


def _to_row(
    row: JobRun, *, include_logs: bool = True, strip_heavy: bool = True
) -> JobRunRow:
    """把 DB 行转成 DTO。

    :param include_logs: 是否保留执行日志。列表端点传 False。
    :param strip_heavy: 是否剥离大字段。列表端点传 True；详情端点传
        False——用户点开某次运行时**要看到完整数据**（含 hash 列表、
        孤儿条目逐条路径等）。
    """
    result = _parse_result(row)
    if isinstance(result, dict):
        if not include_logs:
            result.pop("logs", None)
        if strip_heavy:
            _strip_heavy(result)
    return JobRunRow(
        id=row.id,
        job_name=row.job_name,
        trigger=row.trigger,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        ok=row.ok,
        error=row.error,
        result=result,
    )


async def list_job_runs(
    *,
    job_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_logs: bool = False,
) -> JobRunPage:
    """作业执行历史（按 started_at desc）。

    :param job_name: 可选过滤单个作业。
    :param limit: 上限 200。
    :param include_logs: 是否返回执行日志。**列表默认 False**——
        日志 + 明细让单条 ``result_json`` 达 10–30KB，列表一次 100 条
        会到 MB 级。完整数据走 :func:`get_job_run`。
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

        rows = (await session.execute(base.limit(limit).offset(offset))).scalars().all()

    return JobRunPage(
        total=total,
        items=[_to_row(r, include_logs=include_logs) for r in rows],
    )


async def get_latest_job_run(
    job_name: str, *, include_logs: bool = False
) -> JobRunRow | None:
    """取某作业**最近一次**执行记录；没有任何记录时返回 None。

    专供高频轮询（如面板 5s 拉一次的免费种观测）：:func:`list_job_runs`
    固定先做一次 ``COUNT(*)`` 全表统计，只取 1 行时纯属浪费——``job_runs``
    按作业频率持续增长（每 10 分钟一条），长跑后是数万行。这里直接
    ``ORDER BY started_at DESC LIMIT 1``，不计数。

    :param job_name: 作业名（``JobSpec.name``）。
    :param include_logs: 是否保留执行日志（轮询默认不要）。
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(JobRun)
                .where(JobRun.job_name == job_name)
                .order_by(JobRun.started_at.desc(), JobRun.id.desc())
                .limit(1)
            )
        ).scalars().first()
    if row is None:
        return None
    return _to_row(row, include_logs=include_logs)


async def get_job_run(run_id: int) -> JobRunRow:
    """单条作业执行详情（含 result JSON 解析 + 完整日志 + 完整大字段）。

    详情端点返回**完整**数据（执行日志、大字段明细都在）——
    列表端点为了控制响应体积会把它们剥离掉。

    :raises ServiceError: 404（记录不存在）
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("记录不存在", status=404)
        # 详情端点返回**完整**数据：日志 + 大字段明细都在
        return _to_row(row, include_logs=True, strip_heavy=False)
