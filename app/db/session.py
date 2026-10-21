"""异步 SQLAlchemy engine 与会话（§8 SQLite 约定）。

本模块只管 **engine / session 生命周期**。相关模块：

- :mod:`app.db.models` —— ORM 表结构
- :mod:`app.db.migrations` —— 幂等迁移（ALTER / datetime 转换 / 回填）
- :mod:`app.db.bootstrap` —— 建表 + 迁移编排（``DbBootstrap``）

三条硬约定：

1. ``journal_mode=WAL`` + ``foreign_keys=ON``：巡检写不阻塞面板读。
2. **写操作短事务**：不在事务内做网络 IO（见 :mod:`app.services.jobs`）。
   SQLite 单写者限制只在**多进程**下才成问题——本项目是单进程单 worker
   （Dockerfile 里 ``--workers 1``），无需进程内写队列。
3. 下载器写操作再叠加**按实例的操作锁**（非功能·并发安全），
   见 :mod:`app.adapters.downloaders.locks`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import AppSettings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_pragma_applier(settings: AppSettings):  # type: ignore[no-untyped-def]
    """连接级 PRAGMA 闭包（读 settings 配置，非硬编码）。"""

    def apply(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"PRAGMA journal_mode={settings.db_journal_mode}")
            cursor.execute(f"PRAGMA synchronous={settings.db_synchronous}")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()

    return apply


def create_db_engine(settings: AppSettings) -> AsyncEngine:
    """构造 engine。测试可用独立 settings 建内存库。"""
    settings.ensure_dirs()
    engine = create_async_engine(
        settings.db_url,
        echo=False,
        pool_pre_ping=True,
        connect_args={"timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _make_pragma_applier(settings))
    return engine


def init_db(settings: AppSettings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """初始化全局 engine 与 session factory（幂等）。"""
    global _engine, _session_factory
    if _engine is not None:
        return _engine, _session_factory  # type: ignore[return-value]
    _engine = create_db_engine(settings)
    _session_factory = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    return _engine, _session_factory


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """事务性会话上下文。正常退出提交，异常回滚。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_db() -> None:
    """关闭连接池（优雅停机）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("数据库连接池已关闭")
    _engine = None
    _session_factory = None


async def reset_db_for_test(settings: AppSettings) -> None:
    """丢弃现有 engine 并按新 settings 重建（**仅测试用**）。

    :func:`init_db` 是幂等的（``_engine is not None`` 时直接返回），
    因此测试之间无法切换 DB 路径。本函数先 dispose 旧连接池再重建，
    让每个测试用例能用独立的临时 DB。

    注意：``aiosqlite`` 连接池绑定创建它的 event loop —— 配合
    ``pytest-asyncio`` 的 function 级 loop scope，必须**每个测试**
    重建 engine，不能跨用例复用。
    """
    await dispose_db()
    init_db(settings)


