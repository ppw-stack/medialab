"""HdskyAdapter 契约测试。

覆盖 5 个契约方法 + parse_torrents_html 静态方法。
所有 HTTP 调用 mock，不依赖网络。
"""

from __future__ import annotations

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
    resp = MagicMock()
    resp.status_code = 200
    resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=resp)
    content = await adapter.download_torrent("10001")
    assert content == MINIMAL_TORRENT


async def test_download_torrent_no_cookies() -> None:
    a = HdskyAdapter(PROFILE)
    with pytest.raises(_TorrentDownloadError):
        await a.download_torrent("10001")


async def test_download_torrent_non_bencode(adapter: HdskyAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"not torrent"
    adapter._client.get = AsyncMock(return_value=resp)
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


# ---- 5. get_torrent_detail ----


async def test_get_torrent_detail_ok(adapter: HdskyAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<html>detail</html>"
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("10001")
    assert detail.item_id == "10001"
    assert detail.raw_html is not None


# ---- 2. search_torrents (parsel 解析 spstate=7) ----


async def test_search_torrents_parses_spstate7(adapter: HdskyAdapter) -> None:
    """parsel CSS 解析 HDSky 标准 NexusPHP 表格 + 批量抓详情填 torrent_url。

    fixture 模拟 spstate=7 页面的真实结构：
    - 锚点带 title="..." href="details.php?id=N&hit=1"
    - 永久免费：<img class="pro_free">
    - 限时免费：<img class="pro_30pctdown"> + <span title="绝对时刻">
    """
    # 第一次 GET：spstate=7 列表；后续 GET：每条详情页（带 sign）
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
    # 10001 / 10002 / 10003 中：10001 有 sign URL 被填入；10002/10003 没填（mock 不变）
    assert len(items) == 3
    assert items[0].item_id == "10001"
    assert items[0].title == "Movie A"
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]
    # torrent_url 已填：HDSky sign 形式
    assert items[0].torrent_url is not None
    assert "sign=" in items[0].torrent_url
    assert items[0].torrent_url and items[0].torrent_url.startswith("https://hdsky.me/download.php?id=10001&t=")
    # 第二条：限时 30% off（mock 只对第 1 个详情响应，所以 torrent_url 仍 None）
    assert items[1].item_id == "10002"
    assert items[1].free_payload is not None
    assert "thirtypercent" in items[1].free_payload["free_classes"]  # type: ignore[operator]
    # 第三条：非免费
    assert items[2].free_payload is None


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


# ---- fixture: HDSky spstate=7 真实结构片段 ----

HDSKY_SPSTATE7_HTML = (
    '<html><body><table class="torrents">'
    # 永久免费
    '<tr>'
    '<td><a target="_blank" title="Movie A" href="details.php?id=10001&amp;hit=1">Movie A</a></td>'
    '<td><img class="pro_free" src="free.png"/></td>'
    '<td>4.95<br />GB</td>'
    '</tr>'
    # 限时 30% off
    '<tr>'
    '<td><a target="_blank" title="Movie B" href="details.php?id=10002&amp;hit=1">Movie B</a></td>'
    '<td><img class="pro_30pctdown" src="30.png" alt="30%"/></td>'
    '<td>优惠剩余时间：<b><span title="2026-12-31 23:59:00">3天2小时</span></b></td>'
    '<td>1.00<br />GB</td>'
    '</tr>'
    # 非免费
    '<tr>'
    '<td><a target="_blank" title="Movie C" href="details.php?id=10003&amp;hit=1">Movie C</a></td>'
    '<td>2.00<br />GB</td>'
    '</tr>'
    '</table></body></html>'
)

#: HDSky details.php?id=10001 HTML（form action 含 sign URL）
HDSKY_DETAIL_HTML_10001 = (
    '<html><body>'
    '<form action="https://hdsky.me/download.php?id=10001&amp;t=1791689105&amp;sign=6a47635b3134c1b3275984768eb3b2a6" method="get">'
    '<input type="submit" value="下载"/>'
    '</form>'
    '</body></html>'
)
