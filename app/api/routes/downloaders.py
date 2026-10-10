"""下载器管理 API（FR-1.1）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import DownloaderError
from app.api.auth import require_session
from app.core.config import get_settings
from app.core.crypto import build_cipher
from app.db.models import DownloaderInstance
from app.db.session import session_scope
from app.services.factory import get_factory

router = APIRouter(
    prefix="/downloaders",
    dependencies=[Depends(require_session)],
    tags=["downloaders"],
)

#: 已实现的下载器类型（M1+M2 范围）。其他类型后续按 FR-1 补齐。
SUPPORTED_TYPES: tuple[str, ...] = ("qbittorrent",)
ALL_TYPES: tuple[str, ...] = ("qbittorrent", "transmission", "deluge", "aria2")


class DownloaderIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    type: str = Field(..., description="qbittorrent/transmission/deluge/aria2")
    url: str = Field(..., min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    download_root: str | None = Field(default=None, max_length=512)
    is_default: bool = False
    enabled: bool = True
    verify_tls: bool = True


class DownloaderUpdate(BaseModel):
    id: int
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    url: str | None = Field(default=None, min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    download_root: str | None = Field(default=None, max_length=512)
    is_default: bool | None = None
    enabled: bool | None = None
    verify_tls: bool | None = None


class DownloaderOut(BaseModel):
    id: int
    name: str
    type: str
    url: str
    username: str | None
    has_password: bool
    download_root: str | None
    is_default: bool
    enabled: bool
    verify_tls: bool
    created_at: str
    updated_at: str


def _to_out(row: DownloaderInstance) -> DownloaderOut:
    return DownloaderOut(
        id=row.id,
        name=row.name,
        type=row.type,
        url=row.url,
        username=row.username,
        has_password=bool(row.password_enc),
        download_root=row.download_root,
        is_default=row.is_default,
        enabled=row.enabled,
        verify_tls=row.verify_tls,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


async def _apply_default(session: AsyncSession, target_id: int) -> None:
    """确保只有一个 is_default=True（将其他全部置 False）。"""
    rows = (
        await session.execute(
            select(DownloaderInstance).where(
                DownloaderInstance.id != target_id,
                DownloaderInstance.is_default.is_(True),
            )
        )
    ).scalars().all()
    for r in rows:
        r.is_default = False


@router.get("", summary="下载器列表")
async def list_downloaders() -> list[DownloaderOut]:
    async with session_scope() as session:
        rows = (await session.execute(select(DownloaderInstance))).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/{downloader_id}", summary="下载器详情")
async def get_downloader(downloader_id: int) -> DownloaderOut:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        return _to_out(row)


@router.post("", status_code=201, summary="新增下载器")
async def create_downloader(payload: DownloaderIn) -> DownloaderOut:
    if payload.type not in ALL_TYPES:
        raise HTTPException(status_code=400, detail=f"未知下载器类型：{payload.type}")
    if payload.type not in SUPPORTED_TYPES:
        raise HTTPException(
            status_code=501, detail=f"下载器类型 {payload.type} 尚未实现"
        )
    cipher = build_cipher(get_settings())
    async with session_scope() as session:
        exists = (
            await session.execute(
                select(DownloaderInstance.id).where(DownloaderInstance.name == payload.name)
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail=f"下载器 {payload.name} 已存在")
        row = DownloaderInstance(
            name=payload.name,
            type=payload.type,
            url=payload.url,
            username=payload.username,
            download_root=payload.download_root,
            is_default=payload.is_default,
            enabled=payload.enabled,
            verify_tls=payload.verify_tls,
        )
        if payload.password:
            row.password_enc = cipher.encrypt(
                payload.password, owner_type="downloader", owner_id=payload.name
            )
        session.add(row)
        await session.flush()
        if payload.is_default:
            await _apply_default(session, row.id)
        return _to_out(row)


@router.post("/update", summary="更新下载器")
async def update_downloader(payload: DownloaderUpdate) -> DownloaderOut:
    cipher = build_cipher(get_settings())
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == payload.id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        if payload.name and payload.name != row.name:
            if (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.name == payload.name
                    )
                )
            ).first():
                raise HTTPException(
                    status_code=409, detail=f"下载器 {payload.name} 已存在"
                )
            row.name = payload.name
        for key, value in payload.model_dump(
            exclude={"id", "password", "name"}, exclude_unset=True
        ).items():
            setattr(row, key, value)
        if payload.password:
            row.password_enc = cipher.encrypt(
                payload.password, owner_type="downloader", owner_id=row.name
            )
        if payload.is_default:
            await _apply_default(session, row.id)
        return _to_out(row)


@router.post("/delete", summary="删除下载器")
async def delete_downloader(payload: dict) -> dict[str, int]:
    downloader_id = payload.get("id") if isinstance(payload, dict) else None
    if downloader_id is None:
        raise HTTPException(status_code=400, detail="id 必填")
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(
                    DownloaderInstance.id == downloader_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        await session.delete(row)
    return {"id": downloader_id}


@router.post("/{downloader_id}/test", summary="测连（验证 URL + 凭据）")
async def test_downloader(downloader_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        if not row.enabled:
            raise HTTPException(status_code=400, detail="下载器已禁用")
        adapter = get_factory().downloader(row)
    try:
        reachable = await adapter.ping()
        caps = [str(f) for f in adapter.capabilities().features]
        return {
            "reachable": reachable,
            "detail": "ok" if reachable else "unreachable",
            "capabilities": caps,
            "supports_data_deletion": adapter.can_delete_data,
        }
    except DownloaderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
