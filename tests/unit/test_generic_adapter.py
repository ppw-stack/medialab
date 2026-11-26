"""通用适配器的端到端（mock HTTP）测试。"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx
from app.adapters.sites.base import (
    BreakerTripped,
    SiteFetchError,
    SitePermanentError,
    SiteProfile,
)
from app.adapters.sites.generic import GenericAdapter
from app.core.timeutils import parse_bj

#: 测试用的占位凭据（**绝不是真实 API Key**）：只用于断言请求头透传。
#: 允许环境变量覆盖，代码里不出现任何凭据字面量。
_TEST_API_KEY = os.environ.get("MTEAM_APIKEY", "test-placeholder")

_LIST_HTML = """
<table class="torrents">
<tr><td>
  <table class="torrentname"><tr><td>
    <a title="测试.2024.1080p.BluRay" href="details.php?id=12345&amp;hit=1"><b>测试.2024.1080p</b></a>
    <a href="download.php?id=12345&amp;passkey=abc"><img src="/pic/dl.png"></a>
    <span class="pro_free"><img class="pro_free" src="free.png"></span>
    <span title="2024-05-01 12:00:00">2024-05-01 12:00:00</span>
  </td>
  <td class="row">1.5 GB</td>
  <td class="row"><a href="?cat=401">Movies</a></td>
  </table>
</td></tr>
</table>
"""

_DETAIL_HTML = """
<html><head><title>测试.2024.1080p</title></head><body>
<table class="torrents"><tr><td>
  <a href="download.php?id=12345&amp;passkey=abc">下载种子文件</a>
</td></tr></table>
</body></html>
"""

_INDEX_HTML = """
<html><body>
<a href="userdetails.php?id=1">测试用户</a>
<a href="logout.php">退出</a>
</body></html>
"""


def _promotion_fixture(name: str) -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / name
    ).read_text(encoding="utf-8")


#: NexusPHP「下载提示页」的真实页面（2026-11-23 prod 抓取，裁剪自 HDWinG）
_NOTICE_HTML = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "hdwing_download_notice.html"
).read_text(encoding="utf-8")


def _promotion_table(start: str, end: str) -> str:
    return f"""
<div class="noraml_box">
<table class=normal_tab>
<tr><td class=colhead>优惠类型</td><td class=colhead>开始时间</td>
<td class=colhead>截止时间</td><td class=colhead>操作人</td></tr>
<tr><td>Free</td><td>{start}</td><td>{end}</td><td>系统</td></tr>
</table>
</div>
"""


async def test_generic_search_uses_rule_pack() -> None:
    """通用适配器按规则包拼 URL 并解析列表。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/torrents.php").mock(
            return_value=httpx.Response(200, text=_LIST_HTML)
        )
        items = await adapter.search_torrents({"keyword": "测试"})
    await adapter.close()

    assert len(items) == 1
    item = items[0]
    assert item.title == "测试.2024.1080p.BluRay"
    assert item.item_id == "12345"
    assert item.download_factor == 0
    assert (item.free_payload or {}).get("free_classes") == ["free"]


async def test_generic_hdwing_promotion_active(monkeypatch) -> None:
    """HDWinG 当前生效的 Free 记录 → 返回带真实截止时间的 Promotion。"""
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(
        generic_module,
        "bj_now_dt",
        lambda: parse_bj("2026-11-22 15:30:00"),
    )
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    assert adapter.supports_promotion_lookup is True
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=841134").mock(
            return_value=httpx.Response(
                200,
                text=_promotion_fixture("hdwing_promotion_active.html"),
            )
        )
        promo = await adapter.get_promotion("841134")
    await adapter.close()

    assert promo is not None
    assert promo.kind == "Free"
    assert promo.start_at == "2026-11-22 13:19:38"
    assert promo.end_at == "2026-11-23 15:04:44"


