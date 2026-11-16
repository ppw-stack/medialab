"""网盘聚合路由：聚合组 / 组内角色 / 在途台账 / 归拢触发。

**路由层只做参数校验与响应组装**——业务逻辑全在
:mod:`app.services.aggregate_groups` / :mod:`app.services.aggregate_staging` /
:mod:`app.services.aggregate_sync`。

分层约束（``api`` 不依赖 ``db`` / ``adapters``）由
``tests/unit/test_layering.py`` 固化。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.domain.aggregate_types import (
    GUANGYA_DAILY_CLOUD_ADD_QUOTA,
    ROLE_LABELS,
    ROLE_MASTER,
    ROLE_SECONDARY,
    STAGING_STATES,
)
from app.services import aggregate_jobs
from app.services.aggregate_groups import (
    GroupRootSpec,
    assign_driver_role,
    clear_driver_role,
    create_group,
    delete_group,
    get_group,
    get_mapping,
    list_groups,
    set_mapping,
    update_group,
)
from app.services.aggregate_staging import (
    delete_staging,
    enqueue_from_staging_root,
    group_stats,
    list_staging,
    retry_staging,
)
from app.services.errors import ServiceError

router = APIRouter(
    prefix="/api/aggregate",
    tags=["aggregate"],
    dependencies=[Depends(require_session)],
)


class GroupIn(BaseModel):
    """新建聚合组。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)


