"""WebDAV 账号管理 + 审计查询（面板用）。

**与面板账号分离**：WebDAV 账号是「给播放器用的凭证」，不应该能开面板；
面板也不该暴露给拿到播放器的第三方。

账号可停用（``is_active=False``）—— 立即失效且保留审计关联，比删除好。

**改密码 / 停用后必须清认证缓存**：不然旧凭据还能命中缓存继续用
（缓存默认 60 秒，但这段时间内旧密码仍然有效 —— 改密码的语义就该是
「立即失效」）。

**路由层只做参数校验与响应组装**——DB 访问全在
:mod:`app.services.dav_users`（分层约束见
``tests/unit/test_layering.py``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services.dav_users import (
    DavUserServiceError,
    clear_dav_audit,
    create_dav_user,
    delete_dav_user,
    get_dav_root,
    list_dav_audit,
    list_dav_users,
    set_dav_root,
    update_dav_user,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/dav-users",
    tags=["dav-users"],
    dependencies=[Depends(require_session)],
)


# --------------------------------------------------------------------------
# 请求 / 响应模型
# --------------------------------------------------------------------------


class DavUserIn(BaseModel):
    """新建账号。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class DavUserUpdate(BaseModel):
    """修改账号（全部可选）。"""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, min_length=1, max_length=256)
    is_active: bool | None = None


class DavUserOut(BaseModel):
    """账号信息（**不含密码哈希**）。"""

    id: int
    username: str
    is_active: bool
    last_login_at: str | None
    created_at: str


class DavAuditOut(BaseModel):
    """审计记录。"""

    id: int
    user_id: int
    username: str
    method: str
    path: str
    status: int
    client: str
    remote_addr: str
    duration_ms: int
    item_count: int
    created_at: str


class DavAuditPage(BaseModel):
    """审计查询结果（分页）。"""

    items: list[DavAuditOut]
    total: int


class DavRootOut(BaseModel):
    """DAV 根配置。"""

    #: 当前指向的存储源 id；None = 自动（第一个可用）
    driver_id: int | None
    #: 可选的存储源列表（id + 名字 + 是否有 output_dir）
    drivers: list[DavRootDriver]


class DavRootDriver(BaseModel):
    """可选的 DAV 根（存储源）。"""

    id: int
    name: str
    output_dir: str | None
    #: 该存储源能否作为 DAV 根（有 output_dir 且目录存在）
    usable: bool


class DavRootUpdate(BaseModel):
    """设置 DAV 根。"""

    model_config = ConfigDict(extra="forbid")

    #: 目标存储源 id；None = 自动选择
    driver_id: int | None = None


# --------------------------------------------------------------------------
# 账号 CRUD
# --------------------------------------------------------------------------


@router.get("", response_model=list[DavUserOut])
async def list_users() -> list[DavUserOut]:
    """列出所有 WebDAV 账号。"""
    return [
        DavUserOut(
            id=r.id,
            username=r.username,
            is_active=r.is_active,
            last_login_at=r.last_login_at,
            created_at=r.created_at,
        )
        for r in await list_dav_users()
    ]


@router.post("", response_model=DavUserOut, status_code=201)
async def create_user(payload: DavUserIn) -> DavUserOut:
    """新建 WebDAV 账号。"""
    try:
        row = await create_dav_user(payload.username, payload.password)
    except DavUserServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return DavUserOut(
        id=row.id,
        username=row.username,
        is_active=row.is_active,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


@router.get("/audit", response_model=DavAuditPage)
async def list_audit(
    username: str | None = Query(default=None, description="按用户名过滤"),
    method: str | None = Query(default=None, description="按 HTTP 方法过滤"),
    status: int | None = Query(default=None, description="按状态码过滤"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> DavAuditPage:
    """查询审计日志。"""
    page = await list_dav_audit(
        username=username,
        method=method,
        status=status,
        limit=limit,
        offset=offset,
    )
    return DavAuditPage(
        items=[
            DavAuditOut(
                id=r.id,
                user_id=r.user_id,
                username=r.username,
                method=r.method,
                path=r.path,
                status=r.status,
                client=r.client,
                remote_addr=r.remote_addr,
                duration_ms=r.duration_ms,
                item_count=r.item_count,
                created_at=r.created_at,
            )
            for r in page.items
        ],
        total=page.total,
    )


@router.delete("/audit", status_code=204)
async def clear_audit() -> None:
    """清空审计日志。"""
    await clear_dav_audit()


# --------------------------------------------------------------------------
# DAV 根配置
# --------------------------------------------------------------------------


@router.get("/root", response_model=DavRootOut)
async def get_root() -> DavRootOut:
    """查询 DAV 根配置与可选存储源。"""
    cfg = await get_dav_root()
    return DavRootOut(
        driver_id=cfg.driver_id,
        drivers=[
            DavRootDriver(
                id=d.id,
                name=d.name,
                output_dir=d.output_dir,
                usable=d.usable,
            )
            for d in cfg.drivers
        ],
    )


@router.put("/root", response_model=DavRootOut)
async def set_root(payload: DavRootUpdate) -> DavRootOut:
    """设置 DAV 根指向的存储源。"""
    try:
        cfg = await set_dav_root(payload.driver_id)
    except DavUserServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return DavRootOut(
        driver_id=cfg.driver_id,
        drivers=[
            DavRootDriver(
                id=d.id,
                name=d.name,
                output_dir=d.output_dir,
                usable=d.usable,
            )
            for d in cfg.drivers
        ],
    )


# --------------------------------------------------------------------------
# 单账号操作
#
# **必须放在文件末尾** —— 路由按注册顺序匹配，而 ``/audit`` 与 ``/root``
# 是固定路径段。若 ``/{user_id}`` 先注册，``DELETE /audit`` 会被它接走
# （``user_id="audit"`` 解析失败 → 422）。
# --------------------------------------------------------------------------


@router.patch("/{user_id}", response_model=DavUserOut)
async def update_user(user_id: int, payload: DavUserUpdate) -> DavUserOut:
    """修改账号（改密码 / 启停）。

    **改密码或停用会清空全部认证缓存** —— 否则旧凭据还能继续用。
    """
    try:
        row = await update_dav_user(
            user_id,
            password=payload.password,
            is_active=payload.is_active,
        )
    except DavUserServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return DavUserOut(
        id=row.id,
        username=row.username,
        is_active=row.is_active,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: int) -> None:
    """删除账号。"""
    try:
        await delete_dav_user(user_id)
    except DavUserServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


DavRootOut.model_rebuild()
