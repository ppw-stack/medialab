"""DB 模型与迁移测试（Phase 0 集成层）。

覆盖建表、唯一约束、外键级联、幂等迁移。这些是「测试过了但 prod 崩」
的高发区——prod 的表结构是历史累积的（22 条 ALTER + 2 批时间迁移），
新库与老库结构必须一致。
"""

from __future__ import annotations

import pytest
from app.db.models import (
    CrossSeedOrphan,
    CrossSeedSnapshot,
    DownloaderInstance,
    Site,
    SiteUserData,
    TaskQueue,
    TorrentIndex,
)
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

# ------------------------------------------------------------------ 建表

async def test_create_all_builds_expected_tables(db: None) -> None:
    from app.db.session import get_engine

    def _tables(conn: object) -> set[str]:
        return set(inspect(conn).get_table_names())  # type: ignore[arg-type]

    async with get_engine().connect() as conn:
        tables = await conn.run_sync(_tables)
    expected = {
        "sites",
        "downloader_instances",
        "torrent_index",
        "task_queue",
        "app_runtime_params",
        "app_env_settings",
        "job_runs",
        "scheduler_job_settings",
        "site_user_data",
        "cross_seed_snapshot",
        "cross_seed_orphan",
    }
    assert expected <= tables


async def test_create_all_is_idempotent(db: None) -> None:
    """再跑一次建表不能报错（生产每次启动都会跑）。"""
    from app.db.bootstrap import db_bootstrap

    await db_bootstrap.create_all()


async def test_torrent_index_unique_site_item(db: None) -> None:
    """(site_id, item_id) 唯一——upsert 的基础。"""
    async with session_scope() as session:
        site = Site(name="s1", domain="d1", adapter="hdsky")
        session.add(site)
        await session.flush()
        session.add(TorrentIndex(site_id=site.id, item_id="1", name="a"))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            site = (
                await session.execute(select(Site).where(Site.name == "s1"))
            ).scalar_one()
            session.add(TorrentIndex(site_id=site.id, item_id="1", name="dup"))


async def test_task_queue_idem_key_unique(db: None) -> None:
    """idem_key 唯一——幂等入队的基础。"""
    async with session_scope() as session:
        session.add(TaskQueue(idem_key="k1", kind="add_torrent", payload={}))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(TaskQueue(idem_key="k1", kind="add_torrent", payload={}))


async def test_site_user_data_unique_site_id(db: None) -> None:
    """site_user_data 每站一行（UNIQUE site_id）——FR-1.6 当前快照语义。"""
    async with session_scope() as session:
        site = Site(name="s1", domain="d1", adapter="hdsky")
        session.add(site)
        await session.flush()
        session.add(SiteUserData(site_id=site.id))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            site = (
                await session.execute(select(Site).where(Site.name == "s1"))
            ).scalar_one()
            session.add(SiteUserData(site_id=site.id))


async def test_site_name_unique(db: None) -> None:
    async with session_scope() as session:
        session.add(Site(name="dup", domain="d", adapter="hdsky"))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(Site(name="dup", domain="d2", adapter="hdsky"))


# ------------------------------------------------------- 辅种快照 cross_seed

async def test_cross_seed_snapshot_unique_downloader_id(db: None) -> None:
    """辅种快照每下载器一行（UNIQUE downloader_id）——当前快照语义。"""
    async with session_scope() as session:
        dl = DownloaderInstance(
            name="dl1", type="qbittorrent", url="http://x", is_cross_seed=True
        )
        session.add(dl)
        await session.flush()
        session.add(CrossSeedSnapshot(downloader_id=dl.id))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            dl = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.name == "dl1"
                    )
                )
            ).scalar_one()
            session.add(CrossSeedSnapshot(downloader_id=dl.id))


async def test_deleting_downloader_cascades_snapshot(db: None) -> None:
    """删下载器 → 同事务删掉辅种快照（不留孤儿行）。

    FK 故意**不**带 ON DELETE CASCADE 当守卫——漏删会 IntegrityError，
    故删除流程必须显式清理。
    """
    from app.services.downloader_service import delete_downloader

    async with session_scope() as session:
        dl = DownloaderInstance(
            name="dl1", type="qbittorrent", url="http://x", is_cross_seed=True
        )
        session.add(dl)
        await session.flush()
        dl_id = dl.id
        session.add(CrossSeedSnapshot(downloader_id=dl_id))
        await session.flush()

    await delete_downloader(dl_id)

    async with session_scope() as session:
        rows = (await session.execute(select(CrossSeedSnapshot))).scalars().all()
        assert rows == [], "删下载器后不该留下孤儿快照行"


