"""免费种猎手（FR-3）。

流水线（每轮）：

1. **拉取**：复用 M2 的 :class:`~app.services.feed_fetcher.FeedFetcher` 与
   :class:`~app.services.factory.AdapterFactory`。标记来源为 ``free_seed``。
2. **解析窗口**：把 :class:`~app.adapters.sites.base.FeedItem` 转为
   :class:`~app.core.windows.FreeWindow`（用 :class:`SiteAdapter.parse_window`）。
3. **过滤**：永久免费、跳过阈值（90min）、冷却（FR-2.3.1 复用）。
4. **锚点落库**：写 ``free_end_at`` / ``deadline_at`` / ``abort_at`` / 窗口来源。
5. **投递**：与 M2 共用 ``add_torrent``，但幂等键区别 — 同一 hash 在
   ``source=free_seed`` 与 ``source=auto_seed`` 下可同时存在（辅种池与
   免费种池），所以幂等键用 ``info_hash + site + source``。

看护（FR-3.4）由 :mod:`app.services.window_watcher` 单独执行。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import AddTorrentPayload
from app.adapters.sites._bencode import infohash_from_torrent
from app.adapters.sites.base import FeedItem
from app.core.logging import get_logger
from app.core.metrics import free_seed_outcomes
from app.core.timeutils import utcnow
from app.core.windows import FreeWindow, WindowPolicy
from app.db.models import (
    AddHistory,
    CooldownReason,
    DownloaderInstance,
    Feed,
    FreeSource,
    FreeType,
    Site,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.cooldown import CooldownService
from app.services.factory import AdapterFactory
from app.services.feed_fetcher import FeedFetcher, PullStatus
from app.services.matcher import extract_info_hash
from app.services.task_queue import TaskKind, TaskQueueService, make_idem_key
from app.services.window_parser import WindowParser
from app.services.window_service import (
    DeadlineCalculator,
    SiteWindowService,
    is_permanent_free,
)

logger = get_logger(__name__)

#: 单轮最多投递（与 M2 一致，避免单次风暴打爆下载器）
MAX_ADD_PER_ROUND = 200
#: 禁删保护期（M2 同）
PROTECT_HOURS = 12


@dataclass
class FreeRoundStats:
    """一轮免费种捕获统计。"""

    site: str = ""
    status: str = PullStatus.OK
    pulled: int = 0
    accepted: int = 0
    skipped: int = 0
    added: int = 0
    failed: int = 0
    permanent: int = 0  # 转辅种池的永久免费
    duration_ms: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


def free_idem_key(site_id: int, info_hash: str) -> str:
    """免费种任务的幂等键（FR-3 与 M2 互不冲突）。"""
    return make_idem_key(
        TaskKind.ADD_TORRENT, site_id=site_id, info_hash=info_hash, extra="free"
    )


class FreeSeedHunter:
    """免费种捕获编排。"""

    def __init__(self, factory: AdapterFactory) -> None:
        self._factory = factory

    # ------------------------------------------------------------ 单站
    async def run_site(
        self, site_id: int, *, force: bool = False
    ) -> FreeRoundStats:
        """对单个站点执行一轮免费种捕获。

        ``force=True`` 用于手动触发，绕过 FR-2.2 的 15 分钟下限。
        """
        started = time.monotonic()
        async with session_scope() as session:
            site = (
                await session.execute(select(Site).where(Site.id == site_id))
            ).scalar_one_or_none()
            if site is None or not site.enabled:
                return FreeRoundStats(status="disabled")

            feed = (
                await session.execute(
                    select(Feed).where(
                        Feed.site_id == site_id, Feed.enabled.is_(True)
                    )
                )
            ).scalar_one_or_none()
            if feed is None:
                return FreeRoundStats(site=site.name, status="no_feed")

            if not force:
                fetcher = FeedFetcher(site.name)
                allowed, _why = fetcher.should_pull(
                    feed, interval_minutes=site.min_interval_minutes
                )
                if not allowed:
                    return FreeRoundStats(
                        site=site.name, status=PullStatus.INTERVAL_NOT_MET
                    )

            stats = FreeRoundStats(site=site.name)
            fetcher = FeedFetcher(site.name)
            stats = await self._pull_and_capture(session, site, feed, fetcher, stats)

        free_seed_outcomes.labels(outcome=stats.status, reason="").inc()
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return stats

    async def _pull_and_capture(
        self,
        session: AsyncSession,
        site: Site,
        feed: Feed,
        fetcher: FeedFetcher,
        stats: FreeRoundStats,
    ) -> FreeRoundStats:
        adapter = self._factory.site(site)
        url = self._factory.site_rss_url(site)
        result = await fetcher.fetch(adapter, url)
        stats.status = str(result.status)
        if not result.ok:
            return stats

        feed.last_pulled_at = utcnow()
        feed.last_status = str(result.status)
        feed.last_item_count = len(result.items)
        feed.last_duration_ms = result.duration_ms

        items = list(result.items)[:MAX_ADD_PER_ROUND]
        stats.pulled = len(items)
        await self._capture_items(session, site, items, stats)
        return stats

    # ------------------------------------------------------------ 逐条目
    async def _capture_items(
        self,
        session: AsyncSession,
        site: Site,
        items: list[FeedItem],
        stats: FreeRoundStats,
    ) -> None:
        win = SiteWindowService(session)
        cooldown = CooldownService(session)
        dl = await self._resolve_downloader(session, site)

        for item in items:
            await self._capture_one(session, site, item, win, cooldown, dl, stats)

    async def _capture_one(
        self,
        session: AsyncSession,
        site: Site,
        item: FeedItem,
        win: SiteWindowService,
        cooldown: CooldownService,
        dl: DownloaderInstance | None,
        stats: FreeRoundStats,
    ) -> None:
        """单条目完整处理：解析 → 过滤 → 落库 → 投递 → 审计。"""
        info_hash = extract_info_hash(item)
        if info_hash is None:
            # m-team 等 API 站点 search 不返回 hash——惰性下载 torrent 解析
            info_hash = await self._resolve_infohash(site, item)
        if info_hash is None:
            stats.note_skip("no_info_hash")
            return

        try:
            window = WindowParser.parse(item)
        except Exception as exc:
            logger.debug("站点 %s 解析失败：%s", site.name, exc)
            window = FreeWindow()

        # 永久免费：直接转辅种池（FR-3.0 表 + R10）
        if window.is_permanent:
            await self._record_permanent(session, site, info_hash, item, stats, dl)
            return

        # 窗口未知：低置信度降级，按 unknown 处理（FR-1.5.3）
        if not window.is_actionable:
            stats.note_skip("window_unknown")
            await cooldown.record(
                site.id, info_hash, CooldownReason.DEAD_TORRENT
            )
            return

        # FR-3.2 跳过阈值（同一条件只有"跳过"一种动作）
        skip, reason = win.should_skip_short_candidate(
            window.free_type, window.free_end_at
        )
        if skip:
            stats.note_skip(reason)
            return

        # 冷却
        verdict = await cooldown.is_blocked(site.id, info_hash)
        if verdict.blocked:
            stats.note_skip("cooldown")
            return

        if dl is None:
            stats.note_skip("no_downloader")
            return

        await self._add_to_free_seed_pool(
            session, site, dl, item, info_hash, window, stats
        )

    async def _add_to_free_seed_pool(
        self,
        session: AsyncSession,
        site: Site,
        dl: DownloaderInstance,
        item: FeedItem,
        info_hash: str,
        window: FreeWindow,
        stats: FreeRoundStats,
    ) -> None:
        """免费种任务落库并投递。"""
        url = item.torrent_url or item.link
        if not url:
            stats.note_skip("no_url")
            return

        calc = DeadlineCalculator(win_policy(site))
        if window.free_end_at is None:
            stats.note_skip("no_end_at")
            return
        free_end_at, deadline_at, abort_at = calc.compute(window.free_end_at)

        # 幂等：同一 free_seed 任务不入两次
        from sqlalchemy import delete

        await session.execute(
            delete(Torrent).where(
                Torrent.info_hash == info_hash,
                Torrent.source == Source.FREE_SEED,
                Torrent.status == TorrentStatus.ABORTED,
            )
        )

        # 已存在同 source 的有效任务 → 跳过（同一 free_seed 不能有多个）
        existing = (
            await session.execute(
                select(Torrent).where(
                    Torrent.info_hash == info_hash,
                    Torrent.source == Source.FREE_SEED,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            stats.note_skip("duplicate_in_pool")
            return

        # 投递
        await self._add_to_downloader(
            session, site, dl, item, info_hash, stats,
            free_type=window.free_type,
            free_source=window.free_source,
            free_end_at=free_end_at,
            deadline_at=deadline_at,
            abort_at=abort_at,
        )
        return

    async def _add_to_downloader(
        self,
        session: AsyncSession,
        site: Site,
        dl: DownloaderInstance,
        item: FeedItem,
        info_hash: str,
        stats: FreeRoundStats,
        *,
        free_type: str = FreeType.PERMANENT,
        free_source: str = FreeSource.API,
        free_end_at: datetime | None = None,
        deadline_at: datetime | None = None,
        abort_at: datetime | None = None,
    ) -> None:
        """落库 + add_torrent + 入队便于重放。供限时免费与永久免费复用。"""
        adapter = self._factory.downloader(dl)
        tags = frozenset({"auto", "free-seed", f"site:{site.name}"})

        now = utcnow()
        torrent = Torrent(
            info_hash=info_hash,
            name=item.title[:1024],
            site_id=site.id,
            downloader_id=dl.id,
            category="cross-seed",
            tags=",".join(sorted(tags)),
            size=item.size,
            source=Source.FREE_SEED,
            status=TorrentStatus.ACTIVE,
            is_free=True,
            free_type=free_type,
            free_end_at=free_end_at,
            free_source=free_source,
            free_confidence=1.0,
            free_duration=0,
            clock_offset=0,
            deadline_at=deadline_at or (now + timedelta(hours=1)),
            abort_at=abort_at or (now + timedelta(hours=1)),
            protect_until=now + timedelta(hours=PROTECT_HOURS),
        )
        # 先持久化 DB 记录（幂等：同 info_hash 已存在不抛错）
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(Torrent).values(
            info_hash=info_hash,
            name=item.title[:1024],
            site_id=site.id,
            downloader_id=dl.id,
            category="cross-seed",
            tags=",".join(sorted(tags)),
            size=item.size,
            source=Source.FREE_SEED,
            status=TorrentStatus.ACTIVE,
            is_free=True,
            free_type=free_type,
            free_end_at=free_end_at,
            free_source=free_source,
            free_confidence=1.0,
            free_duration=0,
            clock_offset=0,
            deadline_at=deadline_at or (utcnow() + timedelta(hours=1)),
            abort_at=abort_at or (utcnow() + timedelta(hours=1)),
            protect_until=utcnow() + timedelta(hours=PROTECT_HOURS),
        ).on_conflict_do_nothing(index_elements=["info_hash"])
        await session.execute(stmt)
        await session.flush()

        url = item.torrent_url or item.link
        # m-team 适配层把 torrent_url 设为占位符 "api://gen/<id>"。
        # add_torrent 需要真实 URL；惰性通过适配器 torrent_detail 拿。
        if isinstance(url, str) and url.startswith("api://gen/"):
            item_id = url.rsplit("/", 1)[-1]
            try:
                detail = await self._factory.site(site).get_torrent_detail(item_id)
                if detail.download_url:
                    url = detail.download_url
            except Exception as exc:
                logger.debug("取真实下载 URL 失败 %s：%s", item_id, exc)
        logger.info("添加种子 downloader=%s reason=free_seed", dl.name)
        try:
            await adapter.add_torrent(
                AddTorrentPayload(
                    name=item.title,
                    urls=[url] if url else [],
                    category="cross-seed",
                    tags=tags,
                )
            )
        except Exception as exc:
            logger.warning("免费种添加失败 %s：%s", item.title, exc)
            torrent.status = TorrentStatus.ABORTED
            torrent.aborted_reason = "add_failed"
            stats.failed += 1
            free_seed_outcomes.labels(
                outcome="failed", reason="add_error"
            ).inc()
            return

        if url:
            queue = TaskQueueService(session)
            await queue.enqueue(
                TaskKind.ADD_TORRENT,
                {
                    "site_id": site.id,
                    "downloader_id": dl.id,
                    "info_hash": info_hash,
                    "name": item.title,
                    "url": url,
                    "category": "cross-seed",
                    "tags": list(tags),
                    "free_seed": True,
                },
                idem_key=free_idem_key(site.id, info_hash),
            )

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(AddHistory).values(
            site_id=site.id,
            info_hash=info_hash,
            torrent_id=torrent.id,
            name=item.title[:1024],
            source=Source.FREE_SEED,
            outcome="added",
        ).on_conflict_do_nothing(index_elements=["site_id", "info_hash"])
        await session.execute(stmt)
        stats.added += 1
        stats.accepted += 1
        free_seed_outcomes.labels(outcome="added", reason="").inc()

    async def _record_permanent(
        self,
        session: AsyncSession,
        site: Site,
        info_hash: str,
        item: FeedItem,
        stats: FreeRoundStats,
        dl: DownloaderInstance | None = None,
    ) -> None:
        """永久免费：转辅种池（R10），有下载器时立即投递。

        优先：若站点配置了下载器且 m-team 模式（适配层有 torrent_url/dlc），
        立即 add_torrent 投递（避免等辅种轮询）；否则只登记历史，依赖
        辅种轮询命中。
        """
        existing = (
            await session.execute(
                select(AddHistory.id).where(
                    AddHistory.site_id == site.id,
                    AddHistory.info_hash == info_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            # INSERT OR IGNORE：同 (site_id, info_hash) 已存在不抛错
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            stmt = sqlite_insert(AddHistory).values(
                site_id=site.id,
                info_hash=info_hash,
                name=item.title[:1024],
                source=Source.FREE_SEED,
                outcome="permanent_free",
            ).on_conflict_do_nothing(index_elements=["site_id", "info_hash"])
            await session.execute(stmt)
        stats.permanent += 1
        free_seed_outcomes.labels(outcome="permanent", reason="").inc()
        logger.debug("永久免费 %s 登记转辅种池", info_hash[:8])
        # 有下载器：立即 add_torrent 投递（m-team API 站有 torrent_url）
        if dl is not None and item.torrent_url:
            try:
                await self._add_to_downloader(session, site, dl, item, info_hash, stats)
            except Exception as exc:
                logger.debug("永久免费投递失败 %s：%s", item.title[:30], exc)

    # ------------------------------------------------------------ 整站全轮
    async def run_all(self, *, force: bool = False) -> list[FreeRoundStats]:
        async with session_scope() as session:
            site_ids = (
                (
                    await session.execute(
                        select(Site.id).where(Site.enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
        out: list[FreeRoundStats] = []
        for sid in site_ids:
            try:
                out.append(await self.run_site(sid, force=force))
            except Exception as exc:
                logger.exception("免费种站点 %s 轮次失败：%s", sid, exc)
        return out

    # ------------------------------------------------------------ 工具
    async def _resolve_downloader(
        self, session: AsyncSession, site: Site
    ) -> DownloaderInstance | None:
        """取默认下载器：is_default 优先；否则第一个 enabled。"""
        row = (
            await session.execute(
                select(DownloaderInstance)
                .where(
                    DownloaderInstance.is_default.is_(True),
                    DownloaderInstance.enabled.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        return (
            await session.execute(
                select(DownloaderInstance)
                .where(DownloaderInstance.enabled.is_(True))
                .order_by(DownloaderInstance.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _item_id(item: FeedItem) -> str | None:
        """从 link 提取站点种子 ID（m-team 详情 URL 末段）。"""
        if not item.link:
            return None
        tail = item.link.rstrip("/").rsplit("/", 1)[-1]
        return tail or None

    async def _resolve_infohash(self, site: Site, item: FeedItem) -> str | None:
        """info_hash 缺失时，让站点适配器下载 torrent 解析（m-team 等）。"""
        item_id = self._item_id(item)
        if not item_id:
            return None
        try:
            adapter = self._factory.site(site)
        except Exception:
            return None
        try:
            torrent_bytes = await adapter.download_torrent(item_id)
        except Exception as exc:
            logger.debug("站点 %s 下载 %s 失败：%s", site.name, item_id, exc)
            return None
        return infohash_from_torrent(torrent_bytes)


def win_policy(site: Site) -> WindowPolicy:
    """免费种窗口策略（统一用默认 WindowPolicy）。"""
    return WindowPolicy()


#: 公共 re-export，让调用方不必 import 两个模块
__all__ = [
    "DeadlineCalculator",
    "FreeRoundStats",
    "FreeSeedHunter",
    "is_permanent_free",
    "win_policy",
]
