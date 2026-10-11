"""免费种猎手（FR-3）：拉取 → 候选池 → 手动推送。

流水线（每轮）：

1. **拉取**：复用 M2 的 :class:`~app.services.feed_fetcher.FeedFetcher` 与
   :class:`~app.services.factory.AdapterFactory`。
2. **解析窗口**：把 :class:`~app.adapters.sites.base.FeedItem` 转为
   :class:`~app.core.windows.FreeWindow`（业务层 :class:`WindowParser`）。
3. **入候选池**：所有抓取到的种子写入 ``free_candidates`` 表（status=pending）。
   - 跳过阈值过滤（<90min）保留候选但标记 status=skipped
   - 永久免费同样入池
5. **手动推送**：用户从 UI 选择候选，调用 :meth:`push_candidate` /
   :meth:`push_batch`：
   - download_torrent(item_id) → bytes
   - infohash_from_torrent(bytes) → hash
   - downloader.add_torrent(payload)
   - 同步写 torrents 表 + 更新 candidate status=pushed

看护（FR-3.4）后续由 :mod:`app.services.window_watcher` 单独执行。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.adapters.sites._bencode import infohash_from_torrent
from app.adapters.sites.base import FeedItem
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.core.windows import FreeWindow
from app.db.models import (
    DownloaderInstance,
    Feed,
    FreeCandidate,
    Site,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.cooldown import CooldownService
from app.services.factory import AdapterFactory
from app.services.feed_fetcher import FeedFetcher, PullStatus
from app.services.window_parser import WindowParser

logger = get_logger(__name__)

#: 单轮最多投递（避免单次风暴打爆下载器）
MAX_ADD_PER_ROUND = 200

#: 跳过阈值（<90min 视为过短，不推荐下载）
SKIP_WINDOW_MINUTES = 90


#: 候选状态
class CandidateStatus:
    PENDING = "pending"
    PUSHED = "pushed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class PushResult:
    """单条推送结果。"""

    candidate_id: int
    success: bool
    info_hash: str | None = None
    error: str | None = None


@dataclass
class FreeRoundStats:
    """一轮免费种捕获统计（拉取 + 入候选池）。"""

    site: str = ""
    status: str = PullStatus.OK
    pulled: int = 0
    accepted: int = 0  # 实际入候选池数量
    skipped: int = 0
    failed: int = 0
    duration_ms: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


class FreeSeedHunter:
    """免费种捕获编排：拉取入候选池 + 手动推送。"""

    def __init__(self, factory: AdapterFactory) -> None:
        self._factory = factory

    # ------------------------------------------------------------ 拉取入候选池
    async def pull_site(self, site_id: int, *, force: bool = False) -> FreeRoundStats:
        """对单个站点执行抓取，结果入 free_candidates。

        ``force=True`` 用于手动触发，绕过 FR-2.2 的 15 分钟下限。
        """
        started = time.monotonic()
        async with session_scope() as session:
            site = (
                await session.execute(select(Site).where(Site.id == site_id))
            ).scalar_one_or_none()
            if site is None or not site.enabled:
                return FreeRoundStats(site=str(site_id) if site else "", status="disabled")

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
            stats = await self._pull_to_pool(session, site, feed, fetcher, stats)

        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return stats

    async def pull_all(self, *, force: bool = False) -> list[FreeRoundStats]:
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
                out.append(await self.pull_site(sid, force=force))
            except Exception as exc:
                logger.exception("免费种站点 %s 抓取失败：%s", sid, exc)
        return out

    async def _pull_to_pool(
        self,
        session: AsyncSession,
        site: Site,
        feed: Feed,
        fetcher: FeedFetcher,
        stats: FreeRoundStats,
    ) -> FreeRoundStats:
        """拉取 → 入候选池（幂等 INSERT OR IGNORE）。"""
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
        cooldown = CooldownService(session)
        for item in items:
            await self._capture_one(session, site, item, cooldown, stats)
        return stats

    async def _capture_one(
        self,
        session: AsyncSession,
        site: Site,
        item: FeedItem,
        cooldown: CooldownService,
        stats: FreeRoundStats,
    ) -> None:
        """单条目处理：解析窗口 → 入候选池。"""
        # 1. 窗口解析
        try:
            window = WindowParser.parse(item)
        except Exception as exc:
            logger.debug("站点 %s 解析失败：%s", site.name, exc)
            window = FreeWindow()

        # 2. 过滤逻辑：永久免费保留；限时 <90min 标记 skipped；其余 pending
        if window.is_permanent:
            status = CandidateStatus.PENDING
            reject_reason = None
        elif not window.is_actionable:
            # 窗口未知（无 free_end_at 且非永久）
            status = CandidateStatus.SKIPPED
            reject_reason = "window_unknown"
        elif (
            window.free_end_at is not None
            and (window.free_end_at - utcnow()).total_seconds() < SKIP_WINDOW_MINUTES * 60
        ):
            status = CandidateStatus.SKIPPED
            reject_reason = "remaining_window_too_short"
        else:
            status = CandidateStatus.PENDING
            reject_reason = None

        # 3. 冷却检查（已有冷却的种子不再入池）
        if item.info_hash and status == CandidateStatus.PENDING:
            verdict = await cooldown.is_blocked(site.id, item.info_hash)
            if verdict.blocked:
                status = CandidateStatus.SKIPPED
                reject_reason = "cooldown"

        # 4. 幂等入库（site_id + item_id 唯一；同 site 同 item_id 不重复）
        item_id = item.item_id or item.link or ""
        if not item_id:
            stats.note_skip("no_item_id")
            return

        free_payload = item.free_payload or {}
        stmt = sqlite_insert(FreeCandidate).values(
            site_id=site.id,
            item_id=item_id,
            info_hash=item.info_hash,
            name=item.title[:1024],
            size=item.size,
            free_type=window.free_type,
            free_end_at=window.free_end_at,
            free_source=window.free_source,
            free_confidence=window.confidence,
            free_payload=free_payload,
            status=status,
            reject_reason=reject_reason,
            seen_at=utcnow(),
        ).on_conflict_do_nothing(index_elements=["site_id", "item_id"])
        await session.execute(stmt)

        # 6. 统计
        if status == CandidateStatus.PENDING:
            stats.accepted += 1
        else:
            stats.note_skip(reject_reason or "skipped")

    # ------------------------------------------------------------ 推送
    async def push_candidate(self, candidate_id: int) -> PushResult:
        """单条推送：download_torrent → add_torrent → 标记 pushed。

        失败时 status=failed + error 字段记录原因。
        """
        async with session_scope() as session:
            cand = (
                await session.execute(
                    select(FreeCandidate).where(FreeCandidate.id == candidate_id)
                )
            ).scalar_one_or_none()
            if cand is None:
                return PushResult(candidate_id=candidate_id, success=False, error="候选不存在")
            if cand.status == CandidateStatus.PUSHED:
                return PushResult(
                    candidate_id=candidate_id,
                    success=False,
                    error="已推送过",
                    info_hash=cand.info_hash,
                )

            site = (
                await session.execute(select(Site).where(Site.id == cand.site_id))
            ).scalar_one_or_none()
            if site is None:
                cand.status = CandidateStatus.FAILED
                cand.reject_reason = "站点不存在"
                return PushResult(
                    candidate_id=candidate_id, success=False, error="站点不存在"
                )

            dl = await self._resolve_default_downloader(session)
            if dl is None:
                cand.status = CandidateStatus.FAILED
                cand.reject_reason = "无可用下载器"
                return PushResult(
                    candidate_id=candidate_id, success=False, error="无可用下载器"
                )

            # 1. 下载 torrent bytes（m-team 走 genDlToken + GET；NexusPHP 走 download.php）
            torrent_bytes = await self._download_torrent(site, cand)
            if torrent_bytes is None:
                cand.status = CandidateStatus.FAILED
                cand.reject_reason = "download_torrent 失败"
                return PushResult(
                    candidate_id=candidate_id, success=False, error="download_torrent 失败"
                )

            # 2. 解析 infohash
            info_hash = infohash_from_torrent(torrent_bytes)
            if not info_hash:
                cand.status = CandidateStatus.FAILED
                cand.reject_reason = "infohash 解析失败"
                return PushResult(
                    candidate_id=candidate_id, success=False, error="infohash 解析失败"
                )

            # 3. add_torrent 到默认下载器
            adapter = self._factory.downloader(dl)
            tags = frozenset({"auto", "free-seed", f"site:{site.name}"})
            try:
                await adapter.add_torrent(
                    AddTorrentPayload(
                        name=cand.name,
                        torrent_bytes=torrent_bytes,
                        category="cross-seed",
                        tags=tags,
                    )
                )
            except DownloaderError as exc:
                cand.status = CandidateStatus.FAILED
                cand.reject_reason = str(exc)[:64]
                return PushResult(
                    candidate_id=candidate_id, success=False, error=str(exc)
                )
            except Exception as exc:
                cand.status = CandidateStatus.FAILED
                cand.reject_reason = f"add_torrent 失败：{exc}"[:64]
                return PushResult(
                    candidate_id=candidate_id,
                    success=False,
                    error=f"add_torrent 失败：{exc}",
                )

            # 4. 落库 Torrent（source=FREE_SEED）
            now = utcnow()
            torrent = Torrent(
                info_hash=info_hash,
                name=cand.name[:1024],
                site_id=site.id,
                downloader_id=dl.id,
                category="cross-seed",
                tags=",".join(sorted(tags)),
                size=cand.size,
                source=Source.FREE_SEED,
                status=TorrentStatus.ACTIVE,
                is_free=True,
                free_type=cand.free_type,
                free_end_at=cand.free_end_at,
                free_source=cand.free_source,
                free_confidence=cand.free_confidence or 1.0,
            )
            session.add(torrent)
            await session.flush()

            # 5. 更新 candidate 状态
            cand.info_hash = info_hash
            cand.torrent_id = torrent.id
            cand.status = CandidateStatus.PUSHED
            cand.pushed_at = now

            return PushResult(
                candidate_id=candidate_id, success=True, info_hash=info_hash
            )

    async def push_batch(self, ids: list[int]) -> list[PushResult]:
        """批量推送。"""
        out: list[PushResult] = []
        for cid in ids:
            out.append(await self.push_candidate(cid))
        return out

    async def _download_torrent(
        self, site: Site, cand: FreeCandidate
    ) -> bytes | None:
        """下载 torrent 字节，捕获所有异常返回 None。"""
        item_id = cand.item_id
        if not item_id:
            return None
        try:
            adapter = self._factory.site(site)
        except Exception:
            return None
        try:
            return await adapter.download_torrent(item_id)
        except Exception as exc:
            logger.debug("站点 %s 下载 %s 失败：%s", site.name, item_id, exc)
            return None

    async def _resolve_default_downloader(
        self, session: AsyncSession
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


__all__ = [
    "CandidateStatus",
    "FreeRoundStats",
    "FreeSeedHunter",
    "PushResult",
]