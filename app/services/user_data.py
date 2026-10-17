"""站点用户数据刷新服务（FR-1.6 站点用户数据契约）。

``refresh_user_data`` 周期性作业（``*/30 * * * *``）：

1. 读取所有 ``Site.enabled = 1`` 的站点
2. 按站拿到对应 :class:`SiteAdapter` 实例
3. 串行调 4 个用户数据方法（``get_user_info`` / ``get_seeding_stats``
   / ``get_message_count`` / ``get_hr_status``）
4. 单站失败不阻塞其他站（异常隔离）
5. upsert 到 :class:`~app.db.models.SiteUserData`（``site_id`` UNIQUE）

设计决策（per 用户确认）：

- 暂不发送告警推送（仅落库 + UI 展示）
- 暂不取做种详情（仅汇总 count + size，避免触发 429）
- 9 站全覆盖（hdsky/hhanclub/audiences/hdhome/hddolby/ourbits/soulvoice/
  springsunday + mteam）—— 基类提供默认 ``None``/``0`` 实现，
  各站按需 override
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sites.base import SiteAdapter
from app.adapters.sites.user_data import (
    HrStatus,
    MessageSummary,
    SeedingStats,
    UserInfo,
)
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import Site, SiteUserData
from app.db.session import get_session_factory
from app.services.factory import AdapterFactory

logger = get_logger(__name__)


async def _fetch_user_info(sa: SiteAdapter) -> dict[str, object]:
    """调 adapter 4 个方法并合并成一个 upsert dict。"""
    info: UserInfo | None = await sa.get_user_info()
    stats: SeedingStats | None = await sa.get_seeding_stats()
    msg: MessageSummary = await sa.get_message_count()
    hr: HrStatus = await sa.get_hr_status()

    out: dict[str, object] = {"last_seen_at": bj_now()}
    if info is not None:
        out.update(asdict(info))
    else:
        # 仍写 0 兜底值，让 UI 知道本周期没拉到
        out.update({
            "username": None,
            "userid": None,
            "user_level": None,
            "join_at": None,
            "bonus": 0.0,
            "upload": 0,
            "download": 0,
            "ratio": 0.0,
        })
    if stats is not None:
        out.update({
            "seeding_count": stats.seeding_count,
            "leeching_count": stats.leeching_count,
            "seeding_size": stats.seeding_size,
            "leeching_size": stats.leeching_size,
        })
    else:
        out.update({
            "seeding_count": 0,
            "leeching_count": 0,
            "seeding_size": 0,
            "leeching_size": 0,
        })
    out["message_unread"] = msg.unread_count
    out["hr_status"] = hr.is_hr
    return out


async def refresh_one_site(
    session: AsyncSession,
    factory: AdapterFactory,
    site_row: Site,
) -> bool:
    """刷新单站点的 user_data 行。返回是否成功写入。"""
    try:
        sa = factory.site(site_row)
    except Exception as exc:
        logger.warning("站点 %s 适配器初始化失败：%s", site_row.name, exc)
        return False

    try:
        payload = await _fetch_user_info(sa)
    except Exception as exc:
        logger.warning("站点 %s 拉取用户数据失败：%s", site_row.name, exc)
        return False

    # upsert by site_id
    stmt = select(SiteUserData).where(SiteUserData.site_id == site_row.id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        session.add(SiteUserData(site_id=site_row.id, **payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)
    return True


async def refresh_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """刷新所有启用站点的 user_data。

    Returns: ``{"ok": N, "failed": M, "skipped": K, "total": T}``。
    """
    factory_obj = get_session_factory()
    async with factory_obj() as session:
        # 拉所有 enabled 站点
        site_rows = (await session.execute(
            select(Site).where(Site.enabled == True)  # noqa: E712
        )).scalars().all()

        ok = 0
        failed = 0
        skipped = 0
        for site_row in site_rows:
            try:
                wrote = await refresh_one_site(session, factory, site_row)
                if wrote:
                    ok += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                logger.exception("站点 %s 刷新异常：%s", site_row.name, exc)
        await session.commit()

    logger.info("refresh_user_data 完成：ok=%d failed=%d skipped=%d total=%d",
                ok, failed, skipped, len(site_rows))
    return {
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "total": len(site_rows),
    }


async def get_user_data_for_site(site_id: int) -> dict[str, Any] | None:
    """API 查询用：取一站最近一次 user_data。"""
    factory_obj = get_session_factory()
    async with factory_obj() as session:
        row = (await session.execute(
            select(SiteUserData).where(SiteUserData.site_id == site_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "site_id": row.site_id,
            "username": row.username,
            "userid": row.userid,
            "user_level": row.user_level,
            "join_at": row.join_at,
            "bonus": row.bonus,
            "upload": row.upload,
            "download": row.download,
            "ratio": row.ratio,
            "seeding_count": row.seeding_count,
            "leeching_count": row.leeching_count,
            "seeding_size": row.seeding_size,
            "leeching_size": row.leeching_size,
            "message_unread": row.message_unread,
            "hr_status": row.hr_status,
            "last_seen_at": row.last_seen_at,
        }


async def get_user_data_all() -> list[dict[str, Any]]:
    """API 查询用：取所有站点最近 user_data。"""
    factory_obj = get_session_factory()
    async with factory_obj() as session:
        rows = (await session.execute(
            select(SiteUserData, Site.name, Site.adapter)
            .join(Site, Site.id == SiteUserData.site_id)
            .order_by(SiteUserData.last_seen_at.desc())
        )).all()
        return [
            {
                "site_id": ud.site_id,
                "site_name": site_name,
                "adapter": adapter,
                "username": ud.username,
                "user_level": ud.user_level,
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
