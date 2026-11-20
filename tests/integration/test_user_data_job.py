"""站点用户数据刷新作业集成测试（FR-1.6）。

**锁住的核心契约**（2026-10-21 prod 事故的教训）：

    拉取失败**必须**被统计成失败，且**不能写 0 兜底**。

    之前 ``get_user_snapshot`` 失败返回 ``None``，service 层把它写成
    「上传 0 / 下载 0 / 做种 0」——故障被伪装成真零，前端完全看不出哪站
    挂了（作业还报「全部成功」）。现在失败站计入 ``failed`` 并点名，
    **保留上一次的快照**。

另外锁住：

- 网络 IO 在事务外（并发抓取不持写锁）
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
from app.adapters.sites.user_data import SiteUserSnapshot
from app.db.models import Site, SiteUserData
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.user_data import refresh_all_sites
from sqlalchemy import select

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


class _FakeUserAdapter(SiteAdapter):
    """可控的假 user_data 适配器。

    ``mode`` 决定 ``get_user_snapshot()`` 的行为：

    - ``"ok"``      → 正常快照
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
            session.add(Site(name=n, domain=f"{n}.test", adapter="generic"))
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


async def test_success_writes_snapshot(db: None, factory_with) -> None:
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
    assert row.last_seen_at


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


async def test_steps_含逐站明细(db: None, factory_with) -> None:
    """拉取步骤带**逐站明细**——回答「哪个站失败 / 各站多少」。"""
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(
        factory_with({"s1": "ok", "s2": "raise"})
    )

    step = next(s for s in result["steps"] if s.name == "拉取 user_data")
    assert step.items is not None
    by_name = {i.name: i for i in step.items}
    assert set(by_name) == {"s1", "s2"}
    assert by_name["s1"].status == "ok"
    assert "上传" in (by_name["s1"].detail or "")
    assert by_name["s2"].status == "fail"
    assert by_name["s2"].detail


async def test_failure_keeps_previous_snapshot(db: None, factory_with) -> None:
    """失败站**保留上一次的快照**（不覆盖成 0，也不清空）。"""
    await _add_sites("s1")
    await refresh_all_sites(factory_with({"s1": "ok"}))

    result = await refresh_all_sites(factory_with({"s1": "raise"}))
    assert result["failed"] == 1

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].upload == 1000


async def test_unsupported_site_skipped_not_written(db: None, factory_with) -> None:
    """返回 None（该站不支持 / 无凭据）→ skipped，不写库、不算失败。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "none"}))

    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["ok"] == 0
    assert await _rows() == []


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


