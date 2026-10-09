"""巡检引擎（FR-4.1 十二项指标 + FR-4.1.1 安全边界）。

**最重要的设计约束**（FR-4.1.1）：巡检触发的动作分两级，绝不可混淆：

- **无数据副作用**：告警、暂停、重启任务 —— 可全自动执行。
- **触碰数据**：移除任务、移入回收站 —— **必须走 FR-5.2 全部机制**。

M3 只实现第一级。第二级只把对象**标记为清理候选**（``status=candidate``
或写入候选集），真正执行留给 M5 的 Cleaner 管线——这样 M5 上线前不存在
任何绕过六重保护的代码路径。

FR-2.5 空做种是唯一的"自动暂停"例外，它会经过保护期检查（FR-5.2.6）。
"""

from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import DownloaderAdapter, TorrentInfo, TorrentState
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import (
    AlertCategory,
    AlertLevel,
    DownloaderInstance,
    PausedReason,
    Site,
    Torrent,
    TorrentStatus,
)
from app.services.disk import LOW_WATER_DEFAULT, is_low_water
from app.services.factory import AdapterFactory

logger = get_logger(__name__)

#: 巡检默认间隔（秒，FR-4.1：15 分钟一轮）
INSPECT_INTERVAL = 900
#: 空做种判定：连续 N 轮 num_seeds==0 且上传为 0（FR-2.5）
ZERO_SEED_ROUNDS = 2
#: 下载速度为零持续时长（FR-4.1：30 分钟）
ZERO_SPEED_SECONDS = 1800
#: 连接数超限默认阈值（FR-4.1）
DEFAULT_CONN_LIMIT = 500
#: 磁盘低水位默认阈值（FR-4.1 / FR-5.3）
DEFAULT_DISK_THRESHOLD = LOW_WATER_DEFAULT


@dataclass
class Finding:
    """一条巡检发现。"""

    level: str
    category: str
    message: str
    info_hash: str | None = None
    torrent_id: int | None = None
    #: 建议动作，仅作为面板提示；**引擎不据此自行删除数据**
    action: str = ""


@dataclass
class InspectReport:
    """一轮巡检结果。"""

    inspected: int = 0
    findings: list[Finding] = field(default_factory=list)
    downloaders: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.level == AlertLevel.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.level == AlertLevel.WARNING)

    def by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.category] = out.get(f.category, 0) + 1
        return out


