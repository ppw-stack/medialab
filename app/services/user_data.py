"""站点用户数据刷新服务（FR-1.6 站点用户数据契约·精简版）。

``refresh_user_data`` 周期性作业（``*/30 * * * *``）：

1. 读取所有 ``Site.enabled = 1`` 的站点
2. 按站拿到对应 :class:`SiteAdapter` 实例
3. 调单方法 :meth:`SiteAdapter.get_user_snapshot`（4 业务字段）
4. 单站失败不阻塞其他站（异常隔离）
5. upsert 到 :class:`~app.db.models.SiteUserData`（``site_id`` UNIQUE）

精简：只保留 upload / download / seeding_count / leeching_count。
其他字段（username/level/bonus/H&R/消息/做种积分）已移除。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import Site, SiteUserData
from app.db.session import get_session_factory
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep

logger = get_logger(__name__)


async def refresh_one_site(
    session: AsyncSession,
    factory: AdapterFactory,
    site_row: Site,
) -> bool:
    """刷新单站点的 user_data 行（4 字段）。返回是否成功写入。"""
    try:
        sa = factory.site(site_row)
    except Exception as exc:
        logger.warning("站点 %s 适配器初始化失败：%s", site_row.name, exc)
        return False

    try:
        snap = await sa.get_user_snapshot()
    except Exception as exc:
        logger.warning("站点 %s 拉取用户数据失败：%s", site_row.name, exc)
        return False

    payload: dict[str, object] = {"last_seen_at": bj_now()}
    if snap is not None:
        payload["upload"] = snap.upload
        payload["download"] = snap.download
        payload["seeding_count"] = snap.seeding_count
        payload["leeching_count"] = snap.leeching_count
    else:
        # 抓不到 → 写 0 兜底（让 UI 知道本周期没拉到）
        payload.update({
            "upload": 0,
            "download": 0,
            "seeding_count": 0,
            "leeching_count": 0,
        })

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

    Returns: ``{"ok": N, "failed": M, "skipped": K, "total": T, "steps": [...]}``。
    """
    factory_obj = get_session_factory()
    async with factory_obj() as session:
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
        "summary": (
            f"{len(site_rows)} 站，{ok} 成功"
            + (f"，{failed} 失败" if failed else "")
            + (f"，{skipped} 跳过" if skipped else "")
        ),
        "steps": [
            JobStep(
                "查询启用站点", "ok", len(site_rows), f"{len(site_rows)} 站"
            ),
            JobStep(
                "拉取 user_data",
                "warn" if failed else ("ok" if ok else "skip"),
                ok,
                f"{ok} 成功，{failed} 失败，{skipped} 跳过",
            ),
            JobStep(
                "写入 site_user_data", "ok" if ok else "skip", ok, f"{ok} 行 upsert"
            ),
        ],
    }


async def get_user_data_for_site(site_id: int) -> dict[str, Any] | None:
    """API 查询用：取一站最近一次 user_data（4 业务字段）。"""
    factory_obj = get_session_factory()
    async with factory_obj() as session:
        row = (await session.execute(
            select(SiteUserData).where(SiteUserData.site_id == site_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "site_id": row.site_id,
            "upload": row.upload,
            "download": row.download,
            "seeding_count": row.seeding_count,
            "leeching_count": row.leeching_count,
            "last_seen_at": row.last_seen_at,
        }


async def get_user_data_all() -> list[dict[str, Any]]:
    """API 查询用：取所有站点最近 user_data（4 业务字段）。"""
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
                "upload": ud.upload,
                "download": ud.download,
                "seeding_count": ud.seeding_count,
                "leeching_count": ud.leeching_count,
                "last_seen_at": ud.last_seen_at,
            }
            for ud, site_name, adapter in rows
        ]
