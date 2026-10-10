# ruff: noqa: E501
"""HHanClub 适配器测试（FR-1.5 站点特化 + NexusPHP cookie 鉴权）。

覆盖：
- ``HhanclubAdapter._classify_from_html`` free class / remaining_text / 永久标记
- ``HhanclubAdapter._parse_torrents_html`` HTML 表格解析
- ``HhanclubAdapter.parse_window`` dict + str 路径
- ``HhanclubAdapter.fetch_infohash`` bencode 解析
- ``HhanclubAdapter.ping`` 登录态检测
- ``HhanclubAdapter.capabilities`` 能力声明
- 注册中心 resolve / 拒绝未知
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.hhanclub import HhanclubAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType

# ---- NexusPHP torrents.php HTML 片段（测试用，模拟 HHan 主题 div 布局） ----

HHANCLUB_TORRENTS_HTML = """<div class="flex flex-col w-[95%] m-auto torrent-table-for-spider">
<div class="flex torrent-table-for-spider-info torrent-title items-center gap-x-[15px] justify-between">
<a href='details.php?id=10001&hit=1' class='torrent-info-text-name'>Ubuntu.24.04.amd64</a>
<span class="promotion-tag promotion-tag-free" >免费</span>
<span class='flex text-[12px] text-[#000000]'>[&nbsp;剩余时间：<span title="2026-10-12 18:00:00">2天6小时</span>&nbsp;]</span>
</div>
<div class="torrent-info"><div class="torrent-info-text torrent-info-text-size">4.95 GB</div></div>
<a href="download.php?id=10001&passkey=abc123def456">下载</a>
</div>
<div class="flex flex-col w-[95%] m-auto torrent-table-for-spider">
<div class="flex torrent-table-for-spider-info torrent-title items-center gap-x-[15px] justify-between">
<a href='details.php?id=10002&hit=1' class='torrent-info-text-name'>Debian.12.iso</a>
<span class="promotion-tag promotion-tag-2xfree" >2X免费</span>
<span class='flex text-[12px] text-[#000000]'>[&nbsp;剩余时间：<span title="2026-10-17 12:00:00">7天</span>&nbsp;]</span>
</div>
<div class="torrent-info"><div class="torrent-info-text torrent-info-text-size">1.00 GB</div></div>
<a href="download.php?id=10002&passkey=abc123def456">下载</a>
</div>
<div class="flex flex-col w-[95%] m-auto torrent-table-for-spider">
<div class="flex torrent-table-for-spider-info torrent-title items-center gap-x-[15px] justify-between">
<a href='details.php?id=10003&hit=1' class='torrent-info-text-name'>Free.Movie.2026</a>
<span class="promotion-tag promotion-tag-free" >免费</span>
<span class='flex text-[12px] text-[#000000]'>永久免费</span>
</div>
<div class="torrent-info"><div class="torrent-info-text torrent-info-text-size">2.00 GB</div></div>
<a href="download.php?id=10003&passkey=abc123def456">下载</a>
</div>
<div class="flex flex-col w-[95%] m-auto torrent-table-for-spider">
<div class="flex torrent-table-for-spider-info torrent-title items-center gap-x-[15px] justify-between">
<a href='details.php?id=10004&hit=1' class='torrent-info-text-name'>Normal.Torrent</a>
</div>
<div class="torrent-info"><div class="torrent-info-text torrent-info-text-size">3.50 GB</div></div>
<a href="download.php?id=10004&passkey=abc123def456">下载</a>
</div>
<div class="flex flex-col w-[95%] m-auto torrent-table-for-spider">
<div class="flex torrent-table-for-spider-info torrent-title items-center gap-x-[15px] justify-between">
<a href='details.php?id=10005&hit=1' class='torrent-info-text-name'>Half.Down.Movie</a>
<span class="promotion-tag promotion-tag-50" >50%</span>
<span class='flex text-[12px] text-[#000000]'>[&nbsp;剩余时间：<span title="2026-10-15 12:00:00">5天</span>&nbsp;]</span>
</div>
<div class="torrent-info"><div class="torrent-info-text torrent-info-text-size">8.00 GB</div></div>
<a href="download.php?id=10005&passkey=abc123def456">下载</a>
</div>
</div>"""

#: 最小有效 bencoded torrent（含空 info dict）
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"


@pytest.fixture
def adapter() -> HhanclubAdapter:
    profile = SiteProfile(name="hhanclub", domain="hhanclub.net")
    return HhanclubAdapter(
        profile,
        cookies="c_secure_uid=12345; c_secure_pass=abcdef",
        clock_offset=ClockOffset.zero(),
    )


# ---- _classify_from_html 单元测试 ----


def test_classify_free_with_remaining_timed(adapter: HhanclubAdapter) -> None:
    """free class + 剩余时间 → TIMED（返回 timedelta 由 parse_window 转换）。"""
    ft, delta, conf = adapter._classify_from_html(["free"], "剩余 2天 6小时")
    assert ft == FreeType.TIMED
    assert delta is not None
    assert delta.total_seconds() > 0
    assert conf == 0.95


def test_classify_free_no_remaining_permanent(adapter: HhanclubAdapter) -> None:
    """free class 无剩余时间 → PERMANENT（NexusPHP 默认行为）。"""
    ft, end, conf = adapter._classify_from_html(["free"], None)
    assert ft == FreeType.PERMANENT
    assert end is None
    assert conf == 0.85


def test_classify_permanent_marker(adapter: HhanclubAdapter) -> None:
    """永久免费标记优先级最高。"""
    ft, end, conf = adapter._classify_from_html(["free"], "永久免费")
    assert ft == FreeType.PERMANENT
    assert end is None
    assert conf == 1.0


def test_classify_twoupfree(adapter: HhanclubAdapter) -> None:
    """twoupfree class → TWOU_TIMED。"""
    ft, _delta, conf = adapter._classify_from_html(["twoupfree"], None)
    assert ft == FreeType.TWOU_TIMED
    assert conf == 0.85


def test_classify_twoup(adapter: HhanclubAdapter) -> None:
    """twoup class → TWOU_TIMED。"""
    ft, _delta, _conf = adapter._classify_from_html(["twoup"], None)
    assert ft == FreeType.TWOU_TIMED


def test_classify_halfdown_neutral(adapter: HhanclubAdapter) -> None:
    """halfdown class → NEUTRAL。"""
    ft, _delta, conf = adapter._classify_from_html(["halfdown"], None)
    assert ft == FreeType.NEUTRAL
    assert conf == 0.85


def test_classify_no_markers_unknown(adapter: HhanclubAdapter) -> None:
    """无任何免费标记 → UNKNOWN。"""
    ft, end, conf = adapter._classify_from_html([], None)
    assert ft == FreeType.UNKNOWN
    assert end is None
    assert conf == 0.0


# ---- _parse_torrents_html HTML 解析测试 ----


def test_parse_torrents_html_extracts_5_items() -> None:
    """从 HTML 片段解析出 5 个种子条目。"""
    items = HhanclubAdapter._parse_torrents_html(HHANCLUB_TORRENTS_HTML, limit=100)
    assert len(items) == 5
    assert items[0].title == "Ubuntu.24.04.amd64"
    assert items[0].link == "details.php?id=10001"
    assert items[0].torrent_url == "download.php?id=10001"
    assert items[0].size > 0  # 4.95 GB ≈ 5.3e9


def test_parse_torrents_html_free_payload(adapter: HhanclubAdapter) -> None:
    """HTML 解析后 free_payload 含 free_classes 和 remaining_text。"""
    items = HhanclubAdapter._parse_torrents_html(HHANCLUB_TORRENTS_HTML, limit=100)
    # 第一个种子有 free class
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload.get("free_classes", [])
    assert items[0].free_payload.get("remaining_text") is not None
    # 普通种子无 free_payload
    assert items[3].free_payload is None


def test_parse_torrents_html_size_parsing() -> None:
    """大小解析正确（GB → bytes）。"""
    items = HhanclubAdapter._parse_torrents_html(HHANCLUB_TORRENTS_HTML, limit=100)
    # 4.95 GB ≈ 5,314,013,757 bytes
    assert items[0].size > 4 * 1024**3
    assert items[0].size < 5 * 1024**3
    # 1.00 GB
    assert items[1].size == 1024**3


def test_parse_torrents_html_limit() -> None:
    """limit 参数限制条目数。"""
    items = HhanclubAdapter._parse_torrents_html(HHANCLUB_TORRENTS_HTML, limit=2)
    assert len(items) == 2


# ---- parse_window str 路径 ----


def _parse(adapter: HhanclubAdapter, text: str):
    """同步包装异步 parse_window。"""
    return asyncio.run(adapter.parse_window(text))


def test_parse_window_str_permanent_marker(adapter: HhanclubAdapter) -> None:
    """永久免费标记。"""
    win = _parse(adapter, "剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.LIST_TEXT
    assert win.confidence == 0.9


def test_parse_window_str_remaining_chinese(adapter: HhanclubAdapter) -> None:
    """简体剩余文案。"""
    win = _parse(adapter, "剩余 2 天 6 小时")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


def test_parse_window_str_remaining_traditional(adapter: HhanclubAdapter) -> None:
    """繁体剩余文案（小時）。"""
    win = _parse(adapter, "剩餘 2 天 6 小時")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


def test_parse_window_str_remaining_english(adapter: HhanclubAdapter) -> None:
    """英文剩余文案。"""
    win = _parse(adapter, "left 3 days 5 hours")
    assert win.free_type == FreeType.TIMED


def test_parse_window_str_empty(adapter: HhanclubAdapter) -> None:
    """空文本 → UNKNOWN。"""
    win = _parse(adapter, "")
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None
    assert win.confidence == 0.0


def test_parse_window_str_negative(adapter: HhanclubAdapter) -> None:
    """已结束文案 → UNKNOWN。"""
    win = _parse(adapter, "已结束 1 天")
    assert win.free_type == FreeType.UNKNOWN
    assert win.confidence == 0.0


# ---- parse_window dict 路径 ----


async def test_parse_window_dict_free_permanent(adapter: HhanclubAdapter) -> None:
    """free class 无剩余时间 → PERMANENT。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": None,
    })
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.API
    assert win.confidence == 0.85


