"""辅种下载器巡查 API.

端点 (prefix=/api/v1/cross-seed-monitor):
- GET  /downloaders                       列出 is_cross_seed=True 下载器
- GET  /downloaders/{id}/scan             实时扫描单个下载器 (异常+统计)
- POST /downloaders/{id}/stop             暂停异常种子 (FR-5.2 二次确认)
- POST /downloaders/scan-all              扫描所有辅种下载器
- GET  /downloaders/{id}/torrents         列出下载器原生任务 (辅助)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services.cross_seed_monitor import (
    CrossSeedMonitorService,
    DownloaderDisabled,
    DownloaderNotFound,
    NeedConfirm,
    NoHashes,
    NotCrossSeed,
    StateMismatch,
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


class StopRequest(BaseModel):
    hashes: list[str] = Field(..., min_length=1, max_length=200)
    confirm: bool = False


class StopResponse(BaseModel):
    downloader_id: int
    requested: int
    stopped: int
    failed: int
    results: list[dict[str, Any]]


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


@router.post(
    "/downloaders/{downloader_id}/stop",
    summary="暂停异常种子",
    response_model=StopResponse,
)
async def stop_anomalies(
    downloader_id: int, payload: StopRequest
) -> StopResponse:
    """FR-5.2: 二次确认 + 路径三重校验 (hash 必须在最近扫描中仍属异常)."""
    svc = CrossSeedMonitorService()
    try:
        result = await svc.stop_anomalies(
            downloader_id=downloader_id,
            hashes=payload.hashes,
            confirm=payload.confirm,
        )
    except NeedConfirm as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NoHashes as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StateMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DownloaderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotCrossSeed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DownloaderDisabled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StopResponse(**result)


@router.post("/downloaders/scan-all", summary="扫描所有辅种下载器")
async def scan_all() -> dict[str, Any]:
    """扫描所有 is_cross_seed=True 下载器, 返回每下载器的异常+统计."""
    svc = CrossSeedMonitorService()
    results = await svc.scan_all()
    return {
        "scanned_downloaders": len(results),
        "results": [r.to_dict() for r in results],
    }
