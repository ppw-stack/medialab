"""WebDAV 账号管理 + 审计查询 + DAV 根配置。

**为什么独立于 :mod:`app.services.dav_auth`**：``dav_auth`` 只管**认证**
（校验 Basic Auth、限流、锁定），本模块管**账号生命周期**（建号、改密、
停用、删号）与**审计查询**、**DAV 根切换**。

**API 层不感知 ORM**：对外一律返回 dataclass，路由层只做参数校验与
响应组装（分层约束见 ``tests/unit/test_layering.py``）。

**改密码 / 停用后必须清认证缓存**：不然旧凭据还能命中缓存继续用
（缓存默认 60 秒，但这段时间内旧密码仍然有效 —— 改密码的语义就该是
「立即失效」）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.services.dav_auth import dav_authenticator, hash_password

logger = get_logger(__name__)


class DavUserServiceError(Exception):
    """账号操作失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


# ================================================================ 数据载体


@dataclass(slots=True)
class DavUserRow:
    """一个 WebDAV 账号（**不含密码哈希**）。"""

    id: int
    username: str
    is_active: bool
    last_login_at: str | None
    created_at: str


@dataclass(slots=True)
class DavAuditRow:
    """一条审计记录。"""

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


@dataclass(slots=True)
class DavAuditPage:
    """审计查询结果（分页）。"""

    items: list[DavAuditRow]
    total: int


@dataclass(slots=True)
class DavRootDriver:
    """可选的 DAV 根（存储源）。"""

    id: int
    name: str
    output_dir: str | None
    #: 该存储源能否作为 DAV 根（有 output_dir 且目录存在）
    usable: bool


@dataclass(slots=True)
class DavRootConfig:
    """DAV 根配置。"""

    #: 当前指向的存储源 id；None = 自动（第一个可用）
    driver_id: int | None
    #: 可选的存储源列表
    drivers: list[DavRootDriver]


# ================================================================ 账号 CRUD


async def list_dav_users() -> list[DavUserRow]:
    """列出所有 WebDAV 账号。"""
    from sqlalchemy import select

    from app.db.models import DavUser
    from app.db.session import session_scope

    async with session_scope() as db:
        rows = (await db.execute(select(DavUser).order_by(DavUser.id))).scalars().all()
        return [
            DavUserRow(
                id=r.id,
                username=r.username,
                is_active=r.is_active,
                last_login_at=r.last_login_at,
                created_at=r.created_at,
            )
            for r in rows
        ]