async def test_parse_window_dict_free_timed(adapter: HhanclubAdapter) -> None:
    """free class + 剩余时间 → TIMED。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "剩余 2天 6小时",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95


async def test_parse_window_dict_permanent_marker(adapter: HhanclubAdapter) -> None:
    """永久免费标记优先。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "永久免费",
    })
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 1.0


async def test_parse_window_dict_twoupfree(adapter: HhanclubAdapter) -> None:
    """twoupfree → TWOU_TIMED。"""
    win = await adapter.parse_window({
        "free_classes": ["twoupfree"],
        "remaining_text": None,
    })
    assert win.free_type == FreeType.TWOU_TIMED
    assert win.free_end_at is None


async def test_parse_window_dict_halfdown(adapter: HhanclubAdapter) -> None:
    """halfdown → NEUTRAL。"""
    win = await adapter.parse_window({
        "free_classes": ["halfdown"],
        "remaining_text": None,
    })
    assert win.free_type == FreeType.NEUTRAL


async def test_parse_window_dict_unknown(adapter: HhanclubAdapter) -> None:
    """无标记 → UNKNOWN。"""
    win = await adapter.parse_window({})
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


async def test_parse_window_dict_free_traditional_remaining(adapter: HhanclubAdapter) -> None:
    """繁体剩余时间文案。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "剩餘 2天 6小時",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


# ---- fetch_feed HTML 模式 ----


async def test_fetch_feed_html_parses_items(adapter: HhanclubAdapter) -> None:
    """cookie 模式下 fetch_feed 走 HTML scrape。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = HHANCLUB_TORRENTS_HTML
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/torrents.php"
    adapter._client.get = AsyncMock(return_value=mock_resp)

    items = await adapter.fetch_feed("")  # 无 URL → 走 HTML
    assert len(items) == 5
    assert items[0].title == "Ubuntu.24.04.amd64"


