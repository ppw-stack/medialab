"""免费种任务 API（FR-6.3 / FR-6.4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.auth import require_session
from app.core.timeutils import remaining_until, utcnow
from app.core.windows import feasibility, inspect_interval_seconds, urgency_level
from app.db.models import AbortedReason, Site, Torrent, TorrentStatus
from app.db.session import session_scope
from app.services.factory import get_factory
from app.services.free_seed_hunter import FreeRoundStats, FreeSeedHunter
from app.services.window_watcher import WindowWatcher

router = APIRouter(prefix="/free-seed", dependencies=[Depends(require_session)], tags=["free-seed"])


class FreeSeedCard(BaseModel):
    """FR-6.3 卡片字段。"""

    id: int
    name: str
    site: str
    size: int
    free_type: str | None = None
    free_end_at: datetime | None = None
    free_source: str | None = None
    free_confidence: float | None = None
    remaining_seconds: float | None = None
    urgency: str = "normal"
    next_inspect_seconds: int = 900
    progress: float = 0.0
    downloaded: int = 0
    dlspeed: int = 0
    feasibility: str = "ok"
    eta_hours: float | None = None
    seconds_to_abort: float | None = None
    abort_at: datetime | None = None
    paused_reason: str | None = None
    status: str


class FreeSeedList(BaseModel):
    total: int
    items: list[FreeSeedCard]


class AbortRequest(BaseModel):
    """手动中止入参。"""

    reason: str = Field(default=AbortedReason.MANUAL, max_length=32)


class OutcomeSummary(BaseModel):
    """FR-6.4 收益归属统计。"""

    window_completed: int = 0
    late_completed: int = 0
    missed: int = 0
    aborted: int = 0
    window_completion_rate: float | None = None


@router.get("", summary="免费种任务列表（含窗口倒计时与可行性）")
async def list_free_seeds(
    site_id: int | None = None,
    in_flight_only: bool = True,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> FreeSeedList:
    now = utcnow()
    async with session_scope() as session:
        base = select(Torrent).where(Torrent.source == "free_seed")
        if site_id is not None:
            base = base.where(Torrent.site_id == site_id)
        if in_flight_only:
            base = base.where(
                Torrent.status.in_([TorrentStatus.ACTIVE, TorrentStatus.PAUSED])
            )

        total = (
            await session.execute(
                select(func.count(Torrent.id)).select_from(base.subquery())
            )
        ).scalar_one()

        joined = base.add_columns(Site.name).join(Site, Site.id == Torrent.site_id)
        rows = (
            (
                await session.execute(
                    joined.order_by(Torrent.free_end_at.asc().nullslast())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .all()
        )
        items = [_to_card(t, site_name, now) for t, site_name in rows]

    return FreeSeedList(total=total, items=items)


@router.get("/summary", summary="免费种收益归属统计（FR-6.4）")
async def summary(site_id: int | None = None) -> OutcomeSummary:
    async with session_scope() as session:
        stmt = select(Torrent.status, func.count(Torrent.id)).where(
            Torrent.source == "free_seed"
        )
        if site_id is not None:
            stmt = stmt.where(Torrent.site_id == site_id)
        counts = dict((await session.execute(stmt)).all())

    done = (
        counts.get(TorrentStatus.FREE_COMPLETED, 0),
        counts.get(TorrentStatus.FREE_COMPLETED_LATE, 0),
        counts.get(TorrentStatus.FREE_MISSED, 0),
        counts.get(TorrentStatus.ABORTED, 0),
    )
    total = sum(done)
    return OutcomeSummary(
        window_completed=done[0],
        late_completed=done[1],
        missed=done[2],
        aborted=done[3],
        window_completion_rate=(done[0] / total) if total else None,
    )


@router.get("/{torrent_id}", summary="免费种任务详情")
async def get_free_seed(torrent_id: int) -> FreeSeedCard:
    now = utcnow()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Torrent, Site.name)
                .join(Site, Site.id == Torrent.site_id)
                .where(Torrent.id == torrent_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _to_card(row[0], row[1], now)


@router.post("/{torrent_id}/abort", summary="手动中止免费种（FR-3.5 管线）")
async def abort_now(torrent_id: int, payload: AbortRequest = AbortRequest()) -> dict[str, Any]:
    """M1 仅落库标记；实际移除与数据隔离由 Cleaner 管线执行（M4 接入）。"""
    now = utcnow()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Torrent).where(Torrent.id == torrent_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        row.status = TorrentStatus.ABORTED
        row.aborted_reason = payload.reason
        row.aborted_at = now
        return {"id": row.id, "status": row.status, "aborted_reason": row.aborted_reason}


def _to_card(t: Torrent, site_name: str, now: datetime) -> FreeSeedCard:
    """构造卡片。紧迫度与可行性均基于 ``free_end_at`` 与实时进度。"""
    remaining = remaining_until(t.free_end_at, now=now)
    level = urgency_level(remaining)
    to_abort = (t.abort_at - now).total_seconds() if t.abort_at else None

    dlspeed = 0  # M2 起由巡检写入 torrents.last_dlspeed
    remaining_bytes = max(t.size - t.downloaded, 0)
    feasibility_state = feasibility(remaining_bytes, dlspeed, window=remaining)
    eta_hours = (
        round(remaining_bytes / dlspeed / 3600, 2) if dlspeed > 0 and remaining_bytes else None
    )

    return FreeSeedCard(
        id=t.id,
        name=t.name,
        site=site_name,
        size=t.size,
        free_type=t.free_type,
        free_end_at=t.free_end_at,
        free_source=t.free_source,
        free_confidence=t.free_confidence,
        remaining_seconds=remaining.total_seconds() if remaining else None,
        urgency=level,
        next_inspect_seconds=inspect_interval_seconds(level),
        progress=(t.downloaded / t.size) if t.size else 0.0,
        downloaded=t.downloaded,
        dlspeed=dlspeed,
        feasibility=feasibility_state,
        eta_hours=eta_hours,
        seconds_to_abort=to_abort,
        abort_at=t.abort_at,
        paused_reason=t.paused_reason,
        status=t.status,
    )




class FreeRoundResult(BaseModel):
    site: str
    status: str
    pulled: int
    accepted: int
    skipped: int
    added: int
    failed: int
    permanent: int
    duration_ms: int
    skip_reasons: dict[str, int]


def _round_out(s: FreeRoundStats) -> FreeRoundResult:
    return FreeRoundResult(
        site=s.site,
        status=s.status,
        pulled=s.pulled,
        accepted=s.accepted,
        skipped=s.skipped,
        added=s.added,
        failed=s.failed,
        permanent=s.permanent,
        duration_ms=s.duration_ms,
        skip_reasons=dict(s.skip_reasons),
    )


@router.post("/pull", summary="手动触发免费种捕获（FR-3 全轮）")
async def pull_free_seeds() -> list[FreeRoundResult]:
    """立即对所有启用站点跑一轮免费种捕获。"""
    hunter = FreeSeedHunter(get_factory())
    return [_round_out(s) for s in await hunter.run_all()]


@router.post("/sites/{site_id}/pull", summary="手动触发单站免费种")
async def pull_one_site(site_id: int) -> FreeRoundResult:
    """忽略 15 分钟频率下限（用户显式决策，FR-6.1）。"""
    hunter = FreeSeedHunter(get_factory())
    return _round_out(await hunter.run_site(site_id, force=True))


@router.post("/watch", summary="手动触发窗口看护（FR-3.4）")
async def watch_now() -> dict[str, int]:
    """立即执行一轮看护：对所有在途免费种按紧迫度评估。"""
    watcher = WindowWatcher(get_factory())
    return await watcher.watch_active()
