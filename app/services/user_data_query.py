"""站点用户数据查询服务（FR-1.6 站点用户数据契约·精简版）。

此前 :mod:`app.api.routes.user_data` 的路由层直接写 SQL（3 处
``session_scope``）。本模块把查询下沉，路由只做结果转换。

``SiteUserData`` 是**当前快照**（UNIQUE site_id），不做历史——如需时间
序列需先建 ``site_user_data_history`` 表再迁（见
:mod:`app.services.user_data`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.db.models import Site, SiteUserData
from app.db.session import session_scope

__all__ = ["UserDataRow", "UserDataTrendRow", "list_user_data", "list_user_data_trends"]


@dataclass(frozen=True, slots=True)
class UserDataRow:
    """某站最近一次用户数据（含 adapter 名）。"""

    site_id: int
    site_name: str
    adapter: str
    upload: int
    download: int
    seeding_count: int
    leeching_count: int
    #: 时魔（每小时获得的站点积分·合计口径）
    hourly_bonus: float | None
    #: 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分）
    hourly_bonus_unit: str | None
    #: 是否已达该站的获取上限（None = 该站无法判断）
    hourly_bonus_capped: bool | None
    #: 站点进度百分比
    hourly_bonus_progress: float | None
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class UserDataTrendRow:
    """趋势行（当前只有最近一次快照）。"""

    site_id: int
    site_name: str
    upload: int
    download: int
    seeding_count: int
    leeching_count: int
    hourly_bonus: float | None
    hourly_bonus_unit: str | None
    hourly_bonus_capped: bool | None
    hourly_bonus_progress: float | None
    last_seen_at: str | None


async def list_user_data() -> list[UserDataRow]:
    """所有站最近的用户数据（按 site 排序）。"""
    async with session_scope() as session:
        rows = (await session.execute(
            select(SiteUserData, Site.name, Site.adapter)
            .join(Site, Site.id == SiteUserData.site_id)
            .order_by(Site.id.asc())
        )).all()
    return [
        UserDataRow(
            site_id=ud.site_id,
            site_name=site_name,
            adapter=adapter,
            upload=ud.upload,
            download=ud.download,
            seeding_count=ud.seeding_count,
            leeching_count=ud.leeching_count,
            hourly_bonus=ud.hourly_bonus,
            hourly_bonus_unit=ud.hourly_bonus_unit,
            hourly_bonus_capped=ud.hourly_bonus_capped,
            hourly_bonus_progress=ud.hourly_bonus_progress,
            last_seen_at=ud.last_seen_at,
        )
        for ud, site_name, adapter in rows
    ]


async def list_user_data_trends() -> list[UserDataTrendRow]:
    """各站 user_data 趋势（当前只有最近一次快照）。

    ORM 只存最新一行（UNIQUE site_id），趋势功能需要后续改成 history 表
    （``site_user_data_history``）。本函数先返最近一次。
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(SiteUserData, Site.name)
            .join(Site, Site.id == SiteUserData.site_id)
            .order_by(Site.id.asc())
        )).all()
    return [
        UserDataTrendRow(
            site_id=ud.site_id,
            site_name=site_name,
            upload=ud.upload,
            download=ud.download,
            seeding_count=ud.seeding_count,
            leeching_count=ud.leeching_count,
            hourly_bonus=ud.hourly_bonus,
            hourly_bonus_unit=ud.hourly_bonus_unit,
            hourly_bonus_capped=ud.hourly_bonus_capped,
            hourly_bonus_progress=ud.hourly_bonus_progress,
            last_seen_at=ud.last_seen_at,
        )
        for ud, site_name in rows
    ]
