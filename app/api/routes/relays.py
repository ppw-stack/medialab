"""转存路由：光鸭 → 光鸭 的**跨账号**搬运。

机制是「A 账号分享 → B 账号保存」——服务端内部复制，不占本机带宽，
也不吃云添加额度（云添加每天只有 3 次/账号）。

**路由层只做参数校验与响应组装**——DB 与适配层的访问全在
:mod:`app.services.cloud_relay`（分层约束见 ``tests/unit/test_layering.py``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.services import relay_jobs
from app.services.cloud_relay import RelayBrowseError, browse_dirs
from app.services.relay_jobs import RelayParams

router = APIRouter(
    prefix="/api/relays",
    tags=["relays"],
    dependencies=[Depends(require_session)],
)


class RelayRunIn(BaseModel):
    """开跑参数。"""

    model_config = ConfigDict(extra="forbid")

    source_driver_id: int
    #: 源目录 fileId（光鸭按 id 定位目录，必须非空）
    source_file_id: str = Field(..., min_length=1, max_length=32)
    target_driver_id: int
    #: 目标目录 fileId（空串 = 目标账号根目录）
    target_file_id: str = Field(default="", max_length=32)
    #: 体积阈值（MB）——小于它不转存
    min_size_mb: int = Field(default=0, ge=0)
    #: 体积上限（MB）——0 = 不限
    max_size_mb: int = Field(default=0, ge=0)
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #: **搬完清理源端**——删数据不可逆，默认关
    delete_source: bool = False
    #: 建分享的最小间隔（秒）——防「同账号连续建分享」被限流
    submit_interval_sec: float = Field(default=15.0, ge=0.0, le=600.0)


@router.get("/dirs")
async def list_dirs(driver_id: int, parent_id: str = "") -> dict[str, Any]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。

    源与目标都用这个端点——两边都是光鸭，浏览语义相同。
    """
    try:
        dirs = await browse_dirs(driver_id, parent_id)
    except RelayBrowseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": rid, "name": name} for rid, name in dirs],
    }


@router.post("/run", status_code=201)
async def run_relay(payload: RelayRunIn) -> dict[str, Any]:
    """开跑（**立即返回 job_id**，转存在后台跑）。

    同一时间只允许一个转存任务——多个任务并发只会互相抢光鸭的分享配额。
    """
    if payload.source_driver_id == payload.target_driver_id:
        raise HTTPException(
            status_code=400, detail="源与目标不能是同一个光鸭账号"
        )

    running = await relay_jobs.find_running()
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已有转存任务在跑（job={running.id}）",
        )

    params = RelayParams(
        source_driver_id=payload.source_driver_id,
        source_file_id=payload.source_file_id,
        target_driver_id=payload.target_driver_id,
        target_file_id=payload.target_file_id,
        min_size_mb=payload.min_size_mb,
        max_size_mb=payload.max_size_mb,
        exclude_disc=payload.exclude_disc,
        delete_source=payload.delete_source,
        submit_interval_sec=payload.submit_interval_sec,
    )
    job = await relay_jobs.start_relay_job(params)
    return {"job_id": job.id, "state": job.state}


@router.get("/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    """最近的转存任务（最新在前）——刷新页面可找回进度。"""
    jobs = await relay_jobs.list_jobs()
    return [j.to_dict() for j in jobs]


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """查询转存进度（前端每 2s 拉一次）。"""
    job = await relay_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="转存任务不存在")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """取消转存（停止提交；服务端已开始的保存任务不撤回）。"""
    ok = await relay_jobs.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}
