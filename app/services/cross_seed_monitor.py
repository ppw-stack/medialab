"""辅种下载器巡查 service (FR 辅种下载器).

功能:
- 实时拉取 is_cross_seed=True 下载器的所有种子
- 检测两类异常: tracker_not_registered / no_data_found
- 暂停异常种子 (而非删除, 保留数据文件)
- 按 (原始 name, size) 精确聚合用于"统计"页

设计:
- 不落库: 实时统计, 刷新即新
- FR-5.2 兼容: 暂停是写操作, 走六重保护 (白名单/二次确认/...)

下载器支持: 通过 adapter 的公开方法
:meth:`DownloaderAdapter.list_torrents_with_errors` 取数——Transmission 与
qBittorrent 均已实现（错误码/错误描述由 adapter 归一到统一契约）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.adapters.downloaders.base import DownloaderError
from app.core.logging import get_logger
from app.db.models import DownloaderInstance
from app.db.session import session_scope
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


@dataclass(slots=True)
class StatsRow:
    """(norm_name, size_gb) 分组后的统计行."""

    name: str
    size_gb: float
    count: int
    sample_hash: str


@dataclass(slots=True)
class ScanResult:
    """单次扫描结果 (无状态, 直接给前端/作业)."""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    total_torrents: int
    anomalies: list[AnomalyItem] = field(default_factory=list)
    stats: list[StatsRow] = field(default_factory=list)

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
            "anomaly_count": self.anomaly_count,
            "unique_count": self.unique_count,
            "anomalies": [
                {
                    "id": a.id,
                    "hash": a.hash,
                    "name": a.name,
                    "size_gb": a.size_gb,
                    "status": a.status,
                    "anomaly_type": a.anomaly_type,
                    "error_code": a.error_code,
                    "error_string": a.error_string,
                }
                for a in self.anomalies
            ],
            "stats": [
                {
                    "name": s.name,
                    "size_gb": s.size_gb,
                    "count": s.count,
                    "sample_hash": s.sample_hash,
                }
                for s in self.stats
            ],
        }


# ============================================================ Service
class CrossSeedMonitorService:
    """巡查 service: 实时拉 + 实时检测 + 实时暂停."""

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
        """扫描单个下载器, 返回完整 ScanResult (anomalies + stats)."""
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

    async def scan_all(self) -> tuple[list[ScanResult], list[dict[str, Any]]]:
        """扫描所有 is_cross_seed=True 的下载器.

        返回 ``(成功的结果列表, 失败列表)``——失败的下载器不静默吞掉,
        由调用方（作业/UI）决定如何呈现。
        """
        rows = await self.list_cross_seed_downloaders()
        results: list[ScanResult] = []
        failures: list[dict[str, Any]] = []
        for row in rows:
            try:
                results.append(await self.scan_one(row.id))
            except Exception as exc:
                logger.warning(
                    "辅种下载器 %s (id=%d) 扫描失败: %s",
                    row.name, row.id, exc,
                )
                failures.append({
                    "downloader_id": row.id,
                    "downloader_name": row.name,
                    "error": str(exc),
                })
        return results, failures

    async def run_scan_job(self) -> dict[str, Any]:
        """作业入口: 扫描所有辅种下载器, 暂停异常 (与 cron 调用方对齐)."""
        results, failures = await self.scan_all()
        total_anomalies = sum(r.anomaly_count for r in results)
        total_torrents = sum(r.total_torrents for r in results)
        anomaly_types: dict[str, int] = {}
        for r in results:
            for a in r.anomalies:
                anomaly_types[a.anomaly_type] = anomaly_types.get(a.anomaly_type, 0) + 1

        # 自动暂停 (后台作业, 无二次确认, 走白名单逻辑)
        stopped = 0
        stop_failed = 0
        for r in results:
            for a in r.anomalies:
                try:
                    row = await self._resolve_downloader(r.downloader_id)
                    adapter = self._factory.downloader(row)
                    await adapter.set_pause([a.hash], paused=True)
                    stopped += 1
                    logger.info(
                        "已暂停辅种异常 downloader=%d hash=%s type=%s",
                        r.downloader_id, a.hash, a.anomaly_type,
                    )
                except Exception as exc:
                    stop_failed += 1
                    logger.warning(
                        "自动暂停失败 downloader=%d hash=%s: %s",
                        r.downloader_id, a.hash, exc,
                    )

        tracker_count = anomaly_types.get(ANOMALY_TRACKER_NOT_REGISTERED, 0)
        no_data_count = anomaly_types.get(ANOMALY_NO_DATA_FOUND, 0)
        total_downloaders = len(results) + len(failures)
        fail_detail = (
            f"，{len(failures)} 个失败" if failures else ""
        )
        return {
            "scanned_downloaders": len(results),
            "failed_downloaders": len(failures),
            "total_torrents": total_torrents,
            "total_anomalies": total_anomalies,
            "stopped": stopped,
            "summary": (
                f"{len(results)}/{total_downloaders} 个下载器，{total_torrents} 种子，"
                f"{total_anomalies} 异常已暂停 {stopped}{fail_detail}"
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
                    "warn" if stop_failed else ("ok" if stopped else "skip"),
                    stopped,
                    f"已暂停 {stopped} 个，{stop_failed} 失败",
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
        id / hash / name / size / status / error_code / error_string。
        """
        scanned_at = time.strftime("%Y-%m-%d %H:%M:%S")
        anomalies: list[AnomalyItem] = []
        # 统计: 按 (原始 name, size_bytes) 精确匹配, 不归一
        groups: dict[tuple[str, int], dict[str, Any]] = {}

        for t in torrents:
            t_hash = str(t.get("hash") or "")
            t_name = str(t.get("name") or "")
            t_size = int(t.get("size") or 0)
            t_status = int(t.get("status", -1))
            err_code = int(t.get("error_code") or 0)
            err_str = str(t.get("error_string") or "")

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
                }
            groups[key]["count"] += 1

        stats = sorted(
            (StatsRow(**g) for g in groups.values()),
            key=lambda s: (-s.count, s.name),
        )
        return ScanResult(
            downloader_id=row.id,
            downloader_name=row.name,
            scanned_at=scanned_at,
            total_torrents=len(torrents),
            anomalies=anomalies,
            stats=stats,
        )


# ============================================================ 异常
class DownloaderNotFound(Exception):
    pass


class NotCrossSeed(Exception):
    pass


class DownloaderDisabled(Exception):
    pass


class UnsupportedDownloader(Exception):
    pass
