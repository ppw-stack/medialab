"""调度作业注册——统一 RSS 拉取。

唯一的调度任务是 :func:`fetch_all_sites`：按站遍历所有启用站点，
调用各 adapter 的 :meth:`SiteAdapter.search_torrents` 拉取最新 RSS，
把 :class:`FeedItem` upsert 到 :class:`TorrentIndex` 表。

周期由 cron 配置（默认 ``0 * * * *``，每小时整点）。

注：本模块不再做辅种匹配或限免过滤——全部 RSS 项目都写入索引表
供查询/筛选，外部系统按需使用。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.core.metrics import feed_pulls, stage_duration
from app.core.timeutils import utcnow
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.scheduler import JobSpec, scheduler_center
from app.services.window_parser import WindowParser

logger = get_logger(__name__)


async def fetch_all_sites(factory: AdapterFactory) -> int:
    """按站遍历拉取 RSS，写入 :class:`TorrentIndex`。

    返回成功 upsert 的条目数。失败站记 ERROR 但不阻塞其他站。
    """
    started = asyncio.get_running_loop().time()
    total_written = 0
    summary: list[tuple[int, str, int, int, int]] = []  # (site_id, name, pulled, ok, ko)

    async with session_scope() as session:
        stmt = select(Site).where(Site.enabled.is_(True))
        result = await session.execute(stmt)
        rows: list[Site] = list(result.scalars().all())

        for row in rows:
            ok, ko = await _pull_one_site(factory, row, session)
            total_written += ok
            summary.append((row.id, row.name, ok + ko, ok, ko))

    if total_written:
        logger.info(
            "RSS 拉取：共 %d 条种子写入 torrent_index（站数 %d）",
            total_written, len(rows),
        )
    else:
        logger.info("RSS 拉取：无种子写入（站数 %d）", len(rows))

    stage_duration.labels(stage="fetch_all_sites").observe(
        asyncio.get_running_loop().time() - started
    )
    return total_written


async def _pull_one_site(
    factory: AdapterFactory,
    row: Site,
    session: AsyncSession,
) -> tuple[int, int]:
    """拉取单站 RSS 并 upsert；返回 (ok_count, ko_count)。"""
    # 创建 adapter（含凭据解密）
    try:
        adapter = factory.site(row)
    except Exception as exc:
        logger.warning("站点 %s 适配器创建失败：%s", row.name, exc)
        return (0, 0)

    # 拉 RSS
    try:
        feed_items = await adapter.search_torrents({}, page_size=100, page_number=1)
    except Exception as exc:
        feed_pulls.labels(result="error", site=row.name).inc()
        logger.warning("站点 %s 拉取失败：%s", row.name, exc)
        return (0, 0)

    if not feed_items:
        feed_pulls.labels(result="ok", site=row.name).inc()
        return (0, 0)

    # upsert
    ok = 0
    now = utcnow()
    for fi in feed_items:
        item_id = (fi.item_id or "").strip() or None
        window = WindowParser.parse(fi)
        stmt = sqlite_insert(TorrentIndex).values(
            site_id=row.id,
            item_id=item_id,
            info_hash=fi.info_hash,
            name=fi.title[:1024],
            size=fi.size,
            category=fi.category,
            description=fi.description or None,
            free_type=window.free_type,
            free_end_at=window.free_end_at,
            free_source=window.free_source,
            free_confidence=window.confidence,
            discount_pct=window.discount_pct,
            torrent_url=fi.torrent_url,
            free_payload=fi.free_payload or {},
            published_at=fi.published_at,
            first_seen_at=now,
            last_seen_at=now,
        ).on_conflict_do_update(
            index_elements=["site_id", "item_id"],
            set_={
                "info_hash": fi.info_hash,
                "name": fi.title[:1024],
                "size": fi.size,
                "category": fi.category,
                "description": fi.description or None,
                "free_type": window.free_type,
                "free_end_at": window.free_end_at,
                "free_source": window.free_source,
                "free_confidence": window.confidence,
                "discount_pct": window.discount_pct,
                "torrent_url": fi.torrent_url,
                "free_payload": fi.free_payload or {},
                "published_at": fi.published_at,
                "last_seen_at": now,
            },
        )
        try:
            await session.execute(stmt)
            ok += 1
        except Exception as exc:
            logger.debug(
                "站点 %s item_id=%s upsert 失败：%s", row.name, item_id, exc
            )

    if ok:
        feed_pulls.labels(result="ok", site=row.name).inc(ok)
    return (ok, 0)


def register_jobs(
    factory: AdapterFactory,
    *,
    settings: AppSettings | None = None,
) -> None:
    """注册唯一的 RSS 拉取作业。

    :meth:`register_jobs` 接收 :class:`AdapterFactory` 而不是具体
    adapter——作业函数本身负责按站创建 adapter。这是与之前
    :func:`register_seed_jobs`（接受 engine+hunter 实例）最大的区别。
    """
    cfg = settings or get_settings()
    scheduler_center.register(
        JobSpec(
            name="fetch_all_sites",
            func=lambda: _swallow(fetch_all_sites(factory)),
            cron=cfg.fetch_all_sites_cron,
        )
    )
    logger.info(
        "已注册 RSS 拉取作业：fetch_all_sites cron=%r",
        cfg.fetch_all_sites_cron,
    )


async def _swallow(result: Awaitable[object]) -> None:
    """作业返回值对调度器无意义；仅确保 await 完成。"""
    await result