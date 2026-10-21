"""站点拉取作业（按站遍历拉取种子列表 → torrent_index）。

- :func:`fetch_all_sites` — 按站遍历所有启用站点，调用各 adapter 的
  :meth:`SiteAdapter.search_torrents` 拉取最新种子列表，把 :class:`FeedItem`
  upsert 到 :class:`TorrentIndex` 表。

作业**注册**在 :mod:`app.services.job_registry`（本模块只管业务逻辑）。

注：本模块不再做辅种匹配或限免过滤——全部站点拉取的种子都写入索引表
供查询/筛选，外部系统按需使用。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.adapters.sites.base import FeedItem
from app.core.logging import get_logger
from app.core.metrics import feed_pulls, stage_duration
from app.core.timeutils import bj_now
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep
from app.services.window_parser import WindowParser

logger = get_logger(__name__)


async def fetch_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """按站遍历拉取种子列表，写入 :class:`TorrentIndex`。

    Returns: ``{"total_written": N, "scanned_sites": M, "steps": [...]}``。
    失败站记 ERROR 但不阻塞其他站。

    事务边界：网络拉取在**写事务外**进行，upsert 每站一个短事务——
    原先一个 session 包住全部站点，首个 upsert 即开写锁并横跨后续站点的
    网络 IO，与 push_free 等写作业并发时频繁 ``database is locked``。
    """
    started = asyncio.get_running_loop().time()
    total_written = 0
    # (site_id, name, pulled, ok, ko, pull_error)
    summary: list[tuple[int, str, int, int, int, str | None]] = []
    # 拉取失败的站（网络/DNS/适配器异常）——与"站点没有种子"区分开
    pull_failures: list[tuple[str, str]] = []

    async with session_scope() as session:
        stmt = select(Site).where(Site.enabled.is_(True))
        result = await session.execute(stmt)
        rows: list[Site] = list(result.scalars().all())

    for row in rows:
        feed_items, pull_error = await _pull_site_items(factory, row)
        ok, ko = await _upsert_site_items(row, feed_items)
        total_written += ok
        summary.append((row.id, row.name, ok + ko, ok, ko, pull_error))
        if pull_error is not None:
            pull_failures.append((row.name, pull_error))

    # 失败站 = 拉取失败（网络/DNS/适配器异常）或写入失败（DB 错误）的站。
    # 按站名去重——同一站可能两类错误都命中。
    failed_site_names: set[str] = {
        name for _, name, _, _, ko, _ in summary if ko > 0
    }
    failed_site_names |= {name for name, _ in pull_failures}
    total_failed = len(failed_site_names)

    if total_written:
        logger.info(
            "站点拉取：共 %d 条种子写入 torrent_index（站数 %d）",
            total_written, len(rows),
        )
    else:
        logger.info("站点拉取：无种子写入（站数 %d）", len(rows))
    if pull_failures:
        logger.warning(
            "站点拉取失败 %d 站：%s",
            len(pull_failures),
            ", ".join(f"{name}({err[:60]})" for name, err in pull_failures),
        )

    stage_duration.labels(stage="fetch_all_sites").observe(
        asyncio.get_running_loop().time() - started
    )
    failed_names = ", ".join(sorted(failed_site_names))
    return {
        "total_written": total_written,
        "scanned_sites": len(rows),
        "failed_sites": total_failed,
        "summary": f"拉取 {len(rows)} 站，写入 {total_written} 条种子"
        + (f"，{total_failed} 站失败（{failed_names}）" if total_failed else ""),
        "steps": [
            JobStep(
                "查询启用站点", "ok", len(rows), f"{len(rows)} 站"
            ),
            JobStep(
                "拉取种子列表",
                "warn" if total_failed else "ok",
                len(rows) - total_failed,
                f"逐站拉取（上限 3 页），{total_failed} 站失败"
                + (f"：{failed_names}" if total_failed else "")
                if total_failed
                else "逐站拉取（上限 3 页）",
            ),
            JobStep(
                "写入 torrent_index",
                "ok" if total_written else "skip",
                total_written,
                f"{total_written} 条种子 upsert",
            ),
        ],
    }


async def _pull_site_items(
    factory: AdapterFactory, row: Site
) -> tuple[list[FeedItem], str | None]:
    """拉取单站种子列表（网络 IO，不持写事务）。

    Returns: ``(items, error)``——``error`` 非 None 表示**拉取失败**
    （网络/DNS/解析/适配器异常）。

    区分"拉取失败"与"站点确实没有种子"很重要：两者都返回空列表，
    但前者是故障（站点被墙 / cookie 失效 / DNS 挂了），若混为一谈，
    作业统计会报"全部成功"而实际漏掉了故障站（本次 prod 的 hddolby
    DNS 解析失败就是这样被掩盖的）。
    """
    try:
        adapter = factory.site(row)
    except Exception as exc:
        logger.warning("站点 %s 适配器创建失败：%s", row.name, exc)
        return ([], f"适配器创建失败：{exc}")
    try:
        feed_items: list[FeedItem] = []
        for page in range(1, 4):  # 拉 1-3 页
            page_items = await adapter.search_torrents(
                {}, page_size=200, page_number=page,
            )
            feed_items.extend(page_items)
            if not page_items:
                break  # 后续页无数据就停，避免无效请求
    except Exception as exc:
        feed_pulls.labels(result="error", site=row.name).inc()
        logger.warning("站点 %s 拉取失败：%s", row.name, exc)
        # 保留已拉到的分页数据：第 3 页失败不该让前 2 页白拉
        # （但本站仍记为失败站，交由上层统计）
        return (feed_items, str(exc))
    if feed_items:
        feed_pulls.labels(result="ok", site=row.name).inc(len(feed_items))
    return (feed_items, None)


async def _upsert_site_items(
    row: Site, feed_items: list[FeedItem]
) -> tuple[int, int]:
    """批量 upsert 单站条目（每站一个短事务，避免写锁横跨网络 IO）。"""
    if not feed_items:
        return (0, 0)
    ok = 0
    now = bj_now()
    async with session_scope() as session:
        for fi in feed_items:
            item_id = (fi.item_id or "").strip() or None
            window = WindowParser.parse(fi)
            stmt = sqlite_insert(TorrentIndex).values(
                site_id=row.id,
                item_id=item_id,
                # info_hash 不写：list 适配器硬编码 None，会污染 push_free 写的真 hash
                name=fi.title[:1024],
                size=fi.size,
                category=fi.category,
                description=fi.description or None,
                free_type=window.free_type,
                free_end_at=window.free_end_at,
                free_source=window.free_source,
                free_confidence=window.confidence,
                discount_pct=window.discount_pct,
                free_payload=fi.free_payload or {},
                published_at=fi.published_at,
                first_seen_at=now,
                last_seen_at=now,
            ).on_conflict_do_update(
                index_elements=["site_id", "item_id"],
                set_={
                    # info_hash 不覆盖：list 适配器 None 会清空 push_free 写的真 hash
                    "name": fi.title[:1024],
                    "size": fi.size,
                    "category": fi.category,
                    "description": fi.description or None,
                    "free_type": window.free_type,
                    "free_end_at": window.free_end_at,
                    "free_source": window.free_source,
                    "free_confidence": window.confidence,
                    "discount_pct": window.discount_pct,
                    "free_payload": fi.free_payload or {},
                    "published_at": fi.published_at,
                    "last_seen_at": now,
                },
            )
            try:
                # 用 savepoint 隔离单条 upsert 失败——rollback 会回滚
                # 整个未提交事务（包括前序成功的行），导致 ok 计数与实际
                # 入库行数不一致。begin_nested 失败只回滚到 savepoint。
                async with session.begin_nested():
                    await session.execute(stmt)
                    ok += 1
            except Exception as exc:
                logger.debug(
                    "站点 %s item_id=%s upsert 失败：%s", row.name, item_id, exc
                )
    return (ok, 0)


