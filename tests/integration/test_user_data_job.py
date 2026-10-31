"""站点用户数据刷新作业集成测试（FR-1.6 + 时魔）。

**锁住的核心契约**（2026-10-21 prod 事故的教训）：

    拉取失败**必须**被统计成失败，且**不能写 0 兜底**。

    之前 ``get_user_snapshot`` 失败返回 ``None``，service 层把它写成
    「上传 0 / 下载 0 / 做种 0」——故障被伪装成真零，前端完全看不出哪站
    挂了（作业还报「全部成功」）。现在失败站计入 ``failed`` 并点名，
    **保留上一次的快照**。

另外锁住：

- 网络 IO 在事务外（并发抓取不持写锁）
- 时魔字段缺失时**不覆盖**库里的旧值
- 真实适配器 + 真实 fixture 走通整条链路（mybonus.php → 解析 → 落库）
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)
from app.adapters.sites.user_data import HourlyBonus, SiteUserSnapshot
from app.db.models import Site, SiteUserData
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.user_data import refresh_all_sites
from sqlalchemy import select

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


class _FakeUserAdapter(SiteAdapter):
    """可控的假 user_data 适配器。

    ``mode`` 决定 ``get_user_snapshot()`` 的行为：

    - ``"ok"``      → 正常快照（带时魔）
    - ``"capped"``  → 时魔已封顶
    - ``"nobonus"`` → 正常快照但**没有时魔**（该站不支持）
    - ``"none"``    → 返回 None（该站不支持 / 无凭据）
    - ``"raise"``   → 抛 SiteFetchError（网络 / 解析故障）

    并发计数是**共享**的（``state``）——每站一个适配器实例，实例内的
    计数器永远只有 1，测不出并发。
    """

    adapter_type = "fake"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        mode: str = "ok",
        state: _SharedState | None = None,
        **_: object,
    ) -> None:
        super().__init__(profile)
        self.mode = mode
        self.state = state if state is not None else _SharedState()
        self.calls = 0

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        self.calls += 1
        self.state.concurrent += 1
        self.state.max_concurrent = max(
            self.state.max_concurrent, self.state.concurrent
        )
        try:
            # 让出控制权：并发抓取时多个站会同时处于「抓取中」
            await asyncio.sleep(0)
            match self.mode:
                case "ok":
                    return SiteUserSnapshot(
                        upload=1000,
                        download=2000,
                        seeding_count=3,
                        hourly_bonus=HourlyBonus(
                            amount=88.5, unit="魔力值", capped=False, progress=12.5
                        ),
                    )
                case "capped":
                    return SiteUserSnapshot(
                        upload=1,
                        download=2,
                        seeding_count=3,
                        hourly_bonus=HourlyBonus(
                            amount=154.0, unit="爆米花", capped=True, progress=110.0
                        ),
                    )
                case "nobonus":
                    return SiteUserSnapshot(
                        upload=5, download=6, seeding_count=7
                    )
                case "none":
                    return None
                case "raise":
                    raise SiteFetchError("Name or service not known")
            return None
        finally:
            self.state.concurrent -= 1

    # 其余契约方法不用（user_data 作业只调 get_user_snapshot）
    async def check_reachability(self) -> SiteHealth:
        return SiteHealth(reachable=True, logged_in=True)

    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 100,
        page_number: int = 1,
    ) -> list[FeedItem]:
        return []

    async def download_torrent(self, item_id: str) -> bytes:
        return b""

    async def get_torrent_url(self, item_id: str) -> str | None:
        return None

    async def check_status(self, item_id: str) -> TorrentBrief:
        return TorrentBrief.UNKNOWN

    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        return TorrentDetail(item_id=item_id)


class _SharedState:
    """跨适配器实例共享的并发计数。"""

    def __init__(self) -> None:
        self.concurrent = 0
        self.max_concurrent = 0


@pytest.fixture
def factory_with(monkeypatch: pytest.MonkeyPatch):
    """构造工厂，按站点名注入不同行为的假适配器。"""

    def _make(modes: dict[str, str]) -> AdapterFactory:
        f = AdapterFactory()
        adapters: list[_FakeUserAdapter] = []
        state = _SharedState()

        def _site(row: Site, *, cookies: str | None = None) -> _FakeUserAdapter:
            a = _FakeUserAdapter(
                SiteProfile(name=row.name, domain=row.domain),
                mode=modes.get(row.name, "ok"),
                state=state,
            )
            adapters.append(a)
            return a

        monkeypatch.setattr(f, "site", _site)
        f._test_adapters = adapters  # type: ignore[attr-defined]
        f._test_state = state  # type: ignore[attr-defined]
        return f

    return _make


async def _add_sites(*names: str) -> None:
    async with session_scope() as session:
        for n in names:
            session.add(Site(name=n, domain=f"{n}.test", adapter="hdsky"))
        await session.flush()


async def _rows() -> list[SiteUserData]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(SiteUserData).order_by(SiteUserData.site_id)
                )
            ).scalars()
        )


# ------------------------------------------------------------------ 正常


async def test_success_writes_snapshot_with_hourly_bonus(
    db: None, factory_with
) -> None:
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "ok"}))

    assert result["total"] == 1
    assert result["ok"] == 1
    assert result["failed"] == 0
    assert result["skipped"] == 0

    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.upload == 1000
    assert row.download == 2000
    assert row.seeding_count == 3
    # 时魔落库
    assert row.hourly_bonus == pytest.approx(88.5)
    assert row.hourly_bonus_unit == "魔力值"
    assert row.hourly_bonus_capped is False
    assert row.hourly_bonus_progress == pytest.approx(12.5)
    assert row.last_seen_at


async def test_capped_sites_counted(db: None, factory_with) -> None:
    """已封顶的站要单独计数（前端能一眼看出哪站再投入也白搭）。"""
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(
        factory_with({"s1": "capped", "s2": "ok"})
    )

    assert result["capped"] == 1
    step = next(s for s in result["steps"] if s.name == "时魔已封顶")
    assert step.count == 1
    rows = await _rows()
    capped = [r for r in rows if r.hourly_bonus_capped is True]
    assert len(capped) == 1
    assert capped[0].hourly_bonus == pytest.approx(154.0)


# ------------------------------------------------------------------ 失败


async def test_failure_counted_and_not_written_as_zero(
    db: None, factory_with
) -> None:
    """失败站：计入 failed + 点名，且**不写 0 兜底**。"""
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(
        factory_with({"s1": "ok", "s2": "raise"})
    )

    assert result["ok"] == 1
    assert result["failed"] == 1
    assert "s2" in str(result["summary"])
    step = next(s for s in result["steps"] if s.name == "失败站点")
    assert step.status == "fail"
    assert "s2" in str(step.detail)

    rows = await _rows()
    assert len(rows) == 1, "失败站不应写入 site_user_data"
    assert rows[0].upload == 1000, "写 0 兜底 = 把故障伪装成真零"


async def test_failure_keeps_previous_snapshot(db: None, factory_with) -> None:
    """失败站**保留上一次的快照**（不覆盖成 0，也不清空）。"""
    await _add_sites("s1")
    await refresh_all_sites(factory_with({"s1": "ok"}))

    result = await refresh_all_sites(factory_with({"s1": "raise"}))
    assert result["failed"] == 1

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].upload == 1000
    assert rows[0].hourly_bonus == pytest.approx(88.5)


async def test_unsupported_site_skipped_not_written(db: None, factory_with) -> None:
    """返回 None（该站不支持 / 无凭据）→ skipped，不写库、不算失败。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "none"}))

    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["ok"] == 0
    assert await _rows() == []