async def test_generic_hdwing_promotion_mixed_picks_active(monkeypatch) -> None:
    """同页有生效 + 已过期 Free 时，只取生效的那条（截止最晚）。"""
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(
        generic_module,
        "bj_now_dt",
        lambda: parse_bj("2026-11-22 15:30:00"),
    )
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=640343").mock(
            return_value=httpx.Response(
                200,
                text=_promotion_fixture("hdwing_promotion_mixed.html"),
            )
        )
        promo = await adapter.get_promotion("640343")
    await adapter.close()

    assert promo is not None
    assert promo.start_at == "2026-11-07 11:50:30"
    assert promo.end_at == "2026-12-07 11:50:30"


async def test_generic_hdwing_promotion_expired_returns_none(monkeypatch) -> None:
    """Free 记录已过期 / 只有 50% 记录 → 当前没有生效优惠。"""
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(
        generic_module,
        "bj_now_dt",
        lambda: parse_bj("2026-11-22 15:30:00"),
    )
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=838960").mock(
            return_value=httpx.Response(
                200,
                text=_promotion_fixture("hdwing_promotion_expired.html"),
            )
        )
        router.get("https://hdwing.org/promotion.php?id=841155").mock(
            return_value=httpx.Response(
                200,
                text=_promotion_fixture("hdwing_promotion_no_free.html"),
            )
        )
        expired = await adapter.get_promotion("838960")
        no_free = await adapter.get_promotion("841155")
    await adapter.close()

    assert expired is None
    assert no_free is None


async def test_generic_hdwing_promotion_permanent(monkeypatch) -> None:
    """截止时间为空 = 永久免费。"""
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(
        generic_module,
        "bj_now_dt",
        lambda: parse_bj("2026-11-22 15:30:00"),
    )
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=900001").mock(
            return_value=httpx.Response(
                200,
                text=_promotion_table("2026-11-01 00:00:00", ""),
            )
        )
        promo = await adapter.get_promotion("900001")
    await adapter.close()

    assert promo is not None
    assert promo.end_at is None


async def test_generic_hdwing_promotion_future_start_ignored(monkeypatch) -> None:
    """开始时间在未来 → 当前尚未生效。"""
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(
        generic_module,
        "bj_now_dt",
        lambda: parse_bj("2026-11-22 15:30:00"),
    )
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=900002").mock(
            return_value=httpx.Response(
                200,
                text=_promotion_table("2027-01-01 00:00:00", "2027-02-01 00:00:00"),
            )
        )
        promo = await adapter.get_promotion("900002")
    await adapter.close()

    assert promo is None


async def test_generic_hdwing_promotion_404_and_not_found_page() -> None:
    """种子被删 / 不存在的 200 提示页都按「无优惠」处理，不算故障。"""
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    not_found_html = (
        "<html><head><title>哎呀…您访问的页面不存在</title></head>"
        "<body>您访问的页面不存在</body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=404404").mock(
            return_value=httpx.Response(404, text="not found")
        )
        router.get("https://hdwing.org/promotion.php?id=1").mock(
            return_value=httpx.Response(200, text=not_found_html)
        )
        deleted = await adapter.get_promotion("404404")
        missing = await adapter.get_promotion("1")
    await adapter.close()

    assert deleted is None
    assert missing is None


async def test_generic_hdwing_promotion_login_page_is_permanent() -> None:
    """优惠页返回登录页 → 凭据失效，抛永久错误而不是写「非免费」。"""
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    login_html = (
        "<html><head><title>登录</title></head><body>"
        '<form action="login.php"><input type="password" name="passwd">'
        "</form></body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=841134").mock(
            return_value=httpx.Response(200, text=login_html)
        )
        with pytest.raises(SitePermanentError):
            await adapter.get_promotion("841134")
    await adapter.close()


async def test_generic_hdwing_promotion_429_trips_breaker() -> None:
    """优惠页限流 → 熔断，交给 service 层写 breaker_until。"""
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=841134").mock(
            return_value=httpx.Response(429, text="too many requests")
        )
        with pytest.raises(BreakerTripped):
            await adapter.get_promotion("841134")
    await adapter.close()


async def test_generic_hdwing_promotion_unexpected_page_raises() -> None:
    """页面结构异常（无优惠表头）→ 抛错，不能当成「没有优惠」写库。"""
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="hdwing.org"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hdwing.org/promotion.php?id=841134").mock(
            return_value=httpx.Response(
                200,
                text="<html><body>Cloudflare 挑战页</body></html>",
            )
        )
        with pytest.raises(SiteFetchError):
            await adapter.get_promotion("841134")
    await adapter.close()


async def test_generic_non_hdwing_has_no_promotion_lookup() -> None:
    """普通 NexusPHP 站不走逐种优惠查询（列表已 SSR 优惠标记）。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    assert adapter.supports_promotion_lookup is False
    assert await adapter.get_promotion("12345") is None
    await adapter.close()


async def test_generic_check_reachability_detects_login() -> None:
    """首页出现 userdetails/logout 时判定为已登录。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://pt.0ff.cc/").mock(
            return_value=httpx.Response(200, text=_INDEX_HTML)
        )
        health = await adapter.check_reachability()
    await adapter.close()

    assert health.reachable is True
    assert health.logged_in is True


async def test_generic_torrent_url_from_detail_page() -> None:
    """详情页里的下载锚点被补成绝对 URL。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=12345").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        url = await adapter.get_torrent_url("12345")
    await adapter.close()

    assert url == "https://pt.0ff.cc/download.php?id=12345&passkey=abc"


async def test_generic_download_retries_non_torrent_response(monkeypatch) -> None:
    """下载拿到非 .torrent 内容（站点瞬时异常）→ 短退避后重试一次即成功。"""
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(generic_module, "_NON_TORRENT_RETRY_DELAY", 0)
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    torrent = b"d4:infod4:name4:testee"
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=12345").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        router.get(url__startswith="https://pt.0ff.cc/download.php").mock(
            side_effect=[
                httpx.Response(
                    200,
                    text="<html>系统繁忙，请稍后再试</html>",
                    headers={"content-type": "text/html; charset=utf-8"},
                ),
                httpx.Response(
                    200,
                    content=torrent,
                    headers={"content-type": "application/x-bittorrent"},
                ),
            ]
        )
        data = await adapter.download_torrent("12345")
    await adapter.close()

    assert data == torrent


async def test_generic_download_reports_non_torrent_diagnostics(monkeypatch) -> None:
    """两次都不是 torrent → 异常里带 status / content-type / 正文前缀。

    此前只报「响应不是 bencoded」，无法区分登录页、限频页与 CF 挑战；
    2026-11-21 prod 排查时因此无法定位（只能事后复现确认站点已恢复）。
    """
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(generic_module, "_NON_TORRENT_RETRY_DELAY", 0)
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=840968").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        router.get(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(
                200,
                text="<html>请登录后下载</html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        with pytest.raises(SiteFetchError) as exc:
            await adapter.download_torrent("840968")
    await adapter.close()

    message = str(exc.value)
    assert "不是 bencoded torrent" in message
    assert "HTTP 200" in message
    assert "text/html" in message
    assert "请登录后下载" in message


async def test_generic_download_login_page_is_permanent(monkeypatch) -> None:
    """下载返回**登录页** → 永久失败（凭据失效），不是可重试。

    凭据过期重试 5 次也不会成功——若仍按「可重试」处理，就会骗用户
    「已入队自动重试，无需重复点击」，真故障被掩盖。
    """
    from app.adapters.sites import generic as generic_module

    monkeypatch.setattr(generic_module, "_NON_TORRENT_RETRY_DELAY", 0)
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    login_html = (
        '<html><head><title>登录</title></head><body>'
        '<form action="login.php"><input type="password" name="passwd">'
        "</form></body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=840968").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        router.get(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(
                200,
                text=login_html,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        with pytest.raises(SitePermanentError) as exc:
            await adapter.download_torrent("840968")
    await adapter.close()

    assert "登录页" in str(exc.value)


async def test_generic_download_detail_without_link_is_permanent(monkeypatch) -> None:
    """详情页正常但**没有下载地址** → 永久失败（重试也不会有）。"""
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=840968").mock(
            return_value=httpx.Response(
                200, text="<html><body>这个种子没有下载链接</body></html>"
            )
        )
        with pytest.raises(SitePermanentError) as exc:
            await adapter.download_torrent("840968")
    await adapter.close()

    assert "没有下载地址" in str(exc.value)


async def test_generic_download_detail_login_redirect_is_permanent(monkeypatch) -> None:
    """详情页跳登录页（cookie 失效）→ 永久失败。"""
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=840968").mock(
            return_value=httpx.Response(
                200,
                text='<html><head><title>登录</title></head><body>请先登录</body></html>',
            )
        )
        with pytest.raises(SitePermanentError) as exc:
            await adapter.download_torrent("840968")
    await adapter.close()

    assert "登录态失效" in str(exc.value)


async def test_generic_download_detail_unreachable_is_transient(monkeypatch) -> None:
    """详情页不可达（HTTP 500）→ **瞬时**失败（可重试）。

    站点抽风是常见的，重试有意义——不能误判成永久失败。
    """
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=840968").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(SiteFetchError) as exc:
            await adapter.download_torrent("840968")
    await adapter.close()

    # 瞬时失败用 SiteFetchError（不是 SitePermanentError）
    assert not isinstance(exc.value, SitePermanentError)


async def test_generic_download_confirms_nexusphp_notice_page() -> None:
    """NexusPHP「下载提示页」→ 走确认流程拿到种子（2026-11-23 prod 修复）。

    HDWinG 分享率 0.985 < 1 时 ``download.php`` 落到
    ``downloadnotice.php?type=ratio``：必须勾选「我会提高分享率的」并提交
    「下载种子文件」才放行。此前一律报「响应不是 bencoded」→ 手动推送失败、
    队列重试 5 次全败。
    """
    from urllib.parse import parse_qs

    torrent = b"d8:announce30:download-notice-confirmed-torente"
    posted: list[dict[str, str]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        posted.append(
            {
                key: value[0]
                for key, value in parse_qs(request.content.decode()).items()
            }
        )
        return httpx.Response(
            302,
            headers={
                "location": (
                    "https://pt.0ff.cc/download.php"
                    "?id=12345&passkey=abc&letdown=1"
                )
            },
        )

    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=12345").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        router.get("https://pt.0ff.cc/download.php?id=12345&passkey=abc").mock(
            return_value=httpx.Response(
                302,
                headers={
                    "location": (
                        "https://pt.0ff.cc/downloadnotice.php"
                        "?torrentid=12345&type=ratio"
                    )
                },
            )
        )
        router.get(url__startswith="https://pt.0ff.cc/downloadnotice.php").mock(
            return_value=httpx.Response(
                200,
                text=_NOTICE_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        router.post(url__startswith="https://pt.0ff.cc/downloadnotice.php").mock(
            side_effect=_capture
        )
        router.get(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(
                200,
                content=torrent,
                headers={"content-type": "application/x-bittorrent"},
            )
        )
        data = await adapter.download_torrent("12345")
    await adapter.close()

    assert data == torrent
    # 确认表单的字段必须齐全：少一个（letmedown / submit）站点只回 "error"
    assert posted, "应向提示页提交一次确认表单"
    assert posted[0]["letmedown"] == "ratio"
    assert posted[0]["submit"] == "下载种子文件"
    assert posted[0]["id"] == "841079"
    assert posted[0]["type"] == "ratio"


async def test_generic_download_notice_detected_by_text_marker() -> None:
    """提示页端点名被站点改掉时，靠正文「下载种子文件」文案也能识别。"""
    torrent = b"d8:announce12:text-marker-te"
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=12345").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        router.get(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(
                200,
                text=_NOTICE_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        router.post(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(
                200,
                content=torrent,
                headers={"content-type": "application/x-bittorrent"},
            )
        )
        data = await adapter.download_torrent("12345")
    await adapter.close()

    assert data == torrent


async def test_generic_download_notice_confirm_failure_is_transient() -> None:
    """提示页确认后站点仍不给种子 → **瞬时**失败（可重试），不是永久失败。

    无法确证是站点封禁（还是限频/抽风），保守按瞬时处理——判永久会让
    用户立刻看到报错而失去自动恢复的机会。
    """
    adapter = GenericAdapter(
        SiteProfile(name="HDWinG", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=12345").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        router.get(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(
                200,
                text=_NOTICE_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        router.post(url__startswith="https://pt.0ff.cc/download.php").mock(
            return_value=httpx.Response(200, text="error")
        )
        with pytest.raises(SiteFetchError) as exc:
            await adapter.download_torrent("12345")
    await adapter.close()

    assert not isinstance(exc.value, SitePermanentError)
    assert "确认后仍未给出种子" in str(exc.value)


async def test_mteam_connectivity_uses_api_tester() -> None:
    """API 站连通性走专站 test_connection（不看首页登录标记）。"""
    adapter = GenericAdapter(
        SiteProfile(name="馒头", domain="m-team.io"),
        apikey="test-key",
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith="https://api.m-team.io/api/member/profile").mock(
            return_value=httpx.Response(200, json={"code": "0", "data": {"id": 1}})
        )
        health = await adapter.check_reachability()
    await adapter.close()

    assert health.reachable is True
    assert health.logged_in is True
    assert "成功" in health.detail


async def test_mteam_connectivity_reports_auth_failure() -> None:
    """鉴权失败：可达但未登录（不是网络故障）。"""
    adapter = GenericAdapter(
        SiteProfile(name="馒头", domain="m-team.io"),
        apikey="bad-key",
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith="https://api.m-team.io/api/member/profile").mock(
            return_value=httpx.Response(
                200, json={"code": "1", "message": "鉴权已过期或无效"}
            )
        )
        health = await adapter.check_reachability()
    await adapter.close()

    assert health.reachable is True
    assert health.logged_in is False
    assert "鉴权" in health.detail


_SUBTITLE_HTML = """
<table>
<tr>
  <td class="rowfollow"><img title="简体中文" src="/lang/schinese.png"></td>
  <td class="rowfollow"><a href="download.php?id=123&amp;subid=456">测试字幕.ass</a></td>
  <td class="rowfollow"><span title="2024-05-01 12:00:00">2024-05-01</span></td>
  <td class="rowfollow">12.5 KB</td>
  <td class="rowfollow">3</td>
  <td class="rowfollow">uploader1</td>
  <td class="rowfollow"><a href="report.php?id=123">举报</a></td>
