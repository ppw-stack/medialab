"""废弃表删除逻辑的测试。

**合并说明（strmmanger 并入）**：

strmmanger 时代有两张废弃表：

- ``dav_acl`` —— 旧版自研 WebDAV 的路径 ACL 表（新版不做 ACL）
- ``job_runs`` —— 旧版**调度模块**的作业记录表（该调度模块已移除）

⚠️ **``job_runs`` 在合并后是辅种域的活跃表**（调度作业历史，见
:class:`app.db.models.JobRun`）——两个来源**表名相同但语义不同**。
删表操作**不可逆**，故合并后：

- ``dav_acl`` —— **照删**（确实是废弃表）
- ``job_runs`` —— **绝不删**（辅种域在用，删了调度历史全丢）

本测试同时固化这两条契约。

删表是**不可逆**操作，故重点验证：幂等、只删指定的表、不碰有数据的表。
"""

from __future__ import annotations

import pytest
from app.db.bootstrap import DbBootstrap
from sqlalchemy import text


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        return {r[0] for r in rows.all()}


async def _create_legacy_tables(engine) -> None:
    """模拟旧库：建出废弃的 ``dav_acl`` 表。

    ⚠️ **不建 ``job_runs``** —— 那个表名在合并后属于辅种域，见模块 docstring。
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE dav_acl ("
                " id INTEGER NOT NULL PRIMARY KEY,"
                " user_id INTEGER NOT NULL,"
                " path_prefix TEXT NOT NULL)"
            )
        )


async def test_obsolete_tables_dropped(db_engine):
    """废弃表 ``dav_acl`` 被删除。"""
    engine, _ = db_engine
    await _create_legacy_tables(engine)
    assert "dav_acl" in await _tables(engine)

    await DbBootstrap()._run_media_migrations()

    tables = await _tables(engine)
    assert "dav_acl" not in tables


async def test_job_runs_survives(db_engine):
    """**关键**：``job_runs`` **不能**被删——它是辅种域的调度历史表。

    合并前 strmmanger 的迁移会删它（那时的 ``job_runs`` 是它自己已移除的
    调度模块的残留）。合并后两个来源表名撞车，若照抄 strm 的删表逻辑，
    **辅种域的调度作业历史会被静默清空**。

    这条测试就是防这个的。
    """
    engine, _ = db_engine
    await DbBootstrap().create_all()

    # 先塞一行作业历史，确认数据也在
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO job_runs (job_name, trigger, started_at, ok) "
                "VALUES ('fetch_all_sites', 'cron', '2026-01-01 00:00:00', 1)"
            )
        )

    await DbBootstrap()._run_media_migrations()

    tables = await _tables(engine)
    assert "job_runs" in tables, "job_runs 被误删——辅种域的调度历史会丢"

    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT COUNT(*) FROM job_runs"))
        count = rows.scalar_one()
    assert count == 1, "job_runs 数据被清空"


async def test_current_tables_survive(db_engine):
    """**关键**：现役表**不能**被删。

    ``dav_users`` / ``dav_audit_log`` 是新版 WebDAV 在用的 —— 若误删，
    账号和审计全丢。
    """
    engine, _ = db_engine
    await DbBootstrap().create_all()

    tables = await _tables(engine)
    for name in (
        "dav_users",
        "dav_audit_log",
        "drivers",
        "media_files",
        "link_cache",
        "settings",
        "probe_results",
    ):
        assert name in tables, f"{name} 被误删"


async def test_dav_users_data_survives(db_engine):
    """``dav_users`` 里的数据不受删表影响。"""
    import time

    from app.db.models import DavUser
    from app.db.session import session_scope

    engine, _ = db_engine
    await _create_legacy_tables(engine)
    await DbBootstrap().create_all()

    async with session_scope() as db:
        db.add(
            DavUser(
                username="infuse",
                password_hash="pbkdf2$600000$aa$bb",
                is_active=True,
                last_login_at=0,
                created_at=int(time.time()),
                updated_at=int(time.time()),
            )
        )

    await DbBootstrap()._run_media_migrations()

    async with session_scope() as db:
        from sqlalchemy import select

        rows = (await db.execute(select(DavUser))).scalars().all()
    assert len(rows) == 1
    assert rows[0].username == "infuse"


async def test_migration_is_idempotent(db_engine):
    """重复跑不报错（表已不存在时直接跳过）。"""
    engine, _ = db_engine
    await _create_legacy_tables(engine)
    bootstrap = DbBootstrap()

    await bootstrap._run_media_migrations()
    await bootstrap._run_media_migrations()  # 第二次：表已没了
    await bootstrap._run_media_migrations()

    tables = await _tables(engine)
    assert "dav_acl" not in tables


async def test_absent_tables_are_fine(db_engine):
    """表本来就不存在（全新库）也不报错。"""
    engine, _ = db_engine
    await DbBootstrap().create_all()
    # 全新库里没有 ``dav_acl`` —— 迁移不应抛异常
    await DbBootstrap()._run_media_migrations()
    assert "dav_acl" not in await _tables(engine)


@pytest.mark.parametrize("table", ["dav_acl"])
async def test_each_table_dropped_independently(db_engine, table: str):
    """逐张删——只删指定的表，不误伤其它。

    「其它」用 ``job_runs`` 作参照——它是**活表**，绝不能被连带删掉。
    """
    engine, _ = db_engine
    await DbBootstrap().create_all()
    await _create_legacy_tables(engine)

    await DbBootstrap()._drop_obsolete_tables((table,))

    tables = await _tables(engine)
    assert table not in tables
    # 活表还在
    assert "job_runs" in tables
