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

import pathlib
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from app.adapters.sites import resolve
from app.adapters.sites._registry import list_available, list_meta
from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    Promotion,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)
from app.adapters.sites.hdwing import _current_promotion, _parse_promotion_records
from app.core.timeutils import bj_now_dt, format_bj

#: 10 个已注册站点适配器
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
    "hdwing",
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

#: 已实现自动签到的 4 站（实测 2026-10-22 可用）
#: - hhanclub / soulvoice / hdhome：NexusPHP 标准 /attendance.php，GET 即签到
#: - hdsky：自建 showup.php 三段式，图形验证码走 AI 读码
CHECKIN_ADAPTERS = ("hhanclub", "soulvoice", "hdhome", "hdsky", "hdwing")

#: 未实现签到的站：无此功能（springSunday / mteam）或需 Cloudflare Turnstile
#: （audiences / ourbits）。Turnstile 实测服务端环境无法完成
#: （TurnstileError 600010），本仓库不实现。
NO_CHECKIN_ADAPTERS = tuple(a for a in ALL_ADAPTERS if a not in CHECKIN_ADAPTERS)

#: 走 AI 读码流程的站（签到需要图形验证码）
AI_CHECKIN_ADAPTERS = ("hdsky",)

#: 走 NexusPHP 标准 /attendance.php 的站（无验证码，GET 即签到）
HTML_CHECKIN_ADAPTERS = ("hhanclub", "soulvoice", "hdhome")


@pytest.fixture
def fake_reader():
    """注入一个"永远读对"的假读码器。

    用 ``set_ai_reader`` 替换 core 级单例（适配器通过 ``get_ai_reader()`` 取），
    用完复位——避免污染其它用例。

    用鸭子类型而非继承：测试只需要 ``available`` 与 ``read_captcha`` 两个成员。
    """
    from app.core.ai import set_ai_reader

    class _FakeReader:
        available = True
        calls = 0

        async def read_captcha(self, image: bytes, *, length: int = 6) -> str:
            self.calls += 1
            return "ABC123"[:length]

    set_ai_reader(_FakeReader())
    yield
    set_ai_reader(None)


@pytest.fixture
def no_reader():
    """把读码器置为「未配置」状态。"""
    from app.core.ai import set_ai_reader

    set_ai_reader(None)
    yield
    set_ai_reader(None)


@pytest.mark.parametrize("name", ALL_ADAPTERS)
async def test_checkin_returns_checkin_result(name: str, no_reader: None) -> None:
    """签到契约：返回 ``CheckinResult``（不抛）。

    ``no_reader`` 保证未配置 AI 时 hdsky 也走降级路径而非发请求。
    """
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


@pytest.mark.parametrize("name", HTML_CHECKIN_ADAPTERS)
async def test_checkin_detects_success(name: str) -> None:
    """已实现的 3 个 HTML 站：签到页返回成功文案 → SUCCESS。"""
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


@pytest.mark.parametrize("name", HTML_CHECKIN_ADAPTERS)
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


@pytest.mark.parametrize("name", HTML_CHECKIN_ADAPTERS)
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
    name: str, status: int, fake_reader: None
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


@pytest.mark.parametrize("name", HTML_CHECKIN_ADAPTERS)
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


@pytest.mark.parametrize("name", HTML_CHECKIN_ADAPTERS)
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


@pytest.mark.parametrize("name", HTML_CHECKIN_ADAPTERS)
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


# --------------------------------------- hdsky：showup 三段式 + AI 读码

#: hdsky 的签到端点（与其它站的 /attendance.php 不同）
_HD_CHECK = "https://example.com/showup.php"
_HD_NEW_CODE = "https://example.com/image_code_ajax.php"
_HD_IMAGE = "https://example.com/image.php"

#: 一张最小"验证码图"（内容无关，AI 读码器在测试里是假的）
_FAKE_PNG = httpx.Response(
    200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64, headers={"content-type": "image/png"}
).content


