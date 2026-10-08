"""告警中心测试（FR-4.3：去重、风暴、静默、分级）。"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.core.timeutils import utcnow
from app.services.alerter import (
    Alerter,
    AlertPolicyConfig,
    dedupe_key,
    in_quiet_hours,
    parse_quiet_hours,
)


# ------------------------------------------------------------ 工具
def test_dedupe_key_format() -> None:
    assert dedupe_key("ratio_low", "abc") == "ratio_low:abc"
    assert dedupe_key("ratio_low", None) == "ratio_low:-"


def test_parse_quiet_hours_night() -> None:
    s, e = parse_quiet_hours("23:00-07:00")
    assert s == time(23, 0)
    assert e == time(7, 0)


def test_parse_quiet_hours_invalid() -> None:
    assert parse_quiet_hours("not-a-time") is None
    assert parse_quiet_hours(None) is None
    assert parse_quiet_hours("a-b-c") is None


def test_in_quiet_hours_normal_range() -> None:
    spec = "09:00-17:00"
    assert in_quiet_hours(spec, datetime(2026, 1, 1, 10, 0)) is True
    assert in_quiet_hours(spec, datetime(2026, 1, 1, 8, 0)) is False


def test_in_quiet_hours_cross_midnight() -> None:
    spec = "23:00-07:00"
    assert in_quiet_hours(spec, datetime(2026, 1, 1, 2, 0)) is True
    assert in_quiet_hours(spec, datetime(2026, 1, 1, 10, 0)) is False
    assert in_quiet_hours(spec, datetime(2026, 1, 1, 23, 30)) is True


# ------------------------------------------------------------ 去重
async def test_first_alert_queued(db) -> None:
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    async with session_scope() as s:
        alerter = Alerter(s, PC(mute_window_hours=1))
        await alerter.record("warning", "ratio_low", "msg1", info_hash="a")
        plan = await alerter.plan(
            [("warning", "ratio_low", "a"), ("warning", "ratio_low", "a")]
        )
    assert plan.suppressed == 2, "同一 key 在 mute window 内全部压制"
    assert plan.alerts == []


async def test_different_keys_not_deduped(db) -> None:
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    async with session_scope() as s:
        alerter = Alerter(s, PC(mute_window_hours=1))
        await alerter.record("warning", "ratio_low", "x", info_hash="a")
        await alerter.record("warning", "ratio_low", "y", info_hash="b")
        plan = await alerter.plan(
            [
                ("warning", "ratio_low", "a"),
                ("warning", "ratio_low", "b"),
                ("warning", "ratio_low", "c"),  # 全新，未压制
            ]
        )
    assert plan.suppressed == 2  # a, b 已在窗口内


async def test_dedupe_window_expires(db) -> None:
    """窗口过期后必须能再次推送——否则持续问题会永远沉默。"""
    from datetime import timedelta

    from app.db.models import Alert
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC
    from sqlalchemy import select

    async with session_scope() as s:
        alerter = Alerter(s, PC(mute_window_hours=1))
        await alerter.record("warning", "ratio_low", "x", info_hash="a")

    # 把 ts 与 muted_until 都改到过去 → 窗口已过
    async with session_scope() as s:
        alert = (
            await s.execute(select(Alert).where(Alert.dedupe_key == "ratio_low:a"))
        ).scalar_one()
        past = utcnow() - timedelta(hours=2)
        alert.ts = past
        alert.muted_until = past

    async with session_scope() as s:
        alerter = Alerter(s, PC(mute_window_hours=1))
        plan = await alerter.plan([("warning", "ratio_low", "a")])
    assert plan.suppressed == 0


# ------------------------------------------------------------ 风暴保护
async def test_storm_protection_collects_into_one(db) -> None:
    """单日超过阈值时所有候选降级为单条汇总推送。"""
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    async with session_scope() as s:
        # 预存 50 条告警触发风暴
        for i in range(50):
            await s.merge(
                # 复用 record，但 plan 才是测试目标
                # 简单起见直接插入 Alert
                __import__("app.db.models", fromlist=["Alert"]).Alert(
                    level="warning", category="x", message=str(i),
                    dedupe_key=f"x:{i}", muted_until=utcnow() + timedelta(hours=1),
                )
            )
        alerter = Alerter(s, PC(storm_threshold=50))
        plan = await alerter.plan(
            [("critical", "ratio_low", "h1"), ("critical", "torrent_error", "h2")]
        )
    assert plan.storm is True
    assert len(plan.alerts) == 1
    assert plan.alerts[0].category == "storm"
    assert "50" in plan.alerts[0].message


async def test_below_storm_threshold_passes_through(db) -> None:
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    async with session_scope() as s:
        alerter = Alerter(s, PC(storm_threshold=100))
        plan = await alerter.plan(
            [("critical", "ratio_low", "h1"), ("critical", "ratio_low", "h2")]
        )
    assert plan.storm is False
    assert plan.alerts


# ------------------------------------------------------------ 分级
async def test_critical_bypasses_quiet_hours(db) -> None:
    """🔴 绕过静默时段——夜间正是端口被关、限速被改的时段。"""
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    quiet_now = datetime(2026, 1, 1, 3, 0)  # 03:00
    async with session_scope() as s:
        alerter = Alerter(s, PC(quiet_hours="02:00-06:00", critical_immediate=True))
        plan = await alerter.plan(
            [("critical", "port_unreachable", None)], now=quiet_now
        )
    assert plan.quiet is True
    assert plan.alerts, "🔴 必须绕过静默时段"


async def test_warning_suppressed_in_quiet_hours(db) -> None:
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    quiet_now = datetime(2026, 1, 1, 3, 0)
    async with session_scope() as s:
        alerter = Alerter(s, PC(quiet_hours="02:00-06:00", warning_daily_digest=False))
        plan = await alerter.plan(
            [("warning", "ratio_low", "h")], now=quiet_now
        )
    assert plan.suppressed >= 1


async def test_critical_bypasses_daily_digest(db) -> None:
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    async with session_scope() as s:
        alerter = Alerter(s, PC(critical_immediate=True, warning_daily_digest=False))
        plan = await alerter.plan([("critical", "x", "h")])
    # critical 不应被 daily digest 政策压制
    assert any(a for a in plan.alerts if a.category == "x")


# ------------------------------------------------------------ 落库
async def test_record_sets_mute_until_and_dedupe_key(db) -> None:
    from app.db.models import Alert
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC
    from sqlalchemy import select

    async with session_scope() as s:
        alerter = Alerter(s, PC(mute_window_hours=6))
        await alerter.record("warning", "ratio_low", "msg", info_hash="abc")
        rows = (await s.execute(select(Alert))).scalars().all()
    assert len(rows) == 1
    assert rows[0].dedupe_key == "ratio_low:abc"
    assert rows[0].muted_until is not None


async def test_record_truncates_long_message(db) -> None:
    from app.db.models import Alert
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC
    from sqlalchemy import select

    async with session_scope() as s:
        alerter = Alerter(s, PC())
        await alerter.record("info", "x", "x" * 5000)
    async with session_scope() as s:
        msg = (await s.execute(select(Alert.message))).scalar_one()
    assert len(msg) <= 4000


# ------------------------------------------------------------ 查询
async def test_today_stats_aggregates(db) -> None:
    from app.db.session import session_scope
    from app.services.alerter import AlertPolicyConfig as PC

    async with session_scope() as s:
        alerter = Alerter(s, PC())
        for _ in range(3):
            await alerter.record("warning", "x", "m")
        await alerter.record("critical", "y", "m")
        stats = await alerter.today_stats()
    assert stats.get("warning") == 3
    assert stats.get("critical") == 1


# ------------------------------------------------------------ 默认配置
def test_default_policy_is_sane() -> None:
    p = AlertPolicyConfig()
    assert p.mute_window_hours == 6
    assert p.storm_threshold == 50
    assert p.warning_daily_digest is True
    assert p.critical_immediate is True


def test_mute_window_is_timedelta() -> None:
    p = AlertPolicyConfig(mute_window_hours=2)
    assert p.mute_window == timedelta(hours=2)


def test_mute_window_never_negative() -> None:
    p = AlertPolicyConfig(mute_window_hours=-5)
    assert p.mute_window == timedelta(0)