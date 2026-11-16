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

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
import respx
from app.adapters.sites.base import (
    FeedItem,
    Promotion,
    SiteAdapter,
    SiteHealth,
    SiteProfile,
)
from app.core.timeutils import bj_now, bj_now_dt, format_bj, parse_bj
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.jobs import fetch_all_sites
from sqlalchemy import func, select, update


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

    def __init__(
        self,
        profile: SiteProfile,
        *,
        mode: str = "ok",
        fail_times: int = 0,
        **_: object,
    ) -> None:
        super().__init__(profile)
        self.mode = mode
        #: 前 N 次调用抛异常（模拟瞬态故障），之后恢复正常
        self.fail_times = fail_times
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
        if self.mode == "flaky" and self.calls <= self.fail_times:
            # 瞬态故障：前 fail_times 次失败，之后成功
            from app.adapters.sites.base import SiteFetchError

            raise SiteFetchError("连接被重置")
        if self.mode == "breaker":
            from app.adapters.sites.base import BreakerTripped

            raise BreakerTripped("拉取返回 429，需熔断")
        if self.mode == "breaker403":
            from app.adapters.sites.base import BreakerTripped

            raise BreakerTripped("拉取返回 403，需熔断")
        if self.mode == "parse":
            # 解析类异常（站点改版）——重试无意义
            raise ValueError("HTML 结构变了")
        if self.mode == "empty":
            return []
        if page_number > 1:
            return []
        return [
            FeedItem(
                title=f"torrent-{self.profile.name}-{i}",
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
    """构造工厂，按站点名注入不同行为的假适配器。

    ``modes`` 的值可以是：
    - 字符串：模式名（"ok" / "empty" / "raise" / "breaker" / "breaker403" / "flaky"）
    - 元组 ``(模式, fail_times)``：flaky 模式用，指定前几次失败
    """

    def _make(modes: dict[str, str | tuple[str, int]]) -> AdapterFactory:
        f = AdapterFactory()

        def _site(row: Site, *, cookies: str | None = None) -> _FakeAdapter:
            spec = modes.get(row.name, "ok")
            if isinstance(spec, tuple):
                mode, fail_times = spec
            else:
                mode, fail_times = spec, 0
            return _FakeAdapter(
                SiteProfile(name=row.name, domain=row.domain),
                mode=mode,
                fail_times=fail_times,
            )

        monkeypatch.setattr(f, "site", _site)
        return f

    return _make


async def _add_sites(*names: str) -> None:
    async with session_scope() as session:
        for n in names:
            session.add(Site(name=n, domain=f"{n}.test", adapter="hdsky"))
        await session.flush()


# ------------------------------------------- 优惠判定（hdwing 式「AJAX 占位符」站）

class _PromoAdapter(_FakeAdapter):
    """带优惠查询能力的假适配器（模拟 hdwing）。

    hdwing 的优惠标记是列表页里的**空占位**（``span.sp_state_placeholder``），
    免费判定只能逐种子查服务端的优惠历史表 → 适配器要声明
    ``supports_promotion_lookup``，由 service 层做增量判定。
    """

    supports_promotion_lookup = True

    def __init__(
        self,
        profile: SiteProfile,
        *,
        promo_by_id: dict[str, Promotion | None] | None = None,
        promo_error_ids: set[str] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(profile, **kwargs)
        self.promo_by_id = promo_by_id or {}
        #: 这些 item_id 查优惠时抛 SiteFetchError（模拟单条故障）
        self.promo_error_ids = promo_error_ids or set()
        #: 被查过优惠的 item_id（增量判定的核心断言对象）
        self.promo_calls: list[str] = []

    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 100,
        page_number: int = 1,
    ) -> list[FeedItem]:
        items = await super().search_torrents(
            query, page_size=page_size, page_number=page_number
        )
        # 列表页**没有**优惠标记（hdwing 的真实形态）→ 不带 free_payload，
        # description 也置空（副标题可能含「剩余」字样，会污染 window_parser
        # 的文本兜底路径——hdwing 适配器就是这么做的）
        return [replace(i, free_payload=None, description="") for i in items]

    async def get_promotion(self, item_id: str) -> Promotion | None:
        self.promo_calls.append(item_id)
        if item_id in self.promo_error_ids:
            from app.adapters.sites.base import SiteFetchError

            raise SiteFetchError("优惠历史返回 500")
        return self.promo_by_id.get(item_id)


@pytest.fixture
def promo_factory(monkeypatch: pytest.MonkeyPatch):
    """构造工厂，注入带优惠查询能力的假适配器（按站名缓存实例）。

    返回 ``(factory, adapters)``——``adapters`` 里能看到每个适配器被查过
    哪些 item_id 的优惠。
    """

    def _make(
        promo_by_id: dict[str, Promotion | None],
        promo_error_ids: set[str] | None = None,
    ) -> tuple[AdapterFactory, dict[str, _PromoAdapter]]:
        f = AdapterFactory()
        adapters: dict[str, _PromoAdapter] = {}

        def _site(row: Site, *, cookies: str | None = None) -> _PromoAdapter:
            ad = adapters.get(row.name)
            if ad is None:
                ad = _PromoAdapter(
                    SiteProfile(name=row.name, domain=row.domain),
                    promo_by_id=promo_by_id,
                    promo_error_ids=promo_error_ids,
                )
                adapters[row.name] = ad
            return ad

        monkeypatch.setattr(f, "site", _site)
        return (f, adapters)

    return _make


async def test_promotion_lookup_writes_real_free_window(
    db: None, promo_factory
) -> None:
    """站点返回的优惠 → 落库成**限时免费 + 真实截止时间**。

    这是 hdwing 改动的核心：以前免费标记来自那个被 Turnstile 闸住的
    AJAX 接口（对任意 id 都回「免费」）→ 全被当成永久免费，窗口永不到期。
    """
    await _add_sites("s1")
    end_at = format_bj(bj_now_dt() + timedelta(days=2))
    factory, adapters = promo_factory(
        {"0": Promotion(kind="Free", start_at=bj_now(), end_at=end_at)}
    )
    await fetch_all_sites(factory)

    async with session_scope() as session:
        row = (
            await session.execute(select(TorrentIndex).where(TorrentIndex.item_id == "0"))
        ).scalar_one()
        assert row.free_type == "freeleech_timed"
        assert row.free_end_at == end_at
    # 三个条目都是新增 → 都查一遍（并发执行，顺序不保证）
    assert sorted(adapters["s1"].promo_calls) == ["0", "1", "2"]


async def test_promotion_lookup_marks_non_free_items(
    db: None, promo_factory
) -> None:
    """没有生效的优惠 → 不是免费种（free_type=unknown，不进推送候选）。"""
    await _add_sites("s1")
    factory, adapters = promo_factory({})
    await fetch_all_sites(factory)

    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert len(rows) == 3
        assert all(r.free_type == "unknown" for r in rows)
        assert all(r.free_end_at is None for r in rows)
    assert sorted(adapters["s1"].promo_calls) == ["0", "1", "2"]


async def test_promotion_lookup_is_incremental(db: None, promo_factory) -> None:
    """第二轮**不再**查判定仍有效的条目——否则每轮都要打几百个请求。

    增量规则：
    - 判定仍有效（限时免费且窗口未过）→ 沿用旧值，**不发请求**
    - 上轮判定为「不免费」→ 复查（可能新开了促销）
    """
    await _add_sites("s1")
    end_at = format_bj(bj_now_dt() + timedelta(days=2))
    factory, adapters = promo_factory(
        {"0": Promotion(kind="Free", start_at=bj_now(), end_at=end_at)}
    )
    await fetch_all_sites(factory)
    first_calls = sorted(adapters["s1"].promo_calls)
    assert first_calls == ["0", "1", "2"]

    # 第二轮：条目 0 判定仍有效 → 不再查；条目 1/2 上轮不免费 → 复查
    await fetch_all_sites(factory)
    all_calls = adapters["s1"].promo_calls
    assert all_calls.count("0") == 1, "判定仍有效的条目不该再查优惠"
    assert sorted(all_calls[3:]) == ["1", "2"], "上轮不免费的条目要复查"

    async with session_scope() as session:
        row = (
            await session.execute(select(TorrentIndex).where(TorrentIndex.item_id == "0"))
        ).scalar_one()
        # 沿用旧判定：free_type / free_end_at 不能被 upsert 抹掉
        assert row.free_type == "freeleech_timed"
        assert row.free_end_at == end_at


async def test_promotion_lookup_refreshes_expired_items(
    db: None, promo_factory
) -> None:
    """窗口已过的条目要**复查**（可能不再免费，也可能又开了新窗口）。"""
    await _add_sites("s1")
    expired = format_bj(bj_now_dt() - timedelta(days=1))
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.name == "s1"))
        ).scalar_one()
        session.add(
            TorrentIndex(
                site_id=site.id,
                item_id="0",
                name="old",
                free_type="freeleech_timed",
                free_end_at=expired,
                first_seen_at=bj_now(),
                last_seen_at=bj_now(),
            )
        )
        await session.flush()

    factory, adapters = promo_factory({})
    await fetch_all_sites(factory)

    # 过期条目被复查；其它条目（本轮新增）也查
    assert "0" in adapters["s1"].promo_calls
    async with session_scope() as session:
        row = (
            await session.execute(select(TorrentIndex).where(TorrentIndex.item_id == "0"))
        ).scalar_one()
        # 复查结果：当前没有生效优惠 → 免费判定被清掉
        assert row.free_type == "unknown"
        assert row.free_end_at is None


