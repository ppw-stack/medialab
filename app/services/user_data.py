"""站点用户数据刷新服务（FR-1.6 站点用户数据契约·精简版 + 时魔）。

``refresh_user_data`` 周期性作业（``*/30 * * * *``）：

1. 读取所有 ``Site.enabled = 1`` 的站点
2. 按站拿到对应 :class:`SiteAdapter` 实例
3. 调单方法 :meth:`SiteAdapter.get_user_snapshot`（4 业务字段 + 时魔）
4. 单站失败不阻塞其他站（异常隔离）
5. upsert 到 :class:`~app.db.models.SiteUserData`（``site_id`` UNIQUE）

**网络 IO 在事务外**：先并发抓完所有站点 → 再开短事务批量 upsert。
写锁不能横跨网络往返（与 cron 拉取作业争 SQLite 写锁 →
``database is locked``）。

**失败必须计数**：适配器抛 :class:`~app.adapters.sites.base.SiteFetchError`
时计入 ``failed`` 并在 summary 里点名，**不写库**——保留上一次的快照。
不写 0 兜底：故障被伪装成「上传 0 / 做种 0」时，前端完全看不出哪站挂了
（2026-10-21 prod 事故的教训；签到契约已按同一原则改过一轮）。

**并发**：9 站串行最坏 9×20s；并发（上限 5）后墙钟时间 ≈ 最慢的站。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import Site, SiteUserData
from app.db.session import get_session_factory, session_scope
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep, JobStepItem, build_items

logger = get_logger(__name__)

__all__ = ["refresh_all_sites", "refresh_site_by_id"]

#: 同时抓取的站点数上限
#:
#: 9 站串行最坏 9×20s；并发后墙钟 ≈ 最慢的站。上限 5 是为了不给 PT 站
#: 的连接配额压力太大（每站 1 个连接池）。
_FETCH_CONCURRENCY = 5


@dataclass(frozen=True, slots=True)
class _FetchResult:
    """单站抓取结果（成功 / 跳过 / 失败）。"""

    site_id: int
    site_name: str
    #: 抓到的快照；``None`` = 该站不支持 / 无凭据（跳过）
    snapshot: SiteUserSnapshot | None
    #: 失败原因；``None`` = 没失败
    error: str | None = None


async def _fetch_site(
    factory: AdapterFactory, site_row: Site
) -> _FetchResult:
    """抓一个站点的用户数据（**网络 IO，不在事务内**）。

    失败**不抛**——异常收敛成 :class:`_FetchResult.error`，由调用方统一
    计数与点名（单站失败不阻塞其他站）。
    """
    try:
        adapter = factory.site(site_row)
        snap = await adapter.get_user_snapshot()
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        logger.warning("站点 %s 拉取用户数据失败：%s", site_row.name, reason)
        return _FetchResult(site_row.id, site_row.name, None, reason)
    return _FetchResult(site_row.id, site_row.name, snap, None)


def _snapshot_payload(snap: SiteUserSnapshot) -> dict[str, object]:
    """把快照转成 upsert 载荷。

    时魔缺失（该站不支持 / 本次没解析出）→ **不产出**时魔字段，
    upsert 时就不会覆盖库里已知的旧值（保留上次快照）。
    """
    payload: dict[str, object] = {
        "upload": snap.upload,
        "download": snap.download,
        "seeding_count": snap.seeding_count,
        "last_seen_at": bj_now(),
    }
    bonus = snap.hourly_bonus
    if bonus is not None:
        payload["hourly_bonus"] = bonus.amount
        payload["hourly_bonus_unit"] = bonus.unit
        payload["hourly_bonus_capped"] = bonus.capped
        payload["hourly_bonus_progress"] = bonus.progress
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


async def refresh_site_by_id(
    factory: AdapterFactory,
    site_id: int,
) -> bool:
    """刷新单个站点的 user_data（**网络 IO 在事务外**）。

    先开短事务读出站点行 → 关闭 → 网络拉取 → 再开短事务写入。

    手动刷新走的是 HTTP 请求路径，写锁横跨网络往返会阻塞 cron 与其他
    写（``database is locked``），故这里明确拆成两段事务。

    :returns: 是否成功写入。拉取失败返回 ``False``（不写库，保留旧快照）。
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

    # 2) 网络拉取（无事务）
    result = await _fetch_site(factory, site_row)
    if result.error is not None:
        return False
    if result.snapshot is None:
        # 该站不支持 / 无凭据 → 不写库
        return False

    # 3) 短事务 upsert
    async with session_scope() as session:
        await _upsert_user_data(
            session, site_id, _snapshot_payload(result.snapshot)
        )
    return True


