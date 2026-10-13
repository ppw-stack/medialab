"""自动推送免费种到默认下载器（FR-? 待业务重设计编号）。

候选来源：``torrent_index`` 表（``free_type ∈ {freeleech_permanent,
freeleech_timed}``，且限时种 ``free_end_at - now > push_min_remaining_hours``）。
去重：``torrents`` 表 UNIQUE ``(site_id, item_id, downloader_id)``。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.core.config import AppSettings
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import (
    DownloaderInstance,
    FreeType,
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

        # 3) 逐个推送
        pushed = 0
        skipped_dup = 0
        skipped_invalid = 0
        failed = 0
        errors: list[str] = []

        adapter = self._factory.downloader(default_dl)
        for c in candidates:
            # URL 合法性：合成/占位 URL（如 mteam 的 api://gen/{id}）qB 必拒绝
            # —— 在选种阶段就过滤，避免产生无意义 409 错误
            if not _is_valid_download_url(c.torrent_url):
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

            info_hash = await self._add_one(adapter, c, downloader_id)
            if info_hash is None:
                failed += 1
                errors.append(f"site={c.site_id} item={c.item_id}: add_torrent failed")
                continue

            async with session_scope() as session:
                await self._record_push(
                    session, info_hash, c.name, c.site_id, c.item_id, downloader_id
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
        """选候选免费种：未推过 + 剩余时间满足阈值。"""
        now = utcnow()
        min_remaining = timedelta(hours=self._settings.push_min_remaining_hours)
        threshold = now + min_remaining

        # 候选集：torrent_index 上 free_type ∈ 真免费/限时免费 的种
        stmt = select(
            TorrentIndex.id,
            TorrentIndex.site_id,
            TorrentIndex.item_id,
            TorrentIndex.name,
            TorrentIndex.torrent_url,
            TorrentIndex.free_type,
            TorrentIndex.free_end_at,
        ).where(
            TorrentIndex.free_type.in_(
                (FreeType.PERMANENT, FreeType.TIMED)
            ),
            TorrentIndex.item_id.is_not(None),
            TorrentIndex.torrent_url.is_not(None),
        )
        rows = (await session.execute(stmt)).all()

        candidates: list[_Candidate] = []
        for row in rows:
            idx_id, site_id, item_id, name, torrent_url, free_type, free_end_at = row
            # is_not_null 已 WHERE 过滤，类型收窄
            assert item_id is not None and torrent_url is not None
            # 限时种：剩余时间 < 阈值 → 跳过
            if free_type == FreeType.TIMED:
                if free_end_at is None or free_end_at < threshold:
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
                    torrent_url=torrent_url,
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
        self, adapter: DownloaderAdapter, c: _Candidate, downloader_id: int
    ) -> str | None:
        """调 add_torrent，返回 info_hash（qB 返空串时用合成占位）。"""
        try:
            payload = AddTorrentPayload(
                name=c.name,
                urls=(c.torrent_url,),
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
        # 用合成 hash 满足 UNIQUE 约束（dev DB 旧 schema）
        return _synthetic_info_hash(c.site_id, c.item_id, downloader_id)

    async def _record_push(
        self,
        session: AsyncSession,
        info_hash: str | None,
        name: str,
        site_id: int,
        item_id: str,
        downloader_id: int,
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
    torrent_url: str
