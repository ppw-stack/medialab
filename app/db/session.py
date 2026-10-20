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
from typing import cast

from sqlalchemy import CursorResult, event, text
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
            result = cast(CursorResult[object], await session.execute(stmt))
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


#: 需要从 UTC 迁移为北京时间字符串的表 → 时间列名列表。
#: 迁移逻辑：对每张表用 PRAGMA table_info 检查列类型，若含 DATETIME/TIMESTAMP
#: 列则重建表（SQLite 不支持 ALTER COLUMN TYPE），把 UTC 值 +8h 转为北京时间字符串。
#:
#: 按**批次**组织：批次号 = 该批迁移完成后的 ``PRAGMA user_version`` 值。
#: 批次 N 的表只在 ``user_version < N`` 时迁移——已迁过的库（user_version=1）
#: 不会重复 +8h，而后续新增的表（如批次 2 的 site_user_data）仍会被迁移。
#: 新增表时**追加新批次**，不要改旧批次（否则已迁库会重复迁移）。
_DATETIME_MIGRATION_BATCHES: dict[int, dict[str, list[str]]] = {
    1: {
        "downloader_instances": ["created_at", "updated_at"],
        "sites": ["created_at", "updated_at"],
        "torrent_index": [
            "free_end_at", "published_at", "first_seen_at",
            "last_seen_at", "pushed_at",
        ],
        "task_queue": ["run_at", "created_at", "updated_at"],
        "app_runtime_params": ["updated_at"],
        "job_runs": ["started_at", "finished_at"],
    },
    2: {
        # site_user_data 表在批次 1 之后才加入（FR-1.6 站点用户数据契约），
        # 老库的 user_version 已是 1 —— 必须独立批次才能补迁。
        "site_user_data": ["last_seen_at", "created_at", "updated_at"],
    },
}


