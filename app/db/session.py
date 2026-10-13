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


def _extract_discount_pct_from_payload(payload: object) -> int | None:
    """从已落库的 free_payload 反推 discount_pct（用于历史回填）。

    支持 mteam status.discount == "PERCENT_N"，以及 NexusPHP
    free_classes 含 halfdown / thirtypercent / 50 / 30。
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, dict):
        disc = status.get("discount")
        if isinstance(disc, str):
            up = disc.upper()
            if up == "FREE":
                return 100
            if up.startswith("PERCENT_"):
                try:
                    return int(up.split("_", 1)[1])
                except (IndexError, ValueError):
                    return None
        if isinstance(disc, (int, float)):
            return abs(int(disc))
    free_classes = payload.get("free_classes")
    if isinstance(free_classes, list):
        for cls in free_classes:
            if not isinstance(cls, str):
                continue
            c = cls.lower()
            if c in ("halfdown", "twouphalfdown", "50"):
                return 50
            if c in ("thirtypercent", "30"):
                return 30
    return None


async def _backfill_discount_pct() -> None:
    """回填 torrent_index.discount_pct。

    仅更新 free_type ∈ {neutral_leech, freeleech_permanent} 的行；
    幂等：仅在 discount_pct IS NULL 时填，已填的不动。
    """
    from sqlalchemy import select, update

    from app.db.models import FreeType, TorrentIndex

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(
            TorrentIndex.id, TorrentIndex.free_type, TorrentIndex.free_payload
        ).where(
            TorrentIndex.free_type.in_(
                (FreeType.NEUTRAL, FreeType.PERMANENT)
            ),
            TorrentIndex.discount_pct.is_(None),
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return
        updates: list[tuple[int, int]] = []
        for row_id, free_type, free_payload in rows:
            if free_type == FreeType.PERMANENT:
                pct: int | None = 100
            else:
                pct = _extract_discount_pct_from_payload(free_payload)
            if pct is not None:
                updates.append((row_id, pct))
        if not updates:
            return
        # 批量更新（按值分组）
        from collections import defaultdict

        by_pct: dict[int, list[int]] = defaultdict(list)
        for row_id, pct in updates:
            by_pct[pct].append(row_id)
        for pct, ids in by_pct.items():
            await session.execute(
                update(TorrentIndex)
                .where(TorrentIndex.id.in_(ids))
                .values(discount_pct=pct)
            )
        await session.commit()
        logger.info("discount_pct 历史回填：%d 行", len(updates))


async def _backfill_published_at_tz() -> None:
    """把历史 published_at 从"站点本地时间"统一减 8h 转为 UTC。

    历史数据中所有 published_at 均按"原样"写入（即按各站点时区，CST/HKT
    无时区标记的字符串）。系统内部统一以 UTC 存储（``utcnow()``），两者
    存在 8h 偏移。回填等价于 ``new_published_at = old - 8h``。

    幂等：仅当**任一行仍存在 ``published_at > last_seen_at``** 时才回填——
    一旦所有行都满足 ``published_at <= last_seen_at``，说明已回填过。
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select, update

    from app.db.models import TorrentIndex

    factory = get_session_factory()
    async with factory() as session:
        # 仅当 last_seen_at 距今 < 30 天 才回填
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
        anomaly_count = (
            await session.execute(
                select(func.count())
                .select_from(TorrentIndex)
                .where(
                    TorrentIndex.published_at.is_not(None),
                    TorrentIndex.published_at > TorrentIndex.last_seen_at,
                    TorrentIndex.last_seen_at >= cutoff,
                )
            )
        ).scalar_one() or 0
        if anomaly_count == 0:
            return  # 已回填过
        stmt = select(TorrentIndex.id, TorrentIndex.published_at).where(
            TorrentIndex.published_at.is_not(None),
            TorrentIndex.last_seen_at >= cutoff,
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return
        shift = timedelta(hours=-8)
        from collections import defaultdict

        by_new: dict[datetime, list[int]] = defaultdict(list)
        for row_id, old_pub in rows:
            if old_pub is None:
                continue
            by_new[old_pub + shift].append(row_id)
        for new_pub, ids in by_new.items():
            await session.execute(
                update(TorrentIndex)
                .where(TorrentIndex.id.in_(ids))
                .values(published_at=new_pub)
            )
        await session.commit()
        logger.info("published_at 时区回填（-8h）：%d 行", len(rows))


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
                "ALTER TABLE free_candidates ADD COLUMN torrent_url VARCHAR(512)",
                "ALTER TABLE torrent_index ADD COLUMN discount_pct INTEGER",
                "ALTER TABLE downloader_instances DROP COLUMN download_root",
            ):
                with suppress(Exception):  # 列已存在
                    await conn.execute(text(stmt))
        logger.info("数据表已就绪")
        await _backfill_discount_pct()
        await _backfill_published_at_tz()

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