async def test_fetch_feed_no_cookies_returns_empty() -> None:
    """无 cookies → 返回空列表。"""
    profile = SiteProfile(name="hhanclub", domain="hhanclub.net")
    a = HhanclubAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    items = await a.fetch_feed("")
    assert items == []


# ---- fetch_infohash ----


async def test_fetch_infohash_from_torrent(adapter: HhanclubAdapter) -> None:
    """下载 .torrent 并解析 infohash。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=mock_resp)

    infohash = await adapter.fetch_infohash("10001")
    assert infohash is not None
    assert len(infohash) == 40  # SHA1 hex


async def test_fetch_infohash_no_cookies(adapter: HhanclubAdapter) -> None:
    """无 cookies → 返回 None。"""
    profile = SiteProfile(name="hhanclub", domain="hhanclub.net")
    a = HhanclubAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    result = await a.fetch_infohash("10001")
    assert result is None


async def test_fetch_infohash_non_bencode(adapter: HhanclubAdapter) -> None:
    """非 bencode 响应 → None。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"<html>not a torrent</html>"
    adapter._client.get = AsyncMock(return_value=mock_resp)
    result = await adapter.fetch_infohash("10001")
    assert result is None


# ---- ping ----


async def test_ping_logged_in(adapter: HhanclubAdapter) -> None:
    """登录态检测：页面含 userdetails.php → logged_in=True。"""
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


async def test_ping_cookie_expired(adapter: HhanclubAdapter) -> None:
    """cookie 过期：重定向到 login.php → logged_in=False。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html>login.php 重新登录</html>'
    adapter._client.get = AsyncMock(return_value=mock_resp)

    health = await adapter.ping()
    assert health.reachable is True
    assert health.logged_in is False


async def test_ping_no_cookies() -> None:
    """无 cookies → reachable=False。"""
    profile = SiteProfile(name="hhanclub", domain="hhanclub.net")
    a = HhanclubAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    health = await a.ping()
    assert health.reachable is False


# ---- capabilities ----


def test_capabilities(adapter: HhanclubAdapter) -> None:
    from app.adapters.sites.base import SiteFeature
    caps = adapter.capabilities()
    assert SiteFeature.RSS in caps
    assert SiteFeature.CLOCK_API in caps
    assert SiteFeature.TIMESTAMP_FIELD in caps


# ---- 注册中心 ----


def test_registry_resolves_hhanclub():
    from app.adapters.sites import resolve
    cls = resolve("hhanclub")
    assert cls is HhanclubAdapter


def test_registry_rejects_unknown():
    from app.adapters.sites import resolve
    with pytest.raises(ValueError, match="未注册"):
        resolve("nonexistent")


# ---- FreeWindow 契约保护 ----


def test_free_window_rejects_permanent_with_deadline():
    """契约：永久免费不允许带结束时刻（R10）。"""
    from datetime import datetime

    from app.core.windows import FreeWindow
    with pytest.raises(ValueError, match="非限时"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2030, 1, 1),
            free_source=FreeSource.API,
            confidence=1.0,
        )