async def test_promotion_lookup_failure_keeps_site_healthy(
    db: None, promo_factory
) -> None:
    """单个条目的优惠判定失败 → 不影响整站（该条目不写免费标记）。

    优惠判定是**附加信息**：查失败不能把整站记成故障，也不能让其它条目
    跟着丢判定。
    """
    await _add_sites("s1")
    end_at = format_bj(bj_now_dt() + timedelta(days=2))
    factory, adapters = promo_factory(
        {"1": Promotion(kind="Free", start_at=bj_now(), end_at=end_at)},
        promo_error_ids={"0"},
    )
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 0, "单条优惠判定失败不该把站点记成故障"
    assert result["total_written"] == 3
    async with session_scope() as session:
        rows = {
            r.item_id: r
            for r in (await session.execute(select(TorrentIndex))).scalars().all()
        }
        assert rows["0"].free_type == "unknown"  # 查失败 → 不写免费标记
        assert rows["1"].free_type == "freeleech_timed"  # 其它条目照常
        assert rows["1"].free_end_at == end_at
    assert sorted(adapters["s1"].promo_calls) == ["0", "1", "2"]


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


async def test_steps_含逐站明细(db: None, factory_with) -> None:
    """步骤带**逐站明细**——回答「是哪个站失败了」。"""
    await _add_sites("s1", "s2")
    factory = factory_with({"s1": "ok", "s2": "raise"})
    result = await fetch_all_sites(factory)

    pull_step = next(s for s in result["steps"] if s.name == "拉取种子列表")
    assert pull_step.items is not None
    names = {i.name for i in pull_step.items}
    assert names == {"s1", "s2"}
    # 失败的站是 fail 状态 + 带原因
    bad = next(i for i in pull_step.items if i.name == "s2")
    assert bad.status == "fail"
    assert bad.detail
    # 成功的站是 ok 状态
    good = next(i for i in pull_step.items if i.name == "s1")
    assert good.status == "ok"


