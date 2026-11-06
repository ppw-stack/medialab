"""上传任务路由：CRUD + 启停 + 立即同步 + 运行态。

挂在 ``/api/upload-tasks``。目标目录浏览走 ``/targets``——按光鸭的
**fileId** 逐层（光鸭没有「路径 ↔ id」反查，与迁移页一致）。

**路由层只做参数校验与响应组装**——DB 与适配层的访问全在
:mod:`app.services.upload_tasks`（分层约束见
``tests/unit/test_layering.py``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.domain.upload_types import DEFAULT_UPLOAD_CONCURRENCY
from app.services import upload_tasks, watch_manager
from app.services.upload_tasks import UploadTaskError, UploadTaskView

router = APIRouter(
    prefix="/api/upload-tasks",
    tags=["upload-tasks"],
    dependencies=[Depends(require_session)],
)


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
    #: 是否递归子目录
    include_subdirs: bool = True
    #: 是否启用
    enabled: bool = True
    #: 同步成功后清理本地文件（**破坏性**，默认关）
    delete_after_upload: bool = False
    #: 小文件通道的并发数（大文件通道**恒串行**——光鸭上传只能串行，
    #: 实测见 docs/upload-bandwidth.md；<1MB 单 PUT 是唯一支持并发的路径）
    concurrency: int = Field(
        default=DEFAULT_UPLOAD_CONCURRENCY, ge=1, le=8
    )
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
    created_at: str
    updated_at: str
    #: 运行态（监听中？正在传什么？）
    listening: bool = False
    current: str = ""
    #: 小文件通道正在上传的文件
    current_small: str = ""
    total_uploaded: int = 0
    total_failed: int = 0
    total_bytes: int = 0
    #: 真正被丢弃的文件数（队列与溢出暂存都满时；>0 应检查面板日志）
    dropped: int = 0
    #: 运行态的最近一次错误（**监听启动失败等**）——面板据此提示
    runtime_error: str | None = None


def _to_out(row: UploadTaskView) -> UploadTaskOut:
    """service 层 dataclass → 输出模型。"""
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
        listening=row.listening,
        current=row.current,
        current_small=row.current_small,
        total_uploaded=row.total_uploaded,
        total_failed=row.total_failed,
        total_bytes=row.total_bytes,
        dropped=row.dropped,
        runtime_error=row.runtime_error,
    )


@router.get("", response_model=list[UploadTaskOut])
async def list_upload_tasks() -> list[UploadTaskOut]:
    """列出所有上传任务。"""
    return [_to_out(row) for row in await upload_tasks.list_task_views()]


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
    """列出**目标网盘**目录下的子目录（按 id 逐层浏览）。"""
    try:
        dirs = await upload_tasks.browse_target_dirs(driver_id, parent_id)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": rid, "name": name} for rid, name in dirs],
    }


@router.get("/capabilities")
async def upload_capabilities() -> dict[str, Any]:
    """本环境**支持上传的存储源类型**。

    前端用它过滤「目标网盘」下拉——只列支持上传的。
    **加新网盘时前端零改动**：后端在
    ``app/adapters/drivers/upload_base.py`` 的
    ``UPLOADABLE_DRIVER_TYPES`` 里登记即可。
    """
    return await upload_tasks.upload_capabilities()


@router.get("/{task_id}", response_model=UploadTaskOut)
async def get_upload_task(task_id: int) -> UploadTaskOut:
    """取单个任务。

    **必须有这个路由**：否则 ``GET /api/upload-tasks/1`` 会落到 SPA 的
    catch-all → 返回 index.html（HTTP 200 + HTML），调用方以为拿到了
    JSON，实际拿到的是 SPA 页面。
    """
    try:
        row = await upload_tasks.get_task_view(task_id)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_out(row)


@router.post("", response_model=UploadTaskOut, status_code=201)
async def create_upload_task(payload: UploadTaskIn) -> UploadTaskOut:
    """创建上传任务。

    创建后**立即启动监听**（若 ``enabled``）——不用重启服务。
    """
    try:
        row = await upload_tasks.create_task_view(
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
async def update_upload_task(task_id: int, payload: UploadTaskPatch) -> UploadTaskOut:
    """修改上传任务（含启停）。

    **启停会重启监听器**——改了监听目录 / 目标目录必须重启才生效。
    """
    fields = payload.model_dump(exclude_unset=True)
    try:
        row = await upload_tasks.update_task_view(task_id, **fields)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    await _maybe_restart(row)
    return _to_out(row)


@router.delete("/{task_id}")
async def delete_upload_task(task_id: int) -> dict[str, bool]:
    """删除上传任务（先停监听）。"""
    try:
        await upload_tasks.delete_task_view(task_id)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/{task_id}/sync")
async def sync_now(task_id: int) -> dict[str, Any]:
    """立即跑一次全量同步（补偿事件流漏掉的文件）。"""
    try:
        return await upload_tasks.sync_task_now(task_id)
    except UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{task_id}/status")
async def task_status(task_id: int) -> dict[str, Any]:
    """任务的运行态 + 日志（前端轮询用）。"""
    runtime = upload_tasks.get_runtime(task_id)
    watcher = watch_manager.get_watcher(task_id)
    stats = watcher.stats.to_dict() if watcher is not None else {}
    return {**runtime.to_dict(), "watch": stats}


async def _maybe_restart(row: UploadTaskView) -> None:
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
