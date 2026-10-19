"""辅种下载器巡查 API.

端点 (prefix=/api/v1/cross-seed-monitor):
- GET  /downloaders                       列出 is_cross_seed=True 下载器
- GET  /downloaders/{id}/scan             实时扫描单个下载器 (异常+统计)

巡查本身是调度作业 ``scan_cross_seed``（cron ``0 */6 * * *``），由 cron
自动扫描并暂停异常种子——因此本模块**不暴露**写操作端点。
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
    DownloaderNotFound,
    NotCrossSeed,
    UnsupportedDownloader,
)

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


@router.get("/downloaders/{downloader_id}/scan", summary="扫描单个辅种下载器")
async def scan_one(downloader_id: int) -> dict[str, Any]:
    """实时拉 tr, 返回异常 + 统计 + 扫描时间. 无落库."""
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
    return result.to_dict()