def _mock_hdsky_flow(
    router: respx.MockRouter,
    *,
    sign: bool = False,
    submits: list[object] | None = None,
) -> dict[str, object]:
    """铺 hdsky 签到流程的路由。

    ``{action:check}`` 与 ``{action:showup}`` **打的是同一个 URL**，
    respx 只能按 URL 匹配，所以用一个回调按请求体里的 ``action`` 分派。

    :param sign: ``{action:check}`` 返回的今日签到状态。
    :param submits: 每次 ``showup`` 提交依次返回的 JSON（默认永远成功）。

    :returns: 路由引用，测试用它断言"申请了几次验证码"等。
    """
    queue = list(submits) if submits else None

    def _showup(request: httpx.Request) -> httpx.Response:
        action = parse_qs(request.content.decode()).get("action", [""])[0]
        if action == "check":
            return httpx.Response(
                200, json={"success": True, "message": "", "sign": sign}
            )
        payload = queue.pop(0) if queue else {"success": True, "message": 10}
        return httpx.Response(200, json=payload)

    showup = router.post(_HD_CHECK).mock(side_effect=_showup)
    new_code = router.post(_HD_NEW_CODE).mock(
        return_value=httpx.Response(200, json={"success": True, "code": "a" * 32})
    )
    image = router.get(_HD_IMAGE).mock(
        return_value=httpx.Response(200, content=_FAKE_PNG)
    )
    return {"showup": showup, "new_code": new_code, "image": image}


async def test_hdsky_checkin_success(fake_reader: None) -> None:
    """完整三段式：查状态 → 申请验证码 → AI 读码 → 提交 → SUCCESS。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            _mock_hdsky_flow(router, submits=[{"success": True, "message": 12}])
            result = await adapter.checkin()
        assert result.status == CheckinStatus.SUCCESS
        assert result.reward == 12
    finally:
        await adapter.close()


async def test_hdsky_checkin_already_skips_captcha(fake_reader: None) -> None:
    """今日已签 → 直接 ALREADY，**不申请验证码**（省一次 AI 调用）。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            routes = _mock_hdsky_flow(router, sign=True)
            result = await adapter.checkin()
            assert not routes["new_code"].called, "已签到时不该申请验证码"
            assert not routes["image"].called, "已签到时不该取验证码"
        assert result.status == CheckinStatus.ALREADY
    finally:
        await adapter.close()


