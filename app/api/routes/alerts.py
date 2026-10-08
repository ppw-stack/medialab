"""告警 API（FR-6.1）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Alert
from app.db.session import session_scope

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertOut(BaseModel):
    id: int
    ts: datetime
    level: str
    category: str
    info_hash: str | None
    message: str
    acknowledged: bool


@router.get("", summary="告警列表")
async def list_alerts(
    level: str | None = None,
    unacked_only: bool = False,
    limit: int = Query(100, ge=1, le=1000),
) -> list[AlertOut]:
    stmt = select(Alert)
    if level:
        stmt = stmt.where(Alert.level == level)
    if unacked_only:
        stmt = stmt.where(Alert.acknowledged.is_(False))
    async with session_scope() as session:
        rows = (
            (await session.execute(stmt.order_by(Alert.ts.desc()).limit(limit)))
            .scalars()
            .all()
        )
    return [
        AlertOut(
            id=r.id,
            ts=r.ts,
            level=r.level,
            category=r.category,
            info_hash=r.info_hash,
            message=r.message,
            acknowledged=r.acknowledged,
        )
        for r in rows
    ]


@router.post("/{alert_id}/ack", summary="确认告警")
async def acknowledge(alert_id: int) -> dict[str, int | bool]:
    async with session_scope() as session:
        row = (
            await session.execute(select(Alert).where(Alert.id == alert_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        row.acknowledged = True
        return {"id": alert_id, "acknowledged": True}