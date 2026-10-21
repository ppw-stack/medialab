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
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.adapters.sites.base import BreakerTripped, FeedItem
from app.core.logging import get_logger
from app.core.metrics import feed_pulls, stage_duration
from app.core.timeutils import bj_now, bj_now_dt, format_bj, parse_bj
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep
from app.services.window_parser import WindowParser

logger = get_logger(__name__)

#: 429 限流 → 熔断时长（分钟）。429 明确是限流，冷却 1 小时。
_BREAKER_MINUTES_429 = 60
#: 403 → 熔断时长（分钟）。403 可能是 cookie 失效而非限流，
#: 熔断 1 小时会让「改完 cookie 仍被跳过」，故减半。
_BREAKER_MINUTES_403 = 30


async def fetch_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """按站遍历拉取种子列表，写入 :class:`TorrentIndex`。

    Returns: ``{"total_written": N, "scanned_sites": M, "steps": [...]}``。
    失败站记 ERROR 但不阻塞其他站。

    事务边界：网络拉取在**写事务外**进行，upsert 每站一个短事务——
    原先一个 session 包住全部站点，首个 upsert 即开写锁并横跨后续站点的
    网络 IO，与 push_free 等写作业并发时频繁 ``database is locked``。

    熔断（FR-2.2）：站点返回 403/429 时抛 :class:`BreakerTripped`，
    本站被熔断（写入 ``sites.breaker_until``），后续轮次**跳过该站
    且不发请求**。熔断是保护性跳过，计入 ``skipped_breaker``
    而**不是** ``failed_sites``——混进失败会让告警逻辑误报。
    """
    started = asyncio.get_running_loop().time()
    total_written = 0
    # (site_id, name, pulled, ok, ko, pull_error)
    summary: list[tuple[int, str, int, int, int, str | None]] = []
    # 拉取失败的站（网络/DNS/适配器异常）——与"站点没有种子"区分开
    pull_failures: list[tuple[str, str]] = []
    # 被熔断跳过的站（不发请求）
    breaker_skipped: list[tuple[str, str]] = []

    async with session_scope() as session:
        stmt = select(Site).where(Site.enabled.is_(True))
        result = await session.execute(stmt)
        rows: list[Site] = list(result.scalars().all())

    for row in rows:
        # 熔断期内直接跳过——继续请求只会加速账号暴露
        if _is_breaker_open(row.breaker_until):
            breaker_skipped.append((row.name, row.breaker_until or ""))
            logger.info(
                "站点 %s 处于熔断期（至 %s），跳过本轮拉取",
                row.name, row.breaker_until,
            )
            continue

        feed_items, pull_error, breaker_until = await _pull_site_items(factory, row)
        ok, ko = await _upsert_site_items(row, feed_items)
        total_written += ok
        summary.append((row.id, row.name, ok + ko, ok, ko, pull_error))
        if pull_error is not None:
            pull_failures.append((row.name, pull_error))
        elif breaker_until is not None:
            # 本轮触发熔断的站也计入「熔断跳过」——它本轮没拉到数据，
            # 但性质是保护性熔断而非故障，不该进 failed_sites。
            breaker_skipped.append((row.name, breaker_until))

    # 失败站 = 拉取失败（网络/DNS/适配器异常）或写入失败（DB 错误）的站。
    # 按站名去重——同一站可能两类错误都命中。
    failed_site_names: set[str] = {
        name for _, name, _, _, ko, _ in summary if ko > 0
    }
    failed_site_names |= {name for name, _ in pull_failures}
    total_failed = len(failed_site_names)
    total_breaker_skipped = len(breaker_skipped)

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
    if breaker_skipped:
        logger.warning(
            "站点熔断跳过 %d 站：%s",
            total_breaker_skipped,
            ", ".join(name for name, _ in breaker_skipped),
        )

    stage_duration.labels(stage="fetch_all_sites").observe(
        asyncio.get_running_loop().time() - started
    )
    failed_names = ", ".join(sorted(failed_site_names))
    breaker_names = ", ".join(sorted(name for name, _ in breaker_skipped))
    return {
        "total_written": total_written,
        "scanned_sites": len(rows),
        "failed_sites": total_failed,
        # 熔断跳过（保护性跳过，不算故障）
        "skipped_breaker": total_breaker_skipped,
        "summary": f"拉取 {len(rows)} 站，写入 {total_written} 条种子"
        + (f"，{total_failed} 站失败（{failed_names}）" if total_failed else "")
        + (
            f"，{total_breaker_skipped} 站熔断跳过（{breaker_names}）"
            if total_breaker_skipped
            else ""
        ),
        "steps": [
            JobStep(
                "查询启用站点", "ok", len(rows), f"{len(rows)} 站"
            ),
            JobStep(
                "拉取种子列表",
                "warn" if total_failed else "ok",
                len(rows) - total_failed - total_breaker_skipped,
                f"逐站拉取（上限 3 页），{total_failed} 站失败"
                + (f"：{failed_names}" if total_failed else "")
                + (
                    f"，{total_breaker_skipped} 站熔断跳过"
                    if total_breaker_skipped
                    else ""
                )
                if (total_failed or total_breaker_skipped)
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
) -> tuple[list[FeedItem], str | None, str | None]:
    """拉取单站种子列表（网络 IO，不持写事务）。

    Returns: ``(items, error, breaker_until)``：

    - ``error`` 非 None → **拉取失败**（网络/DNS/解析/适配器异常）
    - ``breaker_until`` 非 None → 本轮触发**熔断**（403/429 限流），
      已写入 ``sites.breaker_until``，本轮不再请求该站
    - 两者都为 None → 正常（items 可能为空 = 站点确实没种子）

    区分"拉取失败"与"站点确实没有种子"很重要：两者都返回空列表，
    但前者是故障（站点被墙 / cookie 失效 / DNS 挂了），若混为一谈，
    作业统计会报"全部成功"而实际漏掉了故障站（本次 prod 的 hddolby
    DNS 解析失败就是这样被掩盖的）。

    熔断（FR-2.2）：适配器抛 :class:`BreakerTripped`（403/429 限流）时
    写 ``sites.breaker_until`` 并**立即放弃**该站——继续请求只会加速
    账号暴露。此时本站算「熔断跳过」而非「拉取失败」。
    """
    try:
        adapter = factory.site(row)
    except Exception as exc:
        logger.warning("站点 %s 适配器创建失败：%s", row.name, exc)
        return ([], f"适配器创建失败：{exc}", None)
    feed_items: list[FeedItem] = []
    try:
        for page in range(1, 4):  # 拉 1-3 页
            page_items = await adapter.search_torrents(
                {}, page_size=200, page_number=page,
            )
            feed_items.extend(page_items)
            if not page_items:
                break  # 后续页无数据就停，避免无效请求
    except BreakerTripped as exc:
        # 限流/封禁 → 熔断本站，本轮不再请求
        feed_pulls.labels(result="breaker", site=row.name).inc()
        breaker_until = await _open_breaker(row, str(exc))
        logger.warning(
            "站点 %s 触发熔断，暂停拉取至 %s：%s",
            row.name, breaker_until, exc,
        )
        return (feed_items, None, breaker_until)  # 熔断不算「拉取失败」
    except Exception as exc:
        feed_pulls.labels(result="error", site=row.name).inc()
        logger.warning("站点 %s 拉取失败：%s", row.name, exc)
        # 保留已拉到的分页数据：第 3 页失败不该让前 2 页白拉
        # （但本站仍记为失败站，交由上层统计）
        return (feed_items, str(exc), None)
    if feed_items:
        feed_pulls.labels(result="ok", site=row.name).inc(len(feed_items))
    return (feed_items, None, None)


def _is_breaker_open(breaker_until: str | None) -> bool:
    """熔断是否仍然生效（``breaker_until`` 晚于当前时间）。"""
    if not breaker_until:
        return False
    until = parse_bj(breaker_until)
    if until is None:
        return False
    return until > bj_now_dt()


async def _open_breaker(row: Site, reason: str) -> str:
    """写入熔断截止时刻，返回写入的时间字符串。

    403 与 429 熔断时长不同：403 可能是 cookie 失效（不是限流），
    熔断 1 小时会让"改 cookie 后仍被跳过"；429 明确是限流，熔断 1 小时。
    """
    minutes = _BREAKER_MINUTES_403 if "403" in reason else _BREAKER_MINUTES_429
    until = format_bj(bj_now_dt() + timedelta(minutes=minutes))
    async with session_scope() as session:
        await session.execute(
            update(Site).where(Site.id == row.id).values(breaker_until=until)
        )
    return until


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


