"""总览仪表盘 API（FR-6.1）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.auth import require_session
from app.db.models import Site, Torrent, TorrentStatus
from app.db.session import session_scope

router = APIRouter(dependencies=[Depends(require_session)], tags=["dashboard"])


@router.get("/dashboard", summary="总览概览")
async def dashboard() -> dict[str, Any]:
    """站点在线状态、任务统计与磁盘概览（M1 提供统计部分）。"""
    async with session_scope() as session:
        site_total = (
            await session.execute(select(func.count(Site.id)))
        ).scalar_one()
        site_enabled = (
            await session.execute(
                select(func.count(Site.id)).where(Site.enabled.is_(True))
            )
        ).scalar_one()

        by_status = (
            await session.execute(
                select(Torrent.status, func.count(Torrent.id)).group_by(Torrent.status)
            )
        ).all()
        by_source = (
            await session.execute(
                select(Torrent.source, func.count(Torrent.id)).group_by(Torrent.source)
            )
        ).all()

        in_flight_free = (
            await session.execute(
                select(func.count(Torrent.id)).where(
                    Torrent.source == "free_seed",
                    Torrent.status.in_([TorrentStatus.ACTIVE, TorrentStatus.PAUSED]),
                )
            )
        ).scalar_one()

    # 下载器统计
    from app.db.models import DownloaderInstance
    dl_total = (
        await session.execute(select(func.count(DownloaderInstance.id)))
    ).scalar_one()
    dl_enabled = (
        await session.execute(
            select(func.count(DownloaderInstance.id)).where(
                DownloaderInstance.enabled.is_(True)
            )
        )
    ).scalar_one()

    return {
        "sites": {"total": site_total, "enabled": site_enabled},
        "downloaders": {"total": dl_total, "enabled": dl_enabled},
        "torrents": {
            "by_status": dict(by_status),
            "by_source": dict(by_source),
        },
        "free_seed": {"in_flight": in_flight_free},
    }