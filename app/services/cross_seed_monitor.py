"""辅种下载器巡查 service (FR 辅种下载器).

功能:
- **定时抓取** is_cross_seed=True 下载器的所有种子 → 落库快照
- 检测两类异常: tracker_not_registered / no_data_found
- 暂停异常种子 (而非删除, 保留数据文件)
- 按 (原始 name, size) 精确聚合用于"统计"页

设计:
- **定时抓取 + 落库**: 辅种下载器的种子量大 (实测 3000+ 个), 实时调
  下载器接口要 20s+。前端每次打开辅种页面都打一次实时接口既慢又给
  下载器加压。故作业定时抓取并把结果写入 ``cross_seed_snapshot``
  (一个辅种下载器一行), 前端读快照。
- **网络 IO 在事务外**: 短事务读下载器 → 抓取 → 短事务批量 upsert →
  批量暂停 (网络 IO 也在事务外)。写锁不能横跨网络往返。
- **失败不写 0 兜底**: 抓取失败计入 failed 并点名, **保留上一次的
  快照**。写 0 等于把故障伪装成"没有种子"。
- 只暂停不删除: 保留数据文件, 用户可自行恢复
- 写操作前校验 hash 为 40 位 hex (拒绝 all/通配)

下载器支持: 通过 adapter 的公开方法
:meth:`DownloaderAdapter.list_torrents_with_errors` 取数——Transmission 与
qBittorrent 均已实现（错误码/错误描述由 adapter 归一到统一契约）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.adapters.downloaders.base import DownloaderError, TorrentState
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import CrossSeedSnapshot, DownloaderInstance
from app.db.session import session_scope
from app.services.cross_seed_space import SpaceStats, collect_space
from app.services.factory import get_factory
from app.services.scheduler import JobStep

logger = get_logger(__name__)


# ============================================================ 异常检测
ANOMALY_TRACKER_NOT_REGISTERED = "tracker_not_registered"
ANOMALY_NO_DATA_FOUND = "no_data_found"


def _detect_anomaly(error_string: str) -> str | None:
    """返回异常类型, 优先匹配更具体的 (no_data_found 在前)."""
    if not error_string:
        return None
    msg = error_string.lower()
    if "no data found" in msg:
        return ANOMALY_NO_DATA_FOUND
    if "not registered" in msg or "unregistered" in msg:
        return ANOMALY_TRACKER_NOT_REGISTERED
    return None


# ============================================================ 批量暂停参数
#: 单次 set_pause 调用的 hash 上限。
#:
#: 异常种子动辄数百个（辅种站 477 个），**逐个**调 ``set_pause([hash])``
#: 就是 477 次下载器 API 往返——慢且给下载器加压。adapter 的 ``set_pause``
#: 本就接受 hash 列表（qB 拼 ``|`` 分隔串、TR 传 ids 数组），故批量发。
#:
#: 上限 100 是权衡：qB 的 URL 长度有限（几百个 hash 拼一起可能超限），
#: TR 的 ids 数组过大也会让响应变慢。
PAUSE_BATCH_SIZE = 100

#: 同时抓取的辅种下载器数上限。
#:
#: 辅种站单次拉取 20s+，并发能压住总墙钟时间；但并发太大会同时占满
#: 下载器的连接/IO 资源。上限 3 与 user_data 作业的 5 同量级。
_FETCH_CONCURRENCY = 3


# ============================================================ 数据模型
@dataclass(slots=True)
class AnomalyItem:
    """单个异常种子."""

    id: int
    hash: str
    name: str
    size_gb: float
    status: int
    anomaly_type: str
    error_code: int
    error_string: str
    #: 抓取时该种子是否**已处于暂停态**（跳过重复暂停用）
    paused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hash": self.hash,
            "name": self.name,
            "size_gb": self.size_gb,
            "status": self.status,
            "anomaly_type": self.anomaly_type,
            "error_code": self.error_code,
            "error_string": self.error_string,
            "paused": self.paused,
        }


@dataclass(slots=True)
class StatsRow:
    """(norm_name, size_gb) 分组后的统计行."""

    name: str
    size_gb: float
    count: int
    sample_hash: str
    #: 该组的**完整 40 位** hash 列表（删除用）。
    #:
    #: ⚠️ :attr:`sample_hash` 是 **12 位截断**的，只作展示/抽样用——
    #: **不能**用于删除（删除必须 40 位完整 hash，否则适配器会拒绝）。
    #:
    #: 旧快照行没有这个字段 → 前端据此把删除按钮置灰（提示重新抓取）。
    hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_gb": self.size_gb,
            "count": self.count,
            "sample_hash": self.sample_hash,
            "hashes": self.hashes,
        }


@dataclass(slots=True)
class ScanResult:
    """单次扫描结果 (无状态, 直接给前端/作业)."""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    total_torrents: int
    inflight_count: int
    anomalies: list[AnomalyItem] = field(default_factory=list)
    stats: list[StatsRow] = field(default_factory=list)
    #: 本次**实际**发出暂停指令的种子数（不含已暂停被跳过的）
    paused_count: int = 0
    #: 暂停失败的种子数
    pause_failed_count: int = 0
    #: 空间统计（磁盘剩余 / 辅种占用 / 按目录分布）
    space: SpaceStats = field(default_factory=SpaceStats)

    @property
    def anomaly_count(self) -> int:
        return len(self.anomalies)

    @property
    def unique_count(self) -> int:
        return len(self.stats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloader_id": self.downloader_id,
            "downloader_name": self.downloader_name,
            "scanned_at": self.scanned_at,
            "total_torrents": self.total_torrents,
            "inflight_count": self.inflight_count,
            "anomaly_count": self.anomaly_count,
            "unique_count": self.unique_count,
            "paused_count": self.paused_count,
            "pause_failed_count": self.pause_failed_count,
            "anomalies": [a.to_dict() for a in self.anomalies],
            "stats": [s.to_dict() for s in self.stats],
            **self.space.to_dict(),
        }


@dataclass(slots=True)
class SnapshotHeader:
    """快照头部（列表视图用；不含 stats / anomalies 大字段)."""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    total_torrents: int
    unique_count: int
    anomaly_count: int
    inflight_count: int
    #: 本次**实际**发出暂停指令的种子数（不含已暂停被跳过的）
    paused_count: int = 0
    #: 暂停失败的种子数
    pause_failed_count: int = 0
    #: 磁盘剩余空间（字节）；None = 没取到
    disk_free_bytes: int | None = None
    #: 磁盘总量（字节）；None = 该下载器不提供（qB）
    disk_total_bytes: int | None = None
    #: 辅种实际占用（字节）
    used_bytes: int = 0
    #: 辅种总大小（字节，理论值）
    total_size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloader_id": self.downloader_id,
            "downloader_name": self.downloader_name,
            "scanned_at": self.scanned_at,
            "total_torrents": self.total_torrents,
            "unique_count": self.unique_count,
            "anomaly_count": self.anomaly_count,
            "inflight_count": self.inflight_count,
            "paused_count": self.paused_count,
            "pause_failed_count": self.pause_failed_count,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "used_bytes": self.used_bytes,
            "total_size_bytes": self.total_size_bytes,
        }


# ============================================================ Service
class CrossSeedMonitorService:
    """巡查 service: 定时抓取 + 落库快照 + 批量暂停异常."""

    def __init__(self) -> None:
        self._factory = get_factory()

    async def list_cross_seed_downloaders(self) -> list[DownloaderInstance]:
        """取所有 is_cross_seed=True 且 enabled=True 的下载器."""
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(DownloaderInstance)
                    .where(
                        DownloaderInstance.is_cross_seed.is_(True),
                        DownloaderInstance.enabled.is_(True),
                    )
                    .order_by(DownloaderInstance.id.asc())
                )
            ).scalars().all()
            # 退出 session_scope 前 detach 防止 lazy load 失败
            return list(rows)

    async def _resolve_downloader(self, downloader_id: int) -> DownloaderInstance:
        """取下载器配置行 (校验存在 + is_cross_seed)."""
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.id == downloader_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise DownloaderNotFound(f"下载器 {downloader_id} 不存在")
            if not row.is_cross_seed:
                raise NotCrossSeed(
                    f"下载器 {row.name} 未标记为辅种 (is_cross_seed=False)"
                )
            if not row.enabled:
                raise DownloaderDisabled(f"下载器 {row.name} 已禁用")
            return row

    async def scan_one(self, downloader_id: int) -> ScanResult:
        """扫描单个下载器, 返回完整 ScanResult (anomalies + stats).

        **不落库**——实时扫描（调试 / 应急用）。落库走
        :meth:`refresh_downloader`.
        """
        row = await self._resolve_downloader(downloader_id)
        adapter = self._factory.downloader(row)
        try:
            torrents = await adapter.list_torrents_with_errors()
        except NotImplementedError as exc:
            raise UnsupportedDownloader(str(exc)) from exc
        except DownloaderError as exc:
            logger.warning("辅种下载器 %s 拉取失败: %s", row.name, exc)
            raise
        return self._build_result(row, torrents)

    # ---------------------------------------------------------- 快照读写
    async def list_snapshot_headers(self) -> list[SnapshotHeader]:
        """所有辅种下载器的快照头部（按下载器 id 升序).

        **没有快照行的辅种下载器不在返回里**——调用方（API 层）用
        下载器列表做全集，比对两者得出"哪些还没抓过".
        """
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(CrossSeedSnapshot).order_by(
                        CrossSeedSnapshot.downloader_id.asc()
                    )
                )
            ).scalars().all()
        return [
            SnapshotHeader(
                downloader_id=r.downloader_id,
                downloader_name=r.downloader_name,
                scanned_at=r.scanned_at,
                total_torrents=r.total_torrents,
                unique_count=r.unique_count,
                anomaly_count=r.anomaly_count,
                inflight_count=r.inflight_count,
                paused_count=r.paused_count,
                pause_failed_count=r.pause_failed_count,
                disk_free_bytes=r.disk_free_bytes,
                disk_total_bytes=r.disk_total_bytes,
                used_bytes=r.used_bytes,
                total_size_bytes=r.total_size_bytes,
            )
            for r in rows
        ]

    async def get_snapshot(self, downloader_id: int) -> dict[str, Any] | None:
        """取单个下载器的快照详情（含 stats / anomalies）.

        返回 ``None`` = 该下载器还没有快照（尚未抓过）.
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(CrossSeedSnapshot).where(
                        CrossSeedSnapshot.downloader_id == downloader_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                **SnapshotHeader(
                    downloader_id=row.downloader_id,
                    downloader_name=row.downloader_name,
                    scanned_at=row.scanned_at,
                    total_torrents=row.total_torrents,
                    unique_count=row.unique_count,
                    anomaly_count=row.anomaly_count,
                    inflight_count=row.inflight_count,
                    paused_count=row.paused_count,
                    pause_failed_count=row.pause_failed_count,
                    disk_free_bytes=row.disk_free_bytes,
                    disk_total_bytes=row.disk_total_bytes,
                    used_bytes=row.used_bytes,
                    total_size_bytes=row.total_size_bytes,
                ).to_dict(),
                "by_dir": list(row.by_dir or []),
                "stats": list(row.stats or []),
                "anomalies": list(row.anomalies or []),
            }

    async def _upsert_snapshot(
        self, downloader_id: int, result: ScanResult
    ) -> None:
        """把扫描结果 upsert 到 ``cross_seed_snapshot``（短事务）."""
        payload: dict[str, Any] = {
            "downloader_name": result.downloader_name,
            "scanned_at": result.scanned_at,
            "total_torrents": result.total_torrents,
            "unique_count": result.unique_count,
            "anomaly_count": result.anomaly_count,
            "inflight_count": result.inflight_count,
            "paused_count": result.paused_count,
            "pause_failed_count": result.pause_failed_count,
            "disk_free_bytes": result.space.disk_free_bytes,
            "disk_total_bytes": result.space.disk_total_bytes,
            "used_bytes": result.space.used_bytes,
            "total_size_bytes": result.space.total_size_bytes,
            "by_dir": [d.to_dict() for d in result.space.by_dir],
            "stats": [s.to_dict() for s in result.stats],
            "anomalies": [a.to_dict() for a in result.anomalies],
        }
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(CrossSeedSnapshot).where(
                        CrossSeedSnapshot.downloader_id == downloader_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    CrossSeedSnapshot(downloader_id=downloader_id, **payload)
                )
            else:
                for k, v in payload.items():
                    setattr(row, k, v)

    # ---------------------------------------------------------- 刷新
    async def refresh_downloader(self, downloader_id: int) -> ScanResult:
        """抓取单个辅种下载器 → 落库快照 → 批量暂停异常种子.

        作业与手动刷新端点**共用这一个方法**（语义完全一致）.

        **网络 IO 在事务外**：抓取 → 短事务 upsert → 批量暂停.

        :raises DownloaderNotFound: 下载器不存在
        :raises NotCrossSeed: 未标记为辅种
        :raises DownloaderDisabled: 已禁用
        :raises UnsupportedDownloader: 适配器不支持该方法
        :raises DownloaderError: 抓取失败（**不写库**，保留上一次的快照）
        """
        row = await self._resolve_downloader(downloader_id)
        adapter = self._factory.downloader(row)
        try:
            torrents = await adapter.list_torrents_with_errors()
        except NotImplementedError as exc:
            raise UnsupportedDownloader(str(exc)) from exc
        except DownloaderError as exc:
            logger.warning("辅种下载器 %s 拉取失败: %s", row.name, exc)
            raise

        result = self._build_result(row, torrents)
        # 空间统计：复用**同一次**全量拉取的结果算占用（不额外打接口），
        # 只有磁盘空间需要额外一次轻量调用。磁盘信息取不到不影响占用。
        result.space = await collect_space(adapter, torrents)
        # **先暂停再落库**：快照里的 ``paused_count`` / ``pause_failed_count``
        # 是本次抓取**实际**的动作结果。先落库再暂停的话，这两列会停留在
        # 上一轮的值（前端据此显示「已暂停 N 个」会撒谎）。
        #
        # 暂停失败不会丢快照：:meth:`_pause_anomalies` 逐批捕获异常并计数，
        # 不向上抛（只有 CancelledError 会传播，那是应当中断的信号）。
        stopped, failed = await self._pause_anomalies(adapter, result.anomalies)
        result.paused_count = stopped
        result.pause_failed_count = failed
        await self._upsert_snapshot(row.id, result)
        return result

    async def _pause_anomalies(
        self, adapter: Any, anomalies: list[AnomalyItem]
    ) -> tuple[int, int]:
        """批量暂停异常种子.

        返回 ``(已暂停数, 失败数)``.

        - **跳过已暂停的**：异常种子在上一轮已被暂停过（qB ``pausedUP``
          / TR ``status=0``），再发一次暂停是白费 API 调用。
        - **分批发送**：按 :data:`PAUSE_BATCH_SIZE` 切批，一次
          ``set_pause([...])`` 发一批。
        """
        pending = [a for a in anomalies if not a.paused]
        if not pending:
            return 0, 0

        stopped = 0
        failed = 0
        for i in range(0, len(pending), PAUSE_BATCH_SIZE):
            batch = pending[i : i + PAUSE_BATCH_SIZE]
            hashes = [a.hash for a in batch if a.hash]
            if not hashes:
                continue
            try:
                await adapter.set_pause(hashes, paused=True)
                stopped += len(hashes)
                logger.info(
                    "已暂停辅种异常 %d 个（downloader=%s）: %s",
                    len(hashes),
                    getattr(adapter, "name", "?"),
                    ",".join(hashes[:5]) + ("…" if len(hashes) > 5 else ""),
                )
            except Exception as exc:
                failed += len(hashes)
                logger.warning(
                    "批量暂停辅种异常失败 downloader=%s 数量=%d: %s",
                    getattr(adapter, "name", "?"), len(hashes), exc,
                )
        return stopped, failed

    # ---------------------------------------------------------- 作业
    async def run_scan_job(self) -> dict[str, Any]:
        """作业入口: 扫描所有辅种下载器, 落库快照 + 暂停异常.

        三段式：**短事务读下载器 → 并发抓取（网络 IO 在事务外）→
        短事务批量 upsert → 批量暂停（网络 IO 也在事务外）**.
        """
        rows = await self.list_cross_seed_downloaders()
        total_downloaders = len(rows)
        if not rows:
            return {
                "scanned_downloaders": 0,
                "failed_downloaders": 0,
                "total_torrents": 0,
                "total_anomalies": 0,
                "stopped": 0,
                "summary": "无辅种下载器",
                "results": [],
                "failures": [],
                "steps": [
                    JobStep("查询辅种下载器", "skip", 0, "无辅种下载器"),
                ],
            }

        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _one(row: DownloaderInstance) -> tuple[ScanResult | None, str | None]:
            async with semaphore:
                try:
                    result = await self.refresh_downloader(row.id)
                    return result, None
                except (NotCrossSeed, DownloaderDisabled, DownloaderNotFound) as exc:
                    # 配置错误：该下载器不再是辅种 / 已禁用 / 已删除
                    logger.info(
                        "辅种下载器 %s (id=%d) 跳过：%s", row.name, row.id, exc
                    )
                    return None, None
                except Exception as exc:
                    logger.warning(
                        "辅种下载器 %s (id=%d) 扫描失败: %s",
                        row.name, row.id, exc,
                    )
                    return None, str(exc)

        outcomes = await asyncio.gather(*(_one(row) for row in rows))

        results: list[ScanResult] = []
        failures: list[dict[str, Any]] = []
        for row, (result, error) in zip(rows, outcomes, strict=True):
            if result is not None:
                results.append(result)
            elif error is not None:
                failures.append({
                    "downloader_id": row.id,
                    "downloader_name": row.name,
                    "error": error,
                })

        total_anomalies = sum(r.anomaly_count for r in results)
        total_torrents = sum(r.total_torrents for r in results)
        total_paused = sum(r.paused_count for r in results)
        total_pause_failed = sum(r.pause_failed_count for r in results)
        total_skipped = total_anomalies - total_paused - total_pause_failed
        anomaly_types: dict[str, int] = {}
        for r in results:
            for a in r.anomalies:
                anomaly_types[a.anomaly_type] = anomaly_types.get(a.anomaly_type, 0) + 1

        tracker_count = anomaly_types.get(ANOMALY_TRACKER_NOT_REGISTERED, 0)
        no_data_count = anomaly_types.get(ANOMALY_NO_DATA_FOUND, 0)
        # 失败必须**点名**——只说"1 个失败"用户不知道是哪台（AGENTS.md
        # 契约：故障不能被伪装成正常）。
        fail_detail = (
            f"，{len(failures)} 个失败（{', '.join(f['downloader_name'] for f in failures)}）"
            if failures
            else ""
        )
        # 异常数 ≠ 实际暂停数：已暂停的被跳过（不重复发指令），失败的不算。
        # 说"31 异常已暂停"而实际只暂停了 4 个 = 谎报。
        pause_detail = (
            f"，暂停 {total_paused} 个"
            + (f"（{total_pause_failed} 失败）" if total_pause_failed else "")
            + (f"，{total_skipped} 个此前已暂停" if total_skipped > 0 else "")
        )
        return {
            "scanned_downloaders": len(results),
            "failed_downloaders": len(failures),
            "total_torrents": total_torrents,
            "total_anomalies": total_anomalies,
            "paused": total_paused,
            "pause_failed": total_pause_failed,
            "summary": (
                f"{len(results)}/{total_downloaders} 个下载器，{total_torrents} 种子，"
                f"{total_anomalies} 异常{pause_detail}{fail_detail}"
            ),
            "results": [r.to_dict() for r in results],
            "failures": failures,
            "steps": [
                JobStep(
                    "扫描辅种下载器",
                    "warn" if failures else ("ok" if results else "skip"),
                    len(results),
                    f"{len(results)}/{total_downloaders} 个实例成功"
                    + (f"，{len(failures)} 个失败" if failures else ""),
                ),
                JobStep(
                    "写入快照",
                    "ok" if results else "skip",
                    len(results),
                    f"{len(results)} 行 cross_seed_snapshot",
                ),
                JobStep(
                    "统计种子总数",
                    "ok",
                    total_torrents,
                    f"{total_torrents} 个种子",
                ),
                JobStep(
                    "检测异常",
                    "warn" if total_anomalies else "ok",
                    total_anomalies,
                    f"tracker 未注册 {tracker_count} / 数据丢失 {no_data_count}",
                ),
                JobStep(
                    "自动暂停",
                    "warn" if total_pause_failed else ("ok" if total_paused else "skip"),
                    total_paused,
                    f"已暂停 {total_paused} 个"
                    + (f"，{total_pause_failed} 失败" if total_pause_failed else "")
                    + (f"，{total_skipped} 个此前已暂停（跳过）" if total_skipped > 0 else ""),
                ),
            ],
        }

    # ---------------------------------------------------------- 内部
    def _build_result(
        self, row: DownloaderInstance, torrents: list[dict[str, Any]]
    ) -> ScanResult:
        """按 adapter 规范化后的种子列表构建结果。

        入参字段契约见
        :meth:`app.adapters.downloaders.base.DownloaderAdapter.list_torrents_with_errors`:
        id / hash / name / size / status / error_code / error_string / state。
        """
        scanned_at = bj_now()
        anomalies: list[AnomalyItem] = []
        # 统计: 按 (原始 name, size_bytes) 精确匹配, 不归一
        groups: dict[tuple[str, int], dict[str, Any]] = {}
        inflight = 0

        for t in torrents:
            t_hash = str(t.get("hash") or "")
            t_name = str(t.get("name") or "")
            t_size = int(t.get("size") or 0)
            t_status = int(t.get("status", -1))
            err_code = int(t.get("error_code") or 0)
            err_str = str(t.get("error_string") or "")

            # 在途计数：非暂停、非致命错误（口径见 TorrentState.is_inflight）
            state = _parse_state(t.get("state"))
            if state.is_inflight:
                inflight += 1

            # 异常检测
            anom = _detect_anomaly(err_str)
            if anom:
                anomalies.append(AnomalyItem(
                    id=int(t.get("id") or 0),
                    hash=t_hash,
                    name=t_name,
                    size_gb=round(t_size / 1024**3, 2),
                    status=t_status,
                    anomaly_type=anom,
                    error_code=err_code,
                    error_string=err_str[:500],
                    # 归一化状态判「是否已暂停」——**不能**用 status==0：
                    # qB 把 error / missingFiles 也映射成 0（无原生 status
                    # 码），用它判会把「数据丢失」的种子误当成已暂停而
                    # 跳过暂停（那正是最该暂停的一类）。
                    paused=state.is_paused,
                ))

            # 统计: 按 (原始 name, size_bytes) — 保留集数/分辨率/制作组差异
            if not t_name:
                continue
            key = (t_name, t_size)
            if key not in groups:
                groups[key] = {
                    "name": t_name,
                    "size_gb": round(t_size / 1024**3, 2),
                    "count": 0,
                    "sample_hash": t_hash[:12] if t_hash else "",
                    # 完整 40 位 hash 列表——删除该组时要用到
                    # （sample_hash 是 12 位截断的，删不了）
                    "hashes": [],
                }
            groups[key]["count"] += 1
            if t_hash:
                groups[key]["hashes"].append(t_hash)

        stats = sorted(
            (StatsRow(**g) for g in groups.values()),
            key=lambda s: (-s.count, s.name),
        )
        return ScanResult(
            downloader_id=row.id,
            downloader_name=row.name,
            scanned_at=scanned_at,
            total_torrents=len(torrents),
            inflight_count=inflight,
            anomalies=anomalies,
            stats=stats,
        )


# ============================================================ 内部工具
def _parse_state(raw: object) -> TorrentState:
    """解析 adapter 给的归一化状态（缺失 / 非法 → UNKNOWN）."""
    if not isinstance(raw, str) or not raw:
        return TorrentState.UNKNOWN
    try:
        return TorrentState.parse(raw)
    except ValueError:
        return TorrentState.UNKNOWN


# ============================================================ 异常
class DownloaderNotFound(Exception):
    pass


class NotCrossSeed(Exception):
    pass


class DownloaderDisabled(Exception):
    pass


class UnsupportedDownloader(Exception):
    pass


#: 适配层异常在此 re-export——API 层只需知道 service 层抛什么，
#: 不必直接依赖适配层（``from app.adapters...``）。
__all__ = [
    "ANOMALY_NO_DATA_FOUND",
    "ANOMALY_TRACKER_NOT_REGISTERED",
    "PAUSE_BATCH_SIZE",
    "AnomalyItem",
    "CrossSeedMonitorService",
    "DownloaderDisabled",
    "DownloaderError",
    "DownloaderNotFound",
    "NotCrossSeed",
    "ScanResult",
    "SnapshotHeader",
    "StatsRow",
    "UnsupportedDownloader",
]