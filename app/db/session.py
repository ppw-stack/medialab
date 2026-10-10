"""异步 SQLAlchemy engine 与会话（§8 SQLite 约定）。

三条硬约定：

1. ``journal_mode=WAL`` + ``foreign_keys=ON``：巡检写不阻塞面板读。
2. **单写者队列**：SQLite 不支持多进程并发写，所有写操作经进程内队列串行化。
3. 下载器写操作在队列之上再叠加**按实例的操作锁**（非功能·并发安全），
   见 :mod:`app.adapters.downloaders.locks`。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import event, text
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


#: 站点 adapter 迁移：从旧值映射到新值。空 dict 表示无迁移。
ADAPTER_VALUE_MIGRATIONS: dict[str, str] = {
    "generic": "mteam",  # 旧通用 adapter → m-team 专用 adapter（重构后唯一支持的实现）
}


async def migrate_adapter_values() -> None:
    """把 :data:`ADAPTER_VALUE_MIGRATIONS` 指定的旧值批量改写为新值（幂等）。

    启动时跑一次：若旧值已不存在则无副作用。
    """
    if not ADAPTER_VALUE_MIGRATIONS:
        return
    factory = get_session_factory()
    from sqlalchemy import update

    from app.db.models import Site

    async with factory() as session:
        for old_value, new_value in ADAPTER_VALUE_MIGRATIONS.items():
            stmt = (
                update(Site)
                .where(Site.adapter == old_value)
                .values(adapter=new_value)
            )
            result = await session.execute(stmt)
            if result.rowcount:
                logger.info(
                    "站点 adapter 迁移：%s → %s，影响 %d 行",
                    old_value, new_value, result.rowcount,
                )
        await session.commit()


class SingleWriterQueue:
    """进程内单写者串行队列（§8 约定 2）。

    SQLite 单写者限制在**多连接**时才会触发 ``database is locked``。巡检、
    免费种高频看护、WebSocket 推送并发写同一库时，串行化可完全避免该错误，
    且比依赖 ``busy_timeout`` 重试更可预测。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._depth = 0

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """获取写会话。**不要**在持锁期间做网络 IO。"""
        factory = get_session_factory()
        async with self._lock, factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        """建表（幂等）。"""
        from app.db.models import Base

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 轻量迁移：create_all 不给已有表加列，补 ALTER（幂等）
            from sqlalchemy import text

            for stmt in (
                "ALTER TABLE torrents ADD COLUMN zero_seed_rounds INTEGER DEFAULT 0",
                "ALTER TABLE sites ADD COLUMN cookies_enc TEXT",
            ):
                with suppress(Exception):  # 列已存在
                    await conn.execute(text(stmt))
        logger.info("数据表已就绪")

    async def healthcheck(self) -> bool:
        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("数据库健康检查失败：%s", exc)
            return False


#: 全局单写者队列
writer = SingleWriterQueue()