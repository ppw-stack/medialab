"""存储源管理服务。

负责：创建存储源、扫码登录落盘、凭据加密、登录态探测。
"""

from __future__ import annotations

import os
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers.credential_crypto import (
    CredentialCryptoError,
    encrypt,
    encrypt_with_scheme,
)
from app.adapters.drivers.device_types import DEFAULT_DEVICE_TYPE
from app.adapters.drivers.p115 import reset_clients
from app.adapters.drivers.p115_credential import SECRET_ENV
from app.core.logging import get_logger
from app.db.models import Driver, ProbeResult

logger = get_logger(__name__)


class DriverServiceError(Exception):
    """存储源操作失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


def _encryption_secret() -> str:
    """取凭据加密密钥。

    Raises:
        DriverServiceError: 未配置密钥（此时不能存密文）。
    """
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise DriverServiceError(
            f"未配置 {SECRET_ENV}，无法加密存储凭据",
            status_code=500,
            kind="config",
        )
    return secret


async def get_driver(db: AsyncSession, driver_id: int) -> Driver:
    """取存储源。"""
    row = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).scalar_one_or_none()
    if row is None:
        raise DriverServiceError(
            f"存储源不存在：{driver_id}", status_code=404, kind="not_found"
        )
    return row


async def create_driver(
    db: AsyncSession,
    *,
    driver_type: str,
    name: str,
    device_type: str | None = None,
) -> Driver:
    """创建存储源（尚无凭据）。

    ``device_type`` 是 **115 专属**的概念（扫码登录绑定的 app）。
    其它驱动（光鸭）没有这个概念 → 存 None，否则界面上会出现
    「光鸭盘的设备类型是 115android」这种 nonsensical 的值。
    """
    now = int(time.time())
    resolved_device = device_type if driver_type == "p115" else None
    row = Driver(
        type=driver_type,
        name=name,
        credential=None,
        device_type=resolved_device or (DEFAULT_DEVICE_TYPE if driver_type == "p115" else None),
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info("创建存储源 id=%s type=%s", row.id, row.type)
    return row


async def store_credential(
    db: AsyncSession,
    driver_id: int,
    credential: str,
    device_type: str | None = None,
    *,
    scheme: str | None = None,
) -> Driver:
    """加密存储凭据。

    Args:
        db: DB session。
        driver_id: 存储源 id。
        credential: 明文凭据（115 是 cookie 字符串；光鸭是 JSON）。
        device_type: 115 设备类型（可同时写入）。
        scheme: 加密方案前缀。``None`` = 用默认的 115 方案；光鸭传
            ``"gypcipher:v2:"``。

    Raises:
        DriverServiceError: 加密失败 / 存储源不存在。
    """
    row = await get_driver(db, driver_id)
    secret = _encryption_secret()
    try:
        if scheme is None:
            encrypted = encrypt(credential, secret)
        else:
            encrypted = encrypt_with_scheme(credential, secret, scheme)
    except CredentialCryptoError as exc:
        raise DriverServiceError(
            f"凭据加密失败：{exc}", status_code=500, kind="config"
        ) from exc

    row.credential = encrypted
    if device_type:
        row.device_type = device_type
    row.updated_at = int(time.time())
    await db.commit()
    await db.refresh(row)
    # 凭据变了 → 缓存的 115 客户端实例失效
    reset_clients()
    logger.info("已存储凭据 driver_id=%s", driver_id)
    return row


async def delete_driver(db: AsyncSession, driver_id: int) -> None:
    """删除存储源（级联删关联数据）。"""
    row = await get_driver(db, driver_id)
    await db.delete(row)
    await db.commit()
    reset_clients()


async def record_probe(
    db: AsyncSession,
    driver_id: int,
    *,
    account_id: str,
    used_gb: float,
    total_gb: float,
    available: bool,
) -> ProbeResult:
    """记录一次可用性探测结果。"""
    row = ProbeResult(
        driver_id=driver_id,
        account_id=account_id,
        used_gb=used_gb,
        total_gb=total_gb,
        available=available,
        probed_at=int(time.time()),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def latest_probe(db: AsyncSession, driver_id: int) -> ProbeResult | None:
    """取最近一次探测结果。"""
    result = await db.execute(
        select(ProbeResult)
        .where(ProbeResult.driver_id == driver_id)
        .order_by(ProbeResult.probed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def mask_credential(credential: str | None) -> str:
    """凭据掩码（前端展示用，不回传明文）。"""
    if not credential:
        return ""
    if credential.startswith(("p115cipher:", "gypcipher:")):
        return "encrypted"
    return f"set({len(credential)} chars)"


__all__ = [
    "DEFAULT_DEVICE_TYPE",
    "DriverServiceError",
    "create_driver",
    "delete_driver",
    "get_driver",
    "latest_probe",
    "mask_credential",
    "record_probe",
    "store_credential",
]