async def test_steps_明细标记熔断跳过(db: None, factory_with) -> None:
    """熔断跳过的站在明细里是 skip 状态（不是 fail——那是保护性跳过）。"""
    await _add_sites("s1")
    async with session_scope() as session:
        await session.execute(
            update(Site).where(Site.name == "s1").values(
                breaker_until="2099-01-01 00:00:00"
            )
        )
    factory = factory_with({"s1": "ok"})
    result = await fetch_all_sites(factory)

    pull_step = next(s for s in result["steps"] if s.name == "拉取种子列表")
    assert pull_step.items is not None
    item = next(i for i in pull_step.items if i.name == "s1")
    assert item.status == "skip"
    assert "熔断" in (item.detail or "")


# ------------------------------------------------------------------ 重试

async def test_transient_failure_is_retried_and_succeeds(
    db: None, factory_with
) -> None:
    """瞬态故障（连接重置）重试后成功 → **不算失败站**。

    这是把退避重试接回主链路的核心收益：DNS 抖动 / 连接重置这类
    故障本来重试一次就能好，之前每次都记成失败站。
    """
    await _add_sites("s1")
    factory = factory_with({"s1": ("flaky", 1)})  # 第 1 次失败，第 2 次成功
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 0, "重试成功不该算失败"
    assert result["total_written"] == 3


