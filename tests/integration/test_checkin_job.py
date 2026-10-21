"""站点签到作业集成测试（FR-3.6）。

**锁住的核心契约**（2026-10-21 prod 事故的教训）：

    拉取/签到失败**必须**被统计成失败，不能吞掉后报「全部成功」。

事故当时 hddolby 的 DNS 解析失败被 adapter 吞成空列表，作业统计
``failed_sites`` 恒为 0——故障完全不可见。这里用假适配器把这条钉住。
"""

from __future__ import annotations

import pytest
from app.adapters.sites.base import (
    BreakerTripped,
    CheckinResult,
    CheckinStatus,
    FeedItem,
    SiteAdapter,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)
from app.db.models import Site
from app.db.session import session_scope
from app.services.checkin import refresh_all_sites
from app.services.factory import AdapterFactory
from sqlalchemy import select


class _FakeCheckinAdapter(SiteAdapter):
    """可控的假签到适配器。

    ``mode`` 决定 ``checkin()`` 的行为：

    - ``"success"``    → 签到成功
    - ``"already"``    → 今日已签
    - ``"unsupported"``→ 不支持（人机验证 / 无此功能）
    - ``"auth"``       → cookie 失效
    - ``"failed"``     → 页面认不出
    - ``"raise"``      → 抛异常（网络故障）
    - ``"breaker"``    → 抛 BreakerTripped（限流）
    """

    adapter_type = "fake"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        mode: str = "success",
        **_: object,
    ) -> None:
        super().__init__(profile)
        self.mode = mode
        self.calls = 0

    async def checkin(self) -> CheckinResult:
        self.calls += 1
        match self.mode:
            case "success":
                return CheckinResult(
                    CheckinStatus.SUCCESS, "签到成功",
                    total_count=100, streak_days=2, reward=10,
                )
            case "already":
                return CheckinResult(CheckinStatus.ALREADY, "今日已签到")
            case "unsupported":
                return CheckinResult(
                    CheckinStatus.UNSUPPORTED, "该站需人机验证，无法自动签到（需人工）"
                )
            case "auth":
                return CheckinResult(
                    CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
                )
            case "failed":
                return CheckinResult(
                    CheckinStatus.FAILED, "签到页结构无法识别（站点可能改版）"
                )
            case "raise":
                raise RuntimeError("Name or service not known")
            case "breaker":
                raise BreakerTripped("签到返回 429，需熔断")
        return CheckinResult(CheckinStatus.FAILED, "未知模式")

    # 其余契约方法不用（签到作业只调 checkin）
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


@pytest.fixture
def factory_with(monkeypatch: pytest.MonkeyPatch):
    """构造工厂，按站点名注入不同行为的假适配器。"""

    def _make(modes: dict[str, str]) -> AdapterFactory:
        f = AdapterFactory()
        adapters: list[_FakeCheckinAdapter] = []

        def _site(row: Site, *, cookies: str | None = None) -> _FakeCheckinAdapter:
            a = _FakeCheckinAdapter(
                SiteProfile(name=row.name, domain=row.domain),
                mode=modes.get(row.name, "success"),
            )
            adapters.append(a)
            return a

        monkeypatch.setattr(f, "site", _site)
        f._test_adapters = adapters  # type: ignore[attr-defined]
        return f

    return _make


async def _add_sites(*names: str) -> None:
    async with session_scope() as session:
        for n in names:
            session.add(Site(name=n, domain=f"{n}.test", adapter="hdsky"))
        await session.flush()


# ------------------------------------------------------------------ 正常


async def test_checkin_counts_success(db: None, factory_with) -> None:
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(factory_with({"s1": "success", "s2": "success"}))

    assert result["total"] == 2
    assert result["success"] == 2
    assert result["failed"] == 0
    assert result["already"] == 0


async def test_already_is_not_a_failure(db: None, factory_with) -> None:
    """今日已签 → 幂等，**不算失败**。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "already"}))

    assert result["already"] == 1
    assert result["failed"] == 0
    assert result["success"] == 0


async def test_unsupported_is_not_a_failure(db: None, factory_with) -> None:
    """不支持签到的站（人机验证 / 无此功能）→ 正常跳过，不算失败。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "unsupported"}))

    assert result["unsupported"] == 1
    assert result["failed"] == 0


async def test_disabled_site_is_skipped(db: None, factory_with) -> None:
    """enabled=0 的站不参与签到。"""
    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="hdsky", enabled=True))
        session.add(Site(name="s2", domain="s2.test", adapter="hdsky", enabled=False))
        await session.flush()

    result = await refresh_all_sites(factory_with({}))

    assert result["total"] == 1
    assert result["success"] == 1


# ------------------------------------------- 核心：故障必须被统计成失败


async def test_checkin_failure_is_counted_as_failed(db: None, factory_with) -> None:
    """签到失败（抛异常）→ 必须计入 failed。

    这是 2026-10-21 prod 事故的核心回归测试：失败被吞掉后，
    作业统计报「全部成功」而实际漏掉了故障站。
    """
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(factory_with({"s1": "success", "s2": "raise"}))

    assert result["failed"] == 1, "故障站必须被统计为失败"
    assert "s2" in str(result["summary"]), "summary 必须点名失败站"
    assert result["success"] == 1, "好站仍要正常签到"


