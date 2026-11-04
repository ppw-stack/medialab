"""离线下载路由（本地文件 → 光鸭，走光鸭服务端拉取）。

与 :mod:`app.api.routes.migrations` 同款结构：只有「手动触发」。

**与「数据同步」（``/upload-tasks``）的区别**：数据同步是**同步**语义
（逐文件直传，调用返回时文件已在云端）；这里是**异步**语义（提交离线
任务，后台跑，前端轮询）。两者**不共用代码路径**。

**与「迁移」（``/migrations``）的区别**：迁移的源是 115（换直链后交给光鸭
拉）；这里的源是**本地文件**（经本机的只读 HTTP 服务交给光鸭拉）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.core.config import get_settings
from app.services import offline_jobs
from app.services.offline_download import (
    OfflineParams,
)

router = APIRouter(
    prefix="/api/offline-downloads",
    tags=["offline-downloads"],
    dependencies=[Depends(require_session)],
)


class OfflineRunIn(BaseModel):
    """开跑参数。"""

    model_config = ConfigDict(extra="forbid")

    #: 目标光鸭 driver id
    target_driver_id: int
    #: 目标光鸭目录 fileId（空 = 根目录）
    target_file_id: str = Field(default="", max_length=32)
    #: 目标路径（只读展示）
    target_path: str = Field(default="", max_length=1024)
    #: 本地根目录（容器内绝对路径）
    local_root: str = Field(..., min_length=1, max_length=1024)
    #: 递归子目录
    include_subdirs: bool = True
    #: 体积下限（MB）
    min_size_mb: int = Field(default=0, ge=0)
    #: 体积上限（MB），0 = 不限
    max_size_mb: int = Field(default=0, ge=0)
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #: 同时进行的离线任务数
    concurrency: int = Field(default=2, ge=1, le=20)
    #: 两次提交之间的最小间隔（秒）
    submit_interval_sec: float = Field(default=2.0, ge=0.0, le=600.0)


@router.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    """离线下载的能力与配置（前端用它决定是否显示这个功能）。"""
    settings = get_settings()
    return {
        "enabled": bool(settings.offline_hmac_secret and settings.offline_public_base),
        "public_base": settings.offline_public_base,
        "files_root": settings.offline_files_root,
        "hint": (
            "离线下载需要：1) 配置 OFFLINE_HMAC_SECRET 与 OFFLINE_PUBLIC_BASE；"
            "2) 公网入口能访问到本服务（端口映射）。"
            "不需要时直接关掉端口映射即可——服务只在有请求时才暴露文件。"
        ),
    }


@router.get("/targets")
async def browse_targets(driver_id: int, parent_id: str = "") -> dict[str, Any]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。"""
    from app.services.migration import MigrationBrowseError, browse_target_dirs

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
async def run_offline(payload: OfflineRunIn) -> dict[str, Any]:
    """开跑（**立即返回 job_id**，下载在后台跑）。"""
    settings = get_settings()
    if not settings.offline_hmac_secret:
        raise HTTPException(
            status_code=503,
            detail="未配置 OFFLINE_HMAC_SECRET，离线下载未启用",
        )
    if not settings.offline_public_base:
        raise HTTPException(
            status_code=503,
            detail="未配置 OFFLINE_PUBLIC_BASE，无法生成公网 URL",
        )

    root = settings.offline_files_root
    if not root:
        raise HTTPException(
            status_code=503,
            detail="未配置 OFFLINE_FILES_ROOT，不知道要暴露哪个目录",
        )

    running = await offline_jobs.find_running()
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已有离线下载任务在跑（job={running.id}）",
        )

    params = OfflineParams(
        target_driver_id=payload.target_driver_id,
        target_file_id=payload.target_file_id,
        target_path=payload.target_path,
        local_root=payload.local_root,
        include_subdirs=payload.include_subdirs,
        min_size_mb=payload.min_size_mb,
        max_size_mb=payload.max_size_mb,
        exclude_disc=payload.exclude_disc,
        concurrency=payload.concurrency,
        submit_interval_sec=payload.submit_interval_sec,
        public_base=settings.offline_public_base,
        secret=settings.offline_hmac_secret,
    )
    job = await offline_jobs.start_offline_job(params)
    return {"job_id": job.id, "state": job.state}


@router.get("/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    """最近的离线下载任务（最新在前）——刷新页面可找回进度。"""
    jobs = await offline_jobs.list_jobs()
    return [j.to_dict() for j in jobs]


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """查询离线下载进度（前端每 2s 拉一次）。"""
    job = await offline_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="离线下载任务不存在")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """取消离线下载（停止提交；已提交的离线任务留在光鸭继续跑）。"""
    ok = await offline_jobs.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}
