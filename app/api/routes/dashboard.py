"""总览仪表盘 API（FR-6.1）。

轻量版：仅展示基础信息（站点/下载器启用情况），不含任务统计。
任务视图改由下载器原生接口提供（直读，不落表）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.auth import require_session
from app.db.models import DownloaderInstance, Site
from app.db.session import session_scope

router = APIRouter(dependencies=[Depends(require_session)], tags=["dashboard"])


@router.get("/dashboard", summary="总览概览")
async def dashboard() -> dict[str, Any]:
    """站点在线状态、下载器启用情况（轻量版）。"""
    async with session_scope() as session:
        site_total = (
            await session.execute(select(func.count(Site.id)))
        ).scalar_one()
        site_enabled = (
            await session.execute(
                select(func.count(Site.id)).where(Site.enabled.is_(True))
            )
        ).scalar_one()

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
    }
