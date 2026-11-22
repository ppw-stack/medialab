"""调度作业配置落库的集成测试。

**锁住的核心契约**（2026-10-24）：

调度配置（cron + 启停）**全部落库**，页面改的跨重启保持：

1. ``POST /scheduler/{job}/cron`` → 写进 ``scheduler_job_settings.cron``
2. 启动期回填 → 重启后仍是用户改的值（不是代码默认值）
3. DB 里没记录 → 用代码里的默认 cron
4. **改 cron 不碰 enabled**（两列各自独立）

另：运行参数的默认值来自 ``PARAM_SPECS``，不再从 env 取（env 里已无对应配置）。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.db.models import AppRuntimeParam, SchedulerJobSetting
from app.db.session import session_scope
from app.services.job_settings import (
    load_crons,
    load_enabled_states,
    load_job_settings,
    merge_job_settings,
    migrate_renamed_job,
    prune_orphaned_settings,
    save_cron,
    save_enabled_state,
)
from sqlalchemy import select


def _boom_scope() -> Any:
    """替身：打开 session 时直接炸（模拟 DB 不可用）。

    Raises:
        RuntimeError: 始终抛出，用于模拟数据库不可用。
    """
    raise RuntimeError("炸了")


async def _rows() -> list[SchedulerJobSetting]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(SchedulerJobSetting).order_by(SchedulerJobSetting.job_name)
                )
            ).scalars()
        )


# ------------------------------------------------------------------ 落库


async def test_save_cron_creates_row(db: None) -> None:
    """保存 cron → 新建一行（enabled 默认 True）。"""
    await save_cron("j1", "15,45 * * * *")

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].job_name == "j1"
    assert rows[0].cron == "15,45 * * * *"
    assert rows[0].enabled is True


async def test_save_cron_updates_existing_row(db: None) -> None:
    """再次保存 → 覆盖同一行（upsert，不堆行）。"""
    await save_cron("j1", "15,45 * * * *")
    await save_cron("j1", "*/7 * * * *")

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].cron == "*/7 * * * *"


async def test_save_cron_does_not_touch_enabled(db: None) -> None:
    """改 cron 不该顺带改启停（两列各自独立）。

    反例：作业被用户停用了，用户改一下 cron 却把它启用了——那是 bug。
    """
    await save_enabled_state("j1", False)
    await save_cron("j1", "15,45 * * * *")

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].enabled is False, "改 cron 不该启用被停用的作业"
    assert rows[0].cron == "15,45 * * * *"


async def test_save_enabled_state_does_not_touch_cron(db: None) -> None:
    """改启停不该清掉 cron。"""
    await save_cron("j1", "15,45 * * * *")
    await save_enabled_state("j1", False)

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].cron == "15,45 * * * *"
    assert rows[0].enabled is False


# ------------------------------------------------------------------ 读取


async def test_load_crons_only_returns_non_null(db: None) -> None:
    """只返回 cron 非 NULL 的行（NULL = 用代码默认值，不该覆盖）。"""
    await save_enabled_state("j1", True)  # 没有 cron → NULL
    await save_cron("j2", "15,45 * * * *")

    crons = await load_crons()
    assert crons == {"j2": "15,45 * * * *"}


async def test_load_crons_empty_when_no_rows(db: None) -> None:
    """DB 空 → 空 dict（全部用代码默认值）。"""
    assert await load_crons() == {}


async def test_load_enabled_states(db: None) -> None:
    await save_enabled_state("j1", False)
    await save_enabled_state("j2", True)

    states = await load_enabled_states()
    assert states == {"j1": False, "j2": True}


async def test_load_job_settings_returns_both(db: None) -> None:
    """组合读取：启停 + cron 一次拿全。"""
    await save_cron("j1", "15,45 * * * *")
    await save_enabled_state("j2", False)

    settings = await load_job_settings()
    assert settings["j1"].cron == "15,45 * * * *"
    assert settings["j1"].enabled is True
    assert settings["j2"].enabled is False
    assert settings["j2"].cron is None


# ------------------------------------------------------------------ 运行参数默认值


async def test_runtime_params_seeded_from_spec_defaults(db: None) -> None:
    """运行参数的默认值来自 ``PARAM_SPECS``（不再是 env）。

    删掉 env 配置后，全新库启动时写入 DB 的必须是 spec 的默认值。
    """
    from app.services.runtime_params import (
        PARAM_SPECS,
        init_runtime_params_async,
        set_runtime_params,
    )

    mgr = await init_runtime_params_async()
    try:
        for spec in PARAM_SPECS:
            assert mgr.get_int(spec.key) == spec.default, (
                f"{spec.key} 的默认值应为 {spec.default}"
            )
    finally:
        set_runtime_params(mgr)

    async with session_scope() as session:
        rows = (
            await session.execute(select(AppRuntimeParam))
        ).scalars().all()
        stored = {r.key: r.value_int for r in rows}
    for spec in PARAM_SPECS:
        assert stored[spec.key] == spec.default


# ------------------------------------------------------------------ 改名迁移
#
# 作业合并/改名（2026-11-01：scan_cross_seed → cross_seed_maintenance）后，
# 用户手动改过的启停 + cron 不能丢——旧行的配置要迁到新作业名。


async def test_migrate_renamed_job_moves_config(db: None) -> None:
    """旧作业名的**启停状态**迁到新作业名。

    **cron 不搬**——cron 是作业级频率，合并后语义变了（三个旧作业合成
    一个），搬过去会让新作业跑在错误频率上；用代码里的新默认值。
    """
    async with session_scope() as session:
        session.add(
            SchedulerJobSetting(
                job_name="scan_cross_seed", enabled=False, cron="5,35 * * * *"
            )
        )
        await session.commit()

    migrated = await migrate_renamed_job(
        "scan_cross_seed", "cross_seed_maintenance", {"cross_seed_maintenance"}
    )
    assert migrated is True

    rows = {r.job_name: r for r in await _rows()}
    assert "scan_cross_seed" not in rows
    new_row = rows["cross_seed_maintenance"]
    # 启停状态迁过来了
    assert new_row.enabled is False
    # cron 不用旧值 → 用代码默认值（None = 不覆盖）
    assert new_row.cron is None


async def test_migrate_renamed_job_does_not_overwrite_existing(db: None) -> None:
    """目标作业已有行 → **不覆盖**（用户可能已在新作业上改过）。"""
    async with session_scope() as session:
        session.add(
            SchedulerJobSetting(job_name="scan_cross_seed", enabled=False, cron="x")
        )
        session.add(
            SchedulerJobSetting(
                job_name="cross_seed_maintenance", enabled=True, cron="y"
            )
        )
        await session.commit()

    await migrate_renamed_job(
        "scan_cross_seed", "cross_seed_maintenance", {"cross_seed_maintenance"}
    )

    rows = {r.job_name: r for r in await _rows()}
    assert "scan_cross_seed" not in rows
    # 新行保持用户改过的值
    assert rows["cross_seed_maintenance"].enabled is True
    assert rows["cross_seed_maintenance"].cron == "y"


async def test_migrate_renamed_job_noop_when_source_missing(db: None) -> None:
    """旧行不存在 → 不做事（不报错）。"""
    migrated = await migrate_renamed_job(
        "scan_cross_seed", "cross_seed_maintenance", {"cross_seed_maintenance"}
    )
    assert migrated is False


# ------------------------------------------------------------------ 孤儿清理


# ------------------------------------------------------------------ 作业合并


async def test_merge_job_settings_ands_enabled_states(db: None) -> None:
    """两个旧作业合成一个：启停取 **AND**，cron 用新默认值。

    2026-11-22 合并 `push_free_torrents` + `monitor_free_window`。任一边被
    用户停用过 → 新作业也停用（合并作业没有「只停半边」的开关，宁可不跑
    也不要静默复活用户关掉的行为）。
    """
    async with session_scope() as session:
        session.add(
            SchedulerJobSetting(
                job_name="push_free_torrents", enabled=True, cron="0 */2 * * *"
            )
        )
        session.add(
            SchedulerJobSetting(
                job_name="monitor_free_window",
                enabled=False,
                cron="5,15,25,35,45,55 * * * *",
            )
        )
        await session.commit()

    merged = await merge_job_settings(
        ["push_free_torrents", "monitor_free_window"],
        "push_and_monitor_free",
        {"push_and_monitor_free"},
    )
    assert merged is True

    rows = {r.job_name: r for r in await _rows()}
    assert set(rows) == {"push_and_monitor_free"}
    assert rows["push_and_monitor_free"].enabled is False
    # cron 不搬（旧的节奏对新作业是错的）→ 留空 = 用代码默认值
    assert rows["push_and_monitor_free"].cron is None


async def test_merge_job_settings_noop_without_old_rows(db: None) -> None:
    """旧行不存在（全新部署）→ 什么都没发生，也不凭空建行。"""
    assert await merge_job_settings(
        ["push_free_torrents"], "push_and_monitor_free", {"push_and_monitor_free"}
    ) is False
    assert await _rows() == []


async def test_prune_orphaned_settings_removes_unknown_jobs(db: None) -> None:
    """已下线作业的残留行被清掉（读出来没人用）。"""
    async with session_scope() as session:
        session.add(SchedulerJobSetting(job_name="scan_orphans", enabled=True))
        session.add(SchedulerJobSetting(job_name="disk_rotation", enabled=True))
        session.add(SchedulerJobSetting(job_name="daily_checkin", enabled=True))
        await session.commit()

    removed = await prune_orphaned_settings({"daily_checkin"})
    assert removed == 2

    rows = {r.job_name for r in await _rows()}
    assert rows == {"daily_checkin"}


async def test_prune_orphaned_settings_keeps_known_jobs(db: None) -> None:
    """已知作业的行不动。"""
    async with session_scope() as session:
        session.add(SchedulerJobSetting(job_name="daily_checkin", enabled=False))
        await session.commit()

    removed = await prune_orphaned_settings({"daily_checkin"})
    assert removed == 0
    assert {r.job_name for r in await _rows()} == {"daily_checkin"}


async def test_prune_orphaned_settings_keeps_protected_jobs(db: None) -> None:
    """``keep`` 里的作业名**不删**——迁移没成功时旧行要留着等下次重试。

    删掉等于把用户的启停状态永久丢了（新作业会按默认 enabled=True 复活）。
    """
    async with session_scope() as session:
        session.add(
            SchedulerJobSetting(
                job_name="push_free_torrents", enabled=False, cron="0 */2 * * *"
            )
        )
        session.add(SchedulerJobSetting(job_name="scan_orphans", enabled=True))
        await session.commit()

    removed = await prune_orphaned_settings(
        set(), keep={"push_free_torrents"}
    )

    assert removed == 1, "只有未被保护的孤儿行被删"
    rows = {r.job_name for r in await _rows()}
    assert rows == {"push_free_torrents"}


async def test_merge_failure_is_registered_as_startup_degradation(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并失败是**启动降级** → 必须登记到 ``startup_status``（/readyz 看得见）。"""
    from app.services import job_settings as js
    from app.services import startup_status

    startup_status.reset()
    monkeypatch.setattr(
        js,
        "session_scope",
        _boom_scope,
    )

    try:
        merged = await merge_job_settings(
            ["push_free_torrents"],
            "push_and_monitor_free",
            {"push_and_monitor_free"},
        )

        assert merged is False
        degradations = startup_status.snapshot()
        assert "job_settings_merge" in degradations
        assert "炸了" in degradations["job_settings_merge"]["reason"]
    finally:
        # 台账是进程级状态，别漏给别的用例（/readyz 的用例断言无降级）
        startup_status.reset()
