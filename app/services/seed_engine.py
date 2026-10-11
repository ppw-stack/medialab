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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sites.base import FeedItem
from app.core.logging import get_logger
from app.core.metrics import cross_seed_added, feed_pulls
from app.core.timeutils import utcnow
from app.db.models import (
    AddHistory,
    CooldownReason,
    DownloaderInstance,
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

    @staticmethod
    async def _resolve_default_downloader(
        session: AsyncSession,
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

    # ------------------------------------------------------------ 单站一轮
    async def run_site(self, site_id: int, *, force: bool = False) -> RoundStats:
        """对单个站点执行一轮辅种。

        ``force=True`` 用于**手动触发**（FR-6.1）：跳过 FR-2.2 的频率下限。
        该下限是针对自动拉取的账号保护（R2），用户显式操作不应被限制，
        但仍会完整记录审计。

        事务模型（P2-1 修复）：任何**网络 IO**（fetcher.fetch、adapter.add_torrent）
        都不在 session_scope 内；DB 读与 DB 写各自用短事务隔离。长 session 跨
        网络会拉长锁持有窗口、阻塞 scheduler 等其它任务，并使下载器慢响应放大
        DB 排队延迟。
        """
        started = time.monotonic()
        # 1) 短事务：site/feed 元数据 + 频率检查（纯 DB 读）
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
                allowed, why = FeedFetcher(site.name).should_pull(
                    feed, interval_minutes=site.min_interval_minutes
                )
                if not allowed:
                    logger.debug("站点 %s 本轮跳过：%s", site.name, why)
                    return RoundStats(
                        site=site.name, status=PullStatus.INTERVAL_NOT_MET
                    )

        # 2) 网络拉取（无 session）。fetcher 自身带断路器/重试。
        fetcher = FeedFetcher(site.name)
        adapter = self._factory.site(site)
        url = self._factory.site_rss_url(site) or ""
        result = await fetcher.fetch(adapter, url)

        stats = RoundStats(site=site.name)
        stats.status = str(result.status)

        # 3) 短事务：写 feed state（含熔断 breaker_until）
        async with session_scope() as session:
            feed_row = (
                await session.execute(
                    select(Feed).where(Feed.id == feed.id)
                )
            ).scalar_one()
            if not result.ok:
                await self._update_feed_state(
                    session, feed_row, result.status,
                    0, result.duration_ms, result.error,
                )
            else:
                feed_row.last_pulled_at = utcnow()
                feed_row.last_status = str(result.status)
                feed_row.last_item_count = len(result.items)
                feed_row.last_duration_ms = result.duration_ms
                await session.flush()

        if not result.ok:
            feed_pulls.labels(site=site.name, result=stats.status).inc()
            stats.duration_ms = int((time.monotonic() - started) * 1000)
            return stats

        # 4) 判定 + 投递（网络）。match_many 在短 session 内执行（只读），然后
        # 退出 session；add_torrent 在无 session 状态下逐条进行。
        items = list(result.items)[:MAX_ADD_PER_ROUND]
        stats.pulled = len(items)

        async with session_scope() as session:
            policy = SiteMatchPolicy(site_id=site.id, site_name=site.name)
            matcher = Matcher(session, policy)
            results = await matcher.match_many(items)

        # 5) 写每条 skip 决策（matcher 状态外移，必须另开会话）
        skip_entries: list[tuple[FeedItem, str]] = []
        accept_entries: list[tuple[FeedItem, MatchResult]] = []
        for item, res in zip(items, results, strict=True):
            if res.outcome is MatchOutcome.SKIP:
                stats.note_skip(res.reason)
                skip_entries.append((item, res.reason))
            else:
                stats.accepted += 1
                accept_entries.append((item, res))

        if skip_entries:
            async with session_scope() as session:
                feed_row = (
                    await session.execute(
                        select(Feed).where(Feed.id == feed.id)
                    )
                ).scalar_one()
                for item, reason in skip_entries:
                    await self._record_skip(session, feed_row, item, reason)

        # 6) 逐条 dispatch：网络在无 session 下执行；DB 写各自短 session。
        for item, res in accept_entries:
            ok = await self._dispatch(site, item, res)
            if ok:
                stats.added += 1
            else:
                stats.failed += 1

        feed_pulls.labels(site=site.name, result=stats.status).inc()
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return stats

    async def _dispatch(
        self,
        site: Site,
        item: FeedItem,
        res: MatchResult,
    ) -> bool:
        """投递一条辅种任务。返回 True=成功。

        网络（add_torrent）无 session；DB 写在短 session 内（enqueue +
        Torrent/AddHistory；失败则 cooldown.record）。
        """
        assert res.save_path is not None
        url = item.torrent_url or item.link
        if not url:
            logger.warning("条目无下载链接，跳过：%s", item.title)
            await self._record_cooldown(site.id, res.info_hash, CooldownReason.MANUAL_SKIP)
            return False

        # 先在短 session 内取默认下载器（必须 join 才能取到字段）
        async with session_scope() as session:
            downloader_row = await self._resolve_default_downloader(session)
            if downloader_row is None:
                logger.warning("未配置可用下载器，无法辅种：%s", site.name)
                return False
            tags = frozenset({"auto", f"site:{site.name}"})
            payload: dict[str, object] = {
                "site_id": site.id,
                "site_name": site.name,
                "info_hash": res.info_hash,
                "name": item.title,
                "url": url,
                "save_path": str(res.save_path.path),
                "category": "cross-seed",
                "size": item.size,
                "downloader_name": downloader_row.name,
            }

        # 网络调用：构造 adapter 并 add_torrent（不在任何 session 内）
        from app.adapters.downloaders.base import AddTorrentPayload

        adapter = self._factory.downloader(downloader_row)
        logger.info(
            "添加种子 downloader=%s reason=cross_seed", downloader_row.name,
        )
        try:
            await adapter.add_torrent(
                AddTorrentPayload(
                    name=item.title,
                    urls=[url],
                    save_path=str(res.save_path.path),
                    category="cross-seed",
                    tags=tags,
                )
            )
        except Exception as exc:
            logger.warning("辅种添加失败 %s：%s", item.title, exc)
            await self._record_cooldown(
                site.id, res.info_hash, CooldownReason.DEAD_TORRENT
            )
            return False

        # 短事务：enqueue 幂等凭证 + Torrent + AddHistory（savepoint 处理重投）
        async with session_scope() as session:
            queue = TaskQueueService(session)
            await queue.enqueue(
                TaskKind.ADD_TORRENT,
                payload,
                idem_key=make_idem_key(
                    TaskKind.ADD_TORRENT, site_id=site.id, info_hash=res.info_hash
                ),
            )
            torrent = Torrent(
                info_hash=res.info_hash,
                name=item.title[:1024],
                site_id=site.id,
                downloader_id=downloader_row.id,
                category="cross-seed",
                tags=",".join(sorted(tags)),
                save_path=str(res.save_path.path),
                size=item.size,
                source=Source.AUTO_SEED,
                status=TorrentStatus.ACTIVE,
                protect_until=utcnow() + timedelta(hours=PROTECT_HOURS),
            )
            try:
                async with session.begin_nested():
                    # savepoint：同轮内多个相同 info_hash 条目或并发写入时，
                    # flush 撞 UNIQUE(info_hash) 不炸整轮（重投风暴根因）。
                    # add_torrent 已成功，DB 有无记录都不改"已投递"事实；下轮
                    # matcher 经 Torrent/AddHistory 去重。
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
            except IntegrityError:
                logger.info(
                    "辅种 %s 已存在（info_hash=%s），幂等跳过落库",
                    item.title, res.info_hash,
                )
        cross_seed_added.labels(site=site.name, downloader=downloader_row.name).inc()
        return True

    async def _record_cooldown(self, site_id: int, info_hash: str, reason: str) -> None:
        """失败落冷却：独立短事务，便于从无 session 上下文调用。"""
        async with session_scope() as session:
            cooldown = CooldownService(session)
            await cooldown.record(site_id, info_hash, reason)

    async def _record_skip(
        self,
        session: AsyncSession,
        feed: Feed,
        item: FeedItem,
        reason: str,
    ) -> None:
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
