"""辅种下载器巡查 API.

端点 (prefix=/api/v1/cross-seed-monitor):
- GET  /downloaders                       列出 is_cross_seed=True 下载器
- GET  /snapshots                         辅种下载器快照列表（DB 读，毫秒级）
- GET  /downloaders/{id}/snapshot         单个辅种下载器快照详情
- POST /downloaders/{id}/refresh          手动刷新单个辅种下载器（抓取+落库）
- GET  /downloaders/{id}/scan             实时扫描单个辅种下载器（调试用）

巡查本身是调度作业 ``scan_cross_seed``（cron ``2,32 * * * *``），由 cron
定时抓取并把结果写入 ``cross_seed_snapshot`` 表，异常种子自动暂停。

**前端读快照，不打实时接口**：辅种下载器的种子量大（实测 3000+ 个），
实时调下载器接口要 20s+。实时扫描端点 ``/scan`` 保留作调试 / 应急用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services.cross_seed_monitor import (
    CrossSeedMonitorService,
    DownloaderDisabled,
    DownloaderError,
    DownloaderNotFound,
    NotCrossSeed,
    UnsupportedDownloader,
)
from app.services.cross_seed_space import SpaceStats, summarize_spaces

logger = get_logger(__name__)

router = APIRouter(
    prefix="/cross-seed-monitor",
    dependencies=[Depends(require_session)],
    tags=["cross-seed-monitor"],
)


# ============================================================ DTO
class DownloaderItem(BaseModel):
    id: int
    name: str
    type: str
    url: str
    enabled: bool
    is_cross_seed: bool


class SnapshotHeaderOut(BaseModel):
    """快照头部（列表视图）。"""

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


class SnapshotListOut(BaseModel):
    """快照列表 + 未抓取过的辅种下载器.

    ``pending`` 列出「是辅种但还没有快照行」的下载器——前端据此显示
    「等待首次抓取」而不是把空数据渲染成「没有种子」。
    """

    snapshots: list[SnapshotHeaderOut]
    pending: list[DownloaderItem]


class RefreshResultOut(BaseModel):
    """手动刷新的结果（与快照详情同结构）。"""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    total_torrents: int
    unique_count: int
    anomaly_count: int
    inflight_count: int
    paused_count: int = 0
    pause_failed_count: int = 0
    stats: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]


# ============================================================ 端点
@router.get("/downloaders", summary="辅种下载器列表")
async def list_downloaders() -> list[DownloaderItem]:
    """返回 is_cross_seed=True 且 enabled=True 的下载器."""
    svc = CrossSeedMonitorService()
    rows = await svc.list_cross_seed_downloaders()
    return [
        DownloaderItem(
            id=r.id,
            name=r.name,
            type=r.type,
            url=r.url,
            enabled=bool(r.enabled),
            is_cross_seed=bool(r.is_cross_seed),
        )
        for r in rows
    ]


@router.get("/snapshots", summary="辅种下载器快照列表")
async def list_snapshots() -> SnapshotListOut:
    """返回所有辅种下载器的快照头部 + 尚未抓取过的下载器.

    **纯 DB 读**（毫秒级）——不碰下载器接口。
    """
    svc = CrossSeedMonitorService()
    rows = await svc.list_cross_seed_downloaders()
    headers = await svc.list_snapshot_headers()
    have = {h.downloader_id for h in headers}
    return SnapshotListOut(
        snapshots=[SnapshotHeaderOut(**h.to_dict()) for h in headers],
        pending=[
            DownloaderItem(
                id=r.id,
                name=r.name,
                type=r.type,
                url=r.url,
                enabled=bool(r.enabled),
                is_cross_seed=bool(r.is_cross_seed),
            )
            for r in rows
            if r.id not in have
        ],
    )


@router.get("/space", summary="辅种空间汇总")
async def list_space() -> dict[str, Any]:
    """所有辅种下载器的空间汇总（磁盘剩余 / 辅种占用）。

    **纯 DB 读**（毫秒级）——数据来自定时作业写入的快照。

    只统计 ``is_cross_seed=True`` 的下载器（非辅种的普通下载器不占辅种
    空间，不在此列）。
    """
    svc = CrossSeedMonitorService()
    headers = await svc.list_snapshot_headers()
    summary = summarize_spaces([
        (
            h.downloader_name,
            SpaceStats(
                used_bytes=h.used_bytes,
                total_size_bytes=h.total_size_bytes,
                disk_free_bytes=h.disk_free_bytes,
                disk_total_bytes=h.disk_total_bytes,
            ),
        )
        for h in headers
    ])
    return {
        "summary": summary.to_dict(),
        "downloaders": [
            {
                "downloader_id": h.downloader_id,
                "downloader_name": h.downloader_name,
                "scanned_at": h.scanned_at,
                "used_bytes": h.used_bytes,
                "total_size_bytes": h.total_size_bytes,
                "disk_free_bytes": h.disk_free_bytes,
                "disk_total_bytes": h.disk_total_bytes,
            }
            for h in headers
        ],
    }


@router.get("/downloaders/{downloader_id}/snapshot", summary="单个辅种下载器快照")
async def get_snapshot(downloader_id: int) -> dict[str, Any]:
    """取单个辅种下载器的快照详情（含分组统计 + 异常明细）.

    还没有快照 → 404（前端据此显示「等待首次抓取」）。
    """
    svc = CrossSeedMonitorService()
    snap = await svc.get_snapshot(downloader_id)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"下载器 {downloader_id} 还没有快照（尚未抓取）",
        )
    return snap


@router.post("/downloaders/{downloader_id}/refresh", summary="手动刷新辅种下载器")
async def refresh_downloader(downloader_id: int) -> RefreshResultOut:
    """立即抓取单个辅种下载器 → 落库快照 → 暂停异常种子.

    **await 完整抓取**（辅种站 20s+）——反向代理超时要 > 60s。
    """
    svc = CrossSeedMonitorService()
    try:
        result = await svc.refresh_downloader(downloader_id)
    except DownloaderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotCrossSeed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DownloaderDisabled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedDownloader as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except DownloaderError as exc:
        # 下载器可达但报错（认证失败 / 网络异常 / 协议错误）——返回 502
        # 结构化错误，而不是漏成 500 带堆栈。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RefreshResultOut(**result.to_dict())


@router.get("/downloaders/{downloader_id}/scan", summary="实时扫描单个辅种下载器")
async def scan_one(downloader_id: int) -> dict[str, Any]:
    """实时拉 tr, 返回异常 + 统计 + 扫描时间. **不落库**.

    调试 / 应急用——前端主路径读快照（``/snapshots``）。
    """
    svc = CrossSeedMonitorService()
    try:
        result = await svc.scan_one(downloader_id)
    except DownloaderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotCrossSeed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DownloaderDisabled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedDownloader as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except DownloaderError as exc:
        # 下载器可达但报错（认证失败 / 网络异常 / 协议错误）——返回 502
        # 结构化错误，而不是漏成 500 带堆栈。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result.to_dict()
