"""辅种作业 API（FR-6.1：手动触发一次拉取 / RSS 连通性测试）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import require_session
from app.core.config import get_settings
from app.core.crypto import build_cipher
from app.core.logging import get_logger
from app.db.models import Feed, Site
from app.db.session import session_scope
from app.services.factory import AdapterFactory, get_factory
from app.services.feed_fetcher import FeedFetcher
from app.services.seed_engine import RoundStats, SeedEngine

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_session)], tags=["cross-seed"])


class FeedTestResult(BaseModel):
    """RSS 连通性测试结果（FR-6.1 站点管理）。"""

    ok: bool
    status: str
    item_count: int = 0
    duration_ms: int = 0
    sample_titles: list[str] = []
    error: str = ""


class RoundResult(BaseModel):
    """一轮辅种的结果。"""

    site: str
    status: str
    pulled: int = 0
    accepted: int = 0
    skipped: int = 0
    added: int = 0
    failed: int = 0
    duration_ms: int = 0
    skip_reasons: dict[str, int] = {}


class RoundList(BaseModel):
    rounds: list[RoundResult]


def _stats_out(stats: RoundStats) -> RoundResult:
    return RoundResult(
        site=stats.site,
        status=stats.status,
        pulled=stats.pulled,
        accepted=stats.accepted,
        skipped=stats.skipped,
        added=stats.added,
        failed=stats.failed,
        duration_ms=stats.duration_ms,
        skip_reasons=dict(stats.skip_reasons),
    )


@router.get("/feeds", summary="RSS 源状态（FR-2.2 拉取记录）")
async def list_feeds() -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Feed, Site.name)
                    .join(Site, Site.id == Feed.site_id)
                    .order_by(Site.name)
                )
            )
            .all()
        )
    return [
        {
            "site": site_name,
            "interval_minutes": feed.interval_minutes,
            "last_pulled_at": feed.last_pulled_at,
            "last_status": feed.last_status,
            "last_item_count": feed.last_item_count,
            "last_duration_ms": feed.last_duration_ms,
            "breaker_until": feed.breaker_until,
            "enabled": feed.enabled,
        }
        for feed, site_name in rows
    ]


@router.get("/sites/{site_id}/feed-test", summary="RSS 连通性测试")
async def test_feed(site_id: int) -> FeedTestResult:
    """测试站点 RSS 可达性与解析结果。

    **不落库、不触发辅种**——纯诊断，避免用户"测试"操作意外添加任务。
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
    except Exception as exc:
        return FeedTestResult(ok=False, status="credential_error", error=str(exc))

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


@router.post("/sites/{site_id}/pull", summary="手动触发一次辅种拉取")
async def pull_now(site_id: int) -> RoundResult:
    """立即对指定站点跑一轮辅种，忽略频率下限（FR-6.1）。

    用户主动操作不受 FR-2.2 的 15 分钟下限约束——该下限是针对**自动**
    拉取的账号保护。手动触发是显式决策，但仍记录审计。
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        if not site.enabled:
            raise HTTPException(status_code=409, detail="站点已禁用")

    engine = SeedEngine(get_factory())
    stats = await engine.run_site(site_id, force=True)
    return _stats_out(stats)


@router.post("/pull-all", summary="手动触发全部站点")
async def pull_all() -> RoundList:
    engine = SeedEngine(get_factory())
    return RoundList(rounds=[_stats_out(s) for s in await engine.run_all()])


@router.get("/scheduler", summary="调度作业状态")
async def scheduler_status() -> dict[str, Any]:
    from app.services.scheduler import scheduler_center

    settings = get_settings()
    cipher = build_cipher(settings)  # 确认密钥可用（FR-7.1）
    _ = cipher
    return {
        "enabled": settings.scheduler_enabled,
        "jobs": scheduler_center.list_jobs(),
        "inflight": scheduler_center.inflight_count(),
        "accepting_new_work": scheduler_center.accepting_new_work,
    }


@router.post("/scheduler/{job_name}/toggle", summary="启停调度作业")
async def toggle_job(job_name: str, enabled: bool) -> dict[str, Any]:
    from app.services.scheduler import scheduler_center

    if not scheduler_center.enable(job_name, enabled):
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_name}")
    return {"job": job_name, "enabled": enabled}