"""HhanclubAdapter 契约测试。

覆盖 5 个契约方法 + parse_torrents_html 静态方法。
所有 HTTP 调用 mock，不依赖网络。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile, TorrentBrief
from app.adapters.sites.hhanclub import HhanclubAdapter, _TorrentDownloadError

PROFILE = SiteProfile(name="hhanclub", domain="hhanclub.net")
COOKIES = "c_secure_uid=MTU3NTk%3D; c_secure_pass=abc123"
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"

#: HHan 主题 HTML 片段（含 promotion-tag-free + 剩余时间 + 大小）
HHAN_HTML = (
    '<div class="torrent-table-for-spider">'
    '<a href="details.php?id=100001&hit=1" class="torrent-info-text-name">Movie A</a>'
    '<div class="torrent-info-text torrent-info-text-size">4.95 GB</div>'
    '<a href="download.php?id=100001&passkey=abc">DL</a>'
    '<span class="promotion-tag promotion-tag-free">免费</span>'
    '剩余时间：<span title="2026-12-31 23:59:00">3天2小时</span>'
    '</div>'
    '<div class="torrent-table-for-spider">'
    '<a href="details.php?id=100002&hit=1" class="torrent-info-text-name">Movie B</a>'
    '<div class="torrent-info-text torrent-info-text-size">1.00 GB</div>'
    '</div>'
)


@pytest.fixture
def adapter() -> HhanclubAdapter:
    return HhanclubAdapter(PROFILE, cookies=COOKIES)


# ---- 1. check_reachability ----


async def test_check_reachability_ok(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '<html><a href="userdetails.php?id=1">用户</a><a href="logout.php">退出</a></html>'
    adapter._client.get = AsyncMock(return_value=resp)

    health = await adapter.check_reachability()
    assert health.reachable is True
    assert health.logged_in is True


async def test_check_reachability_expired_cookie(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '<html><a href="login.php">登录</a></html>'
    resp.url = MagicMock()
    resp.url.path = "/login.php"
    adapter._client.get = AsyncMock(return_value=resp)

    health = await adapter.check_reachability()
    assert health.reachable is True
    assert health.logged_in is False


async def test_check_reachability_no_cookies() -> None:
    a = HhanclubAdapter(PROFILE)
    health = await a.check_reachability()
    assert health.reachable is False


# ---- 2. search_torrents ----


async def test_search_returns_empty(adapter: HhanclubAdapter) -> None:
    """NexusPHP 无搜索 API → 返回空列表。"""
    assert await adapter.search_torrents({"keyword": "test"}) == []


# ---- 3. download_torrent ----


async def test_download_torrent_returns_bytes(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=resp)

    content = await adapter.download_torrent("100001")
    assert content == MINIMAL_TORRENT


async def test_download_torrent_no_cookies() -> None:
    a = HhanclubAdapter(PROFILE)
    with pytest.raises(_TorrentDownloadError):
        await a.download_torrent("100001")


async def test_download_torrent_non_bencode(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"<html>error</html>"
    adapter._client.get = AsyncMock(return_value=resp)
    with pytest.raises(_TorrentDownloadError):
        await adapter.download_torrent("100001")


# ---- 4. check_status ----


async def test_check_status_exists(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("100001") == TorrentBrief.EXISTS


async def test_check_status_deleted(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 404
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("100001") == TorrentBrief.DELETED


async def test_check_status_no_cookies() -> None:
    a = HhanclubAdapter(PROFILE)
    assert await a.check_status("1") == TorrentBrief.UNKNOWN


# ---- 5. get_torrent_detail ----


async def test_get_torrent_detail_ok(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<html>detail page</html>"
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("100001")
    assert detail["id"] == "100001"
    assert "_html" in detail


async def test_get_torrent_detail_error(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 500
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("100001")
    assert "_error" in detail


# ---- parse_torrents_html（静态方法）----


def test_parse_torrents_html_extracts_items() -> None:
    items = HhanclubAdapter.parse_torrents_html(HHAN_HTML, limit=100)
    assert len(items) == 2
    assert items[0].title == "Movie A"
    assert items[0].item_id == "100001"
    assert items[0].size > 4 * 1024**3  # ~5 GB
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]
    assert items[0].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]
    assert items[1].free_payload is None  # 第二条无免费标记


def test_parse_torrents_html_limit() -> None:
    items = HhanclubAdapter.parse_torrents_html(HHAN_HTML, limit=1)
    assert len(items) == 1


def test_parse_torrents_html_empty() -> None:
    assert HhanclubAdapter.parse_torrents_html("<html></html>") == []
