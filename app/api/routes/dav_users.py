"""WebDAV 账号管理 + 审计查询（面板用）。

**与面板账号分离**：WebDAV 账号是「给播放器用的凭证」，不应该能开面板；
面板也不该暴露给拿到播放器的第三方。

账号可停用（``is_active=False``）—— 立即失效且保留审计关联，比删除好。

**改密码 / 停用后必须清认证缓存**：不然旧凭据还能命中缓存继续用
（缓��默认 60 秒，但这段时间内旧密码仍然有效 —— 改密码的语义就该是
「立即失效」）。
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.webdav.paths import KEY_ROOT_DRIVER_ID, set_root_driver_id
from app.api.auth import require_session
from app.core.logging import get_logger
from app.db.models import DavAuditLog, DavUser, Driver, Setting
from app.db.session import session_scope
from app.services.dav_auth import dav_authenticator, hash_password

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/dav-users",
    tags=["dav-users"],
    dependencies=[Depends(require_session)],
)


async def _db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as db:
        yield db


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
    last_login_at: int
    created_at: int


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
    created_at: int


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


async def _to_out(row: DavUser) -> DavUserOut:
    """组装输出。"""
    return DavUserOut(
        id=row.id,
        username=row.username,
        is_active=row.is_active,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


@router.get("", response_model=list[DavUserOut])
async def list_users(db: AsyncSession = Depends(_db)) -> list[DavUserOut]:
    """列出所有 WebDAV 账号。"""
    rows = (await db.execute(select(DavUser).order_by(DavUser.id))).scalars().all()
    return [await _to_out(r) for r in rows]


@router.post("", response_model=DavUserOut, status_code=201)
async def create_user(
    payload: DavUserIn, db: AsyncSession = Depends(_db)
) -> DavUserOut:
    """新建 WebDAV 账号。"""
    existing = (
        await db.execute(select(DavUser).where(DavUser.username == payload.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"用户名已存在：{payload.username}"
        )

    now = int(time.time())
    row = DavUser(
        username=payload.username,
        password_hash=hash_password(payload.password),
        is_active=True,
        last_login_at=0,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.flush()
    logger.info("已创建 WebDAV 账号：%s", payload.username)
    return await _to_out(row)


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


@router.get("/audit", response_model=DavAuditPage)
async def list_audit(
    db: AsyncSession = Depends(_db),
    username: str | None = Query(default=None, description="按用户名过滤"),
    method: str | None = Query(default=None, description="按 HTTP 方法过滤"),
    status: int | None = Query(default=None, description="按状态码过滤"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> DavAuditPage:
    """查询审计日志。"""
    conditions = []
    if username:
        conditions.append(DavAuditLog.username == username)
    if method:
        conditions.append(DavAuditLog.method == method.upper())
    if status is not None:
        conditions.append(DavAuditLog.status == status)

    total = (
        await db.execute(
            select(func.count()).select_from(DavAuditLog).where(*conditions)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(DavAuditLog)
            .where(*conditions)
            .order_by(DavAuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

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
            for r in rows
        ],
        total=int(total),
    )


@router.delete("/audit", status_code=204)
async def clear_audit(db: AsyncSession = Depends(_db)) -> None:
    """清空审计日志。"""
    await db.execute(delete(DavAuditLog))
    logger.info("已清空 WebDAV 审计日志")


# --------------------------------------------------------------------------
# DAV 根配置
# --------------------------------------------------------------------------


@router.get("/root", response_model=DavRootOut)
async def get_root(db: AsyncSession = Depends(_db)) -> DavRootOut:
    """查询 DAV 根配置与可选存储源。"""
    row = (
        await db.execute(select(Setting).where(Setting.key == KEY_ROOT_DRIVER_ID))
    ).scalar_one_or_none()
    driver_id: int | None = None
    if row is not None and row.value:
        try:
            driver_id = int(row.value)
        except ValueError:
            driver_id = None

    drivers = (await db.execute(select(Driver).order_by(Driver.id))).scalars().all()

    return DavRootOut(
        driver_id=driver_id,
        drivers=[
            DavRootDriver(
                id=d.id,
                name=d.name,
                output_dir=d.output_dir,
                usable=bool(d.output_dir) and os.path.isdir(d.output_dir or ""),
            )
            for d in drivers
        ],
    )


@router.put("/root", response_model=DavRootOut)
async def set_root(
    payload: DavRootUpdate, db: AsyncSession = Depends(_db)
) -> DavRootOut:
    """设置 DAV 根指向的存储源。"""
    if payload.driver_id is not None:
        row = (
            await db.execute(select(Driver).where(Driver.id == payload.driver_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="存储源不存在")
        if not row.output_dir:
            raise HTTPException(
                status_code=400, detail=f"存储源「{row.name}」未配置 output_dir"
            )

    await set_root_driver_id(payload.driver_id)
    logger.info("DAV 根已切换到存储源：%s", payload.driver_id or "自动")
    return await get_root(db)


# --------------------------------------------------------------------------
# 单账号操作
#
# **必须放在文件末尾** —— 路由按注册顺序匹配，而 ``/audit`` 与 ``/root``
# 是固定路径段。若 ``/{user_id}`` 先注册，``DELETE /audit`` 会被它接走
# （``user_id="audit"`` 解析失败 → 422）。
# --------------------------------------------------------------------------


@router.patch("/{user_id}", response_model=DavUserOut)
async def update_user(
    user_id: int,
    payload: DavUserUpdate,
    db: AsyncSession = Depends(_db),
) -> DavUserOut:
    """修改账号（改密码 / 启停）。

    **改密码或停用会清空全部认证缓存** —— 否则旧凭据还能继续用。
    """
    row = (
        await db.execute(select(DavUser).where(DavUser.id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    if payload.password is not None:
        row.password_hash = hash_password(payload.password)
        row.updated_at = int(time.time())
    if payload.is_active is not None:
        row.is_active = payload.is_active
        row.updated_at = int(time.time())

    if payload.password is not None or payload.is_active is not None:
        # 凭据或状态变了 → 旧缓存立即作废
        purged = dav_authenticator.purge_all_cache()
        logger.info("账号 %s 已更新，清空 %d 条认证缓存", row.username, purged)

    return await _to_out(row)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: int, db: AsyncSession = Depends(_db)) -> None:
    """删除账号。"""
    row = (
        await db.execute(select(DavUser).where(DavUser.id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    await db.delete(row)
    dav_authenticator.purge_all_cache()
    logger.info("已删除 WebDAV 账号：id=%d", user_id)


DavRootOut.model_rebuild()
