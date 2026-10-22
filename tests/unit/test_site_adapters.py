"""站点适配器契约测试（Phase 0）。

**最重要的契约**（2026-10-21 prod 事故后修复）：

    ``search_torrents`` 有两种截然不同的结果：

    | 返回              | 语义                 | 上层处理     |
    |-------------------|----------------------|--------------|
    | ``[]``            | 站点确实没有种子     | 正常，不算失败 |
    | 抛 ``SiteFetchError`` | 故障（DNS/连接/HTTP） | 计入失败站   |

    之前 9 个 adapter 把 ``httpx.HTTPError`` 吞掉返回 ``[]``，
    导致 prod 的 hddolby DNS 解析失败被统计成"全部成功"，故障完全不可见。

    这条测试把该契约固化下来，任何 adapter 改回吞异常都会红。
"""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.sites import resolve
from app.adapters.sites._registry import list_available, list_meta
from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)

#: 9 个已注册站点适配器
ALL_ADAPTERS = (
    "mteam",
    "hhanclub",
    "hdsky",
    "ourbits",
    "audiences",
    "springsunday",
    "hdhome",
    "hddolby",
    "soulvoice",
)


def _make(name: str) -> SiteAdapter:
    """构造适配器实例（domain 指向 example.com，测试里全部 mock 掉）。

    同时给 cookies 和 apikey：m-team 是 API 站，缺 apikey 时
    ``search_torrents`` 会**直接返回 []**（不发起请求）——那不是
    「拉取失败」，测试会误判成「吞异常」。
    """
    cls = resolve(name)
    return cls(
        SiteProfile(name=name, domain="example.com"),
        cookies="uid=1",
        apikey="test-apikey",
    )


#: m-team 是唯一的 API 站（JSON 响应），其余 8 站是 HTML 站
_API_ONLY_ADAPTERS = frozenset({"mteam"})


def _mock_all(name: str) -> respx.MockRouter:
    """mock 所有主机（m-team 走 api. 前缀，其余走裸域名）。

    m-team 是 API 站，需要 JSON 响应；其余站返回 HTML。

    ``assert_all_mocked=True`` 会连带开启 ``assert_all_called=True``——
    退出时若某条路由没被调用会报错。catch-all 路由在某些用例里
    确实不会被触发（如 audiences 的 ``get_torrent_url`` 直接返回
    URL 不发请求），故这里显式关掉 "must be called"，只保留
    "unmatched request must fail"（防真实网络）。
    """
    router = respx.mock(assert_all_mocked=True, assert_all_called=False)
    if name in _API_ONLY_ADAPTERS:
        router.route().mock(  # type: ignore[no-any-return]
            return_value=httpx.Response(
                200, json={"code": "0", "message": "ok", "data": {}}
            )
        )
    else:
        router.route().mock(  # type: ignore[no-any-return]
            return_value=httpx.Response(200, text="")
        )
    return router


def _mock_all_error(exc: Exception) -> respx.MockRouter:
    """mock 所有主机并返回指定异常。"""
    router = respx.mock(assert_all_mocked=True, assert_all_called=False)
    router.route().mock(side_effect=exc)  # type: ignore[no-any-return]
    return router


# ------------------------------------------------------------------ 注册表

def test_registry_has_nine_adapters() -> None:
    assert set(list_available()) == set(ALL_ADAPTERS)


def test_registry_meta_covers_all() -> None:
    names = [n for n, _ in list_meta()]
    assert set(names) == set(ALL_ADAPTERS)


def test_registry_resolve_unknown_raises() -> None:
    with pytest.raises(ValueError, match="未注册的站点 adapter"):
        resolve("nonexistent-site")


def test_registry_all_are_site_adapter_subclasses() -> None:
    for name in ALL_ADAPTERS:
        cls = resolve(name)
        assert issubclass(cls, SiteAdapter)


# -------------------------------------------------- 契约：失败必须抛错

