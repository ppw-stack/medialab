"""上传任务路由：CRUD + 启停 + 立即同步 + 运行态。

挂在 ``/api/upload-tasks``。目标目录浏览走 ``/targets``——按光鸭的
**fileId** 逐层（光鸭没有「路径 ↔ id」反查，与迁移页一致）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers.base import CloudFile, DriverError
from app.api.auth import require_session
from app.db.models import UploadTask as UploadTaskRow
from app.db.session import session_scope
from app.services import upload_tasks, watch_manager
from app.services.driver_factory import build_driver
from app.services.upload_paths import PathGuardError
from app.services.upload_tasks import UploadTaskError

router = APIRouter(
    prefix="/api/upload-tasks",
    tags=["upload-tasks"],
    dependencies=[Depends(require_session)],
)


async def _db() -> AsyncIterator[AsyncSession]:
    """DB session 依赖。"""
    async with session_scope() as db:
        yield db


class UploadTaskIn(BaseModel):
    """创建上传任务。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    #: 光鸭存储源 id
    driver_id: int
    #: 光鸭目标目录 fileId
    target_file_id: str = Field(default="", max_length=32)
    #: 监听目录（**完整路径**，面板上用目录浏览器选）
    watch_root: str = Field(..., min_length=1, max_length=1024)
    #: 相对监听根的子路径（"" = 整个根）
    #: 是否递归子目录
    include_subdirs: bool = True
    #: 是否启用
    enabled: bool = True
    #: 同步成功后清理本地文件（**破坏性**，默认关）
    delete_after_upload: bool = False
    #: 同时进行的上传数
    concurrency: int = Field(default=1, ge=1, le=8)
    #: 光鸭目标目录路径（只读展示）
    target_path: str = Field(default="", max_length=1024)


class UploadTaskPatch(BaseModel):
    """修改上传任务（只传要改的字段）。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    driver_id: int | None = None
    target_file_id: str | None = Field(default=None, max_length=32)
    watch_root: str | None = Field(default=None, min_length=1, max_length=1024)
    include_subdirs: bool | None = None
    enabled: bool | None = None
    delete_after_upload: bool | None = None
    concurrency: int | None = Field(default=None, ge=1, le=8)
    target_path: str | None = Field(default=None, max_length=1024)


class UploadTaskOut(BaseModel):
    """上传任务的对外形状。"""

    id: int
    name: str
    driver_id: int
    target_file_id: str
    target_path: str | None
    watch_root: str
    include_subdirs: bool
    enabled: bool
    delete_after_upload: bool
    concurrency: int
    created_at: int
    updated_at: int
    last_sync_at: int | None
    last_uploaded_count: int
    last_error: str | None
    #: 运行态（监听中？正在传什么？）
    listening: bool = False
    current: str = ""
    total_uploaded: int = 0
    total_failed: int = 0
    total_bytes: int = 0
    #: 运行态的最近一次错误（**监听启动失败等**）——面板据此提示
    runtime_error: str | None = None


def _to_out(row: UploadTaskRow) -> UploadTaskOut:
    """ORM → DTO（附带运行态）。"""
    runtime = upload_tasks.get_runtime(row.id)
    return UploadTaskOut(
        id=row.id,
        name=row.name,
        driver_id=row.driver_id,
        target_file_id=row.target_file_id,
        target_path=row.target_path,
        watch_root=row.watch_root,
        include_subdirs=row.include_subdirs,
        enabled=row.enabled,
        delete_after_upload=row.delete_after_upload,
        concurrency=row.concurrency,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_sync_at=row.last_sync_at,
        last_uploaded_count=row.last_uploaded_count,
        last_error=row.last_error,
        listening=runtime.listening,
        current=runtime.current,
        total_uploaded=runtime.total_uploaded,
        total_failed=runtime.total_failed,
        total_bytes=runtime.total_bytes,
        runtime_error=runtime.error,
    )


@router.get("", response_model=list[UploadTaskOut])
async def list_upload_tasks(db: AsyncSession = Depends(_db)) -> list[UploadTaskOut]:
    """列出所有上传任务。"""
    rows = await upload_tasks.list_tasks()
    return [_to_out(row) for row in rows]


@router.get("/local-dirs")
async def browse_local_dirs(path: str = "/") -> dict[str, Any]:
    """浏览**本地**目录（列子目录）——面板用来选监听目录。

    没有白名单：监听目录完全由用户在面板上配置。**因此这个端点
    能列出服务进程能看到的任何目录**——包括 ``/app``（含明文密钥的
    ``.env``）。故它与「面板登录」同级别地敏感（面板本身已是
    管理员权限）。
    """
    from app.services.upload_paths import list_subdirs

    target = Path(path or "/")
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{target}")
    return {
        "path": str(target),
        # ``id`` = 完整路径（前端拿它当「下一层路径」直接请求），
        # ``name`` = 目录名（只用于显示）。
        "dirs": [
            {"id": full, "name": full.rsplit("/", 1)[-1]}
            for full in list_subdirs(target)
        ],
    }


@router.get("/targets")
async def browse_targets(driver_id: int, parent_id: str = "") -> dict[str, Any]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。"""
    from app.services.migration import persist_guangya_credential

    async with session_scope() as db:
        from app.db.models import Driver

        driver_row = (
            await db.execute(
                select(Driver).where(Driver.id == driver_id)
            )
        ).scalar_one_or_none()
        if driver_row is None:
            raise HTTPException(status_code=404, detail=f"存储源不存在：{driver_id}")
        if driver_row.type != "guangya":
            raise HTTPException(
                status_code=400,
                detail=f"该接口需要光鸭存储源（当前：{driver_row.type}）",
            )
        credential = driver_row.credential
        device_type = driver_row.device_type
        row_id = driver_row.id

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(row_id, cred)

    driver = build_driver(
        "guangya", credential, device_type, on_token_refresh=_on_refresh
    )
    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        raise HTTPException(status_code=400, detail="该驱动不支持按 id 浏览目录")
    try:
        entries: list[CloudFile] = await lister(parent_id)
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": e.remote_id, "name": e.name} for e in entries if e.is_dir],
    }


