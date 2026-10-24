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

from app.db.models import AppRuntimeParam, SchedulerJobSetting
from app.db.session import session_scope
from app.services.job_settings import (
    load_crons,
    load_enabled_states,
    load_job_settings,
    save_cron,
    save_enabled_state,
)
from sqlalchemy import select


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
