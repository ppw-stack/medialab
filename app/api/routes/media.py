"""媒体库路由：条目查询 + 批量操作 + 密钥轮换。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.auth import require_session
from app.core.config import get_settings
from app.db.models import MediaFile
from app.db.session import session_scope
from app.services.media import MediaServiceError
from app.services.rotation import resign_single, rotate_secret
from app.services.signing import build_redirect_url

router = APIRouter(
    prefix="/api/media", tags=["media"], dependencies=[Depends(require_session)]
)


async def _db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as db:
        yield db


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
async def list_media(
    db: AsyncSession = Depends(_db),
    q: str | None = None,
    driver_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> MediaListOut:
    """列出媒体条目。"""
    conditions: list[ColumnElement[bool]] = []
    if q:
        conditions.append(MediaFile.name.like(f"%{q}%"))
    if driver_id is not None:
        conditions.append(MediaFile.driver_id == driver_id)

    total = (
        await db.execute(select(func.count()).select_from(MediaFile).where(*conditions))
    ).scalar_one()
    rows = (
        await db.execute(
            select(MediaFile)
            .where(*conditions)
            .order_by(MediaFile.path)
            .limit(min(limit, 500))
            .offset(offset)
        )
    ).scalars().all()

    settings = get_settings()
    secret = settings.signing_secret
    base_url = settings.resolved_public_base_url
    items = [
        MediaOut(
            id=r.id,
            driver_id=r.driver_id,
            remote_id=r.remote_id,
            path=r.path,
            name=r.name,
            size=r.size,
            strm_path=r.strm_path,
            strm_url=(
                build_redirect_url(base_url, r.id, secret.get_secret_value())
                if secret and base_url
                else ""
            ),
        )
        for r in rows
    ]
    return MediaListOut(total=total, items=items)


@router.post("/resign")
async def resign(
    payload: ResignRequest, db: AsyncSession = Depends(_db)
) -> dict[str, int]:
    """批量重签（用当前密钥）并重写 STRM 文件。"""
    settings = get_settings()
    ok = 0
    for media_id in payload.media_ids:
        try:
            if await resign_single(db, settings, media_id):
                ok += 1
        except MediaServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"resigned": ok}


@router.post("/rotate-secret")
async def rotate(
    payload: RotateRequest, db: AsyncSession = Depends(_db)
) -> dict[str, object]:
    """轮换签名密钥。

    **新密钥只在响应里返回一次**——调用方须立刻写进环境变量并重启。
    """
    settings = get_settings()
    try:
        result = await rotate_secret(db, settings, payload.new_secret)
    except MediaServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return result.to_dict(include_secret=True)


@router.get("/stats")
async def stats(db: AsyncSession = Depends(_db)) -> dict[str, object]:
    """媒体库统计（仪表盘用）。"""
    total = (await db.execute(select(func.count()).select_from(MediaFile))).scalar_one()
    total_size = (
        await db.execute(select(func.coalesce(func.sum(MediaFile.size), 0)))
    ).scalar_one()
    per_driver = (
        await db.execute(
            select(MediaFile.driver_id, func.count())
            .group_by(MediaFile.driver_id)
            .order_by(MediaFile.driver_id)
        )
    ).all()
    return {
        "total": total,
        "total_size": int(total_size or 0),
        "per_driver": [{"driver_id": d, "count": c} for d, c in per_driver],
        "server_time": int(time.time()),
    }
