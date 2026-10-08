"""冷却阶梯测试（FR-2.3.1）。

覆盖：阶梯递增、reason 独立计数、永久冷却、NO_COOLDOWN 不入冷却、
同站独立冷却、人工解除。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.timeutils import utcnow
from app.db.models import CooldownReason
from app.services.cooldown import (
    NO_COOLDOWN,
    CooldownService,
    ladder_for,
)


@pytest.fixture
async def cooldown(db):
    """构造一个带站点的冷却服务。"""

    from app.db.models import Site
    from app.db.session import session_scope
    from app.services.cooldown import CooldownService as CS

    async with session_scope() as session:
        site = Site(name="pt1", domain="d1.test")
        session.add(site)
        await session.flush()
        site_id = site.id

    async with session_scope() as session:
        yield CS(session), site_id


# ------------------------------------------------------------ 阶梯表
def test_ladder_increases_monotonically() -> None:
    lad = ladder_for(CooldownReason.WINDOW_EXPIRED, 1)
    for n in (2, 3, 4):
        assert ladder_for(CooldownReason.WINDOW_EXPIRED, n) > lad
        lad = ladder_for(CooldownReason.WINDOW_EXPIRED, n)


def test_ladder_caps_at_longest() -> None:
    a = ladder_for(CooldownReason.WINDOW_EXPIRED, 99)
    b = ladder_for(CooldownReason.WINDOW_EXPIRED, 4)
    assert a == b


def test_permanent_reason_returns_huge_duration() -> None:
    assert ladder_for(CooldownReason.USER_BLACKLISTED, 1) > timedelta(days=365 * 50)


def test_zero_fail_count_falls_back_to_first_rung() -> None:
    assert ladder_for(CooldownReason.WINDOW_EXPIRED, 0) == ladder_for(
        CooldownReason.WINDOW_EXPIRED, 1
    )


def test_unknown_reason_has_no_ladder() -> None:
    assert ladder_for("mystery", 3) == timedelta(0)


# ------------------------------------------------------------ 行为
async def test_first_failure_blocks(cooldown) -> None:
    svc, site_id = cooldown
    async with db_session() as session:
        s = CooldownService(session)
        await s.record(site_id, "A" * 40, CooldownReason.WINDOW_EXPIRED)
    assert svc is not None


async def test_record_then_is_blocked(db) -> None:

    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    async with session_scope() as session:
        svc = CooldownService(session)
        h = "b" * 40
        assert (await svc.is_blocked(sid, h)).blocked is False
        await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)

    async with session_scope() as session:
        svc = CooldownService(session)
        verdict = await svc.is_blocked(sid, h)
        assert verdict.blocked is True
        assert verdict.reason == CooldownReason.WINDOW_EXPIRED
        assert verdict.fail_count == 1


async def test_fail_count_increments_per_reason(db) -> None:
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    h = "c" * 40
    for expected in (1, 2, 3):
        async with session_scope() as session:
            svc = CooldownService(session)
            r = await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)
            assert r.fail_count == expected


async def test_reason_change_resets_counter(db) -> None:
    """不同 reason 的 fail_count 独立计数，不得"连坐"（FR-2.3.1）。"""
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    h = "d" * 40
    async with session_scope() as session:
        svc = CooldownService(session)
        await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)
        await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)
        assert (await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)).fail_count == 3

    async with session_scope() as session:
        svc = CooldownService(session)
        r = await svc.record(sid, h, CooldownReason.DEAD_TORRENT)
        assert r.fail_count == 1, "换 reason 后阶梯必须重置"


@pytest.mark.parametrize("reason", sorted(NO_COOLDOWN))
async def test_no_cooldown_reasons_do_not_block(db, reason: str) -> None:
    """disk_full / duplicate / manual_skip 不得入冷却（FR-2.3.1）。

    否则空间恢复或规则变更后仍无法辅种，直接损失收益。
    """
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    h = "e" * 40
    async with session_scope() as session:
        svc = CooldownService(session)
        r = await svc.record(sid, h, reason)
        assert r.blocked is False

    async with session_scope() as session:
        svc = CooldownService(session)
        assert (await svc.is_blocked(sid, h)).blocked is False


async def test_expired_cooldown_auto_clears(db) -> None:
    from app.db.models import CooldownHash, Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    h = "f" * 40
    async with session_scope() as session:
        svc = CooldownService(session)
        await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)

    # 人为把到期时间改到过去
    async with session_scope() as session:
        row = (
            await session.execute(
                CooldownHash.__table__.select()
                .where(CooldownHash.info_hash == h)
                .with_only_columns(CooldownHash.id)
            )
        ).first()
        assert row is not None
        obj = await session.get(CooldownHash, row[0])
        assert obj is not None
        obj.cooldown_until = utcnow() - timedelta(seconds=1)

    async with session_scope() as session:
        svc = CooldownService(session)
        assert (await svc.is_blocked(sid, h)).blocked is False


async def test_blacklist_is_manual_only(db) -> None:
    """拉黑后即使到期也不自动解除（FR-2.3.1）。"""
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    h = "9" * 40
    async with session_scope() as session:
        svc = CooldownService(session)
        await svc.blacklist(sid, h)

    async with session_scope() as session:
        svc = CooldownService(session)
        v = await svc.is_blocked(sid, h)
        assert v.blocked is True
        assert v.permanent is True


async def test_manual_remove_unblocks(db) -> None:
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    h = "7" * 40
    async with session_scope() as session:
        svc = CooldownService(session)
        await svc.record(sid, h, CooldownReason.WINDOW_EXPIRED)

    async with session_scope() as session:
        svc = CooldownService(session)
        assert await svc.remove(sid, h) == 1
        assert (await svc.is_blocked(sid, h)).blocked is False


async def test_cooldown_is_per_site(db) -> None:
    """同站独立冷却：一站冷却不影响另一站（FR-2.3.1）。"""
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        a = Site(name="a", domain="a.test")
        b = Site(name="b", domain="b.test")
        session.add_all([a, b])
        await session.flush()
        aid, bid = a.id, b.id

    h = "8" * 40
    async with session_scope() as session:
        svc = CooldownService(session)
        await svc.record(aid, h, CooldownReason.DEAD_TORRENT)

    async with session_scope() as session:
        svc = CooldownService(session)
        assert (await svc.is_blocked(aid, h)).blocked is True
        assert (await svc.is_blocked(bid, h)).blocked is False, "另一站不应受影响"


async def test_hash_case_insensitive(db) -> None:
    """InfoHash 大小写归一：RSS 常见大写 hex。"""
    from app.db.models import Site
    from app.db.session import session_scope

    async with session_scope() as session:
        site = Site(name="pt", domain="d.test")
        session.add(site)
        await session.flush()
        sid = site.id

    lower = "a1b2c3d4" * 5
    upper = lower.upper()
    async with session_scope() as session:
        svc = CooldownService(session)
        await svc.record(sid, lower, CooldownReason.DEAD_TORRENT)

    async with session_scope() as session:
        svc = CooldownService(session)
        assert (await svc.is_blocked(sid, upper)).blocked is True


def db_session():
    """便捷别名（保留 fixture 引用）。"""
    from app.db.session import session_scope

    return session_scope()