async def test_hdsky_unsupported_without_ai(no_reader: None) -> None:
    """未配置 AI 读码 → UNSUPPORTED，且**不发任何请求**（作业不该因此变红）。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.route().mock(side_effect=AssertionError("未配置 AI 时不该发请求"))
            result = await adapter.checkin()
        assert result.status == CheckinStatus.UNSUPPORTED
    finally:
        await adapter.close()


async def test_hdsky_auth_expired_when_showup_not_json(fake_reader: None) -> None:
    """showup 返回登录页（cookie 失效）→ AUTH_EXPIRED。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.post(_HD_CHECK).mock(
                return_value=httpx.Response(200, text="<html>登录</html>")
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.AUTH_EXPIRED
    finally:
        await adapter.close()


async def test_hdsky_retries_with_new_captcha_on_wrong_answer(fake_reader: None) -> None:
    """验证码答错 → 换一张新图重试，最终成功。

    服务端用 ``invalid_imagehash``（答错）与 ``date_unmatch``（已签）
    区分两种情况——作业必须靠这个决定"重试"还是"收工"。
    """
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.post(_HD_CHECK).mock(
                return_value=httpx.Response(
                    200, json={"success": True, "message": "", "sign": False}
                )
            )
            router.post(_HD_NEW_CODE).mock(
                return_value=httpx.Response(
                    200, json={"success": True, "code": "a" * 32}
                )
            )
            router.get(_HD_IMAGE).mock(
                return_value=httpx.Response(200, content=_FAKE_PNG)
            )
            # 第一次提交答错，第二次成功
            routes = _mock_hdsky_flow(router, submits=[
                {"success": False, "message": "invalid_imagehash"},
                {"success": True, "message": 12},
            ])
            result = await adapter.checkin()
        assert result.status == CheckinStatus.SUCCESS
        assert routes["new_code"].call_count == 2, "答错应换验证码重试"
    finally:
        await adapter.close()


async def test_hdsky_ai_error_is_failed_not_crash() -> None:
    """AI 读码抛错（429 / 网络 / 输出不可用）→ FAILED，不往上抛。

    适配器要把它变成一次**可统计的失败**，让作业继续跑完其它站点，
    而不是整个作业异常退出。实测网关会返回 429 限流。
    """
    from app.adapters.sites.base import CheckinStatus
    from app.core.ai import AIClientError, set_ai_reader

    class _BrokenReader:
        available = True

        async def read_captcha(self, image: bytes, *, length: int = 6) -> str:
            raise AIClientError("AI 读码 HTTP 429")

    set_ai_reader(_BrokenReader())
    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            _mock_hdsky_flow(router)
            result = await adapter.checkin()
        assert result.status == CheckinStatus.FAILED
        assert "429" in result.message
    finally:
        await adapter.close()
        set_ai_reader(None)


async def test_hdsky_gives_up_after_max_attempts(fake_reader: None) -> None:
    """连续答错到上限 → FAILED（不是 UNSUPPORTED，也不是误报成功）。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            routes = _mock_hdsky_flow(
                router,
                submits=[{"success": False, "message": "invalid_imagehash"}] * 5,
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.FAILED
        assert routes["new_code"].call_count == 3, "重试次数应封顶"
    finally:
        await adapter.close()


async def test_hdsky_already_on_submit_stops_retrying(fake_reader: None) -> None:
    """提交时返回 ``date_unmatch``（今日已签）→ ALREADY，**不再重试**。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdsky")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            routes = _mock_hdsky_flow(
                router, submits=[{"success": False, "message": "date_unmatch"}] * 5
            )
            result = await adapter.checkin()
        assert result.status == CheckinStatus.ALREADY
        assert routes["new_code"].call_count == 1, "已签到不该继续重试"
    finally:
        await adapter.close()


# ------------------------------------------------------------------ hdwing

#: hdwing 列表 fixture（真实页面脱敏裁剪，见 scripts/capture_mybonus_fixtures.py
#: 与 docs/merge-strmmanger.md 同款「真实页面固化」方法论）
_HDWING_TORRENTS = (pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "hdwing_torrents.html").read_text(encoding="utf-8")


def _hdwing_fixture(name: str) -> str:
    """读 hdwing 优惠历史 fixture（真实页面裁剪，见
    scripts/capture_hdwing_promotion_fixtures.py）。"""
    path = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / name
    return path.read_text(encoding="utf-8")


def _mock_hdwing_list(router: respx.MockRouter) -> None:
    """铺 hdwing 列表页路由（**不带任何筛选参数**）。"""
    router.get("https://example.com/torrents.php?page=0").mock(
        return_value=httpx.Response(200, text=_HDWING_TORRENTS)
    )


async def test_hdwing_search_parses_list_without_filter() -> None:
    """列表解析：**不带筛选参数**、**不请求优惠接口**、不产 free_payload。

    hdwing 的优惠标记不在列表 HTML 里（只有空占位
    ``span.sp_state_placeholder``），而填充它的 ``ajax_promotion.php`` 被
    Cloudflare Turnstile 闸保护——未过闸时对**任意 id**（含不存在的种子）都
    返回兜底的 ``pro_free``。照它解析会把「限免 6 小时」误判成「永久免费」。

    所以列表抓取**不碰**优惠接口，免费判定走
    :meth:`HdwingAdapter.get_promotion`（服务端渲染的优惠历史表）。
    """
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            _mock_hdwing_list(router)
            promo = router.post("https://example.com/ajax_promotion.php").mock(
                return_value=httpx.Response(200, text="{}")
            )
            items = await adapter.search_torrents({}, page_number=1)
        assert len(items) == 2
        first = items[0]
        assert first.item_id == "836813"
        assert first.title.startswith("Spider-Man.Brand.New.Day.2026")
        assert first.size == int(25.26 * 1024**3)
        assert first.published_at == "2026-10-05 21:59:21"
        assert first.category == "4K UltraHD"
        # 免费判定不来自列表页 → 不带 free_payload
        assert first.free_payload is None
        assert items[1].item_id == "837150"
        # 假数据源一次都不能碰
        assert not promo.called
    finally:
        await adapter.close()