async def test_deleting_downloader_cascades_orphan_stats(db: None) -> None:
    """删下载器 → 同事务删掉孤儿统计行（不留孤儿行）。

    ``cross_seed_orphan`` 是**后加**的表（孤儿文件统计），FK 同样不带
    ``ON DELETE CASCADE``。删除流程最初只删了 ``cross_seed_snapshot``
    → 删有孤儿统计行的下载器直接 IntegrityError 500（实测 dev 环境
    删 tr-172 复现）。

    新增任何 ``downloader_id`` FK 表时**必须**同步加进级联删除，
    本测试就是那道守卫。
    """
    from app.services.downloader_service import delete_downloader

    async with session_scope() as session:
        dl = DownloaderInstance(
            name="dl1", type="qbittorrent", url="http://x", is_cross_seed=True
        )
        session.add(dl)
        await session.flush()
        dl_id = dl.id
        # 两张子表各一行——两张都必须被级联删
        session.add(CrossSeedSnapshot(downloader_id=dl_id))
        session.add(CrossSeedOrphan(downloader_id=dl_id))
        await session.flush()

    await delete_downloader(dl_id)

    async with session_scope() as session:
        snaps = (await session.execute(select(CrossSeedSnapshot))).scalars().all()
        orphans = (await session.execute(select(CrossSeedOrphan))).scalars().all()
        assert snaps == [], "删下载器后不该留下孤儿快照行"
        assert orphans == [], "删下载器后不该留下孤儿统计行"


async def test_unmarking_cross_seed_deletes_snapshot(db: None) -> None:
    """取消辅种标记 → 删掉快照（不再是辅种，快照无意义）。"""
    from app.services.downloader_service import update_downloader

    async with session_scope() as session:
        dl = DownloaderInstance(
            name="dl1", type="qbittorrent", url="http://x", is_cross_seed=True
        )
        session.add(dl)
        await session.flush()
        dl_id = dl.id
        session.add(CrossSeedSnapshot(downloader_id=dl_id))
        await session.flush()

    await update_downloader(dl_id, is_cross_seed=False)

    async with session_scope() as session:
        rows = (await session.execute(select(CrossSeedSnapshot))).scalars().all()
        assert rows == []


async def test_cross_seed_orphan_unique_downloader_id(db: None) -> None:
    """孤儿统计每下载器一行（UNIQUE downloader_id）——当前快照语义。"""
    async with session_scope() as session:
        dl = DownloaderInstance(
            name="dl1", type="qbittorrent", url="http://x", is_cross_seed=True
        )
        session.add(dl)
        await session.flush()
        session.add(CrossSeedOrphan(downloader_id=dl.id))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            dl = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.name == "dl1"
                    )
                )
            ).scalar_one()
            session.add(CrossSeedOrphan(downloader_id=dl.id))


# ------------------------------------------------------------------ 枚举

def test_free_type_values_are_stable() -> None:
    """free_type 字符串是 DB 里的持久化值，改名会丢历史数据。"""
    assert FreeType.TIMED == "freeleech_timed"
    assert FreeType.PERMANENT == "freeleech_permanent"
    assert FreeType.TWOU_TIMED == "twoup_timed"
    assert FreeType.NEUTRAL == "neutral_leech"
    assert FreeType.UNKNOWN == "unknown"
    assert FreeType.REFUND == "refund"


def test_push_status_values_are_stable() -> None:
    assert PushStatus.PUSHED == "pushed"
    assert PushStatus.FAILED == "failed"


# ------------------------------------------------------------------ 迁移

async def test_adapter_value_migration_is_idempotent(db: None) -> None:
    """generic → mteam 的值迁移跑两次都安全。"""
    from app.db.migrations import migrate_adapter_values

    await migrate_adapter_values()
    await migrate_adapter_values()

    async with session_scope() as session:
        rows = (await session.execute(select(Site))).scalars().all()
        assert all(r.adapter != "generic" for r in rows)


