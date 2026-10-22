"""建表与启动迁移编排（§8）。

从 :mod:`app.db.session` 拆出——原先 576 行的文件混了 4 种职责
（engine 管理 / 幂等 ALTER / datetime 迁移 / 回填），职责边界不清。

本模块只管「建表 + 跑迁移」的**编排**，具体迁移逻辑在
:mod:`app.db.migrations`。

关于「单写者队列」：本类原先叫 ``SingleWriterQueue``，其 ``write()``
方法**全仓零调用**——所有写路径走 :func:`app.db.session.session_scope`。
真正的写安全靠「短事务 + 不在事务内做网络 IO」的约定
（见 :mod:`app.services.jobs`），不是靠队列。故本类改名为
:class:`DbBootstrap`，职责收窄为「建表 + 迁移编排 + 健康检查」。
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import get_engine, session_scope

logger = get_logger(__name__)

__all__ = ["DbBootstrap", "db_bootstrap"]


#: 幂等 ALTER 语句（建表后补列删列改名）。
#:
#: ``create_all`` 只建新表，**不给已有表加列**——老库的列增删要靠这些
#: ALTER 补齐。每条都必须幂等（重复跑不报错）。
#:
#: 分类决定失败处理（见 :meth:`DbBootstrap.create_all`）：
#:
#: - ``add_column``   —— 列已存在是正常的（幂等重跑）
#: - ``drop_column``   —— 老库可能没这列，失败无害
#: - ``rename_column`` —— 新库无 ``*_enc`` 列，rename 会失败
#: - ``create_index``  —— 用 ``IF NOT EXISTS``，失败是真问题
_ALTER_STATEMENTS: tuple[tuple[str, str], ...] = (
    (
        "add_column",
        "ALTER TABLE torrent_index ADD COLUMN discount_pct INTEGER",
    ),
    (
        "drop_column",
        "ALTER TABLE downloader_instances DROP COLUMN download_root",
    ),
    (
        "drop_column",
        "ALTER TABLE torrent_index DROP COLUMN torrent_url",
    ),
    # 凭据字段去加密：*_enc → 明文列名（值保持原样，由一次性
    # 迁移脚本/app 读取时按需处理；新库无此列，rename 会失败被 suppress）
    (
        "rename_column",
        "ALTER TABLE sites RENAME COLUMN rss_url_enc TO rss_url",
    ),
    (
        "rename_column",
        "ALTER TABLE sites RENAME COLUMN apikey_enc TO apikey",
    ),
    (
        "rename_column",
        "ALTER TABLE sites RENAME COLUMN cookies_enc TO cookies",
    ),
    (
        "rename_column",
        "ALTER TABLE downloader_instances RENAME COLUMN password_enc TO password",
    ),
    (
        "add_column",
        "ALTER TABLE downloader_instances ADD COLUMN save_path VARCHAR(512)",
    ),
    (
        "add_column",
        "ALTER TABLE torrent_index ADD COLUMN push_status VARCHAR(16)",
    ),
    (
        "add_column",
        "ALTER TABLE torrent_index ADD COLUMN pushed_at VARCHAR(32)",
    ),
    # 下载器辅种标记字段 (FR 跨种/辅种监测, 2026-10-18)
    (
        "add_column",
        "ALTER TABLE downloader_instances ADD COLUMN is_cross_seed BOOLEAN DEFAULT 0",
    ),
    # 站点熔断截止时刻 (FR-2.2 熔断生效, 2026-10-21)
    (
        "add_column",
        "ALTER TABLE sites ADD COLUMN breaker_until VARCHAR(32)",
    ),
    # 站点最小拉取间隔字段删除（7ef7c68 已移除 FR-2.2 下限逻辑，
    # 字段成死配置：存在/可配/校验但拉取作业从不读 → 误导用户）
    (
        "drop_column",
        "ALTER TABLE sites DROP COLUMN min_interval_minutes",
    ),
    # 站点用户数据契约精简 (FR-1.6 v2)：移除 username/userid/level/join_at/
    # bonus/ratio/seeding_size/leeching_size/message_unread/hr_status
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN username"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN userid"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN user_level"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN join_at"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN bonus"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN ratio"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN seeding_size"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN leeching_size"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN message_unread"),
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN hr_status"),
    # 站点用户数据契约 (FR-1.6)：补兼容历史库缺索引
    (
        "create_index",
        "CREATE INDEX IF NOT EXISTS ix_site_user_data_site_seen "
        "ON site_user_data(site_id, last_seen_at)",
    ),
    # 时魔字段 (FR-1.6 增补)：每小时获得的站点积分 + 是否封顶
    ("add_column", "ALTER TABLE site_user_data ADD COLUMN hourly_bonus REAL"),
    (
        "add_column",
        "ALTER TABLE site_user_data ADD COLUMN hourly_bonus_unit VARCHAR(16)",
    ),
    (
        "add_column",
        "ALTER TABLE site_user_data ADD COLUMN hourly_bonus_capped BOOLEAN",
    ),
    (
        "add_column",
        "ALTER TABLE site_user_data ADD COLUMN hourly_bonus_progress REAL",
    ),
)

#: 独立创建的索引（不随上面的 ALTER 循环走）
_EXTRA_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_torrent_index_push_status "
    "ON torrent_index(push_status)",
    "CREATE INDEX IF NOT EXISTS ix_job_runs_job_started "
    "ON job_runs(job_name, started_at)",
    "CREATE INDEX IF NOT EXISTS ix_job_runs_job_name ON job_runs(job_name)",
    "CREATE INDEX IF NOT EXISTS ix_job_runs_started_at ON job_runs(started_at)",
)


class DbBootstrap:
    """建表 + 迁移编排。"""

    async def create_all(self) -> None:
        """建表 + 跑迁移（幂等，每次启动调用）。"""
        from app.db.migrations import (
            backfill_discount_pct,
            migrate_adapter_values,
            migrate_datetime_to_bj_str,
        )
        from app.db.models import Base

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 轻量迁移：create_all 不给已有表加列，补 ALTER（幂等）
            for kind, stmt in _ALTER_STATEMENTS:
                await self._run_alter(conn, kind, stmt)
            for stmt in _EXTRA_INDEXES:
                # 用了 IF NOT EXISTS，失败是真问题 → 记 error 但不阻断启动
                try:
                    await conn.execute(text(stmt))
                except Exception as exc:
                    logger.error("创建索引失败（%s）：%s", stmt, exc)
        logger.info("数据表已就绪")
        # 迁移历史 DateTime 列 → 北京时间字符串
        await migrate_datetime_to_bj_str()
        await backfill_discount_pct()
        await migrate_adapter_values()

    async def _run_alter(self, conn: object, kind: str, stmt: str) -> None:
        """跑一条 ALTER，按类型区分「预期失败」与「真失败」。

        之前 22 条 ALTER 全被 ``suppress(Exception)`` 包着——**真失败**
        （列不存在、SQL 语法错）也被静默吞掉，日志里什么都看不到。

        现在的处理：

        - ``add_column``   —— 列已存在是幂等重跑的**正常**结果（静默）
        - ``drop_column``   —— 老库可能没这列，失败无害（warning）
        - ``rename_column`` —— 新库无 ``*_enc`` 列，失败无害（warning）
        - ``create_index``  —— 用了 IF NOT EXISTS，失败是真问题（error）
        """
        try:
            await conn.execute(text(stmt))  # type: ignore[attr-defined]
        except Exception as exc:
            msg = str(exc).lower()
            if kind == "add_column" and "duplicate column name" in msg:
                return  # 幂等重跑，预期内
            if kind == "add_column":
                logger.error("加列失败（不是「列已存在」，需排查）：%s → %s", stmt, exc)
            elif kind in ("drop_column", "rename_column"):
                logger.warning("删列/改名失败（老库可能无此列，可忽略）：%s → %s", stmt, exc)
            else:
                logger.error("执行失败：%s → %s", stmt, exc)

    async def healthcheck(self) -> bool:
        """数据库健康检查（/healthz 用）。"""
        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("数据库健康检查失败：%s", exc)
            return False


#: 全局实例（lifespan 启动时调用 create_all）
db_bootstrap = DbBootstrap()
