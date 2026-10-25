"""辅种孤儿文件统计 service（作业 + 查询）。

与 :mod:`app.services.orphan_scanner` 的分工：

- :mod:`app.services.orphan_scanner` —— **纯算法**（扫盘 + 顶层名匹配），
  不碰 DB、不碰网络，便于单测
- 本模块 —— **编排**：拉下载器任务 → 调算法 → 落库 → 提供查询

作业 ``scan_orphans`` 每天跑一次（辅种站全量扫盘实测 39 秒，
不需要更频繁——孤儿不会快速变化）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import CrossSeedOrphan, DownloaderInstance
from app.db.session import session_scope
from app.services.factory import get_factory
from app.services.orphan_scanner import failed_scan, scan_orphans
from app.services.scheduler import JobStep

logger = get_logger(__name__)

__all__ = [
    "OrphanRow",
    "list_orphans",
    "run_scan_orphans",
    "scan_one_downloader",
]


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """孤儿统计的一行（DB 视图）。"""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    orphan_count: int
    orphan_bytes: int
    total_scan_bytes: int
    total_scan_count: int
    orphans: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloader_id": self.downloader_id,
            "downloader_name": self.downloader_name,
            "scanned_at": self.scanned_at,
            "orphan_count": self.orphan_count,
            "orphan_bytes": self.orphan_bytes,
            "total_scan_bytes": self.total_scan_bytes,
            "total_scan_count": self.total_scan_count,
            "orphans": self.orphans,
            "error": self.error,
        }


async def list_orphans() -> list[OrphanRow]:
    """所有辅种下载器的孤儿统计（按下载器 id 升序）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(CrossSeedOrphan).order_by(CrossSeedOrphan.downloader_id)
            )
        ).scalars().all()
    return [
        OrphanRow(
            downloader_id=r.downloader_id,
            downloader_name=r.downloader_name,
            scanned_at=r.scanned_at,
            orphan_count=r.orphan_count,
            orphan_bytes=r.orphan_bytes,
            total_scan_bytes=r.total_scan_bytes,
            total_scan_count=r.total_scan_count,
            orphans=list(r.orphans or []),
            error=r.error,
        )
        for r in rows
    ]


async def _upsert(downloader_id: int, name: str, result: Any) -> None:
    """把扫描结果 upsert 到 ``cross_seed_orphan``（短事务）。"""
    payload: dict[str, Any] = {
        "downloader_name": name,
        "scanned_at": bj_now(),
        "orphan_count": result.orphan_count,
        "orphan_bytes": result.orphan_bytes,
        "total_scan_bytes": result.total_bytes,
        "total_scan_count": result.total_count,
        "orphans": [o.to_dict() for o in result.orphans],
        "error": result.error,
    }
    async with session_scope() as session:
        row = (
            await session.execute(
                select(CrossSeedOrphan).where(
                    CrossSeedOrphan.downloader_id == downloader_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(CrossSeedOrphan(downloader_id=downloader_id, **payload))
        else:
            for k, v in payload.items():
                setattr(row, k, v)


async def scan_one_downloader(row: DownloaderInstance) -> dict[str, Any]:
    """扫描单个辅种下载器的孤儿文件。

    **网络 IO 在事务外**：拉任务 → 扫盘 → 短事务落库。

    拉取用 :meth:`DownloaderAdapter.list_torrents_with_errors`——它已含
    ``top_names`` 字段（孤儿匹配要用的顶层名），**复用同一次全量拉取**，
    不额外打接口（辅种站拉一次要 20s+）。
    """
    factory = get_factory()
    adapter = factory.downloader(row)
    try:
        torrents = await adapter.list_torrents_with_errors()
    except Exception as exc:
        logger.warning("辅种下载器 %s 拉取失败（孤儿扫描跳过）：%s", row.name, exc)
        result = failed_scan(str(exc) or type(exc).__name__)
        await _upsert(row.id, row.name, result)
        return result.to_dict()

    scan_dir = get_settings().cross_seed_scan_dir or None
    result = scan_orphans(torrents, scan_dir)
    await _upsert(row.id, row.name, result)
    return result.to_dict()


async def run_scan_orphans() -> dict[str, Any]:
    """作业入口：扫描所有辅种下载器的孤儿文件。

    流程：短事务读下载器 → 逐个拉取+扫盘（网络/IO 在事务外）→
    短事务落库。
    """
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.is_cross_seed.is_(True),
                        DownloaderInstance.enabled.is_(True),
                    ).order_by(DownloaderInstance.id)
                )
            ).scalars().all()
        )

    if not rows:
        return {
            "scanned_downloaders": 0,
            "orphan_count": 0,
            "orphan_bytes": 0,
            "summary": "无辅种下载器",
            "steps": [JobStep("查询辅种下载器", "skip", 0, "无辅种下载器")],
        }

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        result = await scan_one_downloader(row)
        results.append(result)
        if result.get("error"):
            errors.append(f"{row.name}: {result['error']}")

    orphan_count = sum(r["orphan_count"] for r in results)
    orphan_bytes = sum(r["orphan_bytes"] for r in results)
    summary = (
        f"{len(rows)} 个下载器，{orphan_count} 个孤儿文件，"
        f"{orphan_bytes / 1024**3:.2f} GB"
    )
    if errors:
        summary += f"，{len(errors)} 个扫描失败"

    return {
        "scanned_downloaders": len(rows),
        "orphan_count": orphan_count,
        "orphan_bytes": orphan_bytes,
        "summary": summary,
        "errors": errors,
        "steps": [
            JobStep(
                "扫描辅种下载器",
                "warn" if errors else "ok",
                len(rows),
                f"{len(rows)} 个下载器"
                + (f"，{len(errors)} 个失败" if errors else ""),
            ),
            JobStep(
                "发现孤儿文件",
                "warn" if orphan_count else "ok",
                orphan_count,
                f"{orphan_count} 个，{orphan_bytes / 1024**3:.2f} GB",
            ),
        ],
    }