async def test_backfill_discount_pct_is_idempotent(db: None) -> None:
    """discount_pct 回填跑两次结果一致（不重复覆盖已填值）。"""
    from app.db.migrations import backfill_discount_pct

    async with session_scope() as session:
        site = Site(name="s1", domain="d1", adapter="hdsky")
        session.add(site)
        await session.flush()
        session.add(
            TorrentIndex(
                site_id=site.id,
                item_id="1",
                name="perm",
                free_type=FreeType.PERMANENT,
                discount_pct=None,
            )
        )
        await session.flush()

    await backfill_discount_pct()
    await backfill_discount_pct()

    async with session_scope() as session:
        row = (
            await session.execute(select(TorrentIndex).where(TorrentIndex.item_id == "1"))
        ).scalar_one()
        assert row.discount_pct == 100


# ------------------------------------------------------------------ 模块拆分

async def test_session_module_has_no_migration_logic(db: None) -> None:
    """session.py 只管 engine 生命周期——迁移逻辑应在 migrations.py。

    防止后续把迁移又塞回 session.py（那正是这次拆分要解决的问题）。
    """
    from pathlib import Path

    src = Path("app/db/session.py").read_text(encoding="utf-8")
    assert "ALTER TABLE" not in src
    assert "discount_pct" not in src
    assert "migrate_datetime" not in src


async def test_bootstrap_module_exists_and_exports(db: None) -> None:
    from app.db.bootstrap import DbBootstrap, db_bootstrap

    assert isinstance(db_bootstrap, DbBootstrap)
    assert hasattr(db_bootstrap, "create_all")
    assert hasattr(db_bootstrap, "healthcheck")


async def test_bootstrap_create_all_is_idempotent(db: None) -> None:
    """建表 + 迁移连跑两次不报错。"""
    from app.db.bootstrap import db_bootstrap

    await db_bootstrap.create_all()
    await db_bootstrap.create_all()


async def test_bootstrap_healthcheck_true(db: None) -> None:
    from app.db.bootstrap import db_bootstrap

    assert await db_bootstrap.healthcheck() is True


async def test_alter_statements_have_kinds(db: None) -> None:
    """每条 ALTER 必须声明类型——决定失败如何处理。

    之前 22 条 ALTER 全被 suppress(Exception) 包着，真失败也静默。
    """
    from app.db.bootstrap import _ALTER_STATEMENTS

    assert _ALTER_STATEMENTS
    kinds = {kind for kind, _ in _ALTER_STATEMENTS}
    assert kinds <= {"add_column", "drop_column", "rename_column", "create_index"}
    for kind, stmt in _ALTER_STATEMENTS:
        assert kind in kinds
        assert stmt.startswith("ALTER TABLE") or stmt.startswith("CREATE INDEX")


async def test_breaker_until_alter_present(db: None) -> None:
    """熔断字段的 ALTER 必须还在（拆分时别漏）。"""
    from app.db.bootstrap import _ALTER_STATEMENTS

    stmts = [s for _, s in _ALTER_STATEMENTS]
    assert any("ADD COLUMN breaker_until" in s for s in stmts)


# ------------------------------------------------------------------ 索引

async def test_expected_indexes_exist(db: None) -> None:
    from app.db.session import get_engine

    def _indexes(conn: object) -> set[str]:
        return {i["name"] for i in inspect(conn).get_indexes("torrent_index")}  # type: ignore[arg-type]

    async with get_engine().connect() as conn:
        names = await conn.run_sync(_indexes)
    assert "ix_torrent_index_push_status" in names
    assert "ix_torrent_index_last_seen" in names


# ------------------------------------------------------------------ 熔断字段

async def test_sites_has_breaker_until_column(db: None) -> None:
    """熔断状态落在 sites 表（站点级状态）。"""
    from app.db.session import get_engine

    def _cols(conn: object) -> list[str]:
        return [c["name"] for c in inspect(conn).get_columns("sites")]  # type: ignore[arg-type]

    async with get_engine().connect() as conn:
        cols = await conn.run_sync(_cols)
    assert "breaker_until" in cols


async def test_breaker_until_defaults_to_null(db: None) -> None:
    """新建站点默认未熔断。"""
    async with session_scope() as session:
        site = Site(name="s1", domain="d1", adapter="hdsky")
        session.add(site)
        await session.flush()
        assert site.breaker_until is None


async def test_breaker_until_is_persisted(db: None) -> None:
    async with session_scope() as session:
        site = Site(name="s1", domain="d1", adapter="hdsky", breaker_until="2026-10-21 20:00:00")
        session.add(site)
        await session.flush()
        site_id = site.id

    async with session_scope() as session:
        row = (await session.execute(select(Site).where(Site.id == site_id))).scalar_one()
        assert row.breaker_until == "2026-10-21 20:00:00"
