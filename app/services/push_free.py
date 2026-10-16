"""自动推送免费种到默认下载器（FR-? 待业务重设计编号）。

候选来源：``torrent_index`` 表（``free_type ∈ {freeleech_permanent,
freeleech_timed}``，且限时种 ``free_end_at - now > push_min_remaining_hours``）。
去重：``torrent_index.push_status`` 列（``pushed`` = 已推过）。

推送流程：``SiteAdapter.download_torrent(item_id)`` → bytes →
``infohash_from_torrent(bytes)`` 解析真 hash →
``DownloaderAdapter.add_torrent(torrent_bytes=bytes)`` →
更新 ``torrent_index.push_status = pushed``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.adapters.sites._bencode import infohash_from_torrent
from app.core.config import AppSettings
from app.core.logging import get_logger
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import (
    DownloaderInstance,
    FreeType,
    PushStatus,
    Site,
    TorrentIndex,
)
from app.db.session import session_scope
from app.services.factory import AdapterFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.adapters.sites.base import SiteAdapter

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PushResult:
    """单次推送作业的运行结果。"""

    selected: int = 0
    pushed: int = 0
    skipped_already_pushed: int = 0
    skipped_remaining_too_short: int = 0
    skipped_download_failed: int = 0
    skipped_parse_failed: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()


class PushFreeService:
    """定时推送免费种到默认下载器。

    运行参数（``push_min_remaining_hours`` / ``push_max_per_run``）从
    :class:`app.services.runtime_params.RuntimeParamManager` 读取——
    每次 run() 实时取，DB 改完下次作业生效，无需重启。
    """

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

        # 2.5) 按 site_id 预建 adapter 映射——download_torrent 实时拉 bytes
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
        skipped_dl_failed = 0
        skipped_parse = 0
        failed = 0
        errors: list[str] = []

        adapter = self._factory.downloader(default_dl)
        from app.services.runtime_params import get_runtime_params

        rp = get_runtime_params()
        interval_ms = rp.get_int("push_download_interval_ms")
        first = True

        for c in candidates:
            site_adapter = site_adapters.get(c.site_id)
            if site_adapter is None:
                failed += 1
                errors.append(f"site={c.site_id} 适配器缺失")
                continue

            # 3.0) 站点限频保护：首个不等待，后续每次下载前 sleep
            if not first and interval_ms > 0:
                await asyncio.sleep(interval_ms / 1000)
            first = False

            # 3.1) 下载 .torrent 文件
            try:
                torrent_bytes = await site_adapter.download_torrent(c.item_id)
            except Exception as exc:
                skipped_dl_failed += 1
                errors.append(
                    f"site={c.site_id} item={c.item_id}: download_torrent: {exc}"
                )
                continue
            if not torrent_bytes or not torrent_bytes.startswith(b"d"):
                skipped_dl_failed += 1
                errors.append(
                    f"site={c.site_id} item={c.item_id}: 响应不是 bencoded torrent"
                )
                continue

            # 3.2) 解析 info_hash
            info_hash = infohash_from_torrent(torrent_bytes)
            if info_hash is None:
                skipped_parse += 1
                errors.append(
                    f"site={c.site_id} item={c.item_id}: infohash 解析失败"
                )
                continue

            # 3.3) 去重（运行时再查一次，防止两作业并发）
            async with session_scope() as session:
                exists = await self._is_already_pushed(
                    session, c.site_id, c.item_id
                )
            if exists:
                skipped_dup += 1
                continue

            # 3.4) 用 torrent_bytes 推送到下载器
            try:
                payload = AddTorrentPayload(
                    name=c.name,
                    torrent_bytes=torrent_bytes,
                    category="auto-push",
                    save_path=default_dl.save_path or None,
                )
                await adapter.add_torrent(payload)
            except DownloaderError as exc:
                failed += 1
                errors.append(
                    f"site={c.site_id} item={c.item_id}: add_torrent: {exc}"
                )
                continue

            # 3.5) 更新 torrent_index.push_status = pushed
            async with session_scope() as session:
                await session.execute(
                    update(TorrentIndex)
                    .where(TorrentIndex.id == c.index_id)
                    .values(
                        push_status=PushStatus.PUSHED,
                        pushed_at=bj_now(),
                        info_hash=info_hash,
                    )
                )
            pushed += 1

        result = PushResult(
            selected=len(candidates),
            pushed=pushed,
            skipped_already_pushed=skipped_dup,
            skipped_download_failed=skipped_dl_failed,
            skipped_parse_failed=skipped_parse,
            failed=failed,
            errors=tuple(errors[:10]),
        )
        logger.info(
            "push_free: 选中 %d，推送 %d，跳过重复 %d，下载失败 %d，"
            "解析失败 %d，推送失败 %d",
            result.selected, result.pushed, result.skipped_already_pushed,
            result.skipped_download_failed, result.skipped_parse_failed,
            result.failed,
        )
        return result

    async def _select_candidates(
        self, session: AsyncSession, downloader_id: int
    ) -> list[_Candidate]:
        """选候选免费种：未推过 + 剩余时间满足阈值。

        注：不再从 torrent_index 读 URL——推送时实时调
        :meth:`SiteAdapter.download_torrent` 获取 .torrent bytes。
        """
        now = bj_now()
        # 运行时参数（DB 持久化，UI 可调；详见 app.services.runtime_params）
        from app.services.runtime_params import get_runtime_params

        rp = get_runtime_params()
        min_remaining = timedelta(hours=rp.get_int("push_min_remaining_hours"))
        threshold = format_bj(bj_now_dt() + min_remaining)

        # 候选集：torrent_index 上 free_type ∈ 真免费/限时免费 且 item_id 非空
        #         且 push_status 未推过（NULL 或 != pushed）
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
                # 显式白名单：永久免费 + 限时免费 + 2X 限时
                # 退还（REFUND）/折扣（NEUTRAL）/未知（UNKNOWN）均不自动推送
                or_(
                    TorrentIndex.free_type == FreeType.PERMANENT,
                    TorrentIndex.free_type == FreeType.TIMED,
                    TorrentIndex.free_type == FreeType.TWOU_TIMED,
                ),
                TorrentIndex.item_id.is_not(None),
                or_(
                    TorrentIndex.push_status.is_(None),
                    TorrentIndex.push_status != PushStatus.PUSHED,
                ),
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
            # 已经推送过则跳过
            if await self._is_already_pushed(session, site_id, item_id):
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
            if len(candidates) >= rp.get_int("push_max_per_run"):
                break
        return candidates

    async def _is_already_pushed(
        self, session: AsyncSession, site_id: int, item_id: str
    ) -> bool:
        """是否已推过：torrent_index 上 push_status == pushed。"""
        stmt = select(TorrentIndex.id).where(
            TorrentIndex.site_id == site_id,
            TorrentIndex.item_id == item_id,
            TorrentIndex.push_status == PushStatus.PUSHED,
        )
        return (await session.execute(stmt)).first() is not None


@dataclass(frozen=True, slots=True)
class _Candidate:
    """推送候选（仅内部用）。"""

    index_id: int
    site_id: int
    item_id: str
    name: str
    free_type: str | None = None
    free_end_at: str | None = None
