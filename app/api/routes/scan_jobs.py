"""扫描任务路由：轮询进度 + 取消。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_session
from app.services.scan_jobs import cancel_job, get_job, list_jobs

router = APIRouter(
    prefix="/api/scan-jobs",
    tags=["scan-jobs"],
    dependencies=[Depends(require_session)],
)


@router.get("/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """查询扫描任务进度。

    前端每 2s 拉一次，画进度条 + 实时日志。
    """
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    return job.to_dict()


@router.get("")
async def recent_jobs(driver_id: int | None = None) -> list[dict[str, Any]]:
    """最近的任务（最新在前）——前端刷新后可找回进度。"""
    jobs = await list_jobs(driver_id)
    return [j.to_dict() for j in jobs]


@router.post("/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """取消扫描任务。"""
    ok = await cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}
