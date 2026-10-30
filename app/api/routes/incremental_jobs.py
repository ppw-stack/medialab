"""增量任务路由：轮询进度 + 取消。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_session
from app.services import incremental_jobs

router = APIRouter(
    prefix="/api/incremental-jobs",
    tags=["incremental-jobs"],
    dependencies=[Depends(require_session)],
)


@router.get("/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """查询增量任务进度。"""
    job = await incremental_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="增量任务不存在")
    return job.to_dict()


@router.post("/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """取消增量任务。"""
    job = await incremental_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.task is not None and not job.task.done():
        job.task.cancel()
    job.state = "cancelled"
    job.finished_at = int(__import__("time").time())
    return {"ok": True}
