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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import Site, SiteUserData
from app.db.session import get_session_factory, session_scope
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep

logger = get_logger(__name__)


async def refresh_one_site(
    session: AsyncSession,
    factory: AdapterFactory,
    site_row: Site,
) -> bool:
    """刷新单站点的 user_data 行（4 字段）。返回是否成功写入。

    **注意**：本函数会在传入的 session 上下文内做网络调用
    (``sa.get_user_snapshot()``)。调用方应确保 session 不是长事务写锁
    （参考 :func:`refresh_all_sites` 与 :func:`refresh_site_user_data`
    的短事务模式）。若需在网络拉取期间避免持锁，调用方应在外层
    先完成网络拉取拿到 snapshot，再开短事务写入。
    """
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

    payload: dict[str, object] = _snapshot_payload(snap)

    stmt = select(SiteUserData).where(SiteUserData.site_id == site_row.id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        session.add(SiteUserData(site_id=site_row.id, **payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)
    return True


async def refresh_site_by_id(
    factory: AdapterFactory,
    site_id: int,
) -> bool:
    """刷新单个站点的 user_data（**网络 IO 在事务外**）。

    与 :func:`refresh_one_site` 的区别：后者在**传入的** session 上下文内
    做网络调用，调用方要自己保证 session 不是长事务写锁；本函数自己
    管事务——先开短事务读出站点行 → 关闭 → 网络拉取 → 再开短事务写入。

    手动刷新走的是 HTTP 请求路径，写锁横跨网络往返会阻塞 cron 与其他
    写（``database is locked``），故这里明确拆成两段事务。

    :returns: 是否成功写入。
    """
    # 1) 短事务读站点行
    # 注：``expire_on_commit=False`` → session 关闭后已加载的属性仍可读，
    # 故 detached 的 site_row 能安全地交给 factory.site()（与
    # ``site_service.test_site_feed`` 同一模式）。
    async with session_scope() as session:
        site_row = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site_row is None:
            return False
        site_name = site_row.name

    # 2) 网络拉取（无事务）
    try:
        sa = factory.site(site_row)
    except Exception as exc:
        logger.warning("站点 %s 适配器初始化失败：%s", site_name, exc)
        return False
    try:
        snap = await sa.get_user_snapshot()
    except Exception as exc:
        logger.warning("站点 %s 拉取用户数据失败：%s", site_name, exc)
        return False

    # 3) 短事务 upsert
    async with session_scope() as session:
        await _upsert_user_data(session, site_id, _snapshot_payload(snap))
    return True


def _snapshot_payload(snap: SiteUserSnapshot | None) -> dict[str, object]:
    """把快照转成 upsert 载荷。

    抓不到（``snap is None``）→ 写 0 兜底（让 UI 知道本周期没拉到）。
    """
    payload: dict[str, object] = {"last_seen_at": bj_now()}
    if snap is not None:
        payload["upload"] = snap.upload
        payload["download"] = snap.download
        payload["seeding_count"] = snap.seeding_count
        payload["leeching_count"] = snap.leeching_count
    else:
        payload.update({
            "upload": 0,
            "download": 0,
            "seeding_count": 0,
            "leeching_count": 0,
        })
    return payload


async def _upsert_user_data(
    session: AsyncSession, site_id: int, payload: dict[str, object]
) -> None:
    """把载荷 upsert 到 ``site_user_data``（site_id UNIQUE）。"""
    stmt = select(SiteUserData).where(SiteUserData.site_id == site_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        session.add(SiteUserData(site_id=site_id, **payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)


async def refresh_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """刷新所有启用站点的 user_data。

    Returns: ``{"ok": N, "failed": M, "skipped": K, "total": T, "steps": [...]}``。

    每站独立短事务：避免单 session 内循环网络 IO 导致写锁横跨所有
    站点网络拉取（违反"作业短事务化"，与 push_free 并发时易 database is locked）。
    """
    factory_obj = get_session_factory()
    # 短事务读站点列表，脱离 session 后只用标量字段
    async with factory_obj() as session:
        site_rows = (await session.execute(
            select(Site).where(Site.enabled.is_(True))
        )).scalars().all()
        # 只提取需要的标量（id / name）——其余字段在每站的事务里重新加载，
        # 不在这里读，避免 detached 访问 ORM 实例
        sites = [(r.id, r.name) for r in site_rows]
    total = len(sites)

    ok = 0
    failed = 0
    skipped = 0
    for site_id, site_name in sites:
        try:
            # 每站独立 session（短事务），网络 IO 不持写锁。
            # 必须用 session_scope()——它负责 commit；裸 `async with
            # factory() as session:` 退出时只 close 不 commit，
            # 写进去的 site_user_data 会全部丢失（而 refresh_one_site
            # 仍返回 True，计数会谎报成功）。
            async with session_scope() as session:
                # 重新加载 site 行（拿到完整 ORM 实例供 refresh_one_site）
                site_row = (await session.execute(
                    select(Site).where(Site.id == site_id)
                )).scalar_one_or_none()
                if site_row is None:
                    skipped += 1
                    continue
                wrote = await refresh_one_site(session, factory, site_row)
            if wrote:
                ok += 1
            else:
                skipped += 1
        except Exception as exc:
            failed += 1
            logger.exception("站点 %s 刷新异常：%s", site_name, exc)

    logger.info("refresh_user_data 完成：ok=%d failed=%d skipped=%d total=%d",
                ok, failed, skipped, total)
    return {
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "summary": (
            f"{total} 站，{ok} 成功"
            + (f"，{failed} 失败" if failed else "")
            + (f"，{skipped} 跳过" if skipped else "")
        ),
        "steps": [
            JobStep(
                "查询启用站点", "ok", total, f"{total} 站"
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
