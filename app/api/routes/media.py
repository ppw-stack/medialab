"""媒体库路由：条目查询 + 批量操作 + 密钥轮换。

**路由层只做参数校验与响应组装**——DB 访问全在
:mod:`app.services.media`（:func:`list_media` / :func:`media_stats` /
:func:`resign_media` / :func:`rotate_signing_secret`）。

分层约束（``api`` 不依赖 ``db``）由
``tests/unit/test_layering.py`` 固化。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.services.media import (
    MediaServiceError,
    list_media,
    media_stats,
    resign_media,
    rotate_signing_secret,
)

router = APIRouter(
    prefix="/api/media", tags=["media"], dependencies=[Depends(require_session)]
)


class MediaOut(BaseModel):
    id: int
    driver_id: int
    remote_id: str
    path: str
    name: str
    size: int
    strm_path: str | None
    #: 当前中转 URL（前端可直接预览 / 复制）
    strm_url: str


class MediaListOut(BaseModel):
    total: int
    items: list[MediaOut]


class ResignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_ids: list[int] = Field(..., min_length=1)


class RotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 新密钥；留空则自动生成
    new_secret: str | None = None


@router.get("", response_model=MediaListOut)
async def list_media_route(
    q: str | None = None,
    driver_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> MediaListOut:
    """列出媒体条目。"""
    page = await list_media(q=q, driver_id=driver_id, limit=limit, offset=offset)
    return MediaListOut(
        total=page.total,
        items=[
            MediaOut(
                id=r.id,
                driver_id=r.driver_id,
                remote_id=r.remote_id,
                path=r.path,
                name=r.name,
                size=r.size,
                strm_path=r.strm_path,
                strm_url=r.strm_url,
            )
            for r in page.items
        ],
    )


@router.post("/resign")
async def resign(payload: ResignRequest) -> dict[str, int]:
    """批量重签（用当前密钥）并重写 STRM 文件。"""
    try:
        ok = await resign_media(payload.media_ids)
    except MediaServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"resigned": ok}


@router.post("/rotate-secret")
async def rotate(payload: RotateRequest) -> dict[str, object]:
    """轮换签名密钥。

    **新密钥只在响应里返回一次**——调用方须立刻写进环境变量并重启。
    """
    try:
        result = await rotate_signing_secret(payload.new_secret)
    except MediaServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return result.to_dict(include_secret=True)


@router.get("/stats")
async def stats() -> dict[str, object]:
    """媒体库统计（仪表盘用）。"""
    s = await media_stats()
    return {
        "total": s.total,
        "total_size": s.total_size,
        "per_driver": [{"driver_id": d, "count": c} for d, c in s.per_driver],
        "server_time": s.server_time,
    }
