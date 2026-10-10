# ruff: noqa: E501
"""OurBits 适配器测试（FR-1.5 站点特化 + NexusPHP cookie 鉴权）。

覆盖：
- ``OurbitsAdapter._classify_from_html`` free class / 永久标记
- ``OurbitsAdapter._parse_remaining_delta`` 纯时长文案（onmouseover tooltip）
- ``OurbitsAdapter._parse_torrents_html`` 标准 NexusPHP 表格解析（&amp; href / 可见剩余时间 / new 徽章过滤）
- ``OurbitsAdapter.parse_window`` dict + str 路径（含 R10 中性种无 end_at 契约）
- ``OurbitsAdapter._parse_rss`` guid 含 hex infohash
- ``OurbitsAdapter.fetch_infohash`` 直接 GET 下载 + bencode 解析
- ``OurbitsAdapter.ping`` 登录态检测
- 注册中心 resolve / 拒绝未知
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.ourbits import OurbitsAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType

# ---- OurBits torrents.php HTML 片段（标准 NexusPHP 表格，&amp; href） ----

OURBITS_TORRENTS_HTML = """<table class="torrents" cellspacing="0" cellpadding="5" width="100%" id="torrenttable">
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c2_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow" width="100%" align="left"><table class="torrentname" width="100%"><tr><td class="embedded"><a title="Ubuntu.24.04.amd64.iso"  href="details.php?id=10001&amp;hit=1"><b>Ubuntu.24.04.iso</b></a> <img class="pro_free" src="pic/trans.gif" alt="Free" />&nbsp;<font class="new">NEW</font>&nbsp;剩余时间：<b><span title="2026-10-12 18:00:00">2天6小时</span></b></td><td class='embedded' width='20px'><a href="download.php?id=10001"><img class="download" src="pic/trans.gif"/></a></td></tr></table></td>
<td class="rowfollow">4.95 GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=402"><img class="c2_tv" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow" width="100%" align="left"><table class="torrentname" width="100%"><tr><td class="embedded"><a title="Debian.12.iso"  href="details.php?id=10002&amp;hit=1"><b>Debian.12.iso</b></a> <img class="pro_free2up" src="pic/trans.gif" alt="2X Free" />&nbsp;剩余时间：<b><span title="2026-10-17 12:00:00">7天</span></b></td><td class='embedded' width='20px'><a href="download.php?id=10002"><img class="download" src="pic/trans.gif"/></a></td></tr></table></td>
<td class="rowfollow">1.00 GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c2_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow" width="100%" align="left"><table class="torrentname" width="100%"><tr><td class="embedded"><a title="Free.Movie.2026.1080p.BluRay"  href="details.php?id=10003&amp;hit=1"><b>Free.Movie.2026</b></a> <img class="pro_free" src="pic/trans.gif" alt="Free" /></td><td class='embedded' width='20px'><a href="download.php?id=10003"><img class="download" src="pic/trans.gif"/></a></td></tr></table></td>
<td class="rowfollow">2.00 GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c2_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow" width="100%" align="left"><table class="torrentname" width="100%"><tr><td class="embedded"><a title="Normal.Torrent.2026"  href="details.php?id=10004&amp;hit=1"><b>Normal.Torrent</b></a></td><td class='embedded' width='20px'><a href="download.php?id=10004"><img class="download" src="pic/trans.gif"/></a></td></tr></table></td>
<td class="rowfollow">3.50 GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c2_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow" width="100%" align="left"><table class="torrentname" width="100%"><tr><td class="embedded"><a title="Half.Down.Movie.2026"  href="details.php?id=10005&amp;hit=1"><b>Half.Down.Movie</b></a> <img class="pro_50pctdown" src="pic/trans.gif" alt="50%" />&nbsp;剩余时间：<b><span title="2026-10-15 12:00:00">5天</span></b></td><td class='embedded' width='20px'><a href="download.php?id=10005"><img class="download" src="pic/trans.gif"/></a></td></tr></table></td>
<td class="rowfollow">8.00 GB</td>
</tr>
</table>"""

#: OurBits 顶部"海报墙"锚点（无 title，应被过滤）
OURBITS_POSTER_WALL = """<a href="details.php?id=99999&hit=1" style="display: inline-block;" onmouseover="domTT_activate(this, event, 'content', '<font class=\\'big\\'><b>Poster Only</b></font>', 'trail', true);"><img src="//cdn/x.jpg" width="101" height="140"/></a>"""

#: 最小有效 bencoded torrent（含空 info dict）
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"


@pytest.fixture
def adapter() -> OurbitsAdapter:
    profile = SiteProfile(name="ourbits", domain="ourbits.club")
    return OurbitsAdapter(
        profile,
        cookies="ourbits_jwt=abc; cf_clearance=xyz",
        clock_offset=ClockOffset.zero(),
    )


# ---- _parse_remaining_delta ----


def test_parse_delta_days_hours(adapter: OurbitsAdapter) -> None:
    delta = adapter._parse_remaining_delta("2天3小时")
    assert delta is not None
    assert delta.total_seconds() == (2 * 86400 + 3 * 3600)


def test_parse_delta_hours_minutes(adapter: OurbitsAdapter) -> None:
    delta = adapter._parse_remaining_delta("5小时12分钟")
    assert delta is not None
    assert delta.total_seconds() == (5 * 3600 + 12 * 60)


def test_parse_delta_minutes_only(adapter: OurbitsAdapter) -> None:
    delta = adapter._parse_remaining_delta("30分钟")
    assert delta is not None
    assert delta.total_seconds() == 30 * 60


def test_parse_delta_days_only(adapter: OurbitsAdapter) -> None:
    delta = adapter._parse_remaining_delta("7天")
    assert delta is not None
    assert delta.total_seconds() == 7 * 86400


def test_parse_delta_with_prefix(adapter: OurbitsAdapter) -> None:
    delta = adapter._parse_remaining_delta("剩余 2 天 6 小时")
    assert delta is not None
    assert delta.total_seconds() == (2 * 86400 + 6 * 3600)


def test_parse_delta_no_duration(adapter: OurbitsAdapter) -> None:
    assert adapter._parse_remaining_delta("已结束") is None
    assert adapter._parse_remaining_delta("") is None
    assert adapter._parse_remaining_delta(None) is None


# ---- _classify_from_html ----


def test_classify_free_with_remaining_timed(adapter: OurbitsAdapter) -> None:
    ft, delta, conf = adapter._classify_from_html(["free"], "2天3小时")
    assert ft == FreeType.TIMED
    assert delta is not None
    assert conf == 0.95


def test_classify_free_no_remaining_permanent(adapter: OurbitsAdapter) -> None:
    ft, delta, conf = adapter._classify_from_html(["free"], None)
    assert ft == FreeType.PERMANENT
    assert delta is None
    assert conf == 0.85


def test_classify_permanent_marker(adapter: OurbitsAdapter) -> None:
    ft, delta, conf = adapter._classify_from_html(["free"], "永久免费")
    assert ft == FreeType.PERMANENT
    assert delta is None
    assert conf == 1.0


def test_classify_twoupfree(adapter: OurbitsAdapter) -> None:
    ft, _delta, conf = adapter._classify_from_html(["twoupfree"], None)
    assert ft == FreeType.TWOU_TIMED
    assert conf == 0.85


def test_classify_halfdown_neutral(adapter: OurbitsAdapter) -> None:
    ft, _delta, conf = adapter._classify_from_html(["halfdown"], None)
    assert ft == FreeType.NEUTRAL
    assert conf == 0.85


def test_classify_no_markers_unknown(adapter: OurbitsAdapter) -> None:
    ft, delta, conf = adapter._classify_from_html([], None)
    assert ft == FreeType.UNKNOWN
    assert delta is None
    assert conf == 0.0


# ---- _parse_torrents_html ----


def test_parse_torrents_html_extracts_5_items() -> None:
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    assert len(items) == 5
    assert items[0].title == "Ubuntu.24.04.amd64.iso"
    assert items[0].link == "details.php?id=10001"
    assert items[0].torrent_url == "download.php?id=10001"


def test_parse_torrents_html_size_parsing() -> None:
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    assert items[0].size > 4 * 1024**3
    assert items[0].size < 5 * 1024**3
    assert items[1].size == 1024**3


def test_parse_torrents_html_timed_from_visible_span() -> None:
    """pro_free + 可见 剩余时间<span title=绝对时刻> → remaining_abs。"""
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    payload = items[0].free_payload
    assert payload is not None
    assert "free" in payload.get("free_classes", [])
    assert payload.get("remaining_abs") == "2026-10-12 18:00:00"
    assert payload.get("remaining_text") is None


def test_parse_torrents_html_permanent_no_time() -> None:
    """pro_free 无剩余时间 → free_payload 无 remaining_abs。"""
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    payload = items[2].free_payload
    assert payload is not None
    assert "free" in payload.get("free_classes", [])
    assert payload.get("remaining_abs") is None


def test_parse_torrents_html_normal_no_payload() -> None:
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    assert items[3].free_payload is None


def test_parse_torrents_html_new_badge_filtered() -> None:
    """<font class="new"> UI 徽章不被误判为免费标记。"""
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    payload = items[0].free_payload
    assert payload is not None
    assert "new" not in payload.get("free_classes", [])


def test_parse_torrents_html_pro_img2up_mapped() -> None:
    """pro_free2up → twoupfree。"""
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    payload = items[1].free_payload
    assert payload is not None
    assert "twoupfree" in payload.get("free_classes", [])


def test_parse_torrents_html_pro_50pctdown_mapped() -> None:
    """pro_50pctdown → halfdown。"""
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=100)
    payload = items[4].free_payload
    assert payload is not None
    assert "halfdown" in payload.get("free_classes", [])


def test_parse_torrents_html_poster_wall_skipped() -> None:
    """海报墙锚点（无 title）被过滤，不计入种子列表。"""
    html = OURBITS_POSTER_WALL + OURBITS_TORRENTS_HTML
    items = OurbitsAdapter._parse_torrents_html(html, limit=100)
    assert len(items) == 5
    assert all("99999" not in (it.link or "") for it in items)


def test_parse_torrents_html_limit() -> None:
    items = OurbitsAdapter._parse_torrents_html(OURBITS_TORRENTS_HTML, limit=2)
    assert len(items) == 2


# ---- parse_window str 路径 ----


def _parse(adapter: OurbitsAdapter, text: str):
    return asyncio.run(adapter.parse_window(text))


def test_parse_window_str_permanent_marker(adapter: OurbitsAdapter) -> None:
    win = _parse(adapter, "剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.LIST_TEXT
    assert win.confidence == 0.9


def test_parse_window_str_remaining_chinese(adapter: OurbitsAdapter) -> None:
    win = _parse(adapter, "剩余 2 天 6 小时")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


def test_parse_window_str_remaining_traditional(adapter: OurbitsAdapter) -> None:
    win = _parse(adapter, "剩餘 2 天 6 小時")
    assert win.free_type == FreeType.TIMED


def test_parse_window_str_empty(adapter: OurbitsAdapter) -> None:
    win = _parse(adapter, "")
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- parse_window dict 路径 ----


async def test_parse_window_dict_timed_from_abs(adapter: OurbitsAdapter) -> None:
    """free + 可见绝对时刻 → TIMED。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": None,
        "remaining_abs": "2026-10-12 18:00:00",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.free_source == FreeSource.API
    assert win.confidence >= 0.95


