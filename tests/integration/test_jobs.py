"""站点拉取作业集成测试（Phase 0）。

**锁住 2026-10-21 prod 事故的修复**：hddolby 站持续 DNS 解析失败，
但作业结果里 ``failed_sites`` 恒为 0，summary 是「拉取 9 站，写入
2550 条种子」——故障完全被掩盖。

根因是两层都吞：
1. adapter 内部 ``except httpx.HTTPError: return []``（已修，9 站全改）
2. ``jobs.py`` 的统计只看 upsert 阶段的 DB 错误（已修）

这里用假适配器把两层一起钉住。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from app.adapters.sites.base import FeedItem, SiteAdapter, SiteHealth, SiteProfile
from app.core.timeutils import bj_now, bj_now_dt, format_bj, parse_bj
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.jobs import fetch_all_sites
from sqlalchemy import select


class _FakeAdapter(SiteAdapter):
    """可控的假站点适配器。

    ``mode`` 决定 ``search_torrents`` 的行为：
    - ``"ok"``     → 返回若干条目
    - ``"empty"``  → 返回 []（站点确实没种子，**不算失败**）
    - ``"raise"``  → 抛 SiteFetchError（故障，**必须计入失败站**）
    - ``"breaker"``→ 抛 BreakerTripped（限流，**熔断跳过**，不算失败）
    - ``"breaker403"`` → 同上，但 403（熔断时长减半）
    """

    adapter_type = "fake"

    def __init__(self, profile: SiteProfile, *, mode: str = "ok", **_: object) -> None:
        super().__init__(profile)
        self.mode = mode
        self.calls = 0

    async def check_reachability(self) -> SiteHealth:
        return SiteHealth(reachable=True, logged_in=True)

    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 100,
        page_number: int = 1,
    ) -> list[FeedItem]:
        self.calls += 1
        if self.mode == "raise":
            from app.adapters.sites.base import SiteFetchError

            raise SiteFetchError("Name or service not known")
        if self.mode == "breaker":
            from app.adapters.sites.base import BreakerTripped

            raise BreakerTripped("拉取返回 429，需熔断")
        if self.mode == "breaker403":
            from app.adapters.sites.base import BreakerTripped

            raise BreakerTripped("拉取返回 403，需熔断")
        if self.mode == "empty":
            return []
        if page_number > 1:
            return []
        return [
            FeedItem(
                title=f"torrent-{self.profile.name}-{i}",
                link="",
                item_id=f"{i}",
                size=1024 * (i + 1),
                description="剩余 2 天",
                free_payload={"free_classes": ["free"], "remaining_text": "剩余 2 天"},
            )
            for i in range(3)
        ]

    async def download_torrent(self, item_id: str) -> bytes:
        return b""

    async def get_torrent_url(self, item_id: str) -> str | None:
        return None

    async def check_status(self, item_id: str) -> object:
        from app.adapters.sites.base import TorrentBrief

        return TorrentBrief.UNKNOWN

    async def get_torrent_detail(self, item_id: str) -> object:
        from app.adapters.sites.base import TorrentDetail

        return TorrentDetail(item_id=item_id)


@pytest.fixture
def factory_with(monkeypatch: pytest.MonkeyPatch):
    """构造工厂，按站点名注入不同行为的假适配器。"""

    def _make(modes: dict[str, str]) -> AdapterFactory:
        f = AdapterFactory()

        def _site(row: Site, *, cookies: str | None = None) -> _FakeAdapter:
            mode = modes.get(row.name, "ok")
            return _FakeAdapter(
                SiteProfile(name=row.name, domain=row.domain), mode=mode
            )

        monkeypatch.setattr(f, "site", _site)
        return f

    return _make


async def _add_sites(*names: str) -> None:
    async with session_scope() as session:
        for n in names:
            session.add(Site(name=n, domain=f"{n}.test", adapter="hdsky"))
        await session.flush()


# ------------------------------------------------------------------ 正常

async def test_fetch_all_sites_writes_index(db: None, factory_with) -> None:
    await _add_sites("s1", "s2")
    factory = factory_with({"s1": "ok", "s2": "ok"})
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 0
    assert result["scanned_sites"] == 2
    assert result["total_written"] == 6  # 每站 3 条

    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert len(rows) == 6


async def test_fetch_all_sites_empty_site_is_not_a_failure(
    db: None, factory_with
) -> None:
    """站点确实没种子 → 正常，不算失败站。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "empty"})
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 0
    assert result["total_written"] == 0


