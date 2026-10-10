"""辅种任务 API（FR-6.1）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.auth import require_session
from app.db.models import Site, Torrent, TorrentStatus
from app.db.session import session_scope

router = APIRouter(prefix="/torrents", dependencies=[Depends(require_session)], tags=["torrents"])


class TorrentOut(BaseModel):
    id: int
    info_hash: str
    name: str
    site: str
    category: str | None
    status: str
    source: str
    size: int
    downloaded: int
    uploaded: int
    ratio: float
    state: str | None
    is_free: bool
    free_type: str | None
    free_end_at: datetime | None
    added_at: datetime
    protect_until: datetime | None


@router.get("", summary="任务列表（多维筛选 + 服务端分页）")
async def list_torrents(
    site_id: int | None = None,
    source: str | None = None,
    status: str | None = None,
    include_deleted: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    async with session_scope() as session:
        base = select(Torrent, Site.name).join(Site, Site.id == Torrent.site_id)
        if site_id is not None:
            base = base.where(Torrent.site_id == site_id)
        if source:
            base = base.where(Torrent.source == source)
        if status:
            base = base.where(Torrent.status == status)
        elif not include_deleted:
            base = base.where(Torrent.status != TorrentStatus.DELETED)

        total = (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(Torrent.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .all()
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_to_out(t, site_name) for t, site_name in rows],
    }


@router.get("/{torrent_id}", summary="任务详情")
async def get_torrent(torrent_id: int) -> TorrentOut:
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
        return _to_out(row[0], row[1])


def _to_out(t: Torrent, site_name: str) -> TorrentOut:
    return TorrentOut(
        id=t.id,
        info_hash=t.info_hash,
        name=t.name,
        site=site_name,
        category=t.category,
        status=t.status,
        source=t.source,
        size=t.size,
        downloaded=t.downloaded,
        uploaded=t.uploaded,
        ratio=t.ratio,
        state=t.state,
        is_free=t.is_free,
        free_type=t.free_type,
        free_end_at=t.free_end_at,
        added_at=t.added_at,
        protect_until=t.protect_until,
    )