async def test_retry_exhausted_still_counts_as_failed(
    db: None, factory_with
) -> None:
    """重试耗尽（连续 3 次失败）→ 计入失败站。"""
    await _add_sites("s1")
    factory = factory_with({"s1": ("flaky", 99)})  # 一直失败
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 1
    assert result["total_written"] == 0


async def test_retry_does_not_apply_to_parse_errors(
    db: None, factory_with
) -> None:
    """解析类失败不重试（站点改版重试无意义，且加剧限流）。"""
    await _add_sites("s1")
    adapters: list[_FakeAdapter] = []
    factory = AdapterFactory()

    def _site(row: Site, *, cookies: str | None = None) -> _FakeAdapter:
        a = _FakeAdapter(SiteProfile(name=row.name, domain=row.domain), mode="parse")
        adapters.append(a)
        return a

    factory.site = _site  # type: ignore[method-assign]
    result = await fetch_all_sites(factory)

    assert result["failed_sites"] == 1
    assert adapters[0].calls == 1, "解析错误不应重试"


async def test_breaker_is_not_retried(db: None, factory_with) -> None:
    """熔断不重试——继续请求只会加速账号暴露。"""
    await _add_sites("s1")
    adapters: list[_FakeAdapter] = []
    factory = AdapterFactory()

    def _site(row: Site, *, cookies: str | None = None) -> _FakeAdapter:
        a = _FakeAdapter(SiteProfile(name=row.name, domain=row.domain), mode="breaker")
        adapters.append(a)
        return a

    factory.site = _site  # type: ignore[method-assign]
    result = await fetch_all_sites(factory)

    assert result["skipped_breaker"] == 1
    assert adapters[0].calls == 1, "熔断后不应重试"


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

async def test_fetch_upsert_after_site_deleted_is_skipped_not_crashed(
    db: None, factory_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    """站点被删后，正在拉取的 fetch 回来 upsert → 跳过，不崩、不留脏数据。

    场景：``fetch_all_sites`` 已读完站点列表、在做网络 IO；此时用户删了
    该站（``delete_site`` 级联清了 ``torrent_index``）。fetch 回来后
    upsert 会撞 FK（``torrent_index.site_id → sites.id``）。

    期望：FK 拒绝孤儿行写入 → ``_upsert_site_items`` 的 savepoint 兜住
    → 只记 debug + 计一次写失败，**作业不崩**、**不留孤儿行**。

    （FK 在 WAL + ``foreign_keys=ON`` 下确实会拒绝孤儿行——已实测。）
    """
    await _add_sites("s1")
    factory = factory_with({"s1": "ok"})

    # 模拟"网络 IO 期间站点被删"：拦下 upsert，在此刻删站
    from app.services import jobs as jobs_mod

    orig_upsert = jobs_mod._upsert_site_items
    state = {"deleted": False}

    async def _upsert_then_delete(row: Site, feed_items: list[FeedItem]):
        if not state["deleted"]:
            state["deleted"] = True
            # 站点被删（级联清 torrent_index）——但本站的 row 对象还在手上
            from app.services import site_service

            await site_service.delete_site(row.id)
        return await orig_upsert(row, feed_items)

    monkeypatch.setattr(jobs_mod, "_upsert_site_items", _upsert_then_delete)

    result = await fetch_all_sites(factory)

    # 作业没崩
    assert result["scanned_sites"] == 1
    # 没有写入成功（FK 拒了）
    assert result["total_written"] == 0
    # 且没有留下孤儿行
    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert rows == []
        orphan = (
            await session.execute(
                select(func.count())
                .select_from(TorrentIndex)
                .outerjoin(Site, Site.id == TorrentIndex.site_id)
                .where(Site.id.is_(None))
            )
        ).scalar_one()
        assert orphan == 0


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
