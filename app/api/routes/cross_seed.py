"""RSS 拉取作业 API 与 RSS 连通性测试。

历史背景：本文件原名 ``cross_seed.py``（辅种作业 API），现在仅保留
三个端点：

- ``GET /scheduler``：查询调度作业状态（自动从注册的 JobSpec 列表生成）
- ``POST /scheduler/{job_name}/toggle``：启停单个作业
- ``POST /scheduler/{job_name}/cron``：运行时修改 cron 表达式
- ``POST /scheduler/{job_name}/run``：手动触发作业（不等 cron）
- ``GET /sites/{site_id}/feed-test``：测试站点 RSS 可达性，纯诊断

辅种匹配（``pull`` / ``pull-all``）与 ``feeds`` 列表已删除——见 commit
``refactor: 统一 RSS 拉取作业 + 删除辅种匹配 + 删除 free_candidates``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from apscheduler.triggers.cron import CronTrigger

from app.api.auth import require_session
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Site
from app.db.session import session_scope
from app.services.factory import AdapterFactory, get_factory
from app.services.feed_fetcher import FeedFetcher
from app.services.scheduler import RunResult, scheduler_center

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_session)], tags=["rss"])


class FeedTestResult(BaseModel):
    """RSS 连通性测试结果。"""

    ok: bool
    status: str
    item_count: int = 0
    duration_ms: int = 0
    sample_titles: list[str] = []
    error: str = ""


class RunJobResult(BaseModel):
    """手动触发作业响应（对齐 :class:`RunResult`）。"""

    ok: bool
    started_at: datetime
    finished_at: datetime
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


@router.get("/sites/{site_id}/feed-test", summary="RSS 连通性测试")
async def test_feed(site_id: int) -> FeedTestResult:
    """测试站点 RSS 可达性与解析结果。

    **不落库、不触发 cron**——纯诊断，避免用户"测试"操作意外添加任务。
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")

    factory: AdapterFactory = get_factory()
    try:
        url = factory.site_rss_url(site)
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        logger.warning("站点 %s 凭据解析失败：%s", site.name, exc)
        return FeedTestResult(ok=False, status="credential_error", error=str(exc))
    except Exception:
        logger.exception("站点 %s RSS 测试内部异常", site.name)
        return FeedTestResult(ok=False, status="internal_error", error="RSS URL 生成失败")

    adapter = factory.site(site)
    fetcher = FeedFetcher(site.name)
    result = await fetcher.fetch(adapter, url)
    return FeedTestResult(
        ok=result.ok,
        status=str(result.status),
        item_count=len(result.items),
        duration_ms=result.duration_ms,
        sample_titles=[item.title for item in result.items[:5]],
        error=result.error,
    )