</tr>
</table>
"""


async def test_generic_subtitle_search_uses_rule() -> None:
    """规则包的 subtitles 配置能解析字幕列表。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/subtitles.php").mock(
            return_value=httpx.Response(200, text=_SUBTITLE_HTML)
        )
        items = await adapter.search_subtitles({"keyword": "测试"})
    await adapter.close()

    assert len(items) == 1
    item = items[0]
    assert item.title == "测试字幕.ass"
    assert item.language == "简体中文"
    assert item.download_url == "https://pt.0ff.cc/download.php?id=123&subid=456"
    assert item.size == 12800
    assert item.subtitle_id == "456"
    assert item.published_at == "2024-05-01 12:00:00"


async def test_generic_subtitle_search_without_config_returns_empty() -> None:
    """没有 subtitles 配置的站点返回 []（不是故障）。"""
    from app.adapters.sites.indexer.rules import load_site_rules

    rule = next(r for r in load_site_rules().indexers.values() if not r.get("subtitles"))
    adapter = GenericAdapter(
        SiteProfile(name=str(rule.get("name")), domain=str(rule.get("domain"))),
        apikey="k",
    )
    assert await adapter.search_subtitles({"keyword": "测试"}) == []
    await adapter.close()


def test_torrent_detail_is_a_frozen_dataclass() -> None:
    """契约结构必须是 dataclass——否则 get_torrent_detail 一调就 TypeError。

    曾因漏掉 ``@dataclass`` 装饰器，``TorrentDetail(item_id=...)`` 在运行时
    抛 ``TypeError: takes no arguments``（静态检查同样报「unexpected keyword」）。
    """
    from dataclasses import fields, is_dataclass

    from app.adapters.sites.base import TorrentDetail

    assert is_dataclass(TorrentDetail)
    assert {f.name for f in fields(TorrentDetail)} >= {
        "item_id",
        "title",
        "size",
        "download_url",
        "free_payload",
        "raw_html",
        "error",
    }
    detail = TorrentDetail(item_id="123", title="片名", size=1024)
    assert detail.item_id == "123"
    assert detail.error is None