def check_torrent_health(
    info: TorrentInfo,
    *,
    tracker_deleted: bool = False,
    hr_deadline_passed: bool = False,
    ratio_floor: float = 1.0,
    site_remaining_hours: float | None = None,
    seed_time_threshold: float = 24.0,
    conn_limit: int = DEFAULT_CONN_LIMIT,
    zero_speed_since: float | None = None,
    now: float | None = None,
) -> list[Finding]:
    """对单个任务做健康判定（FR-4.1 各项指标）。

    **纯函数**：不碰数据库、不碰下载器，因此可以对着构造的
    :class:`TorrentInfo` 逐一验证每条规则，且不依赖任何外部状态。
    """
    out: list[Finding] = []
    ref_now = now or time.time()

    def add(level: str, category: str, msg: str, action: str = "") -> None:
        out.append(
            Finding(
                level=level,
                category=category,
                message=msg,
                info_hash=info.hash,
                action=action,
            )
        )

    # 种子被站点删除 → 🔴（FR-4.1）
    if tracker_deleted:
        add(
            AlertLevel.CRITICAL,
            AlertCategory.TORRENT_DELETED,
            "站点已删除该种子（Tracker 404）",
            "移入清理候选",
        )
        return out  # 已删则后续指标无意义

    # 种子文件错误 → 🔴（FR-4.1）
    if info.state in {TorrentState.ERROR, TorrentState.MISSING_FILES}:
        add(
            AlertLevel.CRITICAL,
            AlertCategory.TORRENT_ERROR,
            f"任务状态异常：{info.state}",
            "移入清理候选",
        )
        return out

    # 非法种子：长期 checking / unknown → 🟡（FR-4.1）
    if info.state in {TorrentState.CHECKING, TorrentState.UNKNOWN}:
        add(
            AlertLevel.WARNING,
            AlertCategory.ILLEGAL_TORRENT,
            f"任务状态可疑：{info.state}，可能校验失败",
            "人工确认",
        )

    # 做种数 = 0 → 🟡（FR-4.1 / FR-2.5）
    if info.state.is_completed and info.num_seeds == 0 and info.upspeed == 0:
        add(
            AlertLevel.WARNING,
            AlertCategory.ZERO_SEEDS,
            "空做种：做种数为 0 且无上传",
            "自动暂停（连续 2 轮后）",
        )

    # 分享率跌破安全线 → 🟡（FR-4.1）
    if info.state.is_completed and info.ratio < ratio_floor and hr_deadline_passed:
        add(
            AlertLevel.WARNING,
            AlertCategory.RATIO_LOW,
            f"分享率 {info.ratio:.3f} < 安全线 {ratio_floor}，且已过 H&R 期限",
            "建议补种或删除",
        )

    # 保种时长不足 → 🔴（FR-4.1）
    if (
        site_remaining_hours is not None
        and site_remaining_hours < 1
        and info.ratio < ratio_floor
        and info.state.is_completed
    ):
        add(
            AlertLevel.CRITICAL,
            AlertCategory.SEED_TIME_SHORT,
            f"站点剩余保种时间 {site_remaining_hours:.1f}h 且分享率 {info.ratio:.3f} 不足，"
            "有 H&R 风险",
            "立即处理",
        )

    # 下载速度为零 → 🟡（FR-4.1：持续 30 分钟且非暂停）
    if (
        zero_speed_since is not None
        and info.state.is_downloading
        and info.dlspeed == 0
        and (ref_now - zero_speed_since) >= ZERO_SPEED_SECONDS
    ):
        add(
            AlertLevel.WARNING,
            AlertCategory.ZERO_DOWNLOAD_SPEED,
            f"下载速度为 0 已持续 {int((ref_now - zero_speed_since) / 60)} 分钟",
            "尝试重启任务",
        )

    # 连接数超限 → 🟡（FR-4.1）
    if info.num_connections > conn_limit:
        add(
            AlertLevel.WARNING,
            AlertCategory.CONN_LIMIT,
            f"连接数 {info.num_connections} 超过阈值 {conn_limit}",
            "检查端口/防火墙",
        )

    return out