@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_search_torrents_raises_on_network_error(name: str) -> None:
    """网络故障必须抛异常，不能返回 [] 伪装成「站点没种子」。"""
    adapter = _make(name)
    try:
        with _mock_all_error(httpx.ConnectError("Name or service not known")):
            with pytest.raises(Exception) as exc_info:
                await adapter.search_torrents({})
            # 必须是 SiteFetchError（或其子类），不能是空列表
            assert isinstance(exc_info.value, (SiteFetchError, httpx.HTTPError))
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_search_torrents_raises_on_http_error(name: str) -> None:
    """HTTP 5xx 必须抛异常。"""
    adapter = _make(name)
    try:
        with _mock_all_error(httpx.Response(500, text="server error")):
            with pytest.raises(Exception) as exc_info:
                await adapter.search_torrents({})
            assert isinstance(exc_info.value, (SiteFetchError, httpx.HTTPError))
    finally:
        await adapter.close()


# ------------------------------------------ 契约：限流必须抛 BreakerTripped

#: m-team 是 API 站（JSON），其余 8 站是 HTML 站
_HTML_ADAPTERS = tuple(a for a in ALL_ADAPTERS if a not in _API_ONLY_ADAPTERS)


@pytest.mark.parametrize("name", ALL_ADAPTERS)
@pytest.mark.parametrize("status", [403, 429])
async def test_search_torrents_raises_breaker_on_rate_limit(
    name: str, status: int
) -> None:
    """403/429 限流必须抛 BreakerTripped（熔断信号），不是普通拉取失败。

    限流与「站点改版 / 404」是两种故障：前者要熔断让站冷却，
    后者重试无意义。之前 8 个 HTML 站一律 ``>= 400`` 抛 SiteFetchError，
    熔断机制形同虚设。
    """
    adapter = _make(name)
    try:
        with _mock_all_error(httpx.Response(status, text="rate limited")):
            with pytest.raises(BreakerTripped) as exc_info:
                await adapter.search_torrents({})
            assert str(status) in str(exc_info.value)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_search_torrents_5xx_is_not_breaker(name: str) -> None:
    """5xx 是站点故障，**不是**限流——不该触发熔断。"""
    adapter = _make(name)
    try:
        with _mock_all_error(httpx.Response(500, text="server error")):
            with pytest.raises(Exception) as exc_info:
                await adapter.search_torrents({})
            assert not isinstance(exc_info.value, BreakerTripped)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_search_torrents_empty_page_returns_empty_list(name: str) -> None:
    """站点正常但没有种子 → 返回 []（不是抛错）。

    这是与「站点挂了」的关键区别：作业统计要能把两者分开。
    """
    adapter = _make(name)
    try:
        with _mock_all(name):
            result = await adapter.search_torrents({})
            assert isinstance(result, list)
            assert result == []
    finally:
        await adapter.close()


# -------------------------------------------------- 契约：方法签名与返回类型

