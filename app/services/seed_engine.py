"""辅种引擎（FR-2）。

一轮辅种 = 拉取 → 判定 → 投递 → 落库。全部通过
:class:`~app.services.cooldown.CooldownService`、
:class:`~app.services.matcher.Matcher` 与
:class:`~app.services.disk` 完成，引擎本身只做编排与统计。

关键设计：**判定与执行分离**。先对整批条目跑完判定（纯查询，无副作用），
再逐条投递。这样：

- 统计口径清晰（accepted/skipped/failed）
- 任何单条失败不影响其他条目（R8 精神）
- 可以在投递前整体放弃（如磁盘不足时提前退出）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sites.base import FeedItem
from app.core.logging import get_logger
from app.core.metrics import cross_seed_added, feed_pulls
from app.core.timeutils import utcnow
from app.db.models import (
    AddHistory,
    CooldownReason,
    Feed,
    Site,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.cooldown import CooldownService
from app.services.factory import AdapterFactory
from app.services.feed_fetcher import FeedFetcher, PullStatus
from app.services.matcher import (
    Matcher,
    MatchOutcome,
    MatchResult,
    SiteMatchPolicy,
)
from app.services.task_queue import TaskKind, TaskQueueService, make_idem_key

logger = get_logger(__name__)

#: 单轮最多投递的任务数，避免一次拉取风暴打爆下载器
MAX_ADD_PER_ROUND = 200
#: 禁删保护期小时数（FR-5.2.6）
PROTECT_HOURS = 12


@dataclass
class RoundStats:
    """一轮辅种统计。"""

    site: str = ""
    pulled: int = 0
    accepted: int = 0
    skipped: int = 0
    added: int = 0
    failed: int = 0
    duration_ms: int = 0
    status: str = PullStatus.OK
    #: 跳过原因分布
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


class SeedEngine:
    """辅种编排。"""

    def __init__(self, factory: AdapterFactory) -> None:
        self._factory = factory

    # ------------------------------------------------------------ 单站一轮
    async def run_site(self, site_id: int, *, force: bool = False) -> RoundStats:
        """对单个站点执行一轮辅种。

        ``force=True`` 用于**手动触发**（FR-6.1）：跳过 FR-2.2 的频率下限。
        该下限是针对自动拉取的账号保护（R2），用户显式操作不应被限制，
        但仍会完整记录审计。
        """
        started = time.monotonic()
        async with session_scope() as session:
            site = (
                await session.execute(select(Site).where(Site.id == site_id))
            ).scalar_one_or_none()
            if site is None or not site.enabled:
                logger.info("站点 %s 未启用，跳过", site_id)
                return RoundStats(status="disabled")

            feed = (
                await session.execute(
                    select(Feed).where(
                        Feed.site_id == site_id, Feed.enabled.is_(True)
                    )
                )
            ).scalar_one_or_none()
            if feed is None:
                logger.info("站点 %s 无启用的 RSS 源", site.name)
                return RoundStats(site=site.name, status="no_feed")

            if not force:
                fetcher = FeedFetcher(site.name)
                allowed, why = fetcher.should_pull(
                    feed, interval_minutes=site.min_interval_minutes
                )
                if not allowed:
                    logger.debug("站点 %s 本轮跳过：%s", site.name, why)
                    return RoundStats(site=site.name, status=PullStatus.INTERVAL_NOT_MET)

            fetcher = FeedFetcher(site.name)
            stats = await self._pull_and_add(session, site, feed, fetcher)

        feed_pulls.labels(site=site.name, result=stats.status).inc()
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return stats

    async def _pull_and_add(
        self,
        session: AsyncSession,
        site: Site,
        feed: Feed,
        fetcher: FeedFetcher,
    ) -> RoundStats:
        """拉取 → 判定 → 投递。"""
        stats = RoundStats(site=site.name)
        adapter = self._factory.site(site)
        # 站点可能没有 RSS URL（m-team 等 API 站点有 apikey 就走 API）。
        # URL 是给 adapter 的提示，adapter 自行决定是否使用（FR-1.5）。
        url = self._factory.site_rss_url(site) or ""

        result = await fetcher.fetch(adapter, url)
        stats.status = str(result.status)
        if not result.ok:
            await self._update_feed_state(
                session, feed, result.status, 0, result.duration_ms, result.error
            )
            return stats

        feed.last_pulled_at = utcnow()
        feed.last_status = str(result.status)
        feed.last_item_count = len(result.items)
        feed.last_duration_ms = result.duration_ms

        items = list(result.items)
        stats.pulled = len(items)
        await self._add_items(session, site, feed, items, stats)
        return stats

    async def _add_items(
        self,
        session: AsyncSession,
        site: Site,
        feed: Feed,
        items: list[FeedItem],
        stats: RoundStats,
    ) -> None:
        """对整批条目执行判定与投递。"""
        policy = SiteMatchPolicy(
            site_id=site.id,
            site_name=site.name,
            save_path_template=site.save_path_template,
            max_size=site.max_size,
            category=site.category,
        )
        matcher = Matcher(session, policy)
        results = await matcher.match_many(items[:MAX_ADD_PER_ROUND])

        queue = TaskQueueService(session)
        cooldown = CooldownService(session)

        for item, res in zip(items[:MAX_ADD_PER_ROUND], results, strict=True):
            if res.outcome is MatchOutcome.SKIP:
                stats.note_skip(res.reason)
                await self._record_skip(session, feed, item, res.reason)
                continue

            stats.accepted += 1
            await self._dispatch(
                session, queue, cooldown, site, item, res, stats
            )

    async def _dispatch(
        self,
        session: AsyncSession,
        queue: TaskQueueService,
        cooldown: CooldownService,
        site: Site,
        item: FeedItem,
        res: MatchResult,
        stats: RoundStats,
    ) -> None:
        """投递一条辅种任务。幂等键保证重放不重复。"""
        assert res.save_path is not None
        payload: dict[str, object] = {
            "site_id": site.id,
            "site_name": site.name,
            "info_hash": res.info_hash,
            "name": item.title,
            "url": item.torrent_url or item.link,
            "save_path": str(res.save_path.path),
            "category": site.category,
            "size": item.size,
        }
        enqueued = await queue.enqueue(
            TaskKind.ADD_TORRENT,
            payload,
            idem_key=make_idem_key(
                TaskKind.ADD_TORRENT, site_id=site.id, info_hash=res.info_hash
            ),
        )
        if not enqueued.created:
            # 队列中已有等价任务（重启重放，AC-09）
            stats.note_skip("duplicate")
            await self._record_skip(session, None, item, "duplicate")
            return

        ok, fail_reason = await self._execute_add(session, site, item, res)
        if ok:
            stats.added += 1
        else:
            stats.failed += 1
            await cooldown.record(site.id, res.info_hash, fail_reason)

    async def _execute_add(
        self,
        session: AsyncSession,
        site: Site,
        item: FeedItem,
        res: MatchResult,
    ) -> tuple[bool, str]:
        """真正向下载器添加任务，并落库。返回 (ok, fail_reason)。

        fail_reason 为空串表示成功；配置类错误返回 ``manual_skip``（不入
        冷却，条件修复后立即可重试），下载器添加异常返回 ``dead_torrent``。

        FR-2.4：辅种任务统一用独立分类与标签，与手动任务物理隔离。
        """
        assert res.save_path is not None
        url = item.torrent_url or item.link
        if not url:
            logger.warning("条目无下载链接，跳过：%s", item.title)
            return False, CooldownReason.MANUAL_SKIP

        downloader_row = None
        if site.downloader_id:
            from app.db.models import DownloaderInstance

            downloader_row = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.id == site.downloader_id
                    )
                )
            ).scalar_one_or_none()
        if downloader_row is None:
            logger.warning("站点 %s 未绑定下载器，无法辅种", site.name)
            return False, CooldownReason.MANUAL_SKIP

        from app.adapters.downloaders.base import AddTorrentPayload

        adapter = self._factory.downloader(downloader_row)
        tags = frozenset({"auto", f"site:{site.name}"})

        logger.info(
            "添加种子 downloader=%s reason=cross_seed", downloader_row.name,
        )
        try:
            await adapter.add_torrent(
                AddTorrentPayload(
                    name=item.title,
                    urls=[url],
                    save_path=str(res.save_path.path),
                    category=site.category,
                    tags=tags,
                )
            )
        except Exception as exc:
            logger.warning("辅种添加失败 %s：%s", item.title, exc)
            return False, CooldownReason.DEAD_TORRENT

        torrent = Torrent(
            info_hash=res.info_hash,
            name=item.title[:1024],
            site_id=site.id,
            downloader_id=downloader_row.id,
            category=site.category,
            tags=",".join(sorted(tags)),
            save_path=str(res.save_path.path),
            size=item.size,
            source=Source.AUTO_SEED,
            status=TorrentStatus.ACTIVE,
            protect_until=utcnow() + timedelta(hours=PROTECT_HOURS),
        )
        session.add(torrent)
        await session.flush()

        session.add(
            AddHistory(
                site_id=site.id,
                info_hash=res.info_hash,
                torrent_id=torrent.id,
                name=item.title[:1024],
                source=Source.AUTO_SEED,
                outcome="added",
            )
        )
        cross_seed_added.labels(site=site.name, downloader=downloader_row.name).inc()
        return True, ""

    async def _record_skip(
        self,
        session: AsyncSession,
        feed: Feed | None,
        item: FeedItem,
        reason: str,
    ) -> None:
        if feed is None:
            return
        from app.services.matcher import MatchResult, record_decision

        await record_decision(
            session,
            feed.id,
            item,
            MatchResult.skip(reason),
        )

    async def _update_feed_state(
        self,
        session: AsyncSession,
        feed: Feed,
        status: object,
        item_count: int,
        duration_ms: int,
        error: str = "",
    ) -> None:
        """更新 RSS 源状态，含熔断写入（FR-2.2）。"""
        feed.last_status = str(status)
        feed.last_duration_ms = duration_ms
        feed.last_item_count = item_count
        if str(status) == PullStatus.BREAKER_TRIPPED:
            from datetime import timedelta

            from app.services.feed_fetcher import BREAKER_SECONDS

            feed.breaker_until = utcnow() + timedelta(seconds=BREAKER_SECONDS)
            logger.warning(
                "站点 %s RSS 熔断 %ds：%s", feed.site_id, BREAKER_SECONDS, error
            )
        else:
            feed.last_pulled_at = utcnow()
        await session.flush()

    # ------------------------------------------------------------ 全站一轮
    async def run_all(self) -> list[RoundStats]:
        """对所有启用站点跑一轮。"""
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
        stats: list[RoundStats] = []
        for site_id in site_ids:
            try:
                stats.append(await self.run_site(site_id))
            except Exception as exc:
                logger.exception("站点 %s 辅种轮次失败：%s", site_id, exc)
        return stats
