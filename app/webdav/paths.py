"""DAV 根目录的解析与运行时切换。

DAV 根 = 某个存储源的 ``output_dir``（同步下来的媒体库目录，即 Emby 挂载的
同一棵树）。可在面板里切换 —— 用 ``settings`` KV 表存「当前指向哪个存储源」。

**为什么存 DB 而不是环境变量**：面板改配置不该要重启容器。
"""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import Driver, Setting
from app.db.session import session_scope

logger = get_logger(__name__)

#: KV 键名：DAV 根指向的存储源 id
KEY_ROOT_DRIVER_ID = "dav.root_driver_id"

#: 根目录解析结果的缓存时长（秒）
#:
#: 面板改配置后不必立刻生效 —— 缓存 5 秒足够，也避免每个 DAV 请求都查库
#: （DAV 请求量比面板大得多）。
CACHE_TTL_SEC = 5.0


class _Cache:
    """根目录解析缓存（进程内）。"""

    def __init__(self) -> None:
        self._path: str | None = None
        self._expires_at: float = 0.0
        self._lock_free = True

    def get(self) -> str | None:
        if self._path is not None and self._expires_at > time.monotonic():
            return self._path
        return None

    def set(self, path: str | None) -> None:
        self._path = path
        self._expires_at = time.monotonic() + CACHE_TTL_SEC

    def invalidate(self) -> None:
        self._path = None
        self._expires_at = 0.0


#: 进程级缓存
_root_cache = _Cache()


async def resolve_dav_root() -> str | None:
    """解析当前生效的 DAV 根目录。

    规则：

    1. ``settings`` 里配了 ``dav.root_driver_id`` → 用那个存储源的
       ``output_dir``
    2. 没配 → 取**第一个**配了 ``output_dir`` 的存储源
    3. 都没有 → 返回 None（DAV 会返回 404）

    Returns:
        真实路径；无解时返回 None。
    """
    cached = _root_cache.get()
    if cached is not None:
        return cached

    path = await _resolve_uncached()
    _root_cache.set(path)
    return path


async def _resolve_uncached() -> str | None:
    async with session_scope() as db:
        driver_id = await _get_configured_driver_id(db)
        if driver_id is not None:
            row = (
                await db.execute(select(Driver).where(Driver.id == driver_id))
            ).scalar_one_or_none()
            if row is not None and row.output_dir:
                return row.output_dir
            logger.warning(
                "DAV 根指向的存储源 %s 没有配 output_dir，回退到「第一个可用存储源」",
                driver_id,
            )

        # 回退：第一个配了 output_dir 的存储源
        rows = (
            await db.execute(
                select(Driver)
                .where(Driver.output_dir.is_not(None), Driver.output_dir != "")
                .order_by(Driver.id)
            )
        ).scalars().all()
        for row in rows:
            if row.output_dir:
                return row.output_dir

    return None


async def _get_configured_driver_id(db: AsyncSession) -> int | None:
    """读 KV 里的「DAV 根指向哪个存储源」。"""
    row = (
        await db.execute(select(Setting).where(Setting.key == KEY_ROOT_DRIVER_ID))
    ).scalar_one_or_none()
    if row is None or not row.value:
        return None
    try:
        return int(row.value)
    except ValueError:
        logger.warning("DAV 根配置的存储源 id 非法：%r", row.value)
        return None


async def set_root_driver_id(driver_id: int | None) -> None:
    """设置 DAV 根指向的存储源（None = 恢复自动选择）。

    切换后立即清缓存 —— 面板点了「应用」，用户不该再等 5 秒。
    """
    async with session_scope() as db:
        now = bj_now()
        row = (
            await db.execute(select(Setting).where(Setting.key == KEY_ROOT_DRIVER_ID))
        ).scalar_one_or_none()
        if driver_id is None:
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(
                Setting(key=KEY_ROOT_DRIVER_ID, value=str(driver_id), updated_at=now)
            )
        else:
            row.value = str(driver_id)
            row.updated_at = now
    _root_cache.invalidate()


def invalidate_root_cache() -> None:
    """清缓存（存储源的 output_dir 改了时调用）。"""
    _root_cache.invalidate()