async def _migrate_datetime_to_bj_str() -> None:
    """把历史 DateTime 列的 UTC 值 +8h 转为北京时间字符串（幂等）。

    SQLite 不支持 ALTER COLUMN TYPE，需用"新建临时表 → 复制（+8h）→ 删旧 →
    重命名"策略。列类型已是 TEXT/VARCHAR 则跳过。

    幂等检测：PRAGMA user_version 标记。注意：UPDATE 只改值不改列声明类型，
    table_info 永远报 DATETIME，故列类型检查不能作为幂等依据，必须用
    数据库级标记。

    **按批次执行**（见 :data:`_DATETIME_MIGRATION_BATCHES`）：批次 N 只在
    ``user_version < N`` 时跑，批次完成即把 user_version 置为 N。这样新增
    表（如 site_user_data）可以独立成批补迁，而已迁过的老表不会被重复
    +8h。中途崩溃则重跑该批次（已迁表会重复 +8h，属可接受的小概率，
    远好于每次启动都重复迁移）。
    """
    from datetime import UTC, timedelta

    factory = get_session_factory()

    async with factory() as session:
        ver = (await session.execute(text("PRAGMA user_version"))).scalar_one()
        current = int(ver or 0)
        for batch_no in sorted(_DATETIME_MIGRATION_BATCHES):
            if current >= batch_no:
                continue  # 该批次已完成
            specs = _DATETIME_MIGRATION_BATCHES[batch_no]
            for table_name, columns in specs.items():
                # 检查表是否存在
                result = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
                    {"n": table_name},
                )
                if result.first() is None:
                    continue

                # 检查列类型
                pragma_result = await session.execute(
                    text(f"PRAGMA table_info({table_name})")
                )
                cols_info = pragma_result.all()
                needs_migration = False
                # 推断主键列：PRAGMA 第 5 字段 pk > 0 即主键；多列主键取第一列
                pk_col: str = "id"
                for col in cols_info:
                    col_name = col[1]
                    col_type = (col[2] or "").upper()
                    if col_name in columns and col_type in ("DATETIME", "TIMESTAMP", "DATE"):
                        needs_migration = True
                    # col 索引: (cid, name, type, notnull, dflt_value, pk)
                    if len(col) >= 6 and col[5] and isinstance(col[5], int) and col[5] > 0:
                        pk_col = col_name
                if not needs_migration:
                    continue

                logger.info(
                    "开始迁移表 %s 的 DateTime 列 → 北京时间字符串（PK=%s）",
                    table_name, pk_col,
                )

                # SQLite datetime 存储格式：
                # 1) "YYYY-MM-DD HH:MM:SS" (naive UTC)
                # 2) "YYYY-MM-DD HH:MM:SS.ffffff" (naive UTC, 微秒)
                # 迁移：把每列的 UTC 值 +8h，去掉微秒后缀，格式统一为 "yyyy-MM-dd HH:mm:ss"
                from datetime import datetime

                for col_name in columns:
                    # 逐行更新：解析旧 UTC 值 → +8h → 写回字符串
                    # 用 SQLite 的 datetime() 函数做批量更新（如果可用）
                    # SQLite 没有 datetime() 函数，用字符串操作：
                    # "YYYY-MM-DD HH:MM:SS" + 8h 不容易用纯 SQL 做。
                    # 所以用 Python 逐行处理（数据量通常不大）。
                    rows = (
                        await session.execute(
                            text(
                                f"SELECT {pk_col}, {col_name} FROM {table_name} "
                                f"WHERE {col_name} IS NOT NULL"
                            )
                        )
                    ).all()
                    if not rows:
                        continue

                    from collections import defaultdict

                    by_new_val: dict[str, list[object]] = defaultdict(list)
                    for row_pk, old_val in rows:
                        if old_val is None:
                            continue
                        try:
                            old_str = str(old_val).strip()
                            # 处理可能的微秒后缀
                            if "." in old_str:
                                old_str = old_str.split(".")[0]
                            # 尝试解析
                            try:
                                old_dt = datetime.strptime(old_str, "%Y-%m-%d %H:%M:%S")
                            except ValueError:
                                try:
                                    old_dt = datetime.fromisoformat(old_str)
                                except ValueError:
                                    continue
                            # UTC → +8h → 北京时间字符串

                            aware_utc = old_dt.replace(tzinfo=UTC)
                            bj_str = (aware_utc + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
                            by_new_val[bj_str].append(row_pk)
                        except Exception:
                            continue

                    for new_val, pks in by_new_val.items():
                        placeholders = ",".join(f":pk{i}" for i in range(len(pks)))
                        params: dict[str, object] = {"new_val": new_val}
                        for i, rpk in enumerate(pks):
                            params[f"pk{i}"] = rpk
                        await session.execute(
                            text(
                                f"UPDATE {table_name} SET {col_name} = :new_val "
                                f"WHERE {pk_col} IN ({placeholders})"
                            ),
                            params,
                        )
                    if by_new_val:
                        logger.info(
                            "表 %s 列 %s：迁移 %d 行 UTC → 北京时间",
                            table_name, col_name, sum(len(v) for v in by_new_val.values()),
                        )

                await session.commit()
                logger.info("表 %s DateTime 迁移完成", table_name)
            # 批次完成 → 写入幂等标记（PRAGMA 赋值立即持久化，非事务性）
            await session.execute(text(f"PRAGMA user_version = {batch_no}"))
            logger.info("DateTime 迁移批次 %d 完成（user_version=%d）", batch_no, batch_no)



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
            for stmt in (
                "ALTER TABLE torrent_index ADD COLUMN discount_pct INTEGER",
                "ALTER TABLE downloader_instances DROP COLUMN download_root",
                "ALTER TABLE torrent_index DROP COLUMN torrent_url",
                # 凭据字段去加密：*_enc → 明文列名（值保持原样，由一次性
                # 迁移脚本/app 读取时按需处理；新库无此列，rename 会失败被 suppress）
                "ALTER TABLE sites RENAME COLUMN rss_url_enc TO rss_url",
                "ALTER TABLE sites RENAME COLUMN apikey_enc TO apikey",
                "ALTER TABLE sites RENAME COLUMN cookies_enc TO cookies",
                "ALTER TABLE downloader_instances RENAME COLUMN password_enc TO password",
                "ALTER TABLE downloader_instances ADD COLUMN save_path VARCHAR(512)",
                "ALTER TABLE torrent_index ADD COLUMN push_status VARCHAR(16)",
                "ALTER TABLE torrent_index ADD COLUMN pushed_at VARCHAR(32)",
                # 下载器辅种标记字段 (FR 跨种/辅种监测, 2026-10-18)
                "ALTER TABLE downloader_instances ADD COLUMN is_cross_seed BOOLEAN DEFAULT 0",
                # 站点用户数据契约精简 (FR-1.6 v2)：移除 username/userid/level/join_at/
                # bonus/ratio/seeding_size/leeching_size/message_unread/hr_status
                "ALTER TABLE site_user_data DROP COLUMN username",
                "ALTER TABLE site_user_data DROP COLUMN userid",
                "ALTER TABLE site_user_data DROP COLUMN user_level",
                "ALTER TABLE site_user_data DROP COLUMN join_at",
                "ALTER TABLE site_user_data DROP COLUMN bonus",
                "ALTER TABLE site_user_data DROP COLUMN ratio",
                "ALTER TABLE site_user_data DROP COLUMN seeding_size",
                "ALTER TABLE site_user_data DROP COLUMN leeching_size",
                "ALTER TABLE site_user_data DROP COLUMN message_unread",
                "ALTER TABLE site_user_data DROP COLUMN hr_status",
                # 站点用户数据契约 (FR-1.6)：补兼容历史库缺索引
                "CREATE INDEX IF NOT EXISTS ix_site_user_data_site_seen "
                "ON site_user_data(site_id, last_seen_at)",
            ):
                with suppress(Exception):  # 列已存在 / 已是新名
                    await conn.execute(text(stmt))
            with suppress(Exception):  # 索引已存在
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_torrent_index_push_status "
                    "ON torrent_index(push_status)"
                ))
            with suppress(Exception):  # job_runs 表索引
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_job_runs_job_started "
                    "ON job_runs(job_name, started_at)"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_job_runs_job_name "
                    "ON job_runs(job_name)"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_job_runs_started_at "
                    "ON job_runs(started_at)"
                ))
        logger.info("数据表已就绪")
        # 迁移历史 DateTime 列 → 北京时间字符串
        await _migrate_datetime_to_bj_str()
        await _backfill_discount_pct()

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