async def test_parse_window_dict_timed_from_onmouse(adapter: OurbitsAdapter) -> None:
    """free + onmouseover 相对时长 → TIMED。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "2天3小时",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


async def test_parse_window_dict_permanent(adapter: OurbitsAdapter) -> None:
    win = await adapter.parse_window({"free_classes": ["free"], "remaining_text": None})
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None


async def test_parse_window_dict_neutral_no_end_at(adapter: OurbitsAdapter) -> None:
    """R10 契约：中性种即使带剩余时间也必须 free_end_at=None。"""
    win = await adapter.parse_window({
        "free_classes": ["halfdown"],
        "remaining_abs": "2026-10-15 12:00:00",
    })
    assert win.free_type == FreeType.NEUTRAL
    assert win.free_end_at is None


async def test_parse_window_dict_twoupfree_timed(adapter: OurbitsAdapter) -> None:
    win = await adapter.parse_window({
        "free_classes": ["twoupfree"],
        "remaining_abs": "2026-10-17 12:00:00",
    })
    assert win.free_type == FreeType.TWOU_TIMED
    assert win.free_end_at is not None


async def test_parse_window_dict_unknown(adapter: OurbitsAdapter) -> None:
    win = await adapter.parse_window({})
    assert win.free_type == FreeType.UNKNOWN


# ---- RSS 解析（guid 含 hex infohash） ----

OURBITS_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item>
<title>Test.Movie.2026.1080p</title>
<link>https://ourbits.club/details.php?id=10001</link>
<description>测试</description>
<guid isPermaLink="false">d50108701c889e10dfb7fb5bd2cf6d75fec79cba</guid>
<enclosure url="https://ourbits.club/download.php?id=10001&amp;passkey=abc" length="5368709120" type="application/x-bittorrent" />
<category>Movies</category>
</item>
</channel></rss>"""