async def test_hdwing_search_page_number_maps_to_zero_based() -> None:
    """``page`` 从 0 起：第 1 页 → ``page=0``，第 2 页 → ``page=1``。"""
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/torrents.php?page=0").mock(
                return_value=httpx.Response(200, text=_HDWING_TORRENTS)
            )
            router.get("https://example.com/torrents.php?page=1").mock(
                return_value=httpx.Response(200, text=_HDWING_TORRENTS)
            )
            await adapter.search_torrents({}, page_number=1)
            await adapter.search_torrents({}, page_number=2)
    finally:
        await adapter.close()


async def test_hdwing_search_empty_page_returns_empty() -> None:
    """空列表（页面结构还在）→ ``[]``，不算失败。"""
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/torrents.php?page=0").mock(
                return_value=httpx.Response(
                    200, text='<html><table class="torrent_list"></table></html>'
                )
            )
            items = await adapter.search_torrents({}, page_number=1)
            assert items == []
    finally:
        await adapter.close()


async def test_hdwing_search_raises_when_list_structure_changed() -> None:
    """页面结构变了（``torrent_list`` 表消失）→ SiteFetchError。

    不报错的话会被当成「站点确实没有种子」静默停摆——与「拉取失败必须抛」
    同一个道理（2026-10-21 prod 事故的教训）。判定靠「页面确实是站点页面
    （导航还在）却没有列表表」——空白页仍按通用契约返回 ``[]``。
    """
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/torrents.php?page=0").mock(
                return_value=httpx.Response(
                    200,
                    text=(
                        '<html><head><title>HDWinG :: 种子列表</title></head>'
                        '<body><a href="torrents.php">种子</a>全新布局</body></html>'
                    ),
                )
            )
            with pytest.raises(SiteFetchError, match="结构变化"):
                await adapter.search_torrents({}, page_number=1)
    finally:
        await adapter.close()


async def test_hdwing_get_promotion_parses_active_free() -> None:
    """优惠历史表 → Free 记录（含起止时间）。

    真实页面固化：``promotion.php?id=840093`` 的「种子优惠日志」区块。

    ⚠️ 这里断言的是**纯解析**结果（``_parse_promotion_records``），不是
    「当前是否生效」——fixture 里的截止时间是 2026-11-16，过了那个点
    「生效中」就变成「已过期」，断言会随时间自己变红。生效判定另有合成
    数据的用例（``test_hdwing_current_promotion_*``）。
    """
    records = _parse_promotion_records(
        _hdwing_fixture("hdwing_promotion_active.html")
    )
    assert len(records) == 1
    rec = records[0]
    assert rec.kind == "Free"
    assert rec.start_at == "2026-11-16 15:29:40"
    assert rec.end_at == "2026-11-16 21:29:40"


async def test_hdwing_current_promotion_keeps_future_and_permanent() -> None:
    """「当前生效」判定：截止在未来 → 生效；截止为空 → 永久 → 生效。"""
    now = bj_now_dt()
    future = format_bj(now + timedelta(days=2))
    past = format_bj(now - timedelta(days=2))

    assert _current_promotion(
        [Promotion(kind="Free", start_at=None, end_at=future)]
    ) is not None
    assert _current_promotion(
        [Promotion(kind="Free", start_at=None, end_at=None)]
    ) is not None
    assert _current_promotion(
        [Promotion(kind="Free", start_at=None, end_at=past)]
    ) is None