@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_check_reachability_returns_site_health(name: str) -> None:
    adapter = _make(name)
    try:
        with _mock_all(name):
            health = await adapter.check_reachability()
            assert isinstance(health, SiteHealth)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_get_user_snapshot_raises_on_failure(name: str) -> None:
    """user_data 契约：拉取/解析失败**必须抛 SiteFetchError**。

    | 返回              | 语义                     | 上层处理     |
    |-------------------|--------------------------|--------------|
    | ``None``          | 该站不支持 / 无凭据      | 跳过         |
    | 抛 ``SiteFetchError`` | 故障（网络/HTTP/解析）  | 计入失败站   |

    之前各 adapter 把失败吞掉返回 ``None``，service 层又写 0 兜底——
    故障被伪装成「上传 0 / 做种 0」，前端完全看不出哪站挂了
    （2026-10-21 prod 事故同款问题）。
    """
    adapter = _make(name)
    try:
        # 200 + 空 HTML → 4 个业务字段解析不出来 → 必须抛错
        with _mock_all(name), pytest.raises(SiteFetchError):
            await adapter.get_user_snapshot()
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_get_user_snapshot_raises_on_network_error(name: str) -> None:
    """网络故障（连接失败）同样必须抛 :class:`SiteFetchError`。"""
    adapter = _make(name)
    try:
        with _mock_all_error(httpx.ConnectError("boom")), pytest.raises(SiteFetchError):
            await adapter.get_user_snapshot()
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_get_torrent_url_returns_none_on_failure(name: str) -> None:
    """详情页不可达时返回 None（不抛），调用方据此跳过。"""
    adapter = _make(name)
    try:
        with _mock_all_error(httpx.ConnectError("boom")):
            url = await adapter.get_torrent_url("123")
            assert url is None or isinstance(url, str)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_get_torrent_detail_returns_torrent_detail(name: str) -> None:
    adapter = _make(name)
    try:
        with _mock_all(name):
            detail = await adapter.get_torrent_detail("123")
            assert isinstance(detail, TorrentDetail)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_check_status_returns_torrent_brief(name: str) -> None:
    adapter = _make(name)
    try:
        with _mock_all(name):
            brief = await adapter.check_status("123")
            assert isinstance(brief, TorrentBrief)
    finally:
        await adapter.close()


# ------------------------------------------------------ 契约：close 可重复调用

@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_close_is_idempotent(name: None) -> None:
    """优雅停机会 close 全部连接池，重复 close 不能抛。"""
    adapter = _make(name)
    await adapter.close()
    await adapter.close()


# -------------------------------------------------- 契约：每日签到（FR-3.6）

#: 已实现自动签到的 3 站（实测 2026-10-22 可用）
CHECKIN_ADAPTERS = ("hhanclub", "soulvoice", "hdhome")

#: 未实现签到的站：无此功能（springSunday / mteam）或需人机验证
#: （audiences / ourbits / hdsky）。**不做验证码绕过**（AGENTS.md 合规红线）。
NO_CHECKIN_ADAPTERS = tuple(a for a in ALL_ADAPTERS if a not in CHECKIN_ADAPTERS)


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_checkin_returns_checkin_result(name: str) -> None:
    """签到契约：返回 ``CheckinResult``（不抛）。"""
    from app.adapters.sites.base import CheckinResult

    adapter = _make(name)
    try:
        with _mock_all(name):
            result = await adapter.checkin()
            assert isinstance(result, CheckinResult)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", NO_CHECKIN_ADAPTERS)