# ------------------------------------------------------------------ API 换票配方 & 接线回归


def test_parse_download_recipe_boundaries() -> None:
    """普通 URL 或畸形前缀返回 None，合法 [base64]url 返回 (params, endpoint)。"""
    import base64
    import json

    from app.adapters.sites.indexer.download import parse_download_recipe

    assert parse_download_recipe("https://example.com/download.php?id=1") is None
    assert parse_download_recipe("[not-base64]https://example.com/api") is None
    bad_json = base64.b64encode(b"[1, 2]").decode("ascii")
    assert parse_download_recipe(f"[{bad_json}]https://example.com/api") is None

    cfg = {"method": "post", "result": "data"}
    encoded = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")
    parsed = parse_download_recipe(f"[{encoded}]https://api.example.com/genDlToken")
    assert parsed == (cfg, "https://api.example.com/genDlToken")


async def test_generic_download_recipe_rejects_off_site_endpoint() -> None:
    """换票 endpoint 指向站外 / 非 https → 永久失败，绝不发请求（SSRF/凭据外泄）。"""
    adapter = GenericAdapter(
        SiteProfile(name="M-Team", domain="kp.m-team.cc"),
        apikey=_TEST_API_KEY,
    )
    cfg = {"method": "post", "cookie": True, "result": "data"}

    for endpoint in (
        "http://169.254.169.254/latest/meta-data",  # 内网元数据 + 非 https
        "https://api.m-team.io.attacker.test/genDlToken",  # 域名后缀伪装
        "https://attacker.test/genDlToken",  # 完全站外
    ):
        with respx.mock(assert_all_called=False) as router:
            # 任意请求都不该发生——若发生 respx 会返回 200，用例就会失败
            router.route().mock(return_value=httpx.Response(200, content=b"d4:spam"))
            with pytest.raises(SitePermanentError, match="换票地址"):
                await adapter._exchange_download_recipe((cfg, endpoint), "1")
    await adapter.close()