async def test_hdwing_get_promotion_skips_expired_and_non_free_rows() -> None:
    """过期记录 + 非 Free 类型（50%）都不算数。

    真实页面（``id=840065``）有两行：生效中的 Free + 已过期的 50%
    （过期行带删除线样式，但**不依赖**那个样式判定）。
    """
    records = _parse_promotion_records(
        _hdwing_fixture("hdwing_promotion_mixed.html")
    )
    # 只取 Free 行：50%（半价下载）不是免费种
    assert len(records) == 1
    assert records[0].kind == "Free"
    assert records[0].end_at == "2026-11-18 17:52:34"


async def test_hdwing_get_promotion_returns_none_without_records() -> None:
    """有表头但无数据行 → ``None``（当前不是免费种，不是故障）。"""
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/promotion.php?id=840080").mock(
                return_value=httpx.Response(
                    200, text=_hdwing_fixture("hdwing_promotion_empty.html")
                )
            )
            promo = await adapter.get_promotion("840080")
        assert promo is None
    finally:
        await adapter.close()


async def test_hdwing_get_promotion_expired_record_is_not_current() -> None:
    """Free 记录存在但**已过期** → 当前不免费（``None``）。

    这是「限时免费」的核心语义：窗口过了就不是免费种了。
    """
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/promotion.php?id=839486").mock(
                return_value=httpx.Response(
                    200, text=_hdwing_fixture("hdwing_promotion_expired.html")
                )
            )
            promo = await adapter.get_promotion("839486")
        assert promo is None
    finally:
        await adapter.close()


async def test_hdwing_get_promotion_404_returns_none() -> None:
    """404 = 种子不存在 → ``None``（不该炸掉整批判定）。"""
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/promotion.php?id=999999999").mock(
                return_value=httpx.Response(404, text="哎呀…您访问的页面不存在")
            )
            promo = await adapter.get_promotion("999999999")
        assert promo is None
    finally:
        await adapter.close()


async def test_hdwing_get_promotion_429_trips_breaker() -> None:
    """429 = 限流 → BreakerTripped（熔断保护账号）。"""
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/promotion.php?id=840093").mock(
                return_value=httpx.Response(429, text="too many")
            )
            with pytest.raises(BreakerTripped):
                await adapter.get_promotion("840093")
    finally:
        await adapter.close()


async def test_hdwing_get_promotion_500_raises_fetch_error() -> None:
    """500 = 故障 → SiteFetchError（如实报障，不静默当「不是免费」）。"""
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/promotion.php?id=840093").mock(
                return_value=httpx.Response(500, text="boom")
            )
            with pytest.raises(SiteFetchError, match="优惠历史"):
                await adapter.get_promotion("840093")
    finally:
        await adapter.close()


async def test_hdwing_get_torrent_url_appends_own_uid() -> None:
    """下载 URL 必须带 &uid=<自己>——不带会落到「下载提示」确认页。

    自身 uid 从 navbar ``invite.php?id=N`` 提取（hash 签名内嵌 uid 但
    服务端还要校验 query 参数，实测 2026-11-07）。
    """
    adapter = _make("hdwing")
    try:
        detail_html = (
            '<html><head><title>HDWinG :: 种子详情 "TestTorrent"</title></head>'
            '<body>[<a href="invite.php?id=328600">发送</a>]'
            '<a title="下载种子" href="download.php?hash=MQozMjg2MDAKMzI4NjAw.ABC-xyz_123">'
            "</a></body></html>"
        )
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/details.php?id=836813").mock(
                return_value=httpx.Response(200, text=detail_html)
            )
            url = await adapter.get_torrent_url("836813")
        assert url == (
            "https://example.com/download.php"
            "?hash=MQozMjg2MDAKMzI4NjAw.ABC-xyz_123&uid=328600"
        )
    finally:
        await adapter.close()