# ------------------------------------------------------- 时魔缺失不覆盖旧值


async def test_missing_hourly_bonus_keeps_old_value(db: None, factory_with) -> None:
    """本周期没解析出时魔 → 保留库里的旧时魔（只更新 4 业务字段）。"""
    await _add_sites("s1")
    await refresh_all_sites(factory_with({"s1": "ok"}))

    await refresh_all_sites(factory_with({"s1": "nobonus"}))

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].upload == 5, "4 业务字段应更新"
    assert rows[0].hourly_bonus == pytest.approx(88.5), "旧时魔应保留"
    assert rows[0].hourly_bonus_unit == "魔力值"


# ------------------------------------------------------------------ 并发


async def test_sites_are_fetched_concurrently(db: None, factory_with) -> None:
    """9 站并发抓取（不是串行）——串行最坏 9×20s。"""
    names = [f"s{i}" for i in range(9)]
    await _add_sites(*names)
    factory = factory_with({})
    await refresh_all_sites(factory)

    adapters = factory._test_adapters  # type: ignore[attr-defined]
    state = factory._test_state  # type: ignore[attr-defined]
    assert len(adapters) == 9
    assert state.max_concurrent > 1, "抓取没有并发"


# ------------------------------------------------- 真实适配器 + 真实 fixture


async def test_real_adapter_end_to_end(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用真实 hhanclub 适配器 + 真实 mybonus fixture 走通整条链路。

    比假适配器更可信：走真实的异常包装、真实解析、真实落库。
    """
    import httpx
    import respx
    from app.adapters.sites.hhanclub import HhanclubAdapter

    html = (FIXTURES / "hhanclub_mybonus.html").read_text(encoding="utf-8")
    await _add_sites("hhanclub_site")

    factory = AdapterFactory()

    def _site(row: Site, *, cookies: str | None = None) -> SiteAdapter:
        return HhanclubAdapter(
            SiteProfile(name=row.name, domain=row.domain), cookies="uid=1"
        )

    monkeypatch.setattr(factory, "site", _site)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route(host="hhanclub_site.test").mock(
            return_value=httpx.Response(200, text=html)
        )
        result = await refresh_all_sites(factory)

    assert result["ok"] == 1
    assert result["failed"] == 0

    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.upload > 0
    assert row.seeding_count > 0
    # 时魔 = hhanclub 的**合计**（含官种 / 勋章加成），不是 headline 基础值
    assert row.hourly_bonus is not None
    assert row.hourly_bonus > 50
    assert row.hourly_bonus_unit == "憨豆"
    assert row.hourly_bonus_capped is False
