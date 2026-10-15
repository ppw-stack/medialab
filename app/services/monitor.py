"""免费窗口监测作业（FR-3.4 巡检 + 临近截止自动暂停）。

流程：
1. 拉默认下载器全量任务（``adapter.list_torrents()``）
2. 查 ``torrent_index`` 上 ``push_status=pushed AND free_type=freeleech_timed
   AND info_hash IS NOT NULL`` 的行
3. 遍历下载器中的每个种子：若 hash 命中 index 的限时免费集合且距
   ``free_end_at`` 不足 :data:`pre_pause_minutes` 且未完成 → 调
   :meth:`DownloaderAdapter.set_pause` 暂停下载器中的任务。

不写 DB。``torrent_index`` 中已推送但不在下载器中的种子不用管
（可能 qB 自动删除 / 用户手动删，不强制标记）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import (
    DownloaderInstance,
    FreeType,
    PushStatus,
    TorrentIndex,
)
from app.db.session import session_scope
from app.services.factory import AdapterFactory

if TYPE_CHECKING:
    from app.adapters.downloaders.base import TorrentInfo

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """单次监测作业的运行结果。"""

    scanned: int = 0
    matched: int = 0
    skipped_unmatched: int = 0
    skipped_far_from_window: int = 0
    skipped_already_completed: int = 0
    skipped_already_paused: int = 0
    pre_paused: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()


class MonitorFreeWindowService:
    """免费窗口巡检 + 临近截止自动暂停。

    提前暂停分钟数从 :class:`app.services.runtime_params.RuntimeParamManager`
    实时读取，DB 改完下次作业生效。
    """

    def __init__(self, factory: AdapterFactory, pre_pause_minutes: int) -> None:
        self._factory = factory
        self._pre_pause = timedelta(minutes=pre_pause_minutes)

    async def run(self) -> MonitorResult:
        """执行一次监测作业。"""
        # 1) 查默认下载器
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
            logger.warning("monitor: 未配置默认下载器，跳过本次作业")
            return MonitorResult()

        # 2) 拉下载器全量任务
        try:
            adapter = self._factory.downloader(default_dl)
            infos: list[TorrentInfo] = await adapter.list_torrents()
        except Exception as exc:
            logger.warning("monitor: 下载器 list_torrents 失败：%s", exc)
            return MonitorResult(failed=1, errors=(f"list_torrents: {exc}",))

        scanned = len(infos)
        if scanned == 0:
            logger.info("monitor: 下载器无任务")
            return MonitorResult(scanned=0)

        # 3) 查 torrent_index 中已推送的限时免费种（构建 hash → free_end_at 映射）
        from app.services.runtime_params import get_runtime_params

        self._pre_pause = timedelta(
            minutes=get_runtime_params().get_int("monitor_pre_pause_minutes")
        )
        now = utcnow()
        threshold = now + self._pre_pause

        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(
                        TorrentIndex.info_hash,
                        TorrentIndex.free_end_at,
                    ).where(
                        TorrentIndex.push_status == PushStatus.PUSHED,
                        TorrentIndex.free_type == FreeType.TIMED,
                        TorrentIndex.info_hash.is_not(None),
                    )
                )
            ).all()
        index_by_hash: dict[str, datetime] = {
            (row[0] or "").lower(): row[1]
            for row in rows
            if row[0] and row[1] is not None
        }

        # 4) 遍历下载器任务，按 hash 匹配决定行为
        matched = 0
        skipped_unmatched = 0
        skipped_far = 0
        skipped_completed = 0
        skipped_paused = 0
        pre_paused = 0
        failed = 0
        errors: list[str] = []

        for info in infos:
            hash_key = (info.hash or "").lower()
            free_end_at = index_by_hash.get(hash_key)
            if free_end_at is None:
                # 不在 index 推送集合中（可能：手动推、永久免费、未推过等）
                skipped_unmatched += 1
                continue
            matched += 1

            if free_end_at > threshold:
                skipped_far += 1
                continue
            if info.progress >= 1.0:
                skipped_completed += 1
                continue
            if info.state.is_paused:
                skipped_paused += 1
                continue

            # 触发暂停
            try:
                await adapter.set_pause([info.hash], paused=True)
            except Exception as exc:
                failed += 1
                errors.append(f"hash={hash_key} set_pause: {exc}")
                continue
            pre_paused += 1

        result = MonitorResult(
            scanned=scanned,
            matched=matched,
            skipped_unmatched=skipped_unmatched,
            skipped_far_from_window=skipped_far,
            skipped_already_completed=skipped_completed,
            skipped_already_paused=skipped_paused,
            pre_paused=pre_paused,
            failed=failed,
            errors=tuple(errors[:10]),
        )
        logger.info(
            "monitor: 下载器 %d，命中 %d，触发暂停 %d，"
            "跳过(无关/远/已完成/已暂停) %d/%d/%d/%d，失败 %d",
            result.scanned, result.matched, result.pre_paused,
            result.skipped_unmatched, result.skipped_far_from_window,
            result.skipped_already_completed, result.skipped_already_paused,
            result.failed,
        )
        return result