def test_parse_rss_extracts_infohash_from_guid() -> None:
    items = OurbitsAdapter._parse_rss(OURBITS_RSS)
    assert len(items) == 1
    assert items[0].info_hash == "d50108701c889e10dfb7fb5bd2cf6d75fec79cba"
    assert items[0].size == 5368709120


# ---- fetch_feed HTML 模式 ----


async def test_fetch_feed_html_parses_items(adapter: OurbitsAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = OURBITS_TORRENTS_HTML
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/torrents.php"
    adapter._client.get = AsyncMock(return_value=mock_resp)

    items = await adapter.fetch_feed("")
    assert len(items) == 5
    assert items[0].title == "Ubuntu.24.04.amd64.iso"


async def test_fetch_feed_no_cookies_returns_empty() -> None:
    profile = SiteProfile(name="ourbits", domain="ourbits.club")
    a = OurbitsAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    assert await a.fetch_feed("") == []


# ---- fetch_infohash（直接 GET 下载） ----


async def test_fetch_infohash_from_torrent(adapter: OurbitsAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=mock_resp)
    infohash = await adapter.fetch_infohash("10001")
    assert infohash is not None
    assert len(infohash) == 40


async def test_fetch_infohash_no_cookies() -> None:
    profile = SiteProfile(name="ourbits", domain="ourbits.club")
    a = OurbitsAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    assert await a.fetch_infohash("10001") is None


async def test_fetch_infohash_non_bencode(adapter: OurbitsAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"<html>not a torrent</html>"
    adapter._client.get = AsyncMock(return_value=mock_resp)
    assert await adapter.fetch_infohash("10001") is None


# ---- ping ----


async def test_ping_logged_in(adapter: OurbitsAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = (
        '<html><a href="userdetails.php?id=123">用户名</a>'
        '<a href="logout.php">退出</a></html>'
    )
    adapter._client.get = AsyncMock(return_value=mock_resp)
    health = await adapter.ping()
    assert health.reachable is True
    assert health.logged_in is True


async def test_ping_cookie_expired(adapter: OurbitsAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>login.php 重新登录</html>"
    adapter._client.get = AsyncMock(return_value=mock_resp)
    health = await adapter.ping()
    assert health.reachable is True
    assert health.logged_in is False


async def test_ping_no_cookies() -> None:
    profile = SiteProfile(name="ourbits", domain="ourbits.club")
    a = OurbitsAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    assert (await a.ping()).reachable is False


# ---- capabilities ----


def test_capabilities(adapter: OurbitsAdapter) -> None:
    from app.adapters.sites.base import SiteFeature
    caps = adapter.capabilities()
    assert SiteFeature.RSS in caps
    assert SiteFeature.CLOCK_API in caps
    assert SiteFeature.TIMESTAMP_FIELD in caps


# ---- 注册中心 ----


def test_registry_resolves_ourbits():
    from app.adapters.sites import resolve
    assert resolve("ourbits") is OurbitsAdapter


def test_registry_rejects_unknown():
    from app.adapters.sites import resolve
    with pytest.raises(ValueError, match="未注册"):
        resolve("nonexistent")


# ---- FreeWindow 契约保护 ----


def test_free_window_rejects_permanent_with_deadline():
    from datetime import datetime

    from app.core.windows import FreeWindow
    with pytest.raises(ValueError, match="非限时"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2030, 1, 1),
            free_source=FreeSource.API,
            confidence=1.0,
        )
