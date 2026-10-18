"""站点用户数据 API（FR-1.6 站点用户数据契约）。

- ``GET /api/v1/user-data`` — 所有站最近 user_data 列表
- 端点用于前端 /user-data 页面展示
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.auth import require_session
from app.db.models import Site, SiteUserData
from app.db.session import session_scope

router = APIRouter(prefix="/user-data", dependencies=[Depends(require_session)])


@router.get("", summary="所有站最近用户数据列表")
async def list_user_data() -> list[dict[str, Any]]:
    """取所有 site_user_data 行（按 site 排序），前端 /user-data 页面用。

    Returns: ``[{site_id, site_name, adapter, username, ..., last_seen_at}, ...]``
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(SiteUserData, Site.name, Site.adapter)
            .join(Site, Site.id == SiteUserData.site_id)
            .order_by(Site.id.asc())
        )).all()
        return [
            {
                "site_id": ud.site_id,
                "site_name": site_name,
                "adapter": adapter,
                "username": ud.username,
                "userid": ud.userid,
                "user_level": ud.user_level,
                "join_at": ud.join_at,
                "bonus": ud.bonus,
                "upload": ud.upload,
                "download": ud.download,
                "ratio": ud.ratio,
                "seeding_count": ud.seeding_count,
                "leeching_count": ud.leeching_count,
                "seeding_size": ud.seeding_size,
                "leeching_size": ud.leeching_size,
                "message_unread": ud.message_unread,
                "hr_status": ud.hr_status,
                "last_seen_at": ud.last_seen_at,
            }
            for ud, site_name, adapter in rows
        ]


@router.get("/trends", summary="所有站 user_data 趋势（最近 N 周期）")
async def list_user_data_trends(*, periods: int = 48) -> list[dict[str, Any]]:
    """取最近 N 个周期的 user_data 历史（用于趋势图）。

    NOTE: 当前 ORM 只存"最新一行" (UNIQUE site_id)，趋势功能需要后续
    改成 history 表 (site_user_data_history)。本端点先返最近一次 + 占位。
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(SiteUserData, Site.name)
            .join(Site, Site.id == SiteUserData.site_id)
            .order_by(Site.id.asc())
        )).all()
        return [
            {
                "site_id": ud.site_id,
                "site_name": site_name,
                "upload": ud.upload,
                "download": ud.download,
                "ratio": ud.ratio,
                "seeding_count": ud.seeding_count,
                "leeching_count": ud.leeching_count,
                "bonus": ud.bonus,
                "last_seen_at": ud.last_seen_at,
            }
            for ud, site_name in rows
        ]