async def test_generic_download_exchanges_mteam_recipe() -> None:
    """M-Team 换票配方：POST 带 x-api-key 换真实链接，再 GET 取回 .torrent。"""
    torrent_bytes = b"d8:announce17:http://t.test/anne"
    adapter = GenericAdapter(
        SiteProfile(name="M-Team", domain="kp.m-team.cc"),
        apikey=_TEST_API_KEY,
    )
    with respx.mock(assert_all_called=False) as router:
        token_route = router.post(
            url__startswith="https://api.m-team.io/api/torrent/genDlToken"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"code": "0", "data": "https://cdn.m-team.io/dl/123.torrent"},
            )
        )
        dl_route = router.get("https://cdn.m-team.io/dl/123.torrent").mock(
            return_value=httpx.Response(200, content=torrent_bytes)
        )
        raw = await adapter.download_torrent("998877")
    await adapter.close()

    assert raw == torrent_bytes
    assert token_route.called
    assert dl_route.called
    req = token_route.calls[0].request
    assert req.headers.get("x-api-key") == _TEST_API_KEY
    assert req.url.params.get("id") == "998877"


async def test_generic_download_recipe_error_classification() -> None:
    """换票失败分级：401 → 永久失败；429 → 熔断；空 data → 瞬时失败。"""
    adapter = GenericAdapter(
        SiteProfile(name="M-Team", domain="kp.m-team.cc"),
        apikey=_TEST_API_KEY,
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(
            url__startswith="https://api.m-team.io/api/torrent/genDlToken"
        ).mock(return_value=httpx.Response(401, json={"code": "401"}))
        with pytest.raises(SitePermanentError):
            await adapter.download_torrent("1")

    with respx.mock(assert_all_called=False) as router:
        router.post(
            url__startswith="https://api.m-team.io/api/torrent/genDlToken"
        ).mock(return_value=httpx.Response(429, text="rate limit"))
        with pytest.raises(BreakerTripped):
            await adapter.download_torrent("2")

    with respx.mock(assert_all_called=False) as router:
        router.post(
            url__startswith="https://api.m-team.io/api/torrent/genDlToken"
        ).mock(return_value=httpx.Response(200, json={"code": "1", "data": None}))
        with pytest.raises(SiteFetchError):
            await adapter.download_torrent("3")
    await adapter.close()


def test_api_spiders_expose_public_get_download_url() -> None:
    """Rousi / Yema / SunnyPT 均暴露公开 get_download_url 并产出可解析配方。"""
    from app.adapters.sites.indexer.download import parse_download_recipe
    from app.adapters.sites.indexer.spider.rousi import RousiSpider
    from app.adapters.sites.indexer.spider.sunnypt import SunnyPTSpider
    from app.adapters.sites.indexer.spider.yema import YemaSpider

    for cls, cfg in [
        (RousiSpider, {"name": "rousi", "domain": "https://rousi.zip/", "apikey": "k1"}),
        (YemaSpider, {"name": "yema", "domain": "https://www.yemapt.org/", "apikey": "k2"}),
        (SunnyPTSpider, {"name": "sunny", "domain": "https://sunnypt.top/", "apikey": "k3"}),
    ]:
        spider = cls(cfg)
        recipe = spider.get_download_url("12345")
        parsed = parse_download_recipe(recipe)
        assert parsed is not None, f"{cls.__name__} 未产出合法换票配方"


def test_row_item_id_supports_ttg_and_promo_maps_30_percent() -> None:
    """TTG `/dl/{id}/{auth}` 可提取稳定 ID；30% 折扣映射到 thirtypercent。"""
    from app.adapters.sites.generic import _promo_payload, _row_item_id

    assert _row_item_id({"enclosure": "https://totheglory.im/dl/556677/deadbeef99"}) == "556677"
    assert _row_item_id({"page_url": "https://totheglory.im/t/889900/"}) == "889900"

    payload = _promo_payload({"downloadvolumefactor": 0.3, "uploadvolumefactor": 1}, {})
    assert payload is not None
    assert payload["free_classes"] == ["thirtypercent"]


def test_host_of_logs_invalid_url_without_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非法 URL 返回空串，但必须留 warning（去掉 query，不泄露票据）。"""
    from app.adapters.sites.generic import _host_of

    with caplog.at_level("WARNING", logger="app.adapters.sites.generic"):
        assert _host_of("https://[bad-host/path?token=secret") == ""

    assert "主机解析失败" in caplog.text
    assert "token=secret" not in caplog.text


async def test_generic_anonymous_client_init_failure_is_site_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """匿名客户端构造失败（如代理配置非法）也映射为 SiteFetchError，不裸冒泡。"""
    adapter = GenericAdapter(SiteProfile(name="OffSite", domain="site.test"))
    try:

        def _boom(*args: object, **kwargs: object) -> None:
            raise httpx.InvalidURL("bad proxy")

        monkeypatch.setattr("app.adapters.sites.generic.httpx.AsyncClient", _boom)
        with pytest.raises(SiteFetchError, match="匿名下载客户端初始化失败"):
            await adapter._fetch_torrent_response(
                "https://cdn.offsite.test/path.torrent"
            )
    finally:
        await adapter.close()
