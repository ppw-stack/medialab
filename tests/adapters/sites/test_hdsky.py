"""HdskyAdapter 契约测试。

覆盖 5 个契约方法 + parse_torrents_html 静态方法。
所有 HTTP 调用 mock，不依赖网络。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile, TorrentBrief
from app.adapters.sites.hdsky import HdskyAdapter, _TorrentDownloadError

PROFILE = SiteProfile(name="hdsky", domain="hdsky.me")
COOKIES = "c_secure_uid=MTAzMjE4; c_secure_pass=abc123"
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"


@pytest.fixture
def adapter() -> HdskyAdapter:
    return HdskyAdapter(PROFILE, cookies=COOKIES)


# ---- 1. check_reachability ----


async def test_check_reachability_ok(adapter: HdskyAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '<html><a href="userdetails.php?id=1">用户</a></html>'
    adapter._client.get = AsyncMock(return_value=resp)
    health = await adapter.check_reachability()
    assert health.reachable is True
    assert health.logged_in is True


async def test_check_reachability_no_cookies() -> None:
    a = HdskyAdapter(PROFILE)
    health = await a.check_reachability()
    assert health.reachable is False


# ---- 2. search_torrents ----


async def test_search_returns_empty(adapter: HdskyAdapter) -> None:
    assert await adapter.search_torrents({}) == []


# ---- 3. download_torrent ----


async def test_download_torrent_returns_bytes(adapter: HdskyAdapter) -> None:
    """走详情页提取 sign → 下载完整 URL。

    两次 GET：details.php?id=10001 → download.php?id=10001&t=...&sign=...
    """
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = HDSKY_DETAIL_HTML_10001
    download_resp = MagicMock()
    download_resp.status_code = 200
    download_resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(side_effect=[detail_resp, download_resp])
    content = await adapter.download_torrent("10001")
    assert content == MINIMAL_TORRENT


async def test_download_torrent_no_cookies() -> None:
    a = HdskyAdapter(PROFILE)
    with pytest.raises(_TorrentDownloadError):
        await a.download_torrent("10001")


async def test_download_torrent_non_bencode(adapter: HdskyAdapter) -> None:
    """详情页正常但下载响应不是 bencoded → 抛错。"""
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = HDSKY_DETAIL_HTML_10001
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.content = b"not torrent"
    adapter._client.get = AsyncMock(side_effect=[detail_resp, bad_resp])
    with pytest.raises(_TorrentDownloadError):
        await adapter.download_torrent("10001")


async def test_download_torrent_no_sign_url(adapter: HdskyAdapter) -> None:
    """详情页不含 sign → 抛错。"""
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = "<html>no sign</html>"
    adapter._client.get = AsyncMock(return_value=detail_resp)
    with pytest.raises(_TorrentDownloadError):
        await adapter.download_torrent("10001")


# ---- 4. check_status ----


async def test_check_status_exists(adapter: HdskyAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("10001") == TorrentBrief.EXISTS


async def test_check_status_deleted(adapter: HdskyAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 404
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("10001") == TorrentBrief.DELETED


async def test_check_status_redirect_to_login(adapter: HdskyAdapter) -> None:
    """cookie 过期 → 302 重定向到 login.php → UNKNOWN（不是 EXISTS）。"""
    resp = MagicMock()
    resp.status_code = 302
    resp.headers = {"Location": "https://hdsky.me/login.php?returnto=..."}
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("10001") == TorrentBrief.UNKNOWN


# ---- 5. get_torrent_detail ----


async def test_get_torrent_detail_ok(adapter: HdskyAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<h1>Test Movie</h1>" + HDSKY_DETAIL_HTML_10001
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("10001")
    assert detail.item_id == "10001"
    assert detail.raw_html is not None
    assert detail.title == "Test Movie"
    # download_url 从 sign form action 解析
    assert detail.download_url is not None
    assert "sign=" in detail.download_url


# ---- 2. search_torrents (parsel 解析 spstate=0) ----


async def test_search_torrents_parses_spstate7(adapter: HdskyAdapter) -> None:
    """parsel CSS 解析 HDSky 标准 NexusPHP 表格 + 批量抓详情填 torrent_url。

    fixture 模拟 spstate=0 页面的真实结构（混排：永久 + 限时 + 折扣）：
    - 永久免费：<img class="pro_free">，无剩余时间
    - 限时免费：<img class="pro_free"> + <span title="绝对时刻">
    - 永久折扣：<img class="pro_30pctdown">，无剩余时间
    """
    # 第一次 GET：spstate=0 列表；后续 GET：每条详情页（带 sign）
    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.text = HDSKY_SPSTATE7_HTML
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = HDSKY_DETAIL_HTML_10001  # 含 sign URL
    # 第 1 次 = 列表；之后 3 次（每个 item 一次）都返回同一个详情 mock
    responses = [list_resp, detail_resp, detail_resp, detail_resp]
    adapter._client.get = AsyncMock(side_effect=responses)

    items = await adapter.search_torrents({}, page_size=100, page_number=1)
    # 10001 / 10002 / 10003
    assert len(items) == 3
    # 第一条：永久免费（pro_free，无 remaining_abs）
    assert items[0].item_id == "10001"
    assert items[0].title == "Movie A"
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]
    assert items[0].free_payload["remaining_abs"] is None  # type: ignore[operator]
    # torrent_url 已填：HDSky sign 形式
    assert items[0].torrent_url is not None
    assert "sign=" in items[0].torrent_url
    assert items[0].torrent_url and items[0].torrent_url.startswith("https://hdsky.me/download.php?id=10001&t=")
    # 第二条：限时免费（pro_free + remaining_abs）
    assert items[1].item_id == "10002"
    assert items[1].free_payload is not None
    assert "free" in items[1].free_payload["free_classes"]  # type: ignore[operator]
    assert items[1].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[operator]
    # 第三条：30% off（pro_30pctdown → thirtypercent → NEUTRAL，无 remaining_abs）
    assert items[2].item_id == "10003"
    assert items[2].free_payload is not None
    assert "thirtypercent" in items[2].free_payload["free_classes"]  # type: ignore[operator]
    assert items[2].free_payload["remaining_abs"] is None  # type: ignore[operator]


async def test_search_torrents_extracts_window_for_timed_free(adapter: Any) -> None:
    """限时免费条目应能在 _parse_nexushp_html 中正确解析出 free_end_at。

    模拟：RSS 列表返回 1 条限时免费（id=10002，含 pro_free + span title）。
    window_parser._parse_nexushp_html() 解析后 free_end_at 应为正确 datetime。
    """
    from datetime import datetime as dt

    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.text = HDSKY_SPSTATE7_HTML  # 3 条列表
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = HDSKY_DETAIL_HTML_10001
    adapter._client.get = AsyncMock(side_effect=[list_resp, detail_resp, detail_resp, detail_resp])

    items = await adapter.search_torrents({}, page_size=100, page_number=1)
    # 找限时条（item_id=10002）
    timed = next(it for it in items if it.item_id == "10002")
    from app.services.window_parser import _parse_nexushp_html
    win = _parse_nexushp_html(timed)
    assert win is not None, "限时免费应能解析窗口"
    assert win.free_end_at == dt(2026, 12, 31, 23, 59, 00)
    assert win.is_actionable is True


async def test_search_torrents_details_failure_keeps_list(adapter: HdskyAdapter) -> None:
    """详情页失败不应影响列表解析（item 仍存在，torrent_url=None）。"""
    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.text = HDSKY_SPSTATE7_HTML
    fail_resp = MagicMock()
    fail_resp.status_code = 500
    adapter._client.get = AsyncMock(side_effect=[list_resp, fail_resp, fail_resp, fail_resp])
    items = await adapter.search_torrents({})
    assert len(items) == 3
    assert all(it.torrent_url is None for it in items)


async def test_search_torrents_http_error(adapter: HdskyAdapter) -> None:
    """HTTP 5xx / 4xx 返回空列表（不抛错）。"""
    resp = MagicMock()
    resp.status_code = 500
    adapter._client.get = AsyncMock(return_value=resp)

    items = await adapter.search_torrents({})
    assert items == []


# ---- 静态解析单元测试（_parse_spstate7_html）----


def test_parse_spstate7_html_extracts_items() -> None:
    items = HdskyAdapter._parse_spstate7_html(HDSKY_SPSTATE7_HTML, limit=100)
    assert len(items) == 3
    assert items[0].item_id == "10001"
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]


def test_parse_spstate7_html_empty() -> None:
    assert HdskyAdapter._parse_spstate7_html("<html></html>") == []


def test_parse_spstate7_html_skips_non_free() -> None:
    """spstate=0 包含普通付费种子，没有 pro_free 不应进候选池。"""
    html = (
        '<html><body><table class="torrents">'
        '<tr>'
        '<td><a target="_blank" title="Free" href="details.php?id=20001&amp;hit=1">Free</a></td>'
        '<td><img class="pro_free" src="free.png"/></td>'
        '<td>1.00<br />GB</td>'
        '</tr>'
        '<tr>'
        '<td><a target="_blank" title="Paid" href="details.php?id=20002&amp;hit=1">Paid</a></td>'
        '<td>&nbsp;</td>'  # 无 pro_*
        '<td>2.00<br />GB</td>'
        '</tr>'
        '<tr>'
        '<td><a target="_blank" title="Timed" href="details.php?id=20003&amp;hit=1">Timed</a></td>'
        '<td><img class="pro_free" src="free.png"/></td>'
        '<td><span title="2026-12-31 23:59:00">1天</span></td>'
        '</tr>'
        '</table></body></html>'
    )
    items = HdskyAdapter._parse_spstate7_html(html, limit=100)
    ids = [it.item_id for it in items]
    assert ids == ["20001", "20003"]  # 20002（无免费标志）被跳过
    assert items[1].free_payload is not None
    assert items[1].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]


# ---- fixture: HDSky spstate=0 真实结构片段（2026-10 抓取）----

# 模拟 HDSky spstate=0 列表（实际是混排：永久 + 限时 + 30% 折扣）。
# 真实页面 DOM 例子：
#   <tr>
#     <td><a target="_blank" title="..." href="details.php?id=625366&amp;hit=1">Stranger Things</a>
#       <img class="pro_30pctdown" src="pic/trans.gif" alt="30%" title="30%" />
#     </td>
#     <td><img class="pro_free" src="free.png" /></td>
#     <td>5.05 GB</td>
#     <td>优惠剩余时间：<b><span title="2026-12-31 23:59:00">3天2小时</span></b></td>
#   </tr>
HDSKY_SPSTATE7_HTML = (
    '<html><body><table class="torrents">'
    # 永久免费（pro_free，无剩余时间）
    '<tr>'
    '<td><a target="_blank" title="Movie A" href="details.php?id=10001&amp;hit=1">Movie A</a></td>'
    '<td><img class="pro_free" src="free.png"/></td>'
    '<td>4.95<br />GB</td>'
    '<td>&nbsp;</td>'
    '</tr>'
    # 限时免费（pro_free + span title 倒计时）
    '<tr>'
    '<td><a target="_blank" title="Movie B" href="details.php?id=10002&amp;hit=1">Movie B</a></td>'
    '<td><img class="pro_free" src="free.png" title="免费"/></td>'
    '<td>1.00<br />GB</td>'
    '<td>优惠剩余时间：<b><span title="2026-12-31 23:59:00">3天2小时</span></b></td>'
    '</tr>'
    # 永久 30% 折扣（pro_30pctdown，无剩余时间）
    '<tr>'
    '<td><a target="_blank" title="Movie C" href="details.php?id=10003&amp;hit=1">Movie C</a></td>'
    '<td><img class="pro_30pctdown" src="30.png" alt="30%"/></td>'
    '<td>2.00<br />GB</td>'
    '<td>&nbsp;</td>'
    '</tr>'
    '</table></body></html>'
)

#: HDSky details.php?id=10001 HTML（form action 含 sign URL）
HDSKY_DETAIL_HTML_10001 = (
    '<html><body>'
    '<form action="https://hdsky.me/download.php?id=10001'
    '&amp;t=1791689105&amp;sign=6a47635b3134c1b3275984768eb3b2a6" method="get">'
    '<input type="submit" value="下载"/>'
    '</form>'
    '</body></html>'
)
