"""迁移路由：光鸭 → 光鸭 的一次性同步。

只有「手动触发」——不做定时调度（以后要加，只需存下入参 + 到点调
:func:`app.services.migration_jobs.start_migration_job`）。

目录浏览走两个端点：

- ``/sources``  —— 源目录树，按 **fileId** 逐层（路径改名会失效，id 不会）
- ``/targets``  —— 光鸭目录树，按 **fileId** 逐层

**路由层只做参数校验与响应组装**——DB 与适配层的访问全在
:mod:`app.services.migration`（分层约束见
``tests/unit/test_layering.py``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.services import migration_jobs
from app.services.migration import (
    MigrationBrowseError,
    browse_source_dirs,
    browse_target_dirs,
    validate_migration_drivers,
)
from app.services.migration_jobs import MigrationParams

router = APIRouter(
    prefix="/api/migrations",
    tags=["migrations"],
    dependencies=[Depends(require_session)],
)


class MigrationRunIn(BaseModel):
    """开跑参数。"""

    model_config = ConfigDict(extra="forbid")

    source_driver_id: int
    source_cid: str = Field(..., min_length=1, max_length=32)
    target_driver_id: int
    target_file_id: str = Field(default="", max_length=32)
    #: 迁移阈值（MB）——小于它的文件不进队列
    min_size_mb: int = Field(default=0, ge=0)
    #: 体积上限（MB）——0 = 不限
    max_size_mb: int = Field(default=0, ge=0)
    # 注：**没有并发参数**——迁移恒串行（2026-11-15 决策）。
    # 旧客户端若仍传 ``concurrency``，``extra="forbid"`` 会**显式拒绝**（422）
    # ——而不是静默忽略（静默忽略会让调用方以为并发生效了）。
    #: 两次提交给光鸭之间的最小间隔（秒）——错开提交，避开落盘名撞车
    submit_interval_sec: float = Field(default=60.0, ge=0.0, le=600.0)
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #: 搬完清理源：迁移成功后删除源端文件（**默认关**；仅光鸭源支持）
    delete_source: bool = False
    #: 只用于展示
    source_path: str = Field(default="", max_length=1024)
    target_path: str = Field(default="", max_length=1024)


@router.get("/sources")
async def browse_sources(driver_id: int, parent_id: str = "") -> dict[str, Any]:
    """列出源目录下的子目录（按 fileId 逐层浏览）。"""
    try:
        dirs = await browse_source_dirs(driver_id, parent_id)
    except MigrationBrowseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": rid, "name": name} for rid, name in dirs],
    }


@router.get("/targets")
async def browse_targets(driver_id: int, parent_id: str = "") -> dict[str, Any]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。"""
    try:
        dirs = await browse_target_dirs(driver_id, parent_id)
    except MigrationBrowseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": rid, "name": name} for rid, name in dirs],
    }


@router.post("/run", status_code=201)
async def run_migration(payload: MigrationRunIn) -> dict[str, Any]:
    """开跑（**立即返回 job_id**，迁移在后台跑）。

    同一时间只允许一个迁移任务——多个任务并发只会互相抢光鸭的下载配额。
    """
    try:
        await validate_migration_drivers(
            payload.source_driver_id, payload.target_driver_id
        )
    except MigrationBrowseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    running = await migration_jobs.find_running()
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已有迁移任务在跑（job={running.id}）",
        )

    params = MigrationParams(
        source_driver_id=payload.source_driver_id,
        source_cid=payload.source_cid,
        target_driver_id=payload.target_driver_id,
        target_file_id=payload.target_file_id,
        source_path=payload.source_path,
        target_path=payload.target_path,
        min_size_mb=payload.min_size_mb,
        max_size_mb=payload.max_size_mb,
        submit_interval_sec=payload.submit_interval_sec,
        exclude_disc=payload.exclude_disc,
        delete_source=payload.delete_source,
    )
    job = await migration_jobs.start_migration_job(params)
    return {"job_id": job.id, "state": job.state}


@router.get("/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    """最近的迁移任务（最新在前）——刷新页面可找回进度。"""
    jobs = await migration_jobs.list_jobs()
    return [j.to_dict() for j in jobs]


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """查询迁移进度（前端每 2s 拉一次）。"""
    job = await migration_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="迁移任务不存在")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """取消迁移（停止提交；已提交的离线任务留在光鸭继续跑）。"""
    ok = await migration_jobs.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}
