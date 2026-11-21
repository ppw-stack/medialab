"""站点 adapter 值迁移与回滚路径的契约测试（PR #27）。

覆盖：

- 正向迁移把旧 adapter 值改写成 ``generic``，并留下**回滚台账**；
- 台账只记首次（幂等重跑不会把 ``generic`` 当成旧值）；
- 回滚按台账逐行还原旧 adapter 值；
- 回滚能把「时魔」四列加回 ``site_user_data``。
"""

from __future__ import annotations

import pytest
from app.db.migrations import (
    ADAPTER_ROLLBACK_TABLE,
    migrate_adapter_values,
    readd_site_user_data_hourly_bonus_columns,
    restore_adapter_values,
)
from app.db.session import get_engine, session_scope
from sqlalchemy import text


async def _insert_site(name: str, adapter: str) -> int:
    """插入一个站点并返回它的 ID。"""
    async with session_scope() as session:
        result = await session.execute(
            text(
                "INSERT INTO sites (name, domain, adapter, enabled, created_at, updated_at) "
                "VALUES (:n, :d, :a, 1, :now, :now)"
            ),
            {"n": name, "d": f"https://{name}.example/", "a": adapter, "now": "2026-11-21 00:00:00"},
        )
        return int(result.lastrowid or 0)


async def _adapter_of(site_id: int) -> str | None:
    """读取站点的 adapter 值。"""
    async with session_scope() as session:
        result = await session.execute(
            text("SELECT adapter FROM sites WHERE id = :id"), {"id": site_id}
        )
        row = result.first()
        return row[0] if row else None


async def _ledger_rows() -> list[tuple[int, str]]:
    """读取回滚台账的全部行。"""
    async with session_scope() as session:
        result = await session.execute(
            text(f"SELECT site_id, old_adapter FROM {ADAPTER_ROLLBACK_TABLE} ORDER BY site_id")
        )
        return [(int(row[0]), str(row[1])) for row in result.all()]


async def _columns_of(table: str) -> set[str]:
    """读取表的列名集合。"""
    async with session_scope() as session:
        result = await session.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in result.all()}


@pytest.mark.asyncio
async def test_migration_rewrites_adapter_and_records_ledger(db: None) -> None:
    """旧 adapter 值被改写成 generic，且回滚台账记下原值。"""
    site_id = await _insert_site("mteam-site", "mteam")
    await migrate_adapter_values()
    assert await _adapter_of(site_id) == "generic"
    assert (site_id, "mteam") in await _ledger_rows()


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_keeps_ledger(db: None) -> None:
    """重复跑迁移不会把 generic 当成旧值写进台账。"""
    site_id = await _insert_site("hdwing-site", "hdwing")
    await migrate_adapter_values()
    first = await _ledger_rows()
    await migrate_adapter_values()
    second = await _ledger_rows()
    assert first == second
    assert (site_id, "hdwing") in second
    assert all(old != "generic" for _, old in second)


@pytest.mark.asyncio
async def test_restore_adapter_values_reverts_by_ledger(db: None) -> None:
    """回滚按台账还原旧 adapter 值。"""
    site_id = await _insert_site("hdsky-site", "hdsky")
    await migrate_adapter_values()
    assert await _adapter_of(site_id) == "generic"
    restored = await restore_adapter_values()
    assert restored >= 1
    assert await _adapter_of(site_id) == "hdsky"


@pytest.mark.asyncio
async def test_restore_is_noop_without_ledger(db: None) -> None:
    """台账不存在时回滚是安全的空操作。"""
    async with get_engine().begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {ADAPTER_ROLLBACK_TABLE}"))
    assert await restore_adapter_values() == 0


@pytest.mark.asyncio
async def test_readd_hourly_bonus_columns_is_idempotent(db: None) -> None:
    """回滚路径能把时魔四列加回来，且重复执行不报错。"""
    before = await _columns_of("site_user_data")
    assert "hourly_bonus" not in before
    added = await readd_site_user_data_hourly_bonus_columns()
    assert added == 4
    after = await _columns_of("site_user_data")
    assert {"hourly_bonus", "hourly_bonus_unit", "hourly_bonus_capped",
            "hourly_bonus_progress"} <= after
    assert await readd_site_user_data_hourly_bonus_columns() == 0