async def test_hdwing_download_rejects_html_notice() -> None:
    """下载 URL 返回 HTML（分享率确认页）→ BreakerTripped，不是假成功。"""
    adapter = _make("hdwing")
    try:
        detail_html = (
            '<html><body>[<a href="invite.php?id=328600">发送</a>]'
            '<a href="download.php?hash=MQozMjg2MDAKMzI4NjAw.ABC-xyz_123"></a>'
            "</body></html>"
        )
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/details.php?id=836813").mock(
                return_value=httpx.Response(200, text=detail_html)
            )
            router.get(
                "https://example.com/download.php"
                "?hash=MQozMjg2MDAKMzI4NjAw.ABC-xyz_123&uid=328600"
            ).mock(
                return_value=httpx.Response(
                    200, text="<html>下载提示：请先勾选我会提高分享率的</html>"
                )
            )
            with pytest.raises(BreakerTripped, match="bencoded"):
                await adapter.download_torrent("836813")
    finally:
        await adapter.close()


async def test_hdwing_check_status_deleted_marker() -> None:
    """不存在的种：HTTP 200 + 「没有该ID的种子」→ DELETED。

    hdwing 的错误页**不是 404**（实测 200），必须靠文案判定。
    """
    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/details.php?id=99999999").mock(
                return_value=httpx.Response(
                    200, text="<html>错误 没有该ID的种子</html>"
                )
            )
            brief = await adapter.check_status("99999999")
        assert brief == TorrentBrief.DELETED
    finally:
        await adapter.close()


# ------------------------------------- 凭据失效检测（2026-11-08 prod 事故）

#: 9 个 HTML 站（mteam 是 API 站，用 apikey 鉴权，无 cookie 概念）
_HTML_ADAPTERS = (
    "audiences",
    "hddolby",
    "hdsky",
    "hhanclub",
    "ourbits",
    "soulvoice",
    "springsunday",
    "hdhome",
    "hdwing",
)


def _login_redirect_router() -> respx.MockRouter:
    """模拟「凭据失效」：受保护页面 302 → login.php（跟随后 200 登录页）。

    站点行为：cookie 过期时把页面 302 到 ``/login.php?returnto=...``，
    跟随后是 **200 的登录页**——所以只查状态码查不出来（不是 4xx）。
    """
    router = respx.mock(assert_all_mocked=True, assert_all_called=False)

    def _handler(request: httpx.Request) -> httpx.Response:
        if "login.php" in str(request.url):
            return httpx.Response(200, text="<html>登录</html>")
        return httpx.Response(
            302, headers={"location": "https://example.com/login.php?returnto=x"}
        )

    router.route().mock(side_effect=_handler)
    return router


@pytest.mark.parametrize("name", _HTML_ADAPTERS)
async def test_search_torrents_raises_on_login_redirect(name: str) -> None:
    """列表页被重定向到登录页 → 抛 SiteFetchError，**不能静默返回 []**。

    2026-11-08 prod：ourbits 凭据过期 10 天，列表页一直 302→登录页 →
    解析出 0 条 → 被当成「站点确实没有种子」→ 作业从不点名，静默停摆。
    """
    adapter = _make(name)
    try:
        with _login_redirect_router(), pytest.raises(SiteFetchError, match="凭据已失效"):
            await adapter.search_torrents({}, page_number=1)
    finally:
        await adapter.close()


@pytest.mark.parametrize("name", _HTML_ADAPTERS)
async def test_user_snapshot_raises_on_login_redirect(name: str) -> None:
    """用户数据页被重定向到登录页 → 报「凭据已失效」而不是「解析失败」。

    拿到的其实是登录页，报「3 个业务字段均未取到」会把排查方向引偏
    （让人去查解析器/页面结构，而不是查 cookie）。
    """
    adapter = _make(name)
    try:
        with _login_redirect_router(), pytest.raises(SiteFetchError, match="凭据已失效"):
            await adapter.get_user_snapshot()
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


# ---------------------------------------------- m-team：API 三端点 + 时魔


