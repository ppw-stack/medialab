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
from app.services.aggregate_groups import (
    assign_driver_role,
    clear_driver_role,
    create_group,
    delete_group,
    list_groups,
    update_group,
)
from app.services.aggregate_staging import (
    delete_staging,
    enqueue_from_staging_root,
    group_stats,
    list_staging,
    retry_staging,
)
from app.services.aggregate_sync import sync_group_now
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


class SyncResultOut(BaseModel):
    """手动跑一轮归拢的结果。"""

    groups: int
    synced: int
    cleaned: int
    paused: int
    failed: int
    skipped: int
    details: list[str]


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


@router.post("/groups/{group_id}/sync", response_model=SyncResultOut)
async def sync_route(group_id: int) -> SyncResultOut:
    """手动跑一轮归拢（同步执行，可能要几分钟）。"""
    try:
        result = await sync_group_now(group_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return SyncResultOut(**result.to_dict())


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