async def test_unrecognized_page_is_counted_as_failed(db: None, factory_with) -> None:
    """页面认不出（站点改版）→ 计入 failed。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "failed"}))

    assert result["failed"] == 1


async def test_auth_expired_is_counted_as_failed(db: None, factory_with) -> None:
    """cookie 失效 → 计入 failed（是故障，需要用户处理）。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "auth"}))

    assert result["failed"] == 1
    assert result["auth_expired"] == 1


async def test_auth_expired_is_not_double_counted(db: None, factory_with) -> None:
    """``auth_expired`` 是 ``failed`` 的**子集**，不能重复计数。

    summary 里要写「1 失败（s1），其中 1 站登录失效」——不是「2 失败」。
    """
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "auth"}))

    summary = str(result["summary"])
    assert "1 失败" in summary
    assert "2 失败" not in summary
    assert "登录失效" in summary


async def test_breaker_is_counted_as_failed(db: None, factory_with) -> None:
    """熔断（限流）→ 计入 failed，且**不写** sites.breaker_until。

    签到是每天 1 次的低频作业，不该因为一次限流就把该站的**种子拉取**
    也停掉（那是 10 分钟一次的高频作业，两者不共享熔断状态）。
    """
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "breaker"}))

    assert result["failed"] == 1

    async with session_scope() as session:
        row = (
            await session.execute(select(Site).where(Site.name == "s1"))
        ).scalar_one()
        assert row.breaker_until is None, "签到熔断不该写站点的 breaker_until"


async def test_failure_does_not_block_other_sites(db: None, factory_with) -> None:
    """单站失败不阻塞其他站（异常隔离）。"""
    await _add_sites("bad", "good")
    result = await refresh_all_sites(factory_with({"bad": "raise", "good": "success"}))

    assert result["failed"] == 1
    assert result["success"] == 1


async def test_all_sites_failing_reports_all_failed(db: None, factory_with) -> None:
    await _add_sites("s1", "s2", "s3")
    result = await refresh_all_sites(
        factory_with({"s1": "raise", "s2": "raise", "s3": "raise"})
    )

    assert result["failed"] == 3
    assert result["success"] == 0


# ------------------------------------------------------------------ 步骤卡片


async def test_steps_report_failure_as_warn(db: None, factory_with) -> None:
    """有失败时「执行签到」步骤是 warn（前端据此染色）。"""
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(factory_with({"s1": "success", "s2": "raise"}))

    steps = result["steps"]
    assert isinstance(steps, list)
    step = next(s for s in steps if s.name == "执行签到")
    assert step.status == "warn"


async def test_steps_ok_when_all_success(db: None, factory_with) -> None:
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "success"}))

    step = next(s for s in result["steps"] if s.name == "执行签到")
    assert step.status == "ok"


async def test_steps_list_manual_sites(db: None, factory_with) -> None:
    """需人工签到的站在步骤卡片里点名（让用户知道这站不会自动签）。"""
    await _add_sites("s1", "s2")
    result = await refresh_all_sites(
        factory_with({"s1": "success", "s2": "unsupported"})
    )

    step = next(s for s in result["steps"] if s.name == "需人工签到")
    assert step.count == 1
    assert "s2" in str(step.detail)


async def test_steps_list_failed_sites(db: None, factory_with) -> None:
    """失败站在步骤卡片里点名 + 原因。"""
    await _add_sites("s1")
    result = await refresh_all_sites(factory_with({"s1": "raise"}))

    step = next(s for s in result["steps"] if s.name == "失败站点")
    assert step.status == "fail"
    assert "s1" in str(step.detail)


# ------------------------------------------------------------------ 幂等


async def test_checkin_twice_is_idempotent(db: None, factory_with) -> None:
    """同一天跑两次 → 第二次全是「已签到」，不报错、不重复计数。"""
    await _add_sites("s1")
    factory = factory_with({"s1": "success"})
    await refresh_all_sites(factory)
    result = await refresh_all_sites(factory)

    assert result["success"] == 1
    assert result["failed"] == 0


# ------------------------------------------- 真实适配器 + respx 冒烟


async def test_real_adapter_success(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """用真实 hdhome 适配器 + respx 走通整条链路。

    比假适配器更可信：走真实的异常包装与解析路径。
    """
    import httpx
    import respx
    from app.adapters.sites.hdhome import HdhomeAdapter

    await _add_sites("hdhome_site")

    factory = AdapterFactory()

    def _site(row: Site, *, cookies: str | None = None) -> SiteAdapter:
        return HdhomeAdapter(
            SiteProfile(name=row.name, domain=row.domain), cookies="uid=1"
        )

    monkeypatch.setattr(factory, "site", _site)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route().mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<html><body>签到成功 这是您的第 614 次签到，"
                    "已连续签到 1 天，本次签到获得 10 个魔力值。</body></html>"
                ),
            )
        )
        result = await refresh_all_sites(factory)

    assert result["success"] == 1
    assert result["failed"] == 0
