"""OurbitsAdapter 契约测试。

覆盖 5 个契约方法 + search_torrents (parsel 解析 spstate=7) 静态方法。
所有 HTTP 调用 mock，不依赖网络。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile, TorrentBrief
from app.adapters.sites.ourbits import OurbitsAdapter, _TorrentDownloadError

PROFILE = SiteProfile(name="ourbits", domain="ourbits.club")
COOKIES = "c_secure_uid=xxx; c_secure_pass=abc123; cf_clearance=xyz"
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"


@pytest.fixture
def adapter() -> OurbitsAdapter:
    return OurbitsAdapter(PROFILE, cookies=COOKIES)


# ---- 1. check_reachability ----


async def test_check_reachability_ok(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '<html><a href="userdetails.php?id=1">用户</a></html>'
    adapter._client.get = AsyncMock(return_value=resp)
    health = await adapter.check_reachability()
    assert health.reachable is True
    assert health.logged_in is True


async def test_check_reachability_no_cookies() -> None:
    a = OurbitsAdapter(PROFILE)
    health = await a.check_reachability()
    assert health.reachable is False


# ---- 2. search_torrents ----


async def test_search_returns_empty(adapter: OurbitsAdapter) -> None:
    assert await adapter.search_torrents({}) == []


# ---- 3. download_torrent ----


async def test_download_torrent_returns_bytes(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=resp)
    content = await adapter.download_torrent("10001")
    assert content == MINIMAL_TORRENT


async def test_download_torrent_no_cookies() -> None:
    a = OurbitsAdapter(PROFILE)
    with pytest.raises(_TorrentDownloadError):
        await a.download_torrent("10001")


# ---- 4. check_status ----


async def test_check_status_exists(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("10001") == TorrentBrief.EXISTS


async def test_check_status_deleted(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 404
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("10001") == TorrentBrief.DELETED


# ---- 5. get_torrent_detail ----


async def test_get_torrent_detail_ok(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<html>detail</html>"
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("10001")
    assert detail.item_id == "10001"
    assert detail.raw_html is not None


# ---- 2. search_torrents (parsel 解析 spstate=7) ----


async def test_search_torrents_parses_spstate7(adapter: OurbitsAdapter) -> None:
    """parsel CSS 解析 Ourbits 标准 NexusPHP 表格 + 批量抓详情填 torrent_url。"""
    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.text = OURBITS_SPSTATE7_HTML
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = OURBITS_DETAIL_HTML_10001
    adapter._client.get = AsyncMock(side_effect=[list_resp, detail_resp, detail_resp, detail_resp])

    items = await adapter.search_torrents({}, page_size=100, page_number=1)
    assert len(items) == 3
    # 第一条：永久免费 + torrent_url 已填
    assert items[0].item_id == "10001"
    assert items[0].title == "Movie A"
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]
    # torrent_url 已填：Ourbits passkey 形式
    assert items[0].torrent_url is not None
    assert "passkey=" in items[0].torrent_url
    assert items[0].torrent_url and "id=10001" in items[0].torrent_url
    # 第二条：限时 30% off
    assert items[1].item_id == "10002"
    assert items[1].free_payload is not None
    assert "thirtypercent" in items[1].free_payload["free_classes"]  # type: ignore[operator]
    assert items[1].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]
    # 第三条：非免费
    assert items[2].free_payload is None


async def test_search_torrents_http_error(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 500
    adapter._client.get = AsyncMock(return_value=resp)
    items = await adapter.search_torrents({})
    assert items == []


# ---- 静态解析单元测试 ----


def test_parse_spstate7_html_extracts_items() -> None:
    items = OurbitsAdapter._parse_spstate7_html(OURBITS_SPSTATE7_HTML, limit=100)
    assert len(items) == 3
    assert items[0].item_id == "10001"
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]


def test_parse_spstate7_html_empty() -> None:
    assert OurbitsAdapter._parse_spstate7_html("<html></html>") == []


# ---- fixture: Ourbits spstate=7 真实结构片段 ----

OURBITS_SPSTATE7_HTML = (
    '<html><body><table class="torrents">'
    '<tr>'
    '<td><a target="_blank" title="Movie A" href="details.php?id=10001&amp;hit=1">Movie A</a></td>'
    '<td><img class="pro_free" src="free.png"/></td>'
    '<td>4.95<br />GB</td>'
    '</tr>'
    '<tr>'
    '<td><a target="_blank" title="Movie B" href="details.php?id=10002&amp;hit=1">Movie B</a></td>'
    '<td><img class="pro_30pctdown" src="30.png" alt="30%"/></td>'
    '<td>剩余时间：<b><span title="2026-12-31 23:59:00">3天2小时</span></b></td>'
    '<td>1.00<br />GB</td>'
    '</tr>'
    '<tr>'
    '<td><a target="_blank" title="Movie C" href="details.php?id=10003&amp;hit=1">Movie C</a></td>'
    '<td>2.00<br />GB</td>'
    '</tr>'
    '</table></body></html>'
)

#: Ourbits details.php?id=10001 HTML（anchor href 含 passkey）
OURBITS_DETAIL_HTML_10001 = (
    '<html><body>'
    '<a href="/download.php?id=10001&passkey=74f6bd64e80ba32094b6f71485727f9a&https=1">下载</a>'
    '</body></html>'
)
