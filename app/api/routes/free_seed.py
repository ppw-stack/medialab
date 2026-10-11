"""免费种候选池 API：拉取入候选池 + 手动选择推送。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.auth import require_session
from app.core.timeutils import utcnow
from app.db.models import FreeCandidate, Site
from app.db.session import session_scope
from app.services.factory import get_factory
from app.services.free_seed_hunter import (
    CandidateStatus,
    FreeRoundStats,
    FreeSeedHunter,
    PushResult,
)

router = APIRouter(prefix="/free-seed", dependencies=[Depends(require_session)], tags=["free-seed"])


#: 候选状态对应的展示颜色
CANDIDATE_STATUS_COLORS: dict[str, str] = {
    CandidateStatus.PENDING: "blue",
    CandidateStatus.PUSHED: "green",
    CandidateStatus.SKIPPED: "default",
    CandidateStatus.FAILED: "red",
}


class CandidateCard(BaseModel):
    """免费种候选卡片。"""

    id: int
    site_id: int
    site: str
    item_id: str | None
    name: str
    size: int
    free_type: str
    free_end_at: datetime | None
    free_source: str | None
    free_confidence: float | None
    remaining_seconds: float | None
    status: str
    reject_reason: str | None
    info_hash: str | None
    torrent_id: int | None
    seen_at: datetime
    pushed_at: datetime | None


class CandidateList(BaseModel):
    total: int
    items: list[CandidateCard]


class PushResultOut(BaseModel):
    candidate_id: int
    success: bool
    info_hash: str | None = None
    error: str | None = None


class PushBatchResult(BaseModel):
    total: int
    success: int
    failed: int
    results: list[PushResultOut]


class FreeRoundResult(BaseModel):
    site: str
    status: str
    pulled: int
    accepted: int
    skipped: int
    failed: int
    duration_ms: int
    skip_reasons: dict[str, int]


class PullBatchResult(BaseModel):
    total: int
    site_results: list[FreeRoundResult]


def _to_card(c: FreeCandidate, site_name: str) -> CandidateCard:
    """构造候选卡片。"""
    remaining: float | None = None
    if c.free_end_at is not None:
        remaining = (c.free_end_at - utcnow()).total_seconds()
    return CandidateCard(
        id=c.id,
        site_id=c.site_id,
        site=site_name,
        item_id=c.item_id,
        name=c.name,
        size=c.size,
        free_type=c.free_type,
        free_end_at=c.free_end_at,
        free_source=c.free_source,
        free_confidence=c.free_confidence,
        remaining_seconds=remaining,
        status=c.status,
        reject_reason=c.reject_reason,
        info_hash=c.info_hash,
        torrent_id=c.torrent_id,
        seen_at=c.seen_at,
        pushed_at=c.pushed_at,
    )


@router.get("/candidates", summary="免费种候选列表")
async def list_candidates(
    site_id: int | None = None,
    status: str | None = Query(default=None, description="pending/pushed/skipped/failed"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> CandidateList:
    async with session_scope() as session:
        base = select(FreeCandidate, Site.name).join(
            Site, Site.id == FreeCandidate.site_id
        )
        if site_id is not None:
            base = base.where(FreeCandidate.site_id == site_id)
        if status is not None:
            base = base.where(FreeCandidate.status == status)

        total = (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        rows = (
            await session.execute(
                base.order_by(FreeCandidate.seen_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_to_card(c, site_name) for c, site_name in rows]

    return CandidateList(total=total, items=items)


@router.post("/candidates/{candidate_id}/push", summary="单条推送")
async def push_one(candidate_id: int) -> PushResultOut:
    hunter = FreeSeedHunter(get_factory())
    result: PushResult = await hunter.push_candidate(candidate_id)
    return PushResultOut(
        candidate_id=result.candidate_id,
        success=result.success,
        info_hash=result.info_hash,
        error=result.error,
    )


class PushBatchRequest(BaseModel):
    """批量推送入参。"""

    ids: list[int] = Field(..., min_length=1, max_length=200)


@router.post("/candidates/push-batch", summary="批量推送")
async def push_batch(payload: PushBatchRequest) -> PushBatchResult:
    hunter = FreeSeedHunter(get_factory())
    results = await hunter.push_batch(payload.ids)
    out = [
        PushResultOut(
            candidate_id=r.candidate_id,
            success=r.success,
            info_hash=r.info_hash,
            error=r.error,
        )
        for r in results
    ]
    success = sum(1 for r in out if r.success)
    return PushBatchResult(
        total=len(out),
        success=success,
        failed=len(out) - success,
        results=out,
    )


@router.post("/pull", summary="触发全部站点抓取")
async def pull_all() -> PullBatchResult:
    """所有启用站点执行抓取，结果入 free_candidates。"""
    hunter = FreeSeedHunter(get_factory())
    results = await hunter.pull_all(force=True)
    return PullBatchResult(
        total=len(results),
        site_results=[_round_out(r) for r in results],
    )


@router.post("/sites/{site_id}/pull", summary="触发单站抓取")
async def pull_one_site(site_id: int) -> FreeRoundResult:
    """用户显式决策（FR-6.1），绕过 15 分钟频率下限。"""
    hunter = FreeSeedHunter(get_factory())
    return _round_out(await hunter.pull_site(site_id, force=True))


def _round_out(s: FreeRoundStats) -> FreeRoundResult:
    return FreeRoundResult(
        site=s.site,
        status=s.status,
        pulled=s.pulled,
        accepted=s.accepted,
        skipped=s.skipped,
        failed=s.failed,
        duration_ms=s.duration_ms,
        skip_reasons=dict(s.skip_reasons),
    )