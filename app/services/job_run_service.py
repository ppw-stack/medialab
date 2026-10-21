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

__all__ = ["JobRunPage", "JobRunRow", "get_job_run", "list_job_runs"]


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


def _to_row(row: JobRun) -> JobRunRow:
    return JobRunRow(
        id=row.id,
        job_name=row.job_name,
        trigger=row.trigger,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        ok=row.ok,
        error=row.error,
        result=_parse_result(row),
    )


async def list_job_runs(
    *, job_name: str | None = None, limit: int = 50, offset: int = 0
) -> JobRunPage:
    """作业执行历史（按 started_at desc）。

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

        rows = (await session.execute(base.limit(limit).offset(offset))).scalars().all()

    return JobRunPage(total=total, items=[_to_row(r) for r in rows])


async def get_job_run(run_id: int) -> JobRunRow:
    """单条作业执行详情（含 result JSON 解析）。

    :raises ServiceError: 404（记录不存在）
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("记录不存在", status=404)
        return _to_row(row)
