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

import httpx
import pytest
import respx
from app.adapters.sites.base import FeedItem, SiteAdapter, SiteHealth, SiteProfile
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
