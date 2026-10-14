"""免费窗口监测作业（FR-3.4 巡检 + 自动暂停触发）。

按 cron 周期扫描 ``torrents`` 表里 ``is_free=1 AND status=active`` 的行，
对每个仍处于限时窗口（``free_type=freeleech_timed``）的种子：

- 距 ``free_end_at`` **不足** :data:`pre_pause_minutes` 且未完成
  （``progress<1.0``）→ 调 :meth:`DownloaderAdapter.set_pause` 暂停下载器中的
  任务，并把 :attr:`Torrent.paused_reason` 写为 ``free_expiring``。
- 其它情况（窗口还远 / 永久免费 / 已完成 / 找不到 hash）→ 跳过或仅计数。

**不**删除数据，**不**改动 status——``active`` 是用户可手动恢复的入口。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import DownloaderInstance, FreeType, Torrent, TorrentStatus
from app.db.session import session_scope
from app.services.factory import AdapterFactory

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """单次监测作业的运行结果。"""

    scanned: int = 0
    skipped_permanent: int = 0
    skipped_far_from_window: int = 0
    skipped_no_end_at: int = 0
    skipped_already_completed: int = 0
    skipped_already_paused: int = 0
    pre_paused: int = 0
    missing_in_downloader: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Candidate:
    """监测候选（仅内部用）。"""

    torrent_id: int
    site_id: int
    item_id: str
    name: str
    info_hash: str
    downloader_id: int
    free_type: str | None
    free_end_at: datetime | None


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
        now = utcnow()
        # 运行时参数实时取（UI 可调）
        from app.services.runtime_params import get_runtime_params

        self._pre_pause = timedelta(
            minutes=get_runtime_params().get_int("monitor_pre_pause_minutes")
        )
        threshold = now + self._pre_pause

        # 1) 拉所有 is_free=1 active 行；筛选与计数都在主循环里
        async with session_scope() as session:
            cands = await self._select_candidates(session)

        if not cands:
            logger.info("monitor_free_window: 无候选")
            return MonitorResult(scanned=0)

        # 2) 按下载器分组
        by_dl: dict[int, list[_Candidate]] = {}
        for c in cands:
            by_dl.setdefault(c.downloader_id, []).append(c)

        scanned = len(cands)
        skipped_permanent = 0
        skipped_far = 0
        skipped_no_end = 0
        skipped_completed = 0
        skipped_paused = 0
        pre_paused = 0
        missing = 0
        failed = 0
        errors: list[str] = []

        for downloader_id, items in by_dl.items():
            # 解析下载器 + adapter
            async with session_scope() as session:
                dl_row = (
                    await session.execute(
                        select(DownloaderInstance).where(
                            DownloaderInstance.id == downloader_id
                        )
                    )
                ).scalar_one_or_none()
            if dl_row is None or not dl_row.enabled:
                # 下载器已不存在/被禁用：孤儿候选全部记 missing
                missing += len(items)
                continue

            try:
                adapter = self._factory.downloader(dl_row)
                infos = await adapter.list_torrents()
            except Exception as exc:  # 防御：网络/凭据故障时整批跳过
                logger.warning(
                    "monitor: 下载器 %d list_torrents 失败：%s", downloader_id, exc
                )
                failed += len(items)
                errors.append(f"downloader={downloader_id} list_torrents: {exc}")
                continue

            info_by_hash = {t.hash: t for t in infos}

            for cand in items:
                # 过滤分支：按原因计数
                if cand.free_type is None or cand.free_end_at is None:
                    skipped_no_end += 1
                    continue
                if cand.free_type == FreeType.PERMANENT:
                    skipped_permanent += 1
                    continue
                if cand.free_type != FreeType.TIMED:
                    skipped_far += 1  # 其它 free_type 不属于"免费窗口"
                    continue
                if cand.free_end_at > threshold:
                    skipped_far += 1
                    continue

                hash_key = cand.info_hash
                info = info_by_hash.get(hash_key)
                # qB add_torrent 不返回真 hash，db 里是 synthetic 占位；用
                # name 兜底：qB 的 ``name`` 字段可能含 ``[category].`` 前缀，
                # 所以用 cand.name 作为子串搜索（cand.name 通常更长更具体）
                if info is None and cand.name:
                    needle = cand.name.lower()
                    for t in infos:
                        tn = (t.name or "").lower()
                        if tn and needle in tn:
                            info = t
                            break

                if info is None:
                    missing += 1
                    continue
                if info.progress >= 1.0:
                    skipped_completed += 1
                    continue
                if info.state.is_paused:
                    # 已被用户或之前的 monitor 暂停，不再重复 set_pause
                    skipped_paused += 1
                    continue

                # 触发暂停：用下载器返回的真实 hash（info.hash），不是 db 里的 synthetic
                real_hash = info.hash
                try:
                    await adapter.set_pause([real_hash], paused=True)
                except Exception as exc:
                    failed += 1
                    errors.append(
                        f"torrent_id={cand.torrent_id} set_pause: {exc}"
                    )
                    continue

                # 写 paused_reason（不写 status），并把真实 hash 回填到 db
                async with session_scope() as session:
                    row = (
                        await session.execute(
                            select(Torrent).where(Torrent.id == cand.torrent_id)
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        row.paused_reason = "free_expiring"
                        if hash_key != real_hash:
                            row.info_hash = real_hash
                pre_paused += 1

        result = MonitorResult(
            scanned=scanned,
            skipped_permanent=skipped_permanent,
            skipped_far_from_window=skipped_far,
            skipped_no_end_at=skipped_no_end,
            skipped_already_completed=skipped_completed,
            skipped_already_paused=skipped_paused,
            pre_paused=pre_paused,
            missing_in_downloader=missing,
            failed=failed,
            errors=tuple(errors[:10]),
        )
        logger.info(
            "monitor_free_window: 扫描 %d，触发暂停 %d，缺任务 %d，"
            "跳过(永久/远/空/已完成/已暂停) %d/%d/%d/%d/%d，失败 %d",
            result.scanned, result.pre_paused, result.missing_in_downloader,
            result.skipped_permanent, result.skipped_far_from_window,
            result.skipped_no_end_at, result.skipped_already_completed,
            result.skipped_already_paused, result.failed,
        )
        return result

    async def _select_candidates(
        self, session: AsyncSession
    ) -> list[_Candidate]:
        """拉所有 ``is_free=1 + status=active`` 的行；主循环按类型/距离过滤。"""
        stmt = (
            select(
                Torrent.id,
                Torrent.site_id,
                Torrent.item_id,
                Torrent.name,
                Torrent.info_hash,
                Torrent.downloader_id,
                Torrent.free_type,
                Torrent.free_end_at,
            )
            .where(
                Torrent.is_free.is_(True),
                Torrent.status == TorrentStatus.ACTIVE,
            )
        )
        raw = (await session.execute(stmt)).all()
        out: list[_Candidate] = []
        for row in raw:
            t_id, site_id, item_id, name, info_hash, downloader_id, ft, fea = row
            hash_key = (info_hash or "").strip()
            if not hash_key:
                continue
            out.append(
                _Candidate(
                    torrent_id=t_id,
                    site_id=site_id,
                    item_id=item_id or "",
                    name=name or "",
                    info_hash=hash_key,
                    downloader_id=downloader_id,
                    free_type=ft,
                    free_end_at=fea,
                )
            )
        return out