# ------------------------------------------- 核心：故障必须被统计成失败

async def test_fetch_failure_is_counted_as_failed_site(
    db: None, factory_with
) -> None:
    """站点故障（抛 SiteFetchError）→ 必须计入 failed_sites。

    这是 2026-10-21 prod 事故的核心回归测试。
    """
    await _add_sites("s1", "s2")
    factory = factory_with({"s1": "ok", "s2": "raise"})
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 1, "故障站必须被统计为失败"
    assert "s2" in str(result["summary"]), "summary 必须点名失败站"
    # 好站仍要正常写入
    assert result["total_written"] == 3


async def test_all_sites_failing_reports_all_failed(db: None, factory_with) -> None:
    await _add_sites("s1", "s2", "s3")
    factory = factory_with({"s1": "raise", "s2": "raise", "s3": "raise"})
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 3
    assert result["total_written"] == 0


async def test_failure_does_not_block_other_sites(db: None, factory_with) -> None:
    """单站故障不阻塞其他站（异常隔离）。"""
    await _add_sites("bad", "good")
    factory = factory_with({"bad": "raise", "good": "ok"})
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 1
    assert result["total_written"] == 3

    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert len(rows) == 3
        assert {r.site_id for r in rows} == {2}  # 只有 good 站


async def test_failed_site_not_written_to_index(db: None, factory_with) -> None:
    """故障站的数据不应进索引（避免脏数据被下游当有效种子用）。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "raise"})
    await fetch_all_sites(factory)

    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert rows == []


async def test_steps_report_failure(db: None, factory_with) -> None:
    """步骤卡片里「拉取种子列表」应是 warn 状态（前端据此染色）。"""
    await _add_sites("s1", "s2")
    factory = factory_with({"s1": "ok", "s2": "raise"})
    result = await fetch_all_sites(factory)

    steps = result["steps"]
    assert isinstance(steps, list)
    pull_step = next(s for s in steps if s.name == "拉取种子列表")
    assert pull_step.status == "warn"


async def test_steps_ok_when_no_failure(db: None, factory_with) -> None:
    await _add_sites("s1")
    factory = factory_with({"s1": "ok"})
    result = await fetch_all_sites(factory)

    pull_step = next(s for s in result["steps"] if s.name == "拉取种子列表")
    assert pull_step.status == "ok"


# ------------------------------------------------------------------ 熔断

async def test_breaker_tripped_opens_breaker(db: None, factory_with) -> None:
    """限流（429）→ 写入 sites.breaker_until。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "breaker"})
    result = await fetch_all_sites(factory)

    async with session_scope() as session:
        row = (
            await session.execute(select(Site).where(Site.name == "s1"))
        ).scalar_one()
        assert row.breaker_until is not None, "熔断后必须写 breaker_until"
        assert row.breaker_until > bj_now(), "breaker_until 应在未来"
    assert result["skipped_breaker"] == 1


async def test_breaker_is_not_counted_as_failed(db: None, factory_with) -> None:
    """熔断是保护性跳过，**不是**故障——不能进 failed_sites。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "breaker"})
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 0
    assert result["skipped_breaker"] == 1
    assert "熔断" in str(result["summary"])


async def test_breaker_skip_does_not_send_request(db: None, factory_with) -> None:
    """熔断期内跳过该站且**不发请求**——继续请求只会加速账号暴露。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "breaker"})
    await fetch_all_sites(factory)  # 第一轮：触发熔断

    # 第二轮：应被熔断跳过，适配器不被调用
    adapter = _FakeAdapter(SiteProfile(name="s1", domain="s1.test"), mode="ok")
    factory2 = AdapterFactory()

    def _site(row: Site, *, cookies: str | None = None) -> _FakeAdapter:
        return adapter

    factory2.site = _site  # type: ignore[method-assign]
    result = await fetch_all_sites(factory2)

    assert adapter.calls == 0, "熔断期内不应发起请求"
    assert result["skipped_breaker"] == 1
    assert result["total_written"] == 0