class GroupUpdate(BaseModel):
    """修改聚合组（重命名 / 启停）。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None


class RoleAssignIn(BaseModel):
    """把存储源指派为组内的主 / 副数据源。"""

    model_config = ConfigDict(extra="forbid")

    group_id: int = Field(..., ge=1)
    role: str = Field(..., min_length=1, max_length=16)


class GroupOut(BaseModel):
    """聚合组。"""

    id: int
    name: str
    enabled: bool
    created_at: str
    updated_at: str
    #: 归拢目标目录 id（主盘上的目录；None = 未配置）
    target_dir_id: str | None = None
    #: 归拢目标目录路径（只读展示）
    target_dir_path: str | None = None


class GroupRootIn(BaseModel):
    """一个副盘的缓冲根（组 ↔ 副盘 的映射项）。"""

    model_config = ConfigDict(extra="forbid")

    driver_id: int = Field(..., ge=1)
    root_id: str = Field(..., min_length=1, max_length=64)
    root_path: str | None = Field(default=None, max_length=1024)


class GroupMappingIn(BaseModel):
    """整组的目录映射：**从哪些副盘的哪些目录，搬到主盘的哪个目录**。

    两端都是**组级配置**——不借存储源的媒体库配置。
    """

    model_config = ConfigDict(extra="forbid")

    #: 主盘上的归拢目标目录 id（留空 = 清除配置）
    target_dir_id: str | None = Field(default=None, max_length=64)
    target_dir_path: str | None = Field(default=None, max_length=1024)
    #: 各副盘的缓冲根（**全量替换**：未列出的副盘会被清掉）
    roots: list[GroupRootIn] = Field(default_factory=list)


class GroupRootOut(BaseModel):
    """一个副盘的缓冲根。"""

    driver_id: int
    root_id: str
    root_path: str | None = None


class GroupMappingOut(BaseModel):
    """一个组的完整目录映射。"""

    group_id: int
    target_dir_id: str | None = None
    target_dir_path: str | None = None
    roots: list[GroupRootOut] = Field(default_factory=list)


class GroupStatsOut(BaseModel):
    """聚合组水位。"""

    group_id: int
    group_name: str
    master_driver_id: int | None
    secondary_driver_ids: list[int]
    counts: dict[str, int]
    pending_bytes: int


class StagingOut(BaseModel):
    """一条在途台账。"""

    id: int
    group_id: int
    source_driver_id: int
    target_driver_id: int
    remote_id: str
    file_name: str
    source_dir: str
    target_dir: str
    size_bytes: int
    state: str
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    synced_at: str | None
    cleaned_at: str | None


class EnqueueResultOut(BaseModel):
    """登记归拢任务的结果。"""

    scanned: int
    added: int
    skipped: int


class SyncJobRefOut(BaseModel):
    """手动归拢任务引用（**立即返回**，归拢在后台跑）。"""

    job_id: str
    state: str


class SyncJobOut(BaseModel):
    """手动归拢任务的运行状态（轮询用）。"""

    id: str
    group_id: int
    group_name: str
    state: str
    started_at: int
    finished_at: int | None
    elapsed_sec: int
    synced: int
    cleaned: int
    paused: int
    failed: int
    skipped: int
    details: list[str]
    logs: list[str]
    error: str | None


class LimitsOut(BaseModel):
    """聚合相关的**服务端常量**（前端不硬写）。"""

    #: 光鸭云添加每日额度（次/账号）——撞墙即暂停，次日自动续传
    daily_cloud_add_quota: int


@router.get("/limits", response_model=LimitsOut)
async def limits_route() -> LimitsOut:
    """服务端常量（单一真源）。

    **为什么给前端**：面板要提示「云添加约 N 次/天（实测），撞上限自动暂停」，
    这个数字是**实测值**、会变——前端硬写就会漂移。
    """
    return LimitsOut(daily_cloud_add_quota=GUANGYA_DAILY_CLOUD_ADD_QUOTA)


@router.get("/groups", response_model=list[GroupOut])
async def list_groups_route() -> list[GroupOut]:
    """列出聚合组。"""
    rows = await list_groups()
    return [
        GroupOut(
            id=r.id,
            name=r.name,
            enabled=r.enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
            target_dir_id=r.target_dir_id,
            target_dir_path=r.target_dir_path,
        )
        for r in rows
    ]


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group_route(payload: GroupIn) -> GroupOut:
    """新建聚合组。"""
    try:
        row = await create_group(payload.name)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return GroupOut(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        target_dir_id=row.target_dir_id,
        target_dir_path=row.target_dir_path,
    )


@router.patch("/groups/{group_id}", response_model=GroupOut)
async def update_group_route(group_id: int, payload: GroupUpdate) -> GroupOut:
    """改聚合组（重命名 / 启停）。停用不删数据。"""
    try:
        row = await update_group(
            group_id, name=payload.name, enabled=payload.enabled
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return GroupOut(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        target_dir_id=row.target_dir_id,
        target_dir_path=row.target_dir_path,
    )


@router.delete("/groups/{group_id}")
async def delete_group_route(group_id: int) -> dict[str, bool]:
    """解散聚合组（组内有盘或有未消化台账 → 409）。"""
    try:
        await delete_group(group_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}


@router.put("/drivers/{driver_id}/role")
async def assign_role_route(driver_id: int, payload: RoleAssignIn) -> dict[str, bool]:
    """把存储源指派为组内的主 / 副数据源。

    **校验在后端**：组内已有主盘（409）、盘上还有媒体库记录（409）、
    盘上还有未消化台账（409）——绕过前端直接打 API 也拦得住。
    """
    try:
        await assign_driver_role(
            driver_id, group_id=payload.group_id, role=payload.role
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/drivers/{driver_id}/role")
async def clear_role_route(driver_id: int) -> dict[str, bool]:
    """把存储源移出聚合组（恢复为独立存储源）。"""
    try:
        await clear_driver_role(driver_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/roles")
async def list_roles() -> list[dict[str, str]]:
    """角色清单（**单一真源**：后端下发，前端不手写）。"""
    return [
        {"value": ROLE_MASTER, "label": ROLE_LABELS[ROLE_MASTER]},
        {"value": ROLE_SECONDARY, "label": ROLE_LABELS[ROLE_SECONDARY]},
    ]


@router.get("/groups/{group_id}/mapping", response_model=GroupMappingOut)
async def get_mapping_route(group_id: int) -> GroupMappingOut:
    """取一个组的**完整目录映射**（主盘落点 + 各副盘缓冲根）。"""
    try:
        mapping = await get_mapping(group_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return GroupMappingOut(
        group_id=mapping.group_id,
        target_dir_id=mapping.target_dir_id,
        target_dir_path=mapping.target_dir_path,
        roots=[
            GroupRootOut(
                driver_id=r.driver_id,
                root_id=r.root_id,
                root_path=r.root_path,
            )
            for r in mapping.roots
        ],
    )


@router.put("/groups/{group_id}/mapping", response_model=GroupMappingOut)
async def set_mapping_route(
    group_id: int, payload: GroupMappingIn
) -> GroupMappingOut:
    """设置一个组的目录映射（**一次提交整组**）。

    副盘缓冲根只接受**组内副数据源**（组外的盘给了也不认 → 400）。
    """
    try:
        mapping = await set_mapping(
            group_id,
            target_dir_id=payload.target_dir_id,
            target_dir_path=payload.target_dir_path,
            roots=[
                GroupRootSpec(
                    driver_id=r.driver_id,
                    root_id=r.root_id,
                    root_path=r.root_path,
                )
                for r in payload.roots
            ],
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return GroupMappingOut(
        group_id=mapping.group_id,
        target_dir_id=mapping.target_dir_id,
        target_dir_path=mapping.target_dir_path,
        roots=[
            GroupRootOut(
                driver_id=r.driver_id,
                root_id=r.root_id,
                root_path=r.root_path,
            )
            for r in mapping.roots
        ],
    )


@router.get("/groups/{group_id}/stats", response_model=GroupStatsOut)
async def group_stats_route(group_id: int) -> GroupStatsOut:
    """某组的水位（各状态条数 + 待归拢字节 + 主盘额度）。"""
    for stats in await group_stats():
        if stats.group_id == group_id:
            return GroupStatsOut(**stats.to_dict())
    raise HTTPException(status_code=404, detail=f"聚合组不存在：{group_id}")


@router.get("/stats", response_model=list[GroupStatsOut])
async def all_stats_route() -> list[GroupStatsOut]:
    """全部聚合组的水位。"""
    return [GroupStatsOut(**s.to_dict()) for s in await group_stats()]


@router.get("/staging", response_model=list[StagingOut])
async def list_staging_route(
    group_id: int | None = None,
    state: str | None = None,
    limit: int = 200,
) -> list[StagingOut]:
    """在途台账列表。"""
    if state is not None and state not in STAGING_STATES:
        raise HTTPException(status_code=400, detail=f"未知的台账状态：{state}")
    try:
        rows = await list_staging(group_id=group_id, state=state, limit=limit)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return [
        StagingOut(
            id=r.id,
            group_id=r.group_id,
            source_driver_id=r.source_driver_id,
            target_driver_id=r.target_driver_id,
            remote_id=r.remote_id,
            file_name=r.file_name,
            source_dir=r.source_dir,
            target_dir=r.target_dir,
            size_bytes=r.size_bytes,
            state=r.state,
            transfer_mode=r.transfer_mode,
            attempts=r.attempts,
            last_error=r.last_error,
            created_at=r.created_at,
            updated_at=r.updated_at,
            synced_at=r.synced_at,
            cleaned_at=r.cleaned_at,
        )
        for r in rows
    ]


@router.post("/groups/{group_id}/enqueue", response_model=EnqueueResultOut)
async def enqueue_route(group_id: int) -> EnqueueResultOut:
    """扫描组内副盘的缓冲根目录，把文件登记进台账（幂等）。"""
    try:
        result = await enqueue_from_staging_root(group_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return EnqueueResultOut(**result)


@router.post("/groups/{group_id}/sync", response_model=SyncJobRefOut)
async def sync_route(group_id: int) -> SyncJobRefOut:
    """手动触发一轮归拢（**立即返回 job_id**，归拢在后台跑）。

    为什么异步：单文件传输要几分钟，同步 HTTP 会被反代 / 网关的 60s 超时
    掐断（``drivers.py`` 的 scan 路由注释里记过同款坑）。进度用
    ``GET /api/aggregate/sync-jobs/{job_id}`` 轮询。

    同组已有人在跑 → 409（组级锁保证归拢互斥，这里提前拦避免排队干等）。
    """
    try:
        group = await get_group(group_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    running = await aggregate_jobs.find_running(group_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该组已有归拢任务在跑（job={running.id}）",
        )

    job = await aggregate_jobs.start_sync_job(group_id, group.name)
    return SyncJobRefOut(job_id=job.id, state=job.state)


@router.get("/sync-jobs/{job_id}", response_model=SyncJobOut)
async def sync_job_status(job_id: str) -> SyncJobOut:
    """查手动归拢任务的进度（前端每 2s 轮询）。"""
    job = await aggregate_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"归拢任务不存在：{job_id}")
    return SyncJobOut(**job.to_dict())


@router.get("/sync-jobs", response_model=list[SyncJobOut])
async def list_sync_jobs(group_id: int | None = None) -> list[SyncJobOut]:
    """列出手动归拢任务（最新在前）。"""
    jobs = await aggregate_jobs.list_jobs(group_id)
    return [SyncJobOut(**j.to_dict()) for j in jobs]


@router.post("/sync-jobs/{job_id}/cancel")
async def cancel_sync_job(job_id: str) -> dict[str, bool]:
    """取消一个在跑的归拢任务（正在传输的文件会被打断，进 failed）。"""
    cancelled = await aggregate_jobs.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail=f"任务不存在或已结束：{job_id}")
    return {"ok": True}


@router.post("/staging/{staging_id}/retry")
async def retry_route(staging_id: int) -> dict[str, bool]:
    """把失败 / 暂停的行重置为「待归拢」。"""
    try:
        await retry_staging(staging_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/staging/{staging_id}")
async def delete_staging_route(staging_id: int) -> dict[str, bool]:
    """删除一条台账（**不动网盘上的文件**）。"""
    try:
        await delete_staging(staging_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}