@router.get("/{task_id}", response_model=UploadTaskOut)
async def get_upload_task(
    task_id: int, db: AsyncSession = Depends(_db)
) -> UploadTaskOut:
    """取单个任务。

    **必须有这个路由**：否则 ``GET /api/upload-tasks/1`` 会落到 SPA 的
    catch-all → 返回 index.html（HTTP 200 + HTML），调用方以为拿到了
    JSON，实际拿到的是 SPA 页面。
    """
    try:
        row = await upload_tasks.get_task(db, task_id)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_out(row)


@router.post("", response_model=UploadTaskOut, status_code=201)
async def create_upload_task(
    payload: UploadTaskIn, db: AsyncSession = Depends(_db)
) -> UploadTaskOut:
    """创建上传任务。

    创建后**立即启动监听**（若 ``enabled``）——不用重启服务。
    """
    try:
        row = await upload_tasks.create_task(
            db,
            name=payload.name,
            driver_id=payload.driver_id,
            target_file_id=payload.target_file_id,
            watch_root=payload.watch_root,
            include_subdirs=payload.include_subdirs,
            enabled=payload.enabled,
            delete_after_upload=payload.delete_after_upload,
            concurrency=payload.concurrency,
            target_path=payload.target_path,
        )
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    await _maybe_restart(row)
    return _to_out(row)


@router.patch("/{task_id}", response_model=UploadTaskOut)
async def update_upload_task(
    task_id: int, payload: UploadTaskPatch, db: AsyncSession = Depends(_db)
) -> UploadTaskOut:
    """修改上传任务（含启停）。

    **启停会重启监听器**——改了监听目录 / 目标目录必须重启才生效。
    """
    fields = payload.model_dump(exclude_unset=True)
    try:
        row = await upload_tasks.update_task(db, task_id, **fields)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    await _maybe_restart(row)
    return _to_out(row)


@router.delete("/{task_id}")
async def delete_upload_task(
    task_id: int, db: AsyncSession = Depends(_db)
) -> dict[str, bool]:
    """删除上传任务（先停监听）。"""
    try:
        await upload_tasks.delete_task(db, task_id)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/{task_id}/sync")
async def sync_now(task_id: int) -> dict[str, Any]:
    """立即跑一次全量同步（补偿事件流漏掉的文件）。"""
    try:
        return await watch_manager.sync_now(task_id)
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PathGuardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{task_id}/status")
async def task_status(task_id: int) -> dict[str, Any]:
    """任务的运行态 + 日志（前端轮询用）。"""
    runtime = upload_tasks.get_runtime(task_id)
    watcher = watch_manager.get_watcher(task_id)
    stats = watcher.stats.to_dict() if watcher is not None else {}
    return {**runtime.to_dict(), "watch": stats}


async def _maybe_restart(row: UploadTaskRow) -> None:
    """按任务的启停状态重启监听器。

    失败只记日志与运行态的 error——**不抛给用户**：任务配置已经存好了，
    只是监听没起来，用户可以在面板上看到错误再改。
    """
    await watch_manager.stop_watcher(row.id)
    runtime = upload_tasks.get_runtime(row.id)
    if not row.enabled:
        # 停用：清掉上次的错误（否则面板上会一直挂着旧错误）
        runtime.error = None
        runtime.listening = False
        return
    try:
        await watch_manager.start_watcher(
            task_id=row.id,
            driver_id=row.driver_id,
            target_file_id=row.target_file_id,
            watch_root=row.watch_root,
                include_subdirs=row.include_subdirs,
            concurrency=row.concurrency,
            name=row.name,
            delete_after_upload=row.delete_after_upload,
        )
    except Exception as exc:
        runtime.error = f"{type(exc).__name__}: {exc}"
        runtime.log(f"❌ 监听启动失败：{exc}")
        runtime.listening = False
