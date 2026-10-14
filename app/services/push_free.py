"""自动推送免费种到默认下载器（FR-? 待业务重设计编号）。

候选来源：``torrent_index`` 表（``free_type ∈ {freeleech_permanent,
freeleech_timed}``，且限时种 ``free_end_at - now > push_min_remaining_hours``）。
去重：``torrents`` 表 UNIQUE ``(site_id, item_id, downloader_id)``。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.core.config import AppSettings
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import (
    DownloaderInstance,
    FreeType,
    Site,
    Source,
    Torrent,
    TorrentIndex,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.factory import AdapterFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.adapters.downloaders.base import DownloaderAdapter
    from app.adapters.sites.base import SiteAdapter

logger = get_logger(__name__)


def _is_valid_download_url(url: str) -> bool:
    """是否可被 qB/Tr 解析的下载 URL。

    拒绝合成/占位 URL（如 mteam adapter 写的 ``api://gen/{id}``，
    qB 会 409 拒绝）。仅接受 http/https/magnet 三种 scheme。
    """
    if not url:
        return False
    lower = url.lower().strip()
    return (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("magnet:?xt=urn:btih:")
    )


@dataclass(frozen=True, slots=True)
class PushResult:
    """单次推送作业的运行结果。"""

    selected: int = 0
    pushed: int = 0
    skipped_already_pushed: int = 0
    skipped_remaining_too_short: int = 0
    skipped_invalid_url: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()


def _synthetic_info_hash(site_id: int, item_id: str, downloader_id: int) -> str:
    """qBittorrent add_torrent 不返回 info_hash（adapter 返空串）。

    为满足既有 info_hash 唯一约束（dev DB 旧 schema），用
    ``(site_id, item_id, downloader_id)`` 的 SHA-1 当占位。生产中
    Transmission 返回真 hash 时覆盖此值。
    """
    h = hashlib.sha1(f"{site_id}:{item_id}:{downloader_id}".encode()).hexdigest()
    return h


class PushFreeService:
    """定时推送免费种到默认下载器。"""

    def __init__(self, settings: AppSettings, factory: AdapterFactory) -> None:
        self._settings = settings
        self._factory = factory

    async def run(self) -> PushResult:
        """执行一次推送作业。"""
        # 1) 找默认下载器
        async with session_scope() as session:
            default_dl = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.is_default.is_(True),
                        DownloaderInstance.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if default_dl is None:
                logger.warning("push_free: 未配置默认下载器，跳过本次作业")
                return PushResult()
            downloader_id = default_dl.id

        # 2) 选候选
        async with session_scope() as session:
            candidates = await self._select_candidates(session, downloader_id)

        if not candidates:
            logger.info("push_free: 无新候选")
            return PushResult()

        # 2.5) 按 site_id 预建 adapter 映射——get_torrent_url 实时拉 URL
        async with session_scope() as session:
            site_rows = (
                await session.execute(
                    select(Site).where(
                        Site.id.in_({c.site_id for c in candidates})
                    )
                )
            ).scalars().all()
        site_adapters: dict[int, SiteAdapter] = {
            row.id: self._factory.site(row) for row in site_rows
        }

        # 3) 逐个推送
        pushed = 0
        skipped_dup = 0
        skipped_invalid = 0
        failed = 0
        errors: list[str] = []

        adapter = self._factory.downloader(default_dl)
        for c in candidates:
            site_adapter = site_adapters.get(c.site_id)
            if site_adapter is None:
                failed += 1
                errors.append(f"site={c.site_id} 适配器缺失")
                continue
            # 实时拉取该 item 的可下载 URL
            url = await site_adapter.get_torrent_url(c.item_id)
            if not url or not _is_valid_download_url(url):
                skipped_invalid += 1
                continue
            # 去重（运行时再查一次，防止两作业并发）
            async with session_scope() as session:
                exists = await self._is_already_pushed(
                    session, c.site_id, c.item_id, downloader_id
                )
            if exists:
                skipped_dup += 1
                continue

            info_hash = await self._add_one(adapter, c, url, downloader_id)
            if info_hash is None:
                failed += 1
                errors.append(f"site={c.site_id} item={c.item_id}: add_torrent failed")
                continue

            async with session_scope() as session:
                await self._record_push(
                    session, info_hash, c.name, c.site_id, c.item_id,
                    downloader_id, c.free_type, c.free_end_at,
                )
            pushed += 1

        result = PushResult(
            selected=len(candidates),
            pushed=pushed,
            skipped_already_pushed=skipped_dup,
            skipped_invalid_url=skipped_invalid,
            failed=failed,
            errors=tuple(errors[:10]),
        )
        logger.info(
            "push_free: 选中 %d，推送 %d，跳过重复 %d，无效URL %d，失败 %d",
            result.selected, result.pushed, result.skipped_already_pushed,
            result.skipped_invalid_url, result.failed,
        )
        return result

    async def _select_candidates(
        self, session: AsyncSession, downloader_id: int
    ) -> list[_Candidate]:
        """选候选免费种：未推过 + 剩余时间满足阈值。

        注：URL 不再从 torrent_index 读——推送时实时调
        :meth:`SiteAdapter.get_torrent_url` 获取。
        """
        now = utcnow()
        min_remaining = timedelta(hours=self._settings.push_min_remaining_hours)
        threshold = now + min_remaining

        # 候选集：torrent_index 上 free_type ∈ 真免费/限时免费 且 item_id 非空
        stmt = (
            select(
                TorrentIndex.id,
                TorrentIndex.site_id,
                TorrentIndex.item_id,
                TorrentIndex.name,
                TorrentIndex.free_type,
                TorrentIndex.free_end_at,
            )
            .where(
                TorrentIndex.free_type.in_(
                    (FreeType.PERMANENT, FreeType.TIMED)
                ),
                TorrentIndex.item_id.is_not(None),
            )
            .order_by(TorrentIndex.last_seen_at.desc())
        )
        rows = (await session.execute(stmt)).all()

        candidates: list[_Candidate] = []
        for row in rows:
            idx_id, site_id, item_id, name, free_type, free_end_at = row
            # is_not_null 已 WHERE 过滤，类型收窄
            assert item_id is not None
            # 限时种：未过期 且 剩余时间 ≥ 阈值 → 入选
            # 显式拒绝已过期（<= now），避免下载器对一个已失效窗口做无意义拉取
            if (free_type == FreeType.TIMED
                    and (free_end_at is None
                         or free_end_at <= now
                         or free_end_at < threshold)):
                continue
            # 永久免费：free_end_at 必为 None，永远入选
            # 已经在 torrents 表里则跳过
            if await self._is_already_pushed(session, site_id, item_id, downloader_id):
                continue
            candidates.append(
                _Candidate(
                    index_id=idx_id,
                    site_id=site_id,
                    item_id=item_id,
                    name=name,
                    free_type=free_type,
                    free_end_at=free_end_at,
                )
            )
            if len(candidates) >= self._settings.push_max_per_run:
                break
        return candidates

    async def _is_already_pushed(
        self, session: AsyncSession, site_id: int, item_id: str, downloader_id: int
    ) -> bool:
        """是否已推过：(site_id, item_id, downloader_id) 在 torrents 表里。"""
        stmt = select(Torrent.id).where(
            Torrent.site_id == site_id,
            Torrent.item_id == item_id,
            Torrent.downloader_id == downloader_id,
        )
        return (await session.execute(stmt)).first() is not None

    async def _add_one(
        self, adapter: DownloaderAdapter, c: _Candidate, url: str, downloader_id: int
    ) -> str | None:
        """调 add_torrent，返回 info_hash（qB 返空串时用合成占位）。"""
        try:
            payload = AddTorrentPayload(
                name=c.name,
                urls=(url,),
                category="auto-push",
            )
            handle = await adapter.add_torrent(payload)
        except DownloaderError as exc:
            logger.warning("push_free: add_torrent 失败 %s: %s", c.item_id, exc)
            return None

        # qBittorrent 返空字符串，Transmission 返真 hash
        real_hash = handle.info_hash.strip() or None
        if real_hash:
            return real_hash

        # qB 不返 hash：触发"推送后回填"——拉一次 auto-push 类目，按归一化
        # 名字匹配拿到真实 hash。回填失败时退回合成占位。
        resolved = await self._reconcile_hash(adapter, c.name)
        if resolved:
            return resolved
        return _synthetic_info_hash(c.site_id, c.item_id, downloader_id)

    async def _reconcile_hash(
        self, adapter: DownloaderAdapter, name: str
    ) -> str | None:
        """qB 推送后，按 category=auto-push 过滤 list_torrents，用归一化
        名字匹配刚加入的种子，回填真实 info_hash。失败返回 None。

        名字归一化：转小写、去所有非字母数字字符（应对站点用空格 / 点
        不同的差异，如 "Death is Coming" vs "Death.is.Coming"）。
        """
        from app.adapters.downloaders.base import TorrentFilter, TorrentState

        try:
            infos = await adapter.list_torrents(
                TorrentFilter(
                    category="auto-push",
                    states=frozenset({
                        TorrentState.DOWNLOADING,
                        TorrentState.STALLED_DL,
                        TorrentState.CHECKING,
                        TorrentState.PAUSED_DL,
                        TorrentState.SEEDING,
                        TorrentState.UPLOADING,
                        TorrentState.PAUSED_UP,
                        TorrentState.ERROR,
                        TorrentState.MISSING_FILES,
                    }),
                )
            )
        except DownloaderError as exc:
            logger.debug("push_free: 回填 hash 时 list_torrents 失败：%s", exc)
            return None

        needle = "".join(ch for ch in name.lower() if ch.isalnum())
        if not needle:
            return None
        # 两轮匹配：先精确归一化相等，再允许子串（处理"Death is Coming" vs
        # "[死刑将至].Death.is.Coming..." 之类前缀/分隔符差异）。
        # 优先精确等，避免对"A Will Eternal S04E15"匹配上"S04E15-S04E20"。
        for t in infos:
            tname = "".join(ch for ch in t.name.lower() if ch.isalnum())
            if tname and tname == needle:
                return t.hash
        for t in infos:
            tname = "".join(ch for ch in t.name.lower() if ch.isalnum())
            if tname and (needle in tname or tname in needle):
                return t.hash
        return None

    async def _record_push(
        self,
        session: AsyncSession,
        info_hash: str | None,
        name: str,
        site_id: int,
        item_id: str,
        downloader_id: int,
        free_type: str | None = None,
        free_end_at: datetime | None = None,
    ) -> None:
        """写入 torrents 表（最小集）。"""
        now = utcnow()
        row = Torrent(
            info_hash=info_hash,
            name=name,
            site_id=site_id,
            downloader_id=downloader_id,
            item_id=item_id,
            source=Source.AUTO_PUSH,
            status=TorrentStatus.ACTIVE,
            is_free=True,
            free_type=free_type,
            free_end_at=free_end_at,
            added_at=now,
        )
        session.add(row)
        try:
            await session.flush()
        except Exception as exc:
            # UNIQUE 冲突 = 别的并发作业抢先了，跳过即可
            logger.info("push_free: 记录冲突（可能并发）%s: %s", item_id, exc)
            await session.rollback()


@dataclass(frozen=True, slots=True)
class _Candidate:
    """推送候选（仅内部用）。"""

    index_id: int
    site_id: int
    item_id: str
    name: str
    free_type: str | None = None
    free_end_at: datetime | None = None