async def create_dav_user(username: str, password: str) -> DavUserRow:
    """新建 WebDAV 账号。

    Raises:
        DavUserServiceError: 用户名已存在（409）。
    """
    from sqlalchemy import select

    from app.db.models import DavUser
    from app.db.session import session_scope

    async with session_scope() as db:
        existing = (
            await db.execute(select(DavUser).where(DavUser.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise DavUserServiceError(
                f"用户名已存在：{username}", status_code=409, kind="conflict"
            )

        now = bj_now()
        row = DavUser(
            username=username,
            password_hash=hash_password(password),
            is_active=True,
            last_login_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        logger.info("已创建 WebDAV 账号：%s", username)
        return DavUserRow(
            id=row.id,
            username=row.username,
            is_active=row.is_active,
            last_login_at=row.last_login_at,
            created_at=row.created_at,
        )


async def update_dav_user(
    user_id: int,
    *,
    password: str | None = None,
    is_active: bool | None = None,
) -> DavUserRow:
    """修改账号（改密码 / 启停）。

    **改密码或停用会清空全部认证缓存** —— 否则旧凭据还能继续用。

    Raises:
        DavUserServiceError: 账号不存在（404）。
    """
    from sqlalchemy import select

    from app.db.models import DavUser
    from app.db.session import session_scope

    async with session_scope() as db:
        row = (
            await db.execute(select(DavUser).where(DavUser.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            raise DavUserServiceError("账号不存在", status_code=404, kind="not_found")

        if password is not None:
            row.password_hash = hash_password(password)
        if is_active is not None:
            row.is_active = is_active
        if password is not None or is_active is not None:
            row.updated_at = bj_now()
            # 凭据或状态变了 → 旧缓存立即作废
            purged = dav_authenticator.purge_all_cache()
            logger.info("账号 %s 已更新，清空 %d 条认证缓存", row.username, purged)
        await db.commit()

        return DavUserRow(
            id=row.id,
            username=row.username,
            is_active=row.is_active,
            last_login_at=row.last_login_at,
            created_at=row.created_at,
        )


async def delete_dav_user(user_id: int) -> None:
    """删除账号。

    Raises:
        DavUserServiceError: 账号不存在（404）。
    """
    from sqlalchemy import select

    from app.db.models import DavUser
    from app.db.session import session_scope

    async with session_scope() as db:
        row = (
            await db.execute(select(DavUser).where(DavUser.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            raise DavUserServiceError("账号不存在", status_code=404, kind="not_found")

        await db.delete(row)
        await db.commit()
        dav_authenticator.purge_all_cache()
        logger.info("已删除 WebDAV 账号：id=%d", user_id)


# ================================================================ 审计


async def list_dav_audit(
    *,
    username: str | None = None,
    method: str | None = None,
    status: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> DavAuditPage:
    """查询审计日志（按 id 倒序，最新的在前）。"""
    from sqlalchemy import func, select

    from app.db.models import DavAuditLog
    from app.db.session import session_scope

    conditions = []
    if username:
        conditions.append(DavAuditLog.username == username)
    if method:
        conditions.append(DavAuditLog.method == method.upper())
    if status is not None:
        conditions.append(DavAuditLog.status == status)

    async with session_scope() as db:
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
            DavAuditRow(
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


async def clear_dav_audit() -> None:
    """清空审计日志。"""
    from sqlalchemy import delete

    from app.db.models import DavAuditLog
    from app.db.session import session_scope

    async with session_scope() as db:
        await db.execute(delete(DavAuditLog))
        await db.commit()
    logger.info("已清空 WebDAV 审计日志")


# ================================================================ DAV 根配置


async def get_dav_root() -> DavRootConfig:
    """查询 DAV 根配置与可选存储源。"""
    from sqlalchemy import select

    from app.db.models import Driver, Setting
    from app.db.session import session_scope
    from app.services.dav_root import KEY_ROOT_DRIVER_ID

    async with session_scope() as db:
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

    return DavRootConfig(
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


async def set_dav_root(driver_id: int | None) -> DavRootConfig:
    """设置 DAV 根指向的存储源（``None`` = 自动选择）。

    Raises:
        DavUserServiceError: 存储源不存在（404）/ 未配置 output_dir（400）。
    """
    from sqlalchemy import select

    from app.db.models import Driver
    from app.db.session import session_scope
    from app.services.dav_root import set_root_driver_id

    if driver_id is not None:
        async with session_scope() as db:
            row = (
                await db.execute(select(Driver).where(Driver.id == driver_id))
            ).scalar_one_or_none()
            if row is None:
                raise DavUserServiceError("存储源不存在", status_code=404, kind="not_found")
            if not row.output_dir:
                raise DavUserServiceError(
                    f"存储源「{row.name}」未配置 output_dir", status_code=400
                )

    await set_root_driver_id(driver_id)
    logger.info("DAV 根已切换到存储源：%s", driver_id or "自动")
    return await get_dav_root()


__all__ = [
    "DavAuditPage",
    "DavAuditRow",
    "DavRootConfig",
    "DavRootDriver",
    "DavUserRow",
    "DavUserServiceError",
    "clear_dav_audit",
    "create_dav_user",
    "delete_dav_user",
    "get_dav_root",
    "list_dav_audit",
    "list_dav_users",
    "set_dav_root",
    "update_dav_user",
]
