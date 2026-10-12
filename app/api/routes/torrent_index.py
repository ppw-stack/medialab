"""拉取索引查询 API（FR-6.3）。

列出所有由 :func:`app.services.jobs.fetch_all_sites` 写入
:class:`~app.db.models.TorrentIndex` 的种子快照。

提供站点 / free_type / 关键字过滤与分页。详情窗口可读可推——后续手动
"按索引推送"功能可基于此 API 实现。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Query as QueryParam
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.auth import require_session
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope

router = APIRouter(
    prefix="/torrent-index",
    dependencies=[Depends(require_session)],
    tags=["torrent-index"],
)


class TorrentIndexItem(BaseModel):
    """单条拉取索引条目。"""

    id: int
    site_id: int
    site_name: str
    item_id: str | None
    info_hash: str | None
    name: str
    size: int
    category: str | None
    free_type: str
    free_end_at: datetime | None
    free_source: str | None
    free_confidence: float | None
    has_torrent_url: bool
    first_seen_at: datetime
    last_seen_at: datetime


class TorrentIndexPage(BaseModel):
    """分页响应。"""

    total: int
    items: list[TorrentIndexItem]


@router.get("", response_model=TorrentIndexPage)
async def list_torrent_index(
    site_id: int | None = QueryParam(default=None, description="按站点过滤"),
    free_type: str | None = QueryParam(default=None, description="按 free_type 过滤"),
    q: str | None = QueryParam(default=None, description="种子名 / info_hash 关键字"),
    offset: int = QueryParam(default=0, ge=0),
    limit: int = QueryParam(default=50, ge=1, le=200),
) -> TorrentIndexPage:
    """返回拉取索引分页列表，按 ``last_seen_at DESC`` 排序。"""
    stmt = select(TorrentIndex, Site.name).join(Site, Site.id == TorrentIndex.site_id)

    if site_id is not None:
        stmt = stmt.where(TorrentIndex.site_id == site_id)
    if free_type:
        stmt = stmt.where(TorrentIndex.free_type == free_type)
    if q:
        stmt = stmt.where(
            or_(
                TorrentIndex.name.contains(q),
                TorrentIndex.info_hash == q.lower(),
                TorrentIndex.info_hash.contains(q.lower()),
            )
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    async with session_scope() as session:
        total = (await session.execute(count_stmt)).scalar_one() or 0
        rows = (
            await session.execute(
                stmt.order_by(TorrentIndex.last_seen_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()

    items = [
        TorrentIndexItem(
            id=row.TorrentIndex.id,
            site_id=row.TorrentIndex.site_id,
            site_name=row.name,
            item_id=row.TorrentIndex.item_id,
            info_hash=row.TorrentIndex.info_hash,
            name=row.TorrentIndex.name,
            size=row.TorrentIndex.size,
            category=row.TorrentIndex.category,
            free_type=row.TorrentIndex.free_type,
            free_end_at=row.TorrentIndex.free_end_at,
            free_source=row.TorrentIndex.free_source,
            free_confidence=row.TorrentIndex.free_confidence,
            has_torrent_url=row.TorrentIndex.torrent_url is not None,
            first_seen_at=row.TorrentIndex.first_seen_at,
            last_seen_at=row.TorrentIndex.last_seen_at,
        )
        for row in rows
    ]
    return TorrentIndexPage(total=int(total), items=items)


@router.get("/sites", response_model=list[dict[str, int | str]])
async def list_sites() -> list[dict[str, int | str]]:
    """返回站点列表供前端过滤下拉。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Site.id, Site.name).order_by(Site.id.asc())
            )
        ).all()
    return [{"id": r[0], "name": r[1]} for r in rows]


@router.get("/free-types", response_model=list[str])
async def list_free_types() -> list[str]:
    """返回当前存在的 free_type 取值列表，供前端过滤下拉。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TorrentIndex.free_type)
                .distinct()
                .order_by(TorrentIndex.free_type.asc())
            )
        ).all()
    return [r[0] for r in rows if r[0]]


@router.get("/{index_id}", response_model=TorrentIndexItem)
async def get_torrent_index(index_id: int) -> TorrentIndexItem:
    """返回单条拉取索引详情。"""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(TorrentIndex, Site.name)
                .join(Site, Site.id == TorrentIndex.site_id)
                .where(TorrentIndex.id == index_id)
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="torrent_index entry not found")
    return TorrentIndexItem(
        id=row.TorrentIndex.id,
        site_id=row.TorrentIndex.site_id,
        site_name=row.name,
        item_id=row.TorrentIndex.item_id,
        info_hash=row.TorrentIndex.info_hash,
        name=row.TorrentIndex.name,
        size=row.TorrentIndex.size,
        category=row.TorrentIndex.category,
        free_type=row.TorrentIndex.free_type,
        free_end_at=row.TorrentIndex.free_end_at,
        free_source=row.TorrentIndex.free_source,
        free_confidence=row.TorrentIndex.free_confidence,
        has_torrent_url=row.TorrentIndex.torrent_url is not None,
        first_seen_at=row.TorrentIndex.first_seen_at,
        last_seen_at=row.TorrentIndex.last_seen_at,
    )