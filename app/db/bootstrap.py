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
    # 「下载中数」从契约移除（2026-10-31）——字段不再有业务价值
    ("drop_column", "ALTER TABLE site_user_data DROP COLUMN leeching_count"),
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
    # 辅种快照的暂停计数 (2026-10-24)：实际暂停数 ≠ 异常数
    # （已暂停的被跳过、失败的不算）——不存这两个字段前端只能说谎。
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN paused_count INTEGER DEFAULT 0",
    ),
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN pause_failed_count INTEGER DEFAULT 0",
    ),
    # 调度作业的 cron 落库 (2026-10-24)：页面改的 cron 跨重启保持。
    # NULL = 用代码里的默认值（该列可空，故不需要 DEFAULT）。
    (
        "add_column",
        "ALTER TABLE scheduler_job_settings ADD COLUMN cron VARCHAR(64)",
    ),
    # 辅种空间统计 (2026-10-24)：磁盘剩余/总量 + 辅种实际占用 + 目录分布。
    # 磁盘两列可空（qB 只给剩余空间、拿不到总量）。
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN disk_free_bytes BIGINT",
    ),
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN disk_total_bytes BIGINT",
    ),
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN used_bytes BIGINT DEFAULT 0",
    ),
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN total_size_bytes BIGINT DEFAULT 0",
    ),
    (
        "add_column",
        "ALTER TABLE cross_seed_snapshot ADD COLUMN by_dir JSON",
    ),
    # 注：辅种孤儿统计表 ``cross_seed_orphan`` 是本次**新加**的表——
    # ``create_all`` 会直接建，老库不需要 ALTER 迁移。
    # 孤儿统计的新列（空目录数 / 逐目录统计）：
    (
        "add_column",
        "ALTER TABLE cross_seed_orphan ADD COLUMN empty_dir_count INTEGER DEFAULT 0",
    ),
    (
        "add_column",
        "ALTER TABLE cross_seed_orphan ADD COLUMN scanned_dirs JSON",
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
    """建表 + 迁移编排。

    **无参**——engine 从全局 :func:`app.db.session.get_engine` 取。
    与 ptHelper 基线保持一致（strmmanger 原版是 ``DbBootstrap(engine)``，
    合并时统一为无参单例形式）。
    """

    async def create_all(self) -> None:
        """建表 + 跑迁移（幂等，每次启动调用）。"""
        from app.db.migrations import (
            backfill_discount_pct,
            backfill_upload_concurrency,
            delete_offline_env_settings,
            drop_obsolete_tables_once,
            migrate_adapter_values,
        )
        from app.db.models import Base

        engine = get_engine()
        await self._rebuild_organize_history()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 轻量迁移：create_all 不给已有表加列，补 ALTER（幂等）
            for kind, stmt in _ALTER_STATEMENTS:
                await self._run_alter(conn, kind, stmt)
            for stmt in _EXTRA_INDEXES:
                # IF NOT EXISTS 已保证幂等，这里失败就是真问题 → 让异常
                # 冒出去，engine.begin() 回滚整个 DDL 事务并阻断启动。
                await conn.execute(text(stmt))
        logger.info("数据表已就绪")
        await backfill_discount_pct()
        await backfill_upload_concurrency()
        await migrate_adapter_values()
        await delete_offline_env_settings()
        # 媒体库域的幂等迁移（补列 / 删废弃列）
        await self._run_media_migrations()
        # 不可逆删表走版本化一次性迁移（先备份，user_version < 3 才跑）
        await drop_obsolete_tables_once()

    async def _rebuild_organize_history(self) -> None:
        """旧结构的 ``organize_history``（绑定下载器）→ DROP 重建。

        该表随「整理与刮削迁移」新增，**上线前就改了判重模型**
        （下载器任务 → 源文件路径，见 :mod:`app.services.organize_service`）。
        旧表里只可能有当天测试产生的空数据，直接重建最干净——SQLite 的
        UNIQUE 约束没法用 ALTER 摘掉，重建是唯一改法。

        只在检测到 ``downloader_id`` 列（旧结构标志）时才动表，幂等。
        """
        engine = get_engine()
        async with engine.begin() as conn:
            rows = (
                await conn.execute(text("PRAGMA table_info(organize_history)"))
            ).all()
            if not rows:
                return  # 表不存在 → create_all 会建新结构
            columns = {row[1] for row in rows}
            if "downloader_id" not in columns:
                return  # 已是新结构
            # 先备份再删（同一事务）；create_all 随后按新模型重建。
            from app.db.migrations import backup_and_drop_table

            await backup_and_drop_table(conn, "organize_history")
            logger.info(
                "已重建旧结构的 organize_history（判重模型改为 source_path）"
            )

    async def _run_media_migrations(self) -> None:
        """媒体库域的幂等迁移（来自 strmmanger）。

        ``create_all`` 只建**新表**，不给已有表加列——老库的列增删必须靠
        这里补齐。所有操作都**幂等**（重复跑不报错）。

        覆盖的变更：

        - ``drivers`` 表补媒体库配置列（``media_cid`` / ``output_dir`` …）
        - ``upload_tasks`` 表新增 ``delete_after_upload``、删除 ``watch_subdir``
        - 删除废弃列 ``drivers.naming_rule``

        ⚠️ ``job_runs`` **是辅种域在用的表**（调度作业历史）——strmmanger
        时代的 ``job_runs`` 是它自己的调度模块残留，两者**表名相同但语义
        不同**。辅种域的 ``job_runs`` 有真实数据，**绝不能删**。
        故此处**不删** ``job_runs``——见下方注释。

        废弃表（``scan_rules`` / ``dav_acl`` / ``quota_ledger``）的删除
        已移到 :func:`app.db.migrations.drop_obsolete_tables_once`——
        版本化一次性执行 + 先备份，见 :meth:`create_all`。
        """
        await self._migrate_driver_config()
        await self._migrate_aggregate_groups()
        await self._migrate_secondary_staging()
        await self._migrate_upload_tasks()
        await self._migrate_scrape_tables()
        await self._drop_obsolete_columns()
        # ``drivers.staging_root_id``（副盘缓冲根）挪到了组级表
        # ``aggregate_group_roots``——聚合的目录映射两端都属组，不混进存储源
        # 配置。存量全为 NULL（从未使用），删列安全。
        await self._drop_column("drivers", "staging_root_id")
        # 废弃表不在这里删——见 create_all() 末尾的 drop_obsolete_tables_once()。

    async def _migrate_driver_config(self) -> None:
        """给 ``drivers`` 表补齐媒体库配置列（幂等）。

        **锁表评估**（2026-11-19 加列迁移必写）：SQLite 的
        ``ALTER TABLE ... ADD COLUMN`` 是**纯元数据操作**——只改 schema 页，
        **不重写表数据、不触发表重建**；写锁窗口是**毫秒级**（GB 级表上
        同样瞬时），**无需停机**。本批新增列**全部可空且无默认值** →
        **不回填、不改写任何现有行** → 无数据迁移风险。
        **回滚路径**：① 列可空 + 无默认值 → 旧代码读不到它们，**留着无害**，
        升级失败**直接回退镜像**即可（首选，零 DB 操作）；② 彻底删列走
        ``_drop_column``（SQLite ≥ 3.35 的 ``DROP COLUMN``，老版本失败无害
        ——列残留对旧代码同样无害）；③ 兜底恢复 Unraid appdata 备份快照。
        """
        columns: tuple[tuple[str, str, str | None], ...] = (
            ("media_cid", "TEXT", None),
            ("media_path", "TEXT", None),
            ("output_dir", "TEXT", None),
            (
                "video_exts",
                "TEXT",
                "'mkv,mp4,avi,mov,wmv,flv,m4v,ts,m2ts,rmvb,rm,webm,iso'",
            ),
            ("image_exts", "TEXT", "''"),
            ("data_exts", "TEXT", "''"),
            ("min_size_mb", "INTEGER", "0"),
            ("include_subdirs", "INTEGER", "1"),
            # 自动清理「网盘上已消失」的条目——默认关（清理不可逆）
            ("auto_prune", "INTEGER", "0"),
            ("last_scan_at", "VARCHAR(32)", None),
            ("last_synced_count", "INTEGER", "0"),
            # 聚合（主从）配置 (2026-11-15)：组归属 + 组内角色。
            # NULL = 独立存储源，行为与引入聚合前完全一致。
            ("aggregate_group_id", "INTEGER", None),
            ("role", "VARCHAR(16)", None),
            # 本月上传额度已耗尽的月份键（2026-11-19）：撞墙探测用。
            # NULL = 未耗尽；跨月自动失效（键 ≠ 当前月即正常）。
            ("upload_quota_exhausted_month", "VARCHAR(16)", None),
        )
        await self._add_columns("drivers", columns)

    async def _migrate_upload_tasks(self) -> None:
        """给 ``upload_tasks`` 表做列变更（幂等）。

        - 新增 ``delete_after_upload``（同步成功后清理本地文件）
        - 新增 ``conflict_policy``（同名且大小不同时的冲突策略，
          默认 1 = 保留大的）
        - 删除 ``watch_subdir`` —— 去掉白名单后，监听目录改成**完整路径**
          （``watch_root``），拆成「根 + 子路径」没有意义。
        - 删除 ``last_sync_at`` / ``last_uploaded_count`` / ``last_error``
          —— 统计一律是**内存运行态**，这三列曾长期「只写不读」
          （前端只渲染运行态），写进 DB 只是给 SQLite 单写者添堵。

        **锁表评估 / 回滚路径**：见 :meth:`_migrate_driver_config` 的
        docstring（同款 ``ADD COLUMN`` 元数据操作；``aggregate_group_id``
        同样**可空 + 无默认值** → 不回填、不改写现有行；回滚首选
        「留列无害 + 回退镜像」）。
        """
        await self._add_columns(
            "upload_tasks",
            (
                ("delete_after_upload", "INTEGER", "0"),
                # 默认 1 = 保留大的（见 models.UploadTask.conflict_policy）
                ("conflict_policy", "INTEGER", "1"),
                # 目标聚合组（2026-11-19）：非空 = 「本地同步到网盘聚合」，
                # 落点由分配器在组内副盘之间自动挑选。
                ("aggregate_group_id", "INTEGER", None),
                # 目标子目录（2026-11-20）：聚合模式下作为**组映射的子路径
                # 覆盖**。可空 + 无默认值 → 不回填、不改写现有行；存量行
                # 全是 NULL → 行为与引入前完全一致。
                ("aggregate_sub_path", "TEXT", None),
            ),
        )
        await self._drop_column("upload_tasks", "watch_subdir")
        await self._drop_column("upload_tasks", "last_sync_at")
        await self._drop_column("upload_tasks", "last_uploaded_count")
        await self._drop_column("upload_tasks", "last_error")

    async def _migrate_aggregate_groups(self) -> None:
        """给 ``aggregate_groups`` 补「归拢目标目录」两列（幂等）。

        归拢落点是**组自己的配置**（不是主盘的媒体库目录）——副盘缓冲根下的
        相对路径原样铺到它下面。
        """
        await self._add_columns(
            "aggregate_groups",
            (
                ("target_dir_id", "VARCHAR(64)", None),
                ("target_dir_path", "TEXT", None),
            ),
        )

    async def _migrate_secondary_staging(self) -> None:
        """给 ``secondary_staging`` 补「搬运通道」列（幂等）。

        ``cloud_download`` / ``share_transfer``——小文件走分享转存时**不消耗
        云添加额度**，看板要能分开统计两条通道各搬了多少。
        """
        await self._add_columns(
            "secondary_staging",
            (("transfer_mode", "VARCHAR(24)", None),),
        )

    async def _migrate_scrape_tables(self) -> None:
        """给 ``media_scrape`` 表补列（幂等）。

        新增 ``media_path``——被刮削的**媒体文件**路径。「一键修正」需要
        从刮削记录回到原媒体文件（否则只能让用户手敲路径）。
        """
        await self._add_columns("media_scrape", (("media_path", "TEXT", None),))

    async def _drop_obsolete_columns(self) -> None:
        """删除已废弃的列（幂等）。

        - ``drivers.naming_rule`` —— 命名规则已移除，``.strm`` 文件名
          直接沿用网盘原文件名
        """
        await self._drop_column("drivers", "naming_rule")

    async def _add_columns(
        self,
        table: str,
        columns: tuple[tuple[str, str, str | None], ...],
    ) -> None:
        """给表补列（幂等）。``columns`` = (列名, DDL 类型, 默认值字面量)。"""
        engine = get_engine()
        async with engine.begin() as conn:
            existing = {
                row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
            }
            if not existing:
                # 表还不存在（全新库）——``create_all`` 会建，这里跳过
                return
            added = 0
            for name, ddl_type, default in columns:
                if name in existing:
                    continue
                default_clause = f" DEFAULT {default}" if default is not None else ""
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}{default_clause}")
                )
                added += 1
            if added:
                logger.info("%s 表新增 %d 个配置列", table, added)

    async def _drop_column(self, table: str, column: str) -> None:
        """删列（幂等）。老版本 SQLite（<3.35）不支持 DROP COLUMN——失败无害。"""
        engine = get_engine()
        async with engine.begin() as conn:
            existing = {
                row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
            }
            if column not in existing:
                return
            try:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                logger.info("已删除废弃列 %s.%s", table, column)
            except Exception as exc:
                # 老版本 SQLite（<3.35）不支持 DROP COLUMN——
                # 留着不影响功能（模型里已无该字段）
                logger.warning("删除列 %s.%s 失败（不影响功能）：%s", table, column, exc)

    async def _drop_obsolete_tables(self, names: tuple[str, ...]) -> None:
        """备份并删除指定废弃表（低层入口，供测试 / 显式调用）。

        启动链路**不要**直接调它——用
        :func:`app.db.migrations.drop_obsolete_tables_once` 的版本化入口。
        """
        from app.db.migrations import backup_and_drop_table

        engine = get_engine()
        async with engine.begin() as conn:
            for name in names:
                await backup_and_drop_table(conn, name)

    async def _run_alter(self, conn: object, kind: str, stmt: str) -> None:
        """跑一条 ALTER，按类型区分「预期失败」与「真失败」。

        之前 22 条 ALTER 全被 ``suppress(Exception)`` 包着——**真失败**
        （列不存在、SQL 语法错）也被静默吞掉，日志里什么都看不到。

        现在的处理：

        - ``add_column``   —— 列已存在是幂等重跑的**正常**结果（静默）；
          其它失败是真问题 → **抛错阻断启动**
        - ``drop_column``   —— 老库可能没这列，失败无害（debug 静默）
        - ``rename_column`` —— 新库无 ``*_enc`` 列，失败无害（debug 静默）
        - ``create_index``  —— 用了 IF NOT EXISTS，失败是真问题 → 抛错

        旧实现把所有真失败都只记 error 然后继续启动，DB 可能停在
        「缺列 / 缺索引」的半迁移状态，而 /readyz 仍显示健康。
        """
        try:
            await conn.execute(text(stmt))  # type: ignore[attr-defined]
        except Exception as exc:
            msg = str(exc).lower()
            if kind == "add_column" and "duplicate column name" in msg:
                return  # 幂等重跑，预期内
            if kind in ("drop_column", "rename_column"):
                # 全新库本就没有这些废弃列 → 每次建库都会失败 N 次。
                # 属预期内、无害（add_column / create_index 的真失败仍会
                # 走到下面的 raise，fail-fast 语义不变），降到 debug 避免
                # 刷屏；保留了 stmt + exc，排查老库迁移问题时仍能开启。
                logger.debug("删列/改名失败（老库可能无此列，可忽略）：%s → %s", stmt, exc)
                return
            raise RuntimeError(f"数据库迁移失败（{kind}）：{stmt}") from exc

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
