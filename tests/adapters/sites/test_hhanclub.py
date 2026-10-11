"""HhanclubAdapter 契约测试。

覆盖 5 个契约方法 + search_torrents (BS4 解析 spstate=7) 静态方法。
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
    assert detail.item_id == "100001"
    assert detail.raw_html is not None


async def test_get_torrent_detail_error(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 500
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("100001")
    assert detail.error is not None


# ---- 2. search_torrents (BS4 解析 spstate=2) ----


async def test_search_torrents_parses_spstate2(adapter: HhanclubAdapter) -> None:
    """BS4 解析 HHan Tailwind 自定义主题的 spstate=2 页面（永久免费）+ torrent_url 提取。

    HHan 用 Tailwind class + 嵌套 div 而非标准 NexusPHP 表格。
    spstate=7 是 JS 动态加载，HTML 不含 torrent 链接；用 spstate=2 SSR。
    """
    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.text = HHAN_SPSTATE2_HTML
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = HHAN_DETAIL_HTML_100001
    adapter._client.get = AsyncMock(side_effect=[list_resp, detail_resp, detail_resp, detail_resp])

    items = await adapter.search_torrents({}, page_size=100, page_number=1)
    assert len(items) == 3
    # 第一条：限时免费 + torrent_url 已填
    assert items[0].item_id == "100001"
    assert items[0].free_payload is not None
    assert items[0].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]
    assert items[0].torrent_url is not None
    assert "passkey=" in items[0].torrent_url
    assert items[0].torrent_url and "id=100001" in items[0].torrent_url
    # 第二条：永久免费（class="free"）
    assert items[1].item_id == "100002"
    assert items[1].free_payload is not None
    assert "free" in items[1].free_payload["free_classes"]  # type: ignore[operator]
    # 第三条：非免费
    assert items[2].free_payload is None


async def test_search_torrents_excludes_userdetails(adapter: HhanclubAdapter) -> None:
    """过滤掉 userdetails.php?id=N（用户名导航链接，不当作 torrent）。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = HHAN_WITH_USER_LINK_HTML
    adapter._client.get = AsyncMock(return_value=resp)
    items = await adapter.search_torrents({})
    # 应该只返回 torrent 100001，userdetails 15759 被过滤
    assert len(items) == 1
    assert items[0].item_id == "100001"


async def test_search_torrents_http_error(adapter: HhanclubAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 500
    adapter._client.get = AsyncMock(return_value=resp)
    items = await adapter.search_torrents({})
    assert items == []


async def test_search_returns_empty_on_redirect(adapter: HhanclubAdapter) -> None:
    """未登录触发 login.php 重定向 → search_torrents 内部 raise → 返回空。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '<html><a href="login.php">login</a></html>'
    adapter._client.get = AsyncMock(return_value=resp)
    items = await adapter.search_torrents({})
    assert isinstance(items, list)


# ---- 静态解析单元测试 ----


def test_parse_spstate2_html_extracts_items() -> None:
    items = HhanclubAdapter._parse_spstate2_html(HHAN_SPSTATE2_HTML, limit=100)
    assert len(items) == 3
    assert items[0].item_id == "100001"
    assert items[0].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]


def test_parse_spstate2_html_limit() -> None:
    items = HhanclubAdapter._parse_spstate2_html(HHAN_SPSTATE2_HTML, limit=1)
    assert len(items) == 1


def test_parse_spstate2_html_empty() -> None:
    assert HhanclubAdapter._parse_spstate2_html("<html></html>") == []


# ---- fixture: HHan Tailwind 自定义主题 spstate=2 真实结构片段 ----

HHAN_SPSTATE2_HTML = (
    '<html><body>'
    # 限时免费：嵌套 div + span title="绝对时刻"
    '<div class="torrent-info-card">'
    '<a href="https://hhanclub.net/details.php?id=100001&amp;hit=1" '
    'class="torrent-info-text-name" title="Movie A">Movie A</a>'
    '<div class="torrent-info-promo">'
    '剩余时间：<span title="2026-12-31 23:59:00">3天2小时</span>'
    '</div>'
    '<div class="torrent-info-text-size">4.95 GB</div>'
    '</div>'
    # 永久免费：class="free"
    '<div class="torrent-info-card">'
    '<a href="https://hhanclub.net/details.php?id=100002&amp;hit=1" '
    'class="torrent-info-text-name" title="Movie B">Movie B</a>'
    '<span class="free">免费</span>'
    '<div class="torrent-info-text-size">1.00 GB</div>'
    '</div>'
    # 非免费
    '<div class="torrent-info-card">'
    '<a href="https://hhanclub.net/details.php?id=100003&amp;hit=1" '
    'class="torrent-info-text-name" title="Movie C">Movie C</a>'
    '<div class="torrent-info-text-size">2.00 GB</div>'
    '</div>'
    '</body></html>'
)

HHAN_WITH_USER_LINK_HTML = (
    '<html><body>'
    # 用户导航链接（应被过滤）
    '<a href="https://hhanclub.net/userdetails.php?id=15759">ppwHhc</a>'
    # 真实 torrent
    '<div class="torrent-info-card">'
    '<a href="https://hhanclub.net/details.php?id=100001&amp;hit=1" '
    'class="torrent-info-text-name" title="Movie A">Movie A</a>'
    '<div class="torrent-info-text-size">4.95 GB</div>'
    '</div>'
    '</body></html>'
)

#: HHanClub details.php?id=100001 HTML（anchor href 含 passkey）
HHAN_DETAIL_HTML_100001 = (
    '<html><body>'
    '<a href="https://hhanclub.net/download.php?id=100001&passkey=9b42c1c53f661c86a250814b5e51149f">下载</a>'
    '</body></html>'
)