async def test_expired_breaker_is_ignored(db: None, factory_with) -> None:
    """熔断已过期 → 正常拉取。"""
    expired = format_bj(bj_now_dt() - timedelta(minutes=1))
    async with session_scope() as session:
        session.add(
            Site(name="s1", domain="s1.test", adapter="hdsky", breaker_until=expired)
        )
        await session.flush()

    factory = factory_with({"s1": "ok"})
    result = await fetch_all_sites(factory)

    assert result["skipped_breaker"] == 0
    assert result["total_written"] == 3


async def test_breaker_does_not_block_other_sites(db: None, factory_with) -> None:
    """A 站熔断不影响 B 站正常拉取。"""
    await _add_sites("bad", "good")
    factory = factory_with({"bad": "breaker", "good": "ok"})
    result = await fetch_all_sites(factory)

    assert result["skipped_breaker"] == 1
    assert result["failed_sites"] == 0
    assert result["total_written"] == 3, "好站仍要正常写入"

    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert {r.site_id for r in rows} == {2}  # 只有 good 站


async def test_breaker_403_has_shorter_duration(db: None, factory_with) -> None:
    """403 熔断时长减半（30min）——403 可能是 cookie 失效而非限流。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "breaker403"})
    await fetch_all_sites(factory)

    async with session_scope() as session:
        row = (
            await session.execute(select(Site).where(Site.name == "s1"))
        ).scalar_one()
        until = parse_bj(row.breaker_until)
        assert until is not None
        minutes = (until - bj_now_dt()).total_seconds() / 60
        assert 29 <= minutes <= 31


async def test_breaker_429_has_full_duration(db: None, factory_with) -> None:
    """429 明确是限流 → 熔断 1 小时。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "breaker"})
    await fetch_all_sites(factory)

    async with session_scope() as session:
        row = (
            await session.execute(select(Site).where(Site.name == "s1"))
        ).scalar_one()
        until = parse_bj(row.breaker_until)
        assert until is not None
        minutes = (until - bj_now_dt()).total_seconds() / 60
        assert 59 <= minutes <= 61


# ------------------------------------------------------------------ 幂等

async def test_fetch_twice_upserts_not_duplicates(db: None, factory_with) -> None:
    """同一站拉两次 → upsert，不产生重复行。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "ok"})
    await fetch_all_sites(factory)
    result = await fetch_all_sites(factory)

    assert result["total_written"] == 3
    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert len(rows) == 3


# ------------------------------------------------------------------ 禁用站

async def test_disabled_site_is_skipped(db: None, factory_with) -> None:
    """enabled=0 的站不参与拉取。"""
    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="hdsky", enabled=True))
        session.add(Site(name="s2", domain="s2.test", adapter="hdsky", enabled=False))
        await session.flush()

    factory = factory_with({"s1": "ok", "s2": "ok"})
    result = await fetch_all_sites(factory)

    assert result["scanned_sites"] == 1
    assert result["total_written"] == 3


# ------------------------------------------------- 真实适配器 + respx 冒烟

async def test_real_adapter_dns_failure_surfaces_as_failed_site(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用真实 hdsky 适配器 + respx 模拟 DNS 失败，端到端验证统计。

    比假适配器更可信：走真实的异常包装路径（SiteFetchError）。
    """
    from app.adapters.sites.hdsky import HdskyAdapter

    await _add_sites("hdsky_site")

    factory = AdapterFactory()
    real_site = factory.site

    def _site(row: Site, *, cookies: str | None = None) -> SiteAdapter:
        return HdskyAdapter(
            SiteProfile(name=row.name, domain=row.domain), cookies="uid=1"
        )

    monkeypatch.setattr(factory, "site", _site)
    assert real_site is not None

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route().mock(
            side_effect=httpx.ConnectError("Name or service not known")
        )
        result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 1
    assert result["total_written"] == 0
    assert "hdsky_site" in str(result["summary"])
