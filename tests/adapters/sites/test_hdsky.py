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

#: HDSky 标准 NexusPHP 表格 HTML 片段
HDSKY_HTML = (
    '<table class="torrents">'
    '<tr>'
    '<td><a target="_blank" title="Movie A" href="details.php?id=10001&hit=1">Movie A</a></td>'
    '<td><img class="pro_free" src="free.png"/></td>'
    '<td>优惠剩余时间：<b><span title="2026-12-31 23:59:00">3天2小时</span></b></td>'
    '<td><a href="download.php?id=10001&passkey=abc">DL</a></td>'
    '<td class="rowfollow">4.95<br />GB</td>'
    '</tr>'
    '<tr>'
    '<td><a target="_blank" title="Movie B" href="details.php?id=10002&hit=1">Movie B</a></td>'
    '<td class="rowfollow">1.00<br />GB</td>'
    '</tr>'
    '</table>'
)


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


# ---- parse_torrents_html ----


def test_parse_torrents_html_extracts_items() -> None:
    items = HdskyAdapter.parse_torrents_html(HDSKY_HTML, limit=100)
    assert len(items) == 2
    assert items[0].title == "Movie A"
    assert items[0].item_id == "10001"
    assert items[0].size > 4 * 1024**3
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]
    assert items[0].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]
    assert items[1].free_payload is None


def test_parse_torrents_html_empty() -> None:
    assert HdskyAdapter.parse_torrents_html("<html></html>") == []
