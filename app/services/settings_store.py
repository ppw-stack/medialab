"""``settings`` 表的 KV 读写。

用途：存**增量游标**这类「与业务配置无关、但需要跨重启保留」的状态。

为什么不加列：游标是**每个存储源一个**的运行时状态，加到 ``drivers``
表会让那张表承担两种语义（配置 + 状态）；放进现成的 ``settings`` KV 表
则不用改表结构、不用写迁移。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import bj_now
from app.db.models import Setting


async def get_value(db: AsyncSession, key: str) -> str | None:
    """读一个键（不存在返回 None）。"""
    row = (
        await db.execute(select(Setting).where(Setting.key == key))
    ).scalar_one_or_none()
    return row.value if row is not None else None


async def set_value(db: AsyncSession, key: str, value: str) -> None:
    """写一个键（存在则更新）。"""
    now = bj_now()
    row = (
        await db.execute(select(Setting).where(Setting.key == key))
    ).scalar_one_or_none()
    if row is None:
        db.add(Setting(key=key, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now


async def delete_value(db: AsyncSession, key: str) -> None:
    """删一个键（不存在不报错）。"""
    row = (
        await db.execute(select(Setting).where(Setting.key == key))
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