class Inspector:
    """巡检编排。"""

    def __init__(self, factory: AdapterFactory) -> None:
        self._factory = factory

    async def inspect_downloaders(self, report: InspectReport) -> None:
        """下载器级巡检：连通性、限速改动（FR-4.1）。"""
        from app.db.session import session_scope

        async with session_scope() as session:
            rows = (
                (await session.execute(select(DownloaderInstance).where(
                    DownloaderInstance.enabled.is_(True)
                )))
                .scalars()
                .all()
            )
            for row in rows:
                try:
                    adapter = self._factory.downloader(row)
                except Exception as exc:
                    report.downloaders[row.name] = f"factory_error: {exc}"
                    report.findings.append(
                        Finding(
                            level=AlertLevel.CRITICAL,
                            category=AlertCategory.CLIENT_ERROR,
                            message=f"下载器 {row.name} 无法构造：{exc}",
                        )
                    )
                    continue

                alive = await adapter.ping()
                report.downloaders[row.name] = "ok" if alive else "unreachable"
                if not alive:
                    report.findings.append(
                        Finding(
                            level=AlertLevel.CRITICAL,
                            category=AlertCategory.CLIENT_ERROR,
                            message=f"下载器 {row.name} WebUI 无响应或账号失效",
                            action="熔断重试",
                        )
                    )
                    continue

                report.findings.extend(
                    await self._check_rate_limit(adapter, row, report)
                )

    async def _check_rate_limit(
        self,
        adapter: DownloaderAdapter,
        row: DownloaderInstance,
        report: InspectReport,
    ) -> list[Finding]:
        """限速被外部改动检测（FR-4.1）。"""
        out: list[Finding] = []
        try:
            stats = await adapter.get_stats()
        except Exception as exc:
            logger.debug("读取 %s 统计失败：%s", row.name, exc)
            return out

        from app.services.units import parse_speed

        for label, actual, configured in (
            ("上传", stats.upload_limit, row.max_upload),
            ("下载", stats.download_limit, row.max_download),
        ):
            if not configured:
                continue
            want = parse_speed(configured)
            if want is None:
                continue
            if actual != want:
                out.append(
                    Finding(
                        level=AlertLevel.WARNING,
                        category=AlertCategory.RATE_LIMIT_CHANGED,
                        message=(
                            f"{row.name} {label}限速被改动："
                            f"当前 {actual}，期望 {want}"
                        ),
                        action="可选自动纠正",
                    )
                )
        return out

    async def inspect_torrents(self, report: InspectReport) -> None:
        """任务级巡检。"""
        from app.db.session import session_scope

        async with session_scope() as session:
            dl_rows = (
                (await session.execute(
                    select(DownloaderInstance).where(DownloaderInstance.enabled.is_(True))
                ))
                .scalars()
                .all()
            )
            adapters = {
                r.id: self._factory.downloader(r)
                for r in dl_rows
                if r.name in report.downloaders and report.downloaders[r.name] == "ok"
            }
            if not adapters:
                logger.info("无可用下载器，跳过任务巡检")
                return

            # 逐下载器串行拉取（单实例操作锁，非功能·并发安全）
            live: dict[int, list[TorrentInfo]] = {}
            for dl_id, adapter in adapters.items():
                async with _instance_lock(adapter):
                    try:
                        live[dl_id] = await adapter.list_torrents()
                    except Exception as exc:
                        logger.warning("拉取任务列表失败（%s）：%s", dl_id, exc)
                        live[dl_id] = []

            await self._evaluate(session, live, report)

    async def _evaluate(
        self,
        session: AsyncSession,
        live: dict[int, list[TorrentInfo]],
        report: InspectReport,
    ) -> None:
        """比对下载器实时状态与本地记录，产出 findings。"""
        rows = (
            (
                await session.execute(
                    select(Torrent).where(
                        Torrent.status.in_([TorrentStatus.ACTIVE, TorrentStatus.PAUSED])
                    )
                )
            )
            .scalars()
            .all()
        )
        # TorrentInfo 的字段名是 hash（与 Torrent.info_hash 对应）
        by_hash = {
            t.hash: t
            for torrents in live.values()
            for t in torrents
        }
        tracker = {t.hash for torrents in live.values() for t in torrents}
        seen_hashes: set[str] = set()

        for torrent in rows:
            seen_hashes.add(torrent.info_hash)
            info = by_hash.get(torrent.info_hash)
            if info is None:
                # 本地有记录但下载器里没有 → 人工确认，不自动处理
                report.findings.append(
                    Finding(
                        level=AlertLevel.WARNING,
                        category=AlertCategory.ILLEGAL_TORRENT,
                        message=f"任务 {torrent.name} 在下载器中不存在",
                        info_hash=torrent.info_hash,
                        torrent_id=torrent.id,
                        action="人工确认",
                    )
                )
                continue

            report.inspected += 1
            findings = check_torrent_health(
                info,
                conn_limit=DEFAULT_CONN_LIMIT,
                now=time.time(),
            )
            for f in findings:
                f.torrent_id = torrent.id
            report.findings.extend(findings)

            if any(f.category == AlertCategory.ZERO_SEEDS for f in findings):
                await self._handle_zero_seed(session, torrent, info)

        # 下载器里有、本地没有的（外部添加）
        external = tracker - seen_hashes
        if external:
            report.findings.append(
                Finding(
                    level=AlertLevel.INFO,
                    category=AlertCategory.ILLEGAL_TORRENT,
                    message=f"下载器存在 {len(external)} 个未登记任务",
                    action="人工确认",
                )
            )

    async def _handle_zero_seed(
        self,
        session: AsyncSession,
        torrent: Torrent,
        info: TorrentInfo,
    ) -> None:
        """FR-2.5 空做种处置：连续 2 轮后自动暂停。

        必须过禁删保护期（FR-5.2.6）——刚添加的任务瞬时 0 做种是正常的，
        直接暂停会打断正常辅种。
        """
        now = utcnow()
        if torrent.protect_until and torrent.protect_until > now:
            return
        if torrent.paused_reason == PausedReason.YIELD:
            return  # 让路暂停不是空做种

        torrent.zero_seed_rounds = getattr(torrent, "zero_seed_rounds", 0) + 1
        await session.flush()
        if torrent.zero_seed_rounds < ZERO_SEED_ROUNDS:
            logger.info("任务 %s 空做种第 %d 轮，继续观察", torrent.name, torrent.zero_seed_rounds)
            return

        adapter = await self._adapter_for(session, torrent.downloader_id)
        if adapter is None:
            return
        try:
            async with _instance_lock(adapter):
                await adapter.set_pause([torrent.info_hash], True)
        except Exception as exc:
            logger.warning("暂停空做种任务失败：%s", exc)
            return
        torrent.status = TorrentStatus.PAUSED
        torrent.paused_reason = PausedReason.SYSTEM
        torrent.zero_seed_rounds = 0
        await session.flush()
        logger.info("任务 %s 空做种，已自动暂停（FR-2.5）", torrent.name)

    async def _adapter_for(
        self, session: AsyncSession, downloader_id: int
    ) -> DownloaderAdapter | None:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            return self._factory.downloader(row)
        except Exception:
            return None

    # ------------------------------------------------------------ 磁盘
    async def inspect_disk(self, report: InspectReport) -> None:
        """磁盘空间巡检（FR-4.1）。"""
        from app.db.session import session_scope

        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(Site.save_path_template).distinct()
                    )
                )
                .scalars()
                .all()
            )
        for tpl in rows:
            root = tpl.split("{", 1)[0] or "/"
            if is_low_water(root, DEFAULT_DISK_THRESHOLD):
                report.findings.append(
                    Finding(
                        level=AlertLevel.CRITICAL,
                        category=AlertCategory.DISK_LOW,
                        message=f"磁盘 {root} 可用空间低于 "
                        f"{DEFAULT_DISK_THRESHOLD // 1024**3}GB",
                        action="触发清理",
                    )
                )

    # ------------------------------------------------------------ 整轮
    async def run_once(self) -> InspectReport:
        """一轮完整巡检（R8：单阶段失败不中断整轮）。"""
        started = time.monotonic()
        report = InspectReport()

        for name, coro in (
            ("下载器", self.inspect_downloaders(report)),
            ("任务", self.inspect_torrents(report)),
            ("磁盘", self.inspect_disk(report)),
        ):
            try:
                await coro
            except Exception as exc:
                logger.exception("%s巡检阶段失败：%s", name, exc)
                report.findings.append(
                    Finding(
                        level=AlertLevel.WARNING,
                        category=AlertCategory.CLIENT_ERROR,
                        message=f"{name}巡检阶段异常：{exc}",
                    )
                )

        report.duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "巡检完成：检查 %d 个任务，🔴 %d / 🟡 %d，耗时 %dms",
            report.inspected,
            report.critical_count,
            report.warning_count,
            report.duration_ms,
        )
        return report


def _instance_lock(adapter: DownloaderAdapter) -> AbstractAsyncContextManager[None]:
    """按下载器实例取操作锁（R5）。"""
    from app.adapters.downloaders.locks import locks

    return locks.acquire(adapter.name)


__all__ = [
    "INSPECT_INTERVAL",
    "Finding",
    "InspectReport",
    "Inspector",
    "check_torrent_health",
]
