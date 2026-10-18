"""站点用户数据 API（FR-1.6 站点用户数据契约·精简版）。

- ``GET /api/v1/user-data`` — 所有站最近 4 字段 user_data 列表
- ``GET /api/v1/user-data/trends`` — 趋势数据（占位，目前只返最新）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.auth import require_session
from app.db.models import Site, SiteUserData
from app.db.session import session_scope

router = APIRouter(prefix="/user-data", dependencies=[Depends(require_session)])


@router.get("", summary="所有站最近用户数据")
async def list_user_data() -> list[dict[str, Any]]:
    """取所有 site_user_data 行（按 site 排序），前端 /user-data 页面用。

    Returns: ``[{site_id, site_name, adapter, upload, download,
    seeding_count, leeching_count, last_seen_at}, ...]``
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
                "upload": ud.upload,
                "download": ud.download,
                "seeding_count": ud.seeding_count,
                "leeching_count": ud.leeching_count,
                "last_seen_at": ud.last_seen_at,
            }
            for ud, site_name, adapter in rows
        ]


@router.get("/trends", summary="所有站 user_data 趋势（最近 N 周期）")
async def list_user_data_trends() -> list[dict[str, Any]]:
    """占位端点：当前 ORM 只存最新一行（UNIQUE site_id），趋势功能需要
    后续改成 history 表（site_user_data_history）。本端点先返最近一次。
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
                "seeding_count": ud.seeding_count,
                "leeching_count": ud.leeching_count,
                "last_seen_at": ud.last_seen_at,
            }
            for ud, site_name in rows
        ]
