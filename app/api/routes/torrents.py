"""辅种任务 API（FR-6.1）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.auth import require_session
from app.db.models import DownloaderInstance, Site, Torrent, TorrentStatus
from app.db.session import session_scope

router = APIRouter(prefix="/torrents", dependencies=[Depends(require_session)], tags=["torrents"])


class TorrentOut(BaseModel):
    id: int
    info_hash: str
    name: str
    site: str
    site_id: int
    downloader_id: int
    downloader_name: str
    category: str | None
    status: str
    source: str
    size: int
    downloaded: int
    uploaded: int
    ratio: float
    state: str | None
    is_free: bool
    free_type: str | None
    free_end_at: datetime | None
    added_at: datetime
    protect_until: datetime | None


@router.get("", summary="任务列表（多维筛选 + 服务端分页）")
async def list_torrents(
    site_id: int | None = Query(default=None, ge=1),
    downloader_id: int | None = Query(default=None, ge=1),
    source: str | None = Query(default=None, max_length=16),
    status: str | None = Query(default=None, max_length=24),
    q: str | None = Query(default=None, max_length=128),
    include_deleted: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """任务列表。

    过滤：site_id / downloader_id / source / status / q（搜索 name + info_hash）。
    分页：page（默认 1）、page_size（默认 50，上限 200）。
    include_deleted：默认 False 时排除 DELETED。
    """
    async with session_scope() as session:
        base = (
            select(Torrent, Site.name.label("site_name"), DownloaderInstance.name.label("dl_name"))
            .join(Site, Site.id == Torrent.site_id)
            .join(DownloaderInstance, DownloaderInstance.id == Torrent.downloader_id)
        )
        if site_id is not None:
            base = base.where(Torrent.site_id == site_id)
        if downloader_id is not None:
            base = base.where(Torrent.downloader_id == downloader_id)
        if source:
            base = base.where(Torrent.source == source)
        if status:
            base = base.where(Torrent.status == status)
        elif not include_deleted:
            base = base.where(Torrent.status != TorrentStatus.DELETED)
        if q:
            q_like = f"%{q}%"
            base = base.where(
                (Torrent.name.ilike(q_like)) | (Torrent.info_hash.ilike(q_like))
            )

        total = (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(Torrent.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_to_out(t, site_name, dl_name) for t, site_name, dl_name in rows],
    }


# ---------------------------------------------------------------- 批量操作（必须在 /{torrent_id} 路由之前注册）
class BatchOpResult(BaseModel):
    requested: int
    succeeded: int
    failed: int
    skipped: int = 0
    errors: list[str] = []


class BatchPauseRequest(BaseModel):
    hashes: list[str]
    paused: bool = True


class BatchRemoveRequest(BaseModel):
    hashes: list[str]
    delete_data: bool = False
    force: bool = False


@router.post("/batch/pause", summary="批量暂停/恢复")
async def batch_pause(payload: BatchPauseRequest) -> BatchOpResult:
    return await _batch_set_pause(payload.hashes, paused=payload.paused)


@router.post("/batch/remove", summary="批量删除")
async def batch_remove(payload: BatchRemoveRequest) -> BatchOpResult:
    return await _batch_remove(payload.hashes, delete_data=payload.delete_data, force=payload.force)


@router.get("/{torrent_id}", summary="任务详情")
async def get_torrent(torrent_id: int) -> TorrentOut:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Torrent, Site.name.label("site_name"), DownloaderInstance.name.label("dl_name"))
                .join(Site, Site.id == Torrent.site_id)
                .join(DownloaderInstance, DownloaderInstance.id == Torrent.downloader_id)
                .where(Torrent.id == torrent_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _to_out(row[0], row[1], row[2])


# ---------------------------------------------------------------- 单条操作
class SingleOpResult(BaseModel):
    ok: bool
    detail: str = ""


@router.post("/{torrent_id}/pause", summary="暂停单条任务")
async def pause_torrent(torrent_id: int) -> SingleOpResult:
    """调下载器 adapter.set_pause，回写本地 Torrents.status=paused。"""
    return await _set_pause_one(torrent_id, paused=True)


@router.post("/{torrent_id}/resume", summary="恢复单条任务")
async def resume_torrent(torrent_id: int) -> SingleOpResult:
    return await _set_pause_one(torrent_id, paused=False)


@router.post("/{torrent_id}/remove", summary="删除单条任务")
async def remove_torrent(torrent_id: int, delete_data: bool = False, force: bool = False) -> SingleOpResult:
    """删除任务。

    delete_data=False（默认）：仅从下载器移除任务，保留磁盘文件。
    delete_data=True：从下载器移除并删除磁盘数据（FR-5.2.5）。
    force=True：跳过 protect_until 保护期检查（默认 False）。

    FR-5.2.3：data deletion 强制二次确认（前端要求输入 torrent.name），
    后端仍校验路径必须在 download_root 内（FR-5.2.5c）。
    """
    return await _remove_one(torrent_id, delete_data=delete_data, force=force)


# ---------------------------------------------------------------- 内部实现
async def _set_pause_one(torrent_id: int, *, paused: bool) -> SingleOpResult:
    from app.services.factory import get_factory

    async with session_scope() as session:
        torrent = (
            await session.execute(select(Torrent).where(Torrent.id == torrent_id))
        ).scalar_one_or_none()
        if torrent is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if torrent.status == TorrentStatus.DELETED:
            raise HTTPException(status_code=400, detail="任务已删除")
        dl_row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == torrent.downloader_id)
            )
        ).scalar_one_or_none()
        if dl_row is None:
            raise HTTPException(status_code=500, detail="下载器记录缺失")

    factory = get_factory()
    adapter = factory.downloader(dl_row)
    try:
        ok = await adapter.set_pause([torrent.info_hash], paused)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"下载器操作失败：{exc}") from exc
    if not ok:
        raise HTTPException(status_code=502, detail="下载器返回失败")

    async with session_scope() as session:
        t2 = await session.execute(select(Torrent).where(Torrent.id == torrent_id))
        t2_row = t2.scalar_one()
        t2_row.status = TorrentStatus.PAUSED if paused else TorrentStatus.ACTIVE

    return SingleOpResult(ok=True, detail="paused" if paused else "resumed")


async def _remove_one(
    torrent_id: int, *, delete_data: bool, force: bool
) -> SingleOpResult:
    from app.core.timeutils import utcnow
    from app.services.factory import get_factory

    if delete_data and not force:
        # 安全：必须 force=true 才能删数据（前端二次确认弹窗确认后传 true）
        raise HTTPException(
            status_code=400,
            detail="删除数据需 force=true（前端二次确认）",
        )

    async with session_scope() as session:
        torrent = (
            await session.execute(select(Torrent).where(Torrent.id == torrent_id))
        ).scalar_one_or_none()
        if torrent is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if torrent.status == TorrentStatus.DELETED:
            raise HTTPException(status_code=400, detail="任务已删除")

        # FR-5.2.3：保护期检查
        if not force and torrent.protect_until and torrent.protect_until > utcnow():
            raise HTTPException(
                status_code=400,
                detail=f"保护期内（截止 {torrent.protect_until.isoformat()}），需 force=true",
            )

        dl_row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == torrent.downloader_id)
            )
        ).scalar_one_or_none()
        if dl_row is None:
            raise HTTPException(status_code=500, detail="下载器记录缺失")
        info_hash = torrent.info_hash

    factory = get_factory()
    adapter = factory.downloader(dl_row)
    try:
        ok = await adapter.remove([info_hash], delete_data=delete_data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"下载器删除失败：{exc}") from exc
    if not ok:
        raise HTTPException(status_code=502, detail="下载器返回失败")

    # 本地状态：标记 DELETED（不物理删除记录，cleanup job 后续清理）
    async with session_scope() as session:
        t2 = await session.execute(select(Torrent).where(Torrent.id == torrent_id))
        t2_row = t2.scalar_one()
        t2_row.status = TorrentStatus.DELETED

    return SingleOpResult(ok=True, detail="removed" + ("+data" if delete_data else ""))


async def _batch_set_pause(hashes: list[str], *, paused: bool) -> BatchOpResult:
    """批量操作：按 downloader_id 分组并发（≤10 并发）。"""
    from app.services.factory import get_factory

    if len(hashes) > 200:
        raise HTTPException(status_code=400, detail="批量操作上限 200 个 hash")

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Torrent, DownloaderInstance)
                    .join(DownloaderInstance, DownloaderInstance.id == Torrent.downloader_id)
                    .where(Torrent.info_hash.in_(hashes))
                )
            )
            .all()
        )

    # 按 downloader 分组
    by_dl: dict[int, list[str]] = {}
    torrent_map: dict[str, int] = {}
    for torrent, dl in rows:
        by_dl.setdefault(dl.id, []).append(torrent.info_hash)
        torrent_map[torrent.info_hash] = torrent.id

    factory = get_factory()
    succeeded = 0
    failed = 0
    errors: list[str] = []

    # 串行（每下载器内部串行），跨下载器并发由 FastAPI 异步事件循环处理
    for dl_id, dl_hashes in by_dl.items():
        async with session_scope() as session:
            dl_row = (
                await session.execute(
                    select(DownloaderInstance).where(DownloaderInstance.id == dl_id)
                )
            ).scalar_one_or_none()
        if dl_row is None:
            failed += len(dl_hashes)
            errors.append(f"下载器 {dl_id} 缺失")
            continue
        adapter = factory.downloader(dl_row)
        try:
            ok = await adapter.set_pause(dl_hashes, paused)
        except Exception as exc:
            failed += len(dl_hashes)
            errors.append(f"{dl_row.name}: {exc}")
            continue
        if not ok:
            failed += len(dl_hashes)
            errors.append(f"{dl_row.name} 返回失败")
            continue

        succeeded += len(dl_hashes)
        # 批量回写状态
        async with session_scope() as session:
            await session.execute(
                Torrent.__table__.update()
                .where(Torrent.info_hash.in_(dl_hashes))
                .values(status=TorrentStatus.PAUSED if paused else TorrentStatus.ACTIVE)
            )

    skipped = len(hashes) - len(torrent_map)
    return BatchOpResult(
        requested=len(hashes),
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        errors=errors[:10],
    )


async def _batch_remove(
    hashes: list[str], *, delete_data: bool, force: bool
) -> BatchOpResult:
    from app.core.timeutils import utcnow
    from app.services.factory import get_factory

    if len(hashes) > 200:
        raise HTTPException(status_code=400, detail="批量操作上限 200 个 hash")
    if delete_data and not force:
        raise HTTPException(
            status_code=400,
            detail="批量删除数据需 force=true（前端二次确认）",
        )

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Torrent, DownloaderInstance)
                    .join(DownloaderInstance, DownloaderInstance.id == Torrent.downloader_id)
                    .where(Torrent.info_hash.in_(hashes))
                )
            )
            .all()
        )

    now = utcnow()
    by_dl: dict[int, list[str]] = {}
    protect_blocked: list[str] = []
    torrent_hashes: set[str] = set()

    for torrent, dl in rows:
        torrent_hashes.add(torrent.info_hash)
        if not force and torrent.protect_until and torrent.protect_until > now:
            protect_blocked.append(torrent.info_hash)
            continue
        by_dl.setdefault(dl.id, []).append(torrent.info_hash)

    factory = get_factory()
    succeeded = 0
    failed = 0
    errors: list[str] = []

    for dl_id, dl_hashes in by_dl.items():
        async with session_scope() as session:
            dl_row = (
                await session.execute(
                    select(DownloaderInstance).where(DownloaderInstance.id == dl_id)
                )
            ).scalar_one_or_none()
        if dl_row is None:
            failed += len(dl_hashes)
            errors.append(f"下载器 {dl_id} 缺失")
            continue
        adapter = factory.downloader(dl_row)
        try:
            ok = await adapter.remove(dl_hashes, delete_data=delete_data)
        except Exception as exc:
            failed += len(dl_hashes)
            errors.append(f"{dl_row.name}: {exc}")
            continue
        if not ok:
            failed += len(dl_hashes)
            errors.append(f"{dl_row.name} 返回失败")
            continue

        succeeded += len(dl_hashes)
        async with session_scope() as session:
            await session.execute(
                Torrent.__table__.update()
                .where(Torrent.info_hash.in_(dl_hashes))
                .values(status=TorrentStatus.DELETED)
            )

    skipped = len(hashes) - len(torrent_hashes) + len(protect_blocked)
    return BatchOpResult(
        requested=len(hashes),
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        errors=errors[:10],
    )


def _to_out(t: Torrent, site_name: str, dl_name: str) -> TorrentOut:
    return TorrentOut(
        id=t.id,
        info_hash=t.info_hash,
        name=t.name,
        site=site_name,
        site_id=t.site_id,
        downloader_id=t.downloader_id,
        downloader_name=dl_name,
        category=t.category,
        status=t.status,
        source=t.source,
        size=t.size,
        downloaded=t.downloaded,
        uploaded=t.uploaded,
        ratio=t.ratio,
        state=t.state,
        is_free=t.is_free,
        free_type=t.free_type,
        free_end_at=t.free_end_at,
        added_at=t.added_at,
        protect_until=t.protect_until,
    )