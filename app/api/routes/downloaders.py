"""下载器管理 API（FR-1.1）。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import (
    DownloaderError,
    TorrentFilter,
    TorrentState,
)
from app.api.auth import require_session
from app.core.logging import get_logger
from app.db.models import DownloaderInstance, PushStatus, TorrentIndex
from app.db.session import session_scope
from app.services.factory import get_factory

logger = get_logger(__name__)

router = APIRouter(
    prefix="/downloaders",
    dependencies=[Depends(require_session)],
    tags=["downloaders"],
)

#: 已实现的下载器类型（M1+M2 范围）。其他类型后续按 FR-1 补齐。
SUPPORTED_TYPES: tuple[str, ...] = ("qbittorrent", "transmission")
ALL_TYPES: tuple[str, ...] = ("qbittorrent", "transmission", "deluge", "aria2")

#: BTIH infohash：40 位 hex。拒绝 ``all`` / ``|`` / 任意非 hash 值——
#: qBittorrent 会把 ``all`` 解析为"应用到所有种子"，可一次删光全部任务及数据。
_VALID_HASH_RE: re.Pattern[str] = re.compile(r"^[a-fA-F0-9]{40}$")


def _reject_invalid_hashes(hashes: list[str]) -> None:
    """校验批量操作的 hash 白名单（FR-5.2 路径三重校验同级约束）。"""
    bad = [h for h in hashes if not _VALID_HASH_RE.fullmatch(h)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"非法 infohash：{bad[:5]}（必须是 40 位 hex，不支持 all/通配）",
        )


class DownloaderIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    type: str = Field(..., description="qbittorrent/transmission/deluge/aria2")
    url: str = Field(..., min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    save_path: str | None = Field(default=None, max_length=512)
    is_default: bool = False
    enabled: bool = True
    verify_tls: bool = True
    is_cross_seed: bool = False


class DownloaderUpdate(BaseModel):
    id: int
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    url: str | None = Field(default=None, min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    save_path: str | None = Field(default=None, max_length=512)
    is_default: bool | None = None
    enabled: bool | None = None
    verify_tls: bool | None = None
    is_cross_seed: bool | None = None


class DownloaderOut(BaseModel):
    id: int
    name: str
    type: str
    url: str
    username: str | None
    has_password: bool
    save_path: str | None
    is_default: bool
    enabled: bool
    verify_tls: bool
    is_cross_seed: bool
    created_at: str
    updated_at: str


def _to_out(row: DownloaderInstance) -> DownloaderOut:
    return DownloaderOut(
        id=row.id,
        name=row.name,
        type=row.type,
        url=row.url,
        username=row.username,
        has_password=bool(row.password),
        save_path=row.save_path,
        is_default=row.is_default,
        enabled=row.enabled,
        verify_tls=row.verify_tls,
        is_cross_seed=bool(getattr(row, "is_cross_seed", False)),
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
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
            password=payload.password,
            save_path=payload.save_path or None,
            is_default=payload.is_default,
            enabled=payload.enabled,
            verify_tls=payload.verify_tls,
        )
        session.add(row)
        await session.flush()
        if payload.is_default:
            await _apply_default(session, row.id)
        return _to_out(row)


@router.post("/update", summary="更新下载器")
async def update_downloader(payload: DownloaderUpdate) -> DownloaderOut:
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
            # 空串归一为 None（save_path 等可选字段，前端清空即取消）
            if key == "save_path" and not value:
                value = None
            setattr(row, key, value)
        # 用 model_fields_set 区分"未提供"与"显式清空"：空串允许清掉密码
        if "password" in payload.model_fields_set:
            row.password = payload.password or None
        if payload.is_default:
            await _apply_default(session, row.id)
    # 更新后失效缓存：否则 cron/推送仍用旧凭据（旧 URL/密码继续生效）
    get_factory().invalidate_downloader(payload.id)
    return _to_out(row)


class DownloaderDeleteIn(BaseModel):
    id: int = Field(..., ge=1)


@router.post("/delete", summary="删除下载器")
async def delete_downloader(payload: DownloaderDeleteIn) -> dict[str, int]:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(
                    DownloaderInstance.id == payload.id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        # 检查引用：有推送过的种子则拒绝物理删除
        ref_count = (
            await session.execute(
                select(func.count())
                .select_from(TorrentIndex)
                .where(TorrentIndex.push_status == PushStatus.PUSHED)
            )
        ).scalar_one()
        if ref_count:
            raise HTTPException(
                status_code=409,
                detail=f"下载器下还有 {ref_count} 条任务，请先清理或迁移后再删除",
            )
        await session.execute(
            delete(DownloaderInstance).where(DownloaderInstance.id == payload.id)
        )
    get_factory().invalidate_downloader(payload.id)
    return {"id": payload.id}


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
        return {
            "reachable": reachable,
            "detail": "ok" if reachable else "unreachable",
        }
    except DownloaderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ================================================================ 下载器原生任务
class DownloaderTorrentOut(BaseModel):
    """下载器原生任务视图（来自下载器 adapter.list_torrents）。"""

    hash: str
    name: str
    state: str
    progress: float
    size: int
    downloaded: int
    uploaded: int
    dlspeed: int
    upspeed: int
    ratio: float
    save_path: str | None = None
    category: str | None = None
    tags: list[str] = []


class DownloaderBatchOpRequest(BaseModel):
    hashes: list[str] = Field(..., min_length=1)
    delete_data: bool = False
    paused: bool = True


class DownloaderBatchOpResult(BaseModel):
    requested: int
    succeeded: int
    failed: int
    errors: list[str] = []


def _to_downloader_torrent_out(t: Any) -> DownloaderTorrentOut:
    return DownloaderTorrentOut(
        hash=t.hash,
        name=t.name,
        state=t.state.name if isinstance(t.state, TorrentState) else str(t.state),
        progress=t.progress,
        size=t.size,
        downloaded=t.downloaded,
        uploaded=t.uploaded,
        dlspeed=t.dlspeed,
        upspeed=t.upspeed,
        ratio=t.ratio,
        save_path=t.save_path,
        category=t.category,
        tags=list(t.tags),
    )


async def _resolve_downloader(downloader_id: int) -> tuple[DownloaderInstance, Any]:
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
    return row, adapter


@router.get("/{downloader_id}/torrents", summary="列出下载器原生任务")
async def list_downloader_torrents(
    downloader_id: int,
    state: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """调 downloader.adapter.list_torrents() 返回原生任务视图。

    过滤：state（downloading/uploading/paused/error 等）/ category。
    """
    _, adapter = await _resolve_downloader(downloader_id)

    states: frozenset[TorrentState] = frozenset()
    if state:
        try:
            states = frozenset({TorrentState(state)})
        except ValueError:
            states = frozenset()

    tags: frozenset[str] = frozenset()
    try:
        items = await adapter.list_torrents(
            TorrentFilter(states=states, category=category, tags=tags)
        )
    except DownloaderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "total": len(items),
        "items": [_to_downloader_torrent_out(t) for t in items],
    }


@router.post("/{downloader_id}/torrents/pause", summary="暂停/恢复下载器原生任务")
async def pause_downloader_torrents(
    downloader_id: int, payload: DownloaderBatchOpRequest
) -> DownloaderBatchOpResult:
    if len(payload.hashes) > 200:
        raise HTTPException(status_code=400, detail="批量操作上限 200")
    _reject_invalid_hashes(payload.hashes)
    _, adapter = await _resolve_downloader(downloader_id)
    try:
        ok = await adapter.set_pause(payload.hashes, payload.paused)
    except DownloaderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return DownloaderBatchOpResult(
        requested=len(payload.hashes),
        succeeded=len(payload.hashes) if ok else 0,
        failed=0 if ok else len(payload.hashes),
    )


@router.post("/{downloader_id}/torrents/remove", summary="删除下载器原生任务")
async def remove_downloader_torrents(
    downloader_id: int, payload: DownloaderBatchOpRequest
) -> DownloaderBatchOpResult:
    if len(payload.hashes) > 200:
        raise HTTPException(status_code=400, detail="批量操作上限 200")
    _reject_invalid_hashes(payload.hashes)
    _, adapter = await _resolve_downloader(downloader_id)
    # 删数据为不可恢复操作：记审计日志（前端已弹窗二次确认）
    if payload.delete_data:
        logger.warning(
            "删除下载器原生任务并删除磁盘数据：downloader_id=%d 数量=%d",
            downloader_id, len(payload.hashes),
        )
    try:
        ok = await adapter.remove(payload.hashes, delete_data=payload.delete_data)
    except DownloaderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return DownloaderBatchOpResult(
        requested=len(payload.hashes),
        succeeded=len(payload.hashes) if ok else 0,
        failed=0 if ok else len(payload.hashes),
    )