async def test_unimplemented_sites_return_unsupported(name: str) -> None:
    """未实现签到的站 → UNSUPPORTED，且**不发请求**。

    不发请求很重要：这些站不该因为加了个签到作业就多打一次站点。
    """
    from app.adapters.sites.base import CheckinStatus

    adapter = _make(name)
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.route().mock(
                side_effect=AssertionError("未实现签到的站不该发请求")
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.UNSUPPORTED
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
async def test_checkin_detects_success(name: str) -> None:
    """已实现的 3 站：签到页返回成功文案 → SUCCESS。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make(name)
    try:
        with respx.mock(assert_all_mocked=True) as router:
            router.route().mock(
                return_value=httpx.Response(
                    200,
                    text=(
                        "<html><body>签到成功 这是您的第 614 次签到，"
                        "已连续签到 1 天，本次签到获得 10 个魔力值。</body></html>"
                    ),
                )
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.SUCCESS
        assert result.total_count == 614
        assert result.reward == 10
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
async def test_checkin_detects_already(name: str) -> None:
    """当日已签 → ALREADY（幂等，**不算失败**）。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make(name)
    try:
        with respx.mock(assert_all_mocked=True) as router:
            router.route().mock(
                return_value=httpx.Response(
                    200, text="<html><body>您今天已经签到过了，请勿重复刷新。</body></html>"
                )
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.ALREADY
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
async def test_checkin_raises_on_network_error(name: str) -> None:
    """网络故障必须抛 ``SiteFetchError``——不能吞掉返回"看起来正常"的结果。

    与 ``search_torrents`` 同一条契约（2026-10-21 prod 事故的教训）。
    """
    adapter = _make(name)
    try:
        with _mock_all_error(
            httpx.ConnectError("Name or service not known")
        ), pytest.raises(SiteFetchError):
            await adapter.checkin()
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
@pytest.mark.parametrize("status", [403, 429])
async def test_checkin_raises_breaker_on_rate_limit(
    name: str, status: int
) -> None:
    """403/429 → ``BreakerTripped``（限流信号，与拉取路径同一契约）。"""
    adapter = _make(name)
    try:
        with _mock_all_error(
            httpx.Response(status, text="rate limited")
        ), pytest.raises(BreakerTripped):
            await adapter.checkin()
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
async def test_checkin_falls_back_to_post_when_get_did_not_signin(
    name: str,
) -> None:
    """GET 没签到（站点改了配置）→ POST 一次执行签到。

    上游 NexusPHP 的语义：GET 只查状态（除非站点关掉了图形验证码，
    那时 GET 会顺带签到）。POST 才是「执行签到」动作。
    """
    from app.adapters.sites.base import CheckinStatus

    adapter = _make(name)
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            get_route = router.get("https://example.com/attendance.php").mock(
                return_value=httpx.Response(
                    200, text="<html><body><p>签到</p><p>???</p></body></html>"
                )
            )
            post_route = router.post("https://example.com/attendance.php").mock(
                return_value=httpx.Response(
                    200,
                    text=(
                        "<html><body>签到成功 这是您的第 614 次签到，"
                        "已连续签到 1 天，本次签到获得 10 个魔力值。</body></html>"
                    ),
                )
            )
            result = await adapter.checkin()

        assert get_route.called, "应该先 GET 查状态"
        assert post_route.called, "GET 没签到时应该 POST 兜底"
        assert result.status == CheckinStatus.SUCCESS
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
async def test_checkin_does_not_post_when_captcha_detected(name: str) -> None:
    """检测到人机验证 → 不 POST（POST 也要验证码，不会更好）。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make(name)
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            get_route = router.get("https://example.com/attendance.php").mock(
                return_value=httpx.Response(
                    200, text='<html><body><div class="cf-turnstile"></div></body></html>'
                )
            )
            post_route = router.post("https://example.com/attendance.php").mock(
                return_value=httpx.Response(200, text="should not be called")
            )
            result = await adapter.checkin()

        assert get_route.called
        assert not post_route.called, "人机验证时不该再 POST"
        assert result.status == CheckinStatus.UNSUPPORTED
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", CHECKIN_ADAPTERS)
async def test_checkin_without_cookies_is_auth_expired(name: str) -> None:
    """缺 cookies → AUTH_EXPIRED，且不发请求。"""
    from app.adapters.sites.base import CheckinStatus, SiteProfile

    cls = resolve(name)
    adapter = cls(SiteProfile(name=name, domain="example.com"))
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.route().mock(
                side_effect=AssertionError("缺 cookies 时不该发请求")
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.AUTH_EXPIRED
    finally:
        await adapter.close()


# ------------------------------------------------------------------ 数据类

def test_feed_item_defaults() -> None:
    item = FeedItem(title="t")
    assert item.item_id is None
    assert item.info_hash is None
    assert item.size == 0
    assert item.category is None
    assert item.published_at is None
    assert item.description == ""
    assert item.download_factor is None
    assert item.free_payload is None


def test_torrent_brief_values() -> None:
    assert TorrentBrief.EXISTS == "exists"
    assert TorrentBrief.DELETED == "deleted"
    assert TorrentBrief.VIOLATION == "violation"
    assert TorrentBrief.UNKNOWN == "unknown"