def _mteam_routes(router: respx.MockRouter, *, final_bs: str = "112.146") -> None:
    """铺 m-team 的 3 个 API 端点。

    m-team 是唯一的 API 站（``x-api-key`` 鉴权、JSON 响应），签到无关，
    但 user_data 要调 3 个端点：profile / myPeerStatistics / mybonus。
    """
    router.post("https://api.example.com/api/member/profile").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "0",
                "message": "SUCCESS",
                "data": {
                    "id": "123",
                    "memberCount": {
                        "uploaded": 1000,
                        "downloaded": 2000,
                        "bonus": "1630793.3",  # 累计余额，不是时魔
                    },
                },
            },
        )
    )
    router.post("https://api.example.com/api/tracker/myPeerStatistics").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "0",
                "message": "SUCCESS",
                "data": {"seederCount": 34, "leecherCount": 2},
            },
        )
    )
    router.post("https://api.example.com/api/tracker/mybonus").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "0",
                "message": "SUCCESS",
                "data": {
                    "bonus": {"BONUS_UPLOAD_TORRENT": "50"},
                    "formulaParams": {
                        "bzeroBonus": "50",
                        "lbonus": "300",
                        "allBonus": "53.581",
                        "callBonus": "53.164",
                        "h24UpBs": "2.711",
                        "userClassBs": "2.679",
                        "finalBs": final_bs,
                    },
                },
            },
        )
    )


async def test_mteam_snapshot_includes_hourly_bonus() -> None:
    """m-team 快照：3 业务字段 + 时魔（来自 mybonus 的 finalBs）。"""
    from app.adapters.sites.user_data import HourlyBonus

    adapter = _make("mteam")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            _mteam_routes(router)
            snap = await adapter.get_user_snapshot()
        assert snap is not None
        assert snap.upload == 1000
        assert snap.download == 2000
        assert snap.seeding_count == 34
        assert isinstance(snap.hourly_bonus, HourlyBonus)
        assert snap.hourly_bonus.amount == pytest.approx(112.146)
        assert snap.hourly_bonus.unit == "魔力值"
        # 页面不提供上限信号 / 总进度
        assert snap.hourly_bonus.capped is None
        assert snap.hourly_bonus.progress is None
    finally:
        await adapter.close()


async def test_mteam_mybonus_failure_fails_whole_snapshot() -> None:
    """mybonus 端点失败 → 抛 SiteFetchError（快照要么完整要么没有）。

    降级成 ``hourly_bonus=None`` 会让 upsert 把上次的时魔值**覆盖成 NULL**，
    UI 上突然空白——所以整个快照作废、保留上次数据。
    """
    adapter = _make("mteam")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            _mteam_routes(router)
            router.post("https://api.example.com/api/tracker/mybonus").mock(
                return_value=httpx.Response(500, text="boom")
            )
            with pytest.raises(SiteFetchError):
                await adapter.get_user_snapshot()
    finally:
        await adapter.close()


# ------------------------------------------------- hdwing 签到（plugin_sign-in）

# hdwing 的签到**不是** NexusPHP 标准的 /attendance.php（本站 404），
# 而是 X2015 主题自带的插件端点：
#
#   GET /usercp.php                              → csrf meta + navbar 签到状态
#   POST /plugin_sign-in.php?cmd=signin {csrf}  → {"state":...}
#
# **不需要图形验证码**（实测裸 csrf 即通过，故本段不涉及 AI 读码）。

_HDWING_STATE_SIGNED = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures"
    / "hdwing_signin_state.html"
).read_text(encoding="utf-8")

_HDWING_STATE_UNSIGNED = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures"
    / "hdwing_signin_state_unsigned.html"
).read_text(encoding="utf-8")


