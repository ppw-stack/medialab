"""迁移路由：115 → 光鸭 的一次性同步。

只有「手动触发」——不做定时调度（以后要加，只需存下入参 + 到点调
:func:`app.services.migration_jobs.start_migration_job`）。

目录浏览走两个端点：

- ``/sources``  —— 115 目录树，按 **cid** 逐层（路径改名会失效，cid 不会）
- ``/targets``  —— 光鸭目录树，按 **fileId** 逐层
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers.base import CloudFile, DriverError
from app.api.auth import require_session
from app.db.models import Driver
from app.db.session import session_scope
from app.services import migration_jobs
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from app.services.migration_jobs import MigrationParams

router = APIRouter(
    prefix="/api/migrations",
    tags=["migrations"],
    dependencies=[Depends(require_session)],
)


async def _db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as db:
        yield db


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
    #: 光鸭上同时进行的离线任务数
    concurrency: int = Field(default=2, ge=1, le=20)
    #: 两次提交给光鸭之间的最小间隔（秒）——错开提交，避开 115 下载频控
    submit_interval_sec: float = Field(default=60.0, ge=0.0, le=600.0)
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #: 只用于展示
    source_path: str = Field(default="", max_length=1024)
    target_path: str = Field(default="", max_length=1024)


def _dirs(entries: list[CloudFile]) -> list[dict[str, str]]:
    return [{"id": e.remote_id, "name": e.name} for e in entries if e.is_dir]


async def _list_by_cid(driver: Any, cid: str) -> list[CloudFile]:
    """按 id 列直接子项。

    ``list_dir_by_cid`` 不在 :class:`CloudDriver` 协议里（115 / 光鸭都有，
    但协议只要求 ``list_dir``），故用 ``getattr`` 取——顺带给出友好报错。
    """
    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        raise HTTPException(
            status_code=400, detail="该存储源驱动不支持按 id 浏览目录"
        )
    entries: list[CloudFile] = await lister(cid)
    return entries


async def _load_driver(
    db: AsyncSession, driver_id: int, expected_type: str
) -> Driver:
    row = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"存储源不存在：{driver_id}")
    if row.type != expected_type:
        raise HTTPException(
            status_code=400,
            detail=f"该接口需要 {expected_type} 存储源（当前：{row.type}）",
        )
    return row


def _default_source_cid(row: Driver) -> str:
    """115 浏览的兜底 cid —— **必须用媒体库 cid**。

    不能用 ``"0"``：那是**115 账号根目录**，实测会列出「手机相册 / 资料 /
    密钥 / 备份」等私人目录（实测输出见下方注释）。存储源没配媒体库 cid 时
    应报错，而不是把账号根目录暴露给迁移页面——用户可能误把私人目录当
    媒体库迁走。

    实测 ``parent_id=0`` 的返回::

        影视 / 手机相册 / 资料 / 密钥 / 备份 / …
    """
    cid = (row.media_cid or "").strip()
    if not cid:
        raise HTTPException(
            status_code=400,
            detail="该存储源未配置媒体库 cid，请先在「存储源」里配置后再浏览目录",
        )
    return cid


@router.get("/sources")
async def browse_sources(
    driver_id: int,
    parent_id: str = "",
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    """列出 115 目录下的子目录（按 cid 逐层浏览）。"""
    row = await _load_driver(db, driver_id, "p115")
    driver = build_driver(row.type, row.credential, row.device_type)
    try:
        entries = await _list_by_cid(driver, parent_id or _default_source_cid(row))
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"driver_id": driver_id, "parent_id": parent_id, "dirs": _dirs(entries)}


@router.get("/targets")
async def browse_targets(
    driver_id: int,
    parent_id: str = "",
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。"""
    row = await _load_driver(db, driver_id, "guangya")

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(row.id, cred)

    driver = build_driver(
        row.type, row.credential, row.device_type, on_token_refresh=_on_refresh
    )
    try:
        entries = await _list_by_cid(driver, parent_id or "")
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"driver_id": driver_id, "parent_id": parent_id, "dirs": _dirs(entries)}


@router.post("/run", status_code=201)
async def run_migration(
    payload: MigrationRunIn, db: AsyncSession = Depends(_db)
) -> dict[str, Any]:
    """开跑（**立即返回 job_id**，迁移在后台跑）。

    同一时间只允许一个迁移任务——多个任务并发只会互相抢光鸭的下载配额。
    """
    await _load_driver(db, payload.source_driver_id, "p115")
    await _load_driver(db, payload.target_driver_id, "guangya")

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
        concurrency=payload.concurrency,
        submit_interval_sec=payload.submit_interval_sec,
        exclude_disc=payload.exclude_disc,
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