async def refresh_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """刷新所有启用站点的 user_data。

    Returns: ``{"ok": N, "failed": M, "skipped": K, "total": T,
    "capped": C, "steps": [...]}``。

    三段式：**短事务读站点 → 并发抓取（网络 IO 在事务外）→ 短事务批量写**。
    """
    factory_obj = get_session_factory()
    # 1) 短事务读站点列表（脱离 session 后只读标量字段）
    async with factory_obj() as session:
        site_rows = (
            await session.execute(
                select(Site).where(Site.enabled.is_(True)).order_by(Site.id)
            )
        ).scalars().all()
        # detached 的 ORM 实例仍可读属性（expire_on_commit=False），
        # 供 factory.site() 用
        sites = list(site_rows)
    total = len(sites)

    # 2) 并发抓取（网络 IO 在事务外，不持写锁）
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(site_row: Site) -> _FetchResult:
        async with semaphore:
            return await _fetch_site(factory, site_row)

    results = await asyncio.gather(*(_one(row) for row in sites))

    # 3) 统计 + 短事务批量 upsert
    ok = 0
    failed = 0
    skipped = 0
    capped = 0
    failures: list[tuple[str, str]] = []
    to_write: list[tuple[int, SiteUserSnapshot]] = []
    # 逐站明细（前端「步骤卡片」展开后可见）——回答"哪个站没抓到 / 多少"
    site_items: list[JobStepItem] = []
    for res in results:
        if res.error is not None:
            failed += 1
            failures.append((res.site_name, res.error))
            site_items.append(
                JobStepItem(res.site_name, "fail", None, res.error)
            )
            continue
        if res.snapshot is None:
            skipped += 1
            site_items.append(
                JobStepItem(res.site_name, "skip", None, "不支持 / 无凭据")
            )
            continue
        ok += 1
        to_write.append((res.site_id, res.snapshot))
        bonus = res.snapshot.hourly_bonus
        detail = (
            f"上传 {res.snapshot.upload}，下载 {res.snapshot.download}，"
            f"做种 {res.snapshot.seeding_count}"
        )
        if bonus is not None:
            detail += f"，时魔 {bonus.amount:g} {bonus.unit}"
            if bonus.capped:
                capped += 1
                detail += "（已封顶）"
        site_items.append(JobStepItem(res.site_name, "ok", None, detail))

    if to_write:
        async with session_scope() as session:
            for site_id, snap in to_write:
                await _upsert_user_data(
                    session, site_id, _snapshot_payload(snap)
                )

    logger.info(
        "refresh_user_data 完成：ok=%d failed=%d skipped=%d capped=%d total=%d",
        ok, failed, skipped, capped, total,
    )
    items, items_total = build_items(site_items)
    return {
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "capped": capped,
        "total": total,
        "summary": _build_summary(total, ok, failed, skipped, capped, failures),
        "steps": _build_steps(
            total, ok, failed, skipped, capped, failures, items, items_total,
        ),
    }


def _build_summary(
    total: int,
    ok: int,
    failed: int,
    skipped: int,
    capped: int,
    failures: list[tuple[str, str]],
) -> str:
    """人话摘要（前端直接展示，避免前端硬编码字段名）。"""
    parts = [f"{total} 站，{ok} 成功"]
    if skipped:
        parts.append(f"{skipped} 跳过")
    if failed:
        names = ", ".join(name for name, _ in failures)
        parts.append(f"{failed} 失败（{names}）")
    if capped:
        parts.append(f"{capped} 站已封顶")
    return "，".join(parts)


def _build_steps(
    total: int,
    ok: int,
    failed: int,
    skipped: int,
    capped: int,
    failures: list[tuple[str, str]],
    items: list[JobStepItem] | None = None,
    items_total: int | None = None,
) -> list[JobStep]:
    """步骤卡片（前端「步骤卡片」数据源）。"""
    steps: list[JobStep] = [
        JobStep("查询启用站点", "ok", total, f"{total} 站"),
    ]

    if failed:
        detail = f"{ok} 成功，{skipped} 跳过，{failed} 失败"
        steps.append(
            JobStep("拉取 user_data", "warn", ok, detail, items=items,
                    items_total=items_total)
        )
    elif ok:
        steps.append(
            JobStep("拉取 user_data", "ok", ok, f"{ok} 成功，{skipped} 跳过",
                    items=items, items_total=items_total)
        )
    else:
        steps.append(JobStep("拉取 user_data", "skip", 0, "无可刷新的站点"))

    steps.append(
        JobStep(
            "写入 site_user_data",
            "ok" if ok else "skip",
            ok,
            f"{ok} 行 upsert" + (f"（{skipped} 站保留旧快照）" if skipped else ""),
        )
    )

    if capped:
        steps.append(JobStep("时魔已封顶", "warn", capped, f"{capped} 站已达获取上限"))

    if failures:
        steps.append(
            JobStep(
                "失败站点",
                "fail",
                len(failures),
                # 不再硬截断到 200 字——逐站明细已进 ``items``，
                # 这里只给一句人话摘要（完整原因在明细里）。
                "；".join(f"{name}: {reason}" for name, reason in failures),
            )
        )
    return steps