async def test_hdwing_checkin_success() -> None:
    """未签到 → 取 csrf → POST 签到 → SUCCESS。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True) as router:
            router.get("https://example.com/usercp.php").mock(
                return_value=httpx.Response(200, text=_HDWING_STATE_UNSIGNED)
            )
            post_route = router.post(
                "https://example.com/plugin_sign-in.php?cmd=signin"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"state": "success", "signindays": 0, "integral": "10"},
                )
            )
            result = await adapter.checkin()

        assert post_route.called, "未签到时应该 POST"
        assert result.status == CheckinStatus.SUCCESS
        assert result.reward == 10
    finally:
        await adapter.close()


async def test_hdwing_checkin_already_skips_post() -> None:
    """navbar 显示「已签到」→ ALREADY，且**不发 POST**。

    不发 POST 有两个好处：省一次请求；不打扰站点（重复签到在部分站点
    会被记成异常）。
    """
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/usercp.php").mock(
                return_value=httpx.Response(200, text=_HDWING_STATE_SIGNED)
            )
            post_route = router.post(
                "https://example.com/plugin_sign-in.php?cmd=signin"
            ).mock(return_value=httpx.Response(200, json={"state": False}))
            result = await adapter.checkin()

        assert not post_route.called, "已签到时不该再 POST"
        assert result.status == CheckinStatus.ALREADY
    finally:
        await adapter.close()


async def test_hdwing_checkin_rechecks_state_when_unreasoned_failure() -> None:
    """``{"state":false}``（无 msg）→ 再查一次状态定性。

    该响应与「真失败」同形。若二次查询发现 navbar 已变成「已签到」，
    说明刚才那次其实签成功了（只是响应没给原因）→ 判 ALREADY，
    **不能**记成失败（幂等的正常结果不该让作业变红）。
    """
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True) as router:
            router.get("https://example.com/usercp.php").mock(
                side_effect=[
                    httpx.Response(200, text=_HDWING_STATE_UNSIGNED),
                    httpx.Response(200, text=_HDWING_STATE_SIGNED),
                ]
            )
            router.post("https://example.com/plugin_sign-in.php?cmd=signin").mock(
                return_value=httpx.Response(200, json={"state": False})
            )
            result = await adapter.checkin()

        assert result.status == CheckinStatus.ALREADY
    finally:
        await adapter.close()


async def test_hdwing_checkin_fails_when_state_unrecognized() -> None:
    """页面结构变了（既无「已签到」也无「签 到」）→ FAILED，**不盲签**。

    把「识别不出」当成「没签到」会对着未知页面乱打 POST。
    """
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/usercp.php").mock(
                return_value=httpx.Response(200, text="<html><body>???</body></html>")
            )
            post_route = router.post(
                "https://example.com/plugin_sign-in.php?cmd=signin"
            ).mock(return_value=httpx.Response(200, json={"state": "success"}))
            result = await adapter.checkin()

        assert not post_route.called, "状态识别不出时不该盲签"
        assert result.status == CheckinStatus.FAILED
    finally:
        await adapter.close()


async def test_hdwing_checkin_fails_when_csrf_missing() -> None:
    """页面缺少 x-csrf meta → FAILED（站点改版），不发 POST。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/usercp.php").mock(
                return_value=httpx.Response(200, text="<html><body>???</body></html>")
            )
            post_route = router.post(
                "https://example.com/plugin_sign-in.php?cmd=signin"
            ).mock(return_value=httpx.Response(200, json={"state": "success"}))
            result = await adapter.checkin()

        assert not post_route.called
        assert result.status == CheckinStatus.FAILED
    finally:
        await adapter.close()


async def test_hdwing_checkin_login_page_is_auth_expired() -> None:
    """cookie 失效（被重定向到登录页）→ AUTH_EXPIRED。"""
    from app.adapters.sites.base import CheckinStatus

    adapter = _make("hdwing")
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get("https://example.com/usercp.php").mock(
                return_value=httpx.Response(
                    200,
                    text="<html><head><title>HDWinG :: 登录</title></head></html>",
                    headers={"location": "https://example.com/login.php"},
                )
            )
            result = await adapter.checkin()

        assert result.status == CheckinStatus.AUTH_EXPIRED
    finally:
        await adapter.close()


async def test_hdwing_checkin_raises_on_network_error() -> None:
    """网络故障必须抛 SiteFetchError——不能吞掉返回"看起来正常"的结果。"""
    adapter = _make("hdwing")
    try:
        with _mock_all_error(
            httpx.ConnectError("Name or service not known")
        ), pytest.raises(SiteFetchError):
            await adapter.checkin()
    finally:
        await adapter.close()
