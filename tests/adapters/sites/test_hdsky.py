# ruff: noqa: E501
"""HDSky 适配器测试（FR-1.5 站点特化 + NexusPHP v1.5 cookie 鉴权）。

覆盖：
- ``HdskyAdapter._classify_from_html`` free class / remaining_text / 永久标记
- ``HdskyAdapter._parse_torrents_html`` HDSky 嵌套表格 HTML 解析
- ``HdskyAdapter.parse_window`` dict + str 路径
- ``HdskyAdapter.fetch_infohash`` 详情页签名表单下载 + bencode 解析
- ``HdskyAdapter.ping`` 登录态检测
- ``HdskyAdapter.capabilities`` 能力声明
- 注册中心 resolve / 拒绝未知
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.hdsky import HdskyAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType

# ---- HDSky torrents.php HTML 片段（测试用，模拟真实嵌套表格 + title 锚点） ----

HDSKY_TORRENTS_HTML = """<table class="torrents progresstable"><tbody>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow progresstd"><table class="torrentname" width="100%">
<tr><td class="embedded"><a target="_blank" title="Ubuntu.24.04.amd64.iso"  href="details.php?id=10001&hit=1"><b><span >Ubuntu.24.04.amd64.iso</span></b></a> <img class="pro_free" src="pic/trans.gif" alt="Free"  onmouseover="domTT_activate(this, event, 'content', '&lt;font class=&quot;free&quot;&gt;免费&lt;/font&gt;优惠剩余时间：&lt;span title=&quot;2026-10-12 18:00:00&quot;&gt;2天6小时&lt;/span&gt;');" /><span >[优惠剩余时间：<b><span title="2026-10-12 18:00:00">2天6小时</span></b>]</span></td></tr>
<tr><td class="embedded"><form action="https://hdsky.me/download.php?id=10001&t=1791609025&sign=aa11bb22" method="POST"><input class="download" type="image"/></form></td></tr>
</table></td>
<td class="rowfollow">4.95<br />GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=402"><img class="c_tv" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow progresstd"><table class="torrentname" width="100%">
<tr><td class="embedded"><a target="_blank" title="Debian.12.iso"  href="details.php?id=10002&hit=1"><b><span >Debian.12.iso</span></b></a> <img class="pro_free2up" src="pic/trans.gif" alt="2X Free" /><span >[优惠剩余时间：<b><span title="2026-10-17 12:00:00">7天</span></b>]</span></td></tr>
<tr><td class="embedded"><form action="https://hdsky.me/download.php?id=10002&t=1791609025&sign=cc33dd44" method="POST"><input class="download" type="image"/></form></td></tr>
</table></td>
<td class="rowfollow">1.00<br />GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow progresstd"><table class="torrentname" width="100%">
<tr><td class="embedded"><a target="_blank" title="Free.Movie.2026.1080p.BluRay"  href="details.php?id=10003&hit=1"><b><span >Free.Movie.2026.1080p.BluRay</span></b></a> <img class="pro_free" src="pic/trans.gif" alt="Free" /><span >永久免费</span></td></tr>
<tr><td class="embedded"><form action="https://hdsky.me/download.php?id=10003&t=1791609025&sign=ee55ff66" method="POST"><input class="download" type="image"/></form></td></tr>
</table></td>
<td class="rowfollow">2.00<br />GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow progresstd"><table class="torrentname" width="100%">
<tr><td class="embedded"><a target="_blank" title="Normal.Torrent.2026"  href="details.php?id=10004&hit=1"><b><span >Normal.Torrent.2026</span></b></a></td></tr>
<tr><td class="embedded"><form action="https://hdsky.me/download.php?id=10004&t=1791609025&sign=11223344" method="POST"><input class="download" type="image"/></form></td></tr>
</table></td>
<td class="rowfollow">3.50<br />GB</td>
</tr>
<tr>
<td class="rowfollow nowrap"><a href="?cat=401"><img class="c_movies" src="pic/cattrans.gif"/></a></td>
<td class="rowfollow progresstd"><table class="torrentname" width="100%">
<tr><td class="embedded"><a target="_blank" title="Half.Down.Movie.2026"  href="details.php?id=10005&hit=1"><b><span >Half.Down.Movie.2026</span></b></a> <img class="pro_50pctdown" src="pic/trans.gif" alt="50% Down" /><span >[优惠剩余时间：<b><span title="2026-10-15 12:00:00">5天</span></b>]</span></td></tr>
<tr><td class="embedded"><form action="https://hdsky.me/download.php?id=10005&t=1791609025&sign=55667788" method="POST"><input class="download" type="image"/></form></td></tr>
</table></td>
<td class="rowfollow">8.00<br />GB</td>
</tr>
</tbody></table>"""

#: 最小有效 bencoded torrent（含空 info dict）
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"


@pytest.fixture
def adapter() -> HdskyAdapter:
    profile = SiteProfile(name="hdsky", domain="hdsky.me")
    return HdskyAdapter(
        profile,
        cookies="c_secure_uid=MTU3NTk%3D; c_secure_pass=abcdef1234567890",
        clock_offset=ClockOffset.zero(),
    )


# ---- _classify_from_html 单元测试 ----


def test_classify_free_with_remaining_timed(adapter: HdskyAdapter) -> None:
    """free class + 剩余时间 → TIMED。"""
    ft, delta, conf = adapter._classify_from_html(["free"], "剩余 2天 6小时")
    assert ft == FreeType.TIMED
    assert delta is not None
    assert delta.total_seconds() > 0
    assert conf == 0.95


def test_classify_free_no_remaining_permanent(adapter: HdskyAdapter) -> None:
    """free class 无剩余时间 → PERMANENT。"""
    ft, delta, conf = adapter._classify_from_html(["free"], None)
    assert ft == FreeType.PERMANENT
    assert delta is None
    assert conf == 0.85


def test_classify_permanent_marker(adapter: HdskyAdapter) -> None:
    """永久免费标记优先级最高。"""
    ft, delta, conf = adapter._classify_from_html(["free"], "永久免费")
    assert ft == FreeType.PERMANENT
    assert delta is None
    assert conf == 1.0


def test_classify_twoupfree(adapter: HdskyAdapter) -> None:
    """twoupfree class → TWOU_TIMED。"""
    ft, _delta, conf = adapter._classify_from_html(["twoupfree"], None)
    assert ft == FreeType.TWOU_TIMED
    assert conf == 0.85


def test_classify_twoup(adapter: HdskyAdapter) -> None:
    """twoup class → TWOU_TIMED。"""
    ft, _delta, _conf = adapter._classify_from_html(["twoup"], None)
    assert ft == FreeType.TWOU_TIMED


def test_classify_halfdown_neutral(adapter: HdskyAdapter) -> None:
    """halfdown class → NEUTRAL。"""
    ft, _delta, conf = adapter._classify_from_html(["halfdown"], None)
    assert ft == FreeType.NEUTRAL
    assert conf == 0.85


def test_classify_no_markers_unknown(adapter: HdskyAdapter) -> None:
    """无任何免费标记 → UNKNOWN。"""
    ft, delta, conf = adapter._classify_from_html([], None)
    assert ft == FreeType.UNKNOWN
    assert delta is None
    assert conf == 0.0


# ---- _parse_torrents_html HTML 解析测试 ----


def test_parse_torrents_html_extracts_5_items() -> None:
    """从 HTML 片段解析出 5 个种子条目。"""
    items = HdskyAdapter._parse_torrents_html(HDSKY_TORRENTS_HTML, limit=100)
    assert len(items) == 5
    assert items[0].title == "Ubuntu.24.04.amd64.iso"
    assert items[0].link == "details.php?id=10001"
    assert items[0].torrent_url == "download.php?id=10001"


def test_parse_torrents_html_free_payload() -> None:
    """HTML 解析后 free_payload 含 free_classes 和 remaining_abs。"""
    items = HdskyAdapter._parse_torrents_html(HDSKY_TORRENTS_HTML, limit=100)
    # 第一个种子有 pro_free img 标记
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload.get("free_classes", [])
    assert items[0].free_payload.get("remaining_abs") is not None
    # 普通种子无 free_payload
    assert items[3].free_payload is None


def test_parse_torrents_html_size_parsing() -> None:
    """大小解析正确（GB → bytes）。"""
    items = HdskyAdapter._parse_torrents_html(HDSKY_TORRENTS_HTML, limit=100)
    assert items[0].size > 4 * 1024**3
    assert items[0].size < 5 * 1024**3
    assert items[1].size == 1024**3


def test_parse_torrents_html_limit() -> None:
    """limit 参数限制条目数。"""
    items = HdskyAdapter._parse_torrents_html(HDSKY_TORRENTS_HTML, limit=2)
    assert len(items) == 2


def test_parse_torrents_html_form_download() -> None:
    """HDSky 用 <form action="download.php?id=xxx"> 下载链接。"""
    items = HdskyAdapter._parse_torrents_html(HDSKY_TORRENTS_HTML, limit=100)
    assert items[0].torrent_url == "download.php?id=10001"
    assert items[4].torrent_url == "download.php?id=10005"


def test_parse_torrents_html_pro_img_classes() -> None:
    """pro_XXX img class 正确映射为标准免费 class。"""
    items = HdskyAdapter._parse_torrents_html(HDSKY_TORRENTS_HTML, limit=100)
    # item 0: pro_free → "free"
    assert "free" in items[0].free_payload.get("free_classes", [])
    # item 1: pro_free2up → "twoupfree"
    assert "twoupfree" in items[1].free_payload.get("free_classes", [])
    # item 4: pro_50pctdown → "halfdown"
    assert "halfdown" in items[4].free_payload.get("free_classes", [])


# ---- parse_window str 路径 ----


def _parse(adapter: HdskyAdapter, text: str):
    """同步包装异步 parse_window。"""
    return asyncio.run(adapter.parse_window(text))


def test_parse_window_str_permanent_marker(adapter: HdskyAdapter) -> None:
    """永久免费标记。"""
    win = _parse(adapter, "剩余 5 天 永久免费")
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.LIST_TEXT
    assert win.confidence == 0.9


def test_parse_window_str_remaining_chinese(adapter: HdskyAdapter) -> None:
    """简体剩余文案。"""
    win = _parse(adapter, "剩余 2 天 6 小时")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.8


def test_parse_window_str_remaining_traditional(adapter: HdskyAdapter) -> None:
    """繁体剩余文案（小時）。"""
    win = _parse(adapter, "剩餘 2 天 6 小時")
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


def test_parse_window_str_remaining_english(adapter: HdskyAdapter) -> None:
    """英文剩余文案。"""
    win = _parse(adapter, "left 3 days 5 hours")
    assert win.free_type == FreeType.TIMED


def test_parse_window_str_empty(adapter: HdskyAdapter) -> None:
    """空文本 → UNKNOWN。"""
    win = _parse(adapter, "")
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None
    assert win.confidence == 0.0


def test_parse_window_str_negative(adapter: HdskyAdapter) -> None:
    """已结束文案 → UNKNOWN。"""
    win = _parse(adapter, "已结束 1 天")
    assert win.free_type == FreeType.UNKNOWN
    assert win.confidence == 0.0


# ---- parse_window dict 路径 ----


async def test_parse_window_dict_free_permanent(adapter: HdskyAdapter) -> None:
    """free class 无剩余时间 → PERMANENT。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": None,
    })
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.free_source == FreeSource.API
    assert win.confidence == 0.85


async def test_parse_window_dict_free_timed_with_abs(adapter: HdskyAdapter) -> None:
    """free class + remaining_abs（绝对时刻）→ TIMED。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "剩余",
        "remaining_abs": "2026-10-12 18:00:00",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95


async def test_parse_window_dict_permanent_marker(adapter: HdskyAdapter) -> None:
    """永久免费标记优先。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "永久免费",
    })
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 1.0


async def test_parse_window_dict_twoupfree(adapter: HdskyAdapter) -> None:
    """twoupfree → TWOU_TIMED。"""
    win = await adapter.parse_window({
        "free_classes": ["twoupfree"],
        "remaining_text": None,
    })
    assert win.free_type == FreeType.TWOU_TIMED
    assert win.free_end_at is None


async def test_parse_window_dict_halfdown(adapter: HdskyAdapter) -> None:
    """halfdown → NEUTRAL。"""
    win = await adapter.parse_window({
        "free_classes": ["halfdown"],
        "remaining_text": None,
    })
    assert win.free_type == FreeType.NEUTRAL


async def test_parse_window_dict_unknown(adapter: HdskyAdapter) -> None:
    """无标记 → UNKNOWN。"""
    win = await adapter.parse_window({})
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


async def test_parse_window_dict_free_traditional_remaining(adapter: HdskyAdapter) -> None:
    """繁体剩余时间文案。"""
    win = await adapter.parse_window({
        "free_classes": ["free"],
        "remaining_text": "剩餘 2天 6小時",
    })
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None


# ---- fetch_feed HTML 模式 ----


async def test_fetch_feed_html_parses_items(adapter: HdskyAdapter) -> None:
    """cookie 模式下 fetch_feed 走 HTML scrape。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = HDSKY_TORRENTS_HTML
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/torrents.php"
    adapter._client.get = AsyncMock(return_value=mock_resp)

    items = await adapter.fetch_feed("")
    assert len(items) == 5
    assert items[0].title == "Ubuntu.24.04.amd64.iso"


async def test_fetch_feed_no_cookies_returns_empty() -> None:
    """无 cookies → 返回空列表。"""
    profile = SiteProfile(name="hdsky", domain="hdsky.me")
    a = HdskyAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    items = await a.fetch_feed("")
    assert items == []


# ---- fetch_infohash ----

#: HDSky 详情页片段（含签名下载表单）
HDSKY_DETAILS_HTML = (
    '<html><table><tr><td class="embedded">'
    '<form action="https://hdsky.me/download.php?id=10001&t=1791609057&sign=abc123def456" '
    'method="POST"><input class="download" type="image"/></form>'
    '</td></tr></table></html>'
)


def _details_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = HDSKY_DETAILS_HTML
    return r


async def test_fetch_infohash_from_torrent(adapter: HdskyAdapter) -> None:
    """详情页取签名表单 → POST 下载 → 解析 infohash。"""
    dl_resp = MagicMock()
    dl_resp.status_code = 200
    dl_resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=_details_response())
    adapter._client.post = AsyncMock(return_value=dl_resp)

    infohash = await adapter.fetch_infohash("10001")
    assert infohash is not None
    assert len(infohash) == 40
    # 验证 POST 的是签名 URL
    post_call = adapter._client.post.call_args
    assert "sign=abc123def456" in post_call.args[0]


async def test_fetch_infohash_no_cookies(adapter: HdskyAdapter) -> None:
    """无 cookies → 返回 None。"""
    profile = SiteProfile(name="hdsky", domain="hdsky.me")
    a = HdskyAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    result = await a.fetch_infohash("10001")
    assert result is None


async def test_fetch_infohash_no_form_returns_none(adapter: HdskyAdapter) -> None:
    """详情页无签名下载表单 → None。"""
    no_form = MagicMock()
    no_form.status_code = 200
    no_form.text = "<html><p>no download form here</p></html>"
    adapter._client.get = AsyncMock(return_value=no_form)
    result = await adapter.fetch_infohash("10001")
    assert result is None


async def test_fetch_infohash_non_bencode(adapter: HdskyAdapter) -> None:
    """POST 返回非 bencode → None。"""
    dl_resp = MagicMock()
    dl_resp.status_code = 200
    dl_resp.content = b"<html>not a torrent</html>"
    adapter._client.get = AsyncMock(return_value=_details_response())
    adapter._client.post = AsyncMock(return_value=dl_resp)
    result = await adapter.fetch_infohash("10001")
    assert result is None


# ---- ping ----


async def test_ping_logged_in(adapter: HdskyAdapter) -> None:
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


async def test_ping_cookie_expired(adapter: HdskyAdapter) -> None:
    """cookie 过期：重定向到 login.php → logged_in=False。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>login.php 重新登录</html>"
    adapter._client.get = AsyncMock(return_value=mock_resp)

    health = await adapter.ping()
    assert health.reachable is True
    assert health.logged_in is False


async def test_ping_no_cookies() -> None:
    """无 cookies → reachable=False。"""
    profile = SiteProfile(name="hdsky", domain="hdsky.me")
    a = HdskyAdapter(profile, cookies=None, clock_offset=ClockOffset.zero())
    health = await a.ping()
    assert health.reachable is False


# ---- capabilities ----


def test_capabilities(adapter: HdskyAdapter) -> None:
    from app.adapters.sites.base import SiteFeature
    caps = adapter.capabilities()
    assert SiteFeature.RSS in caps
    assert SiteFeature.CLOCK_API in caps
    assert SiteFeature.TIMESTAMP_FIELD in caps


# ---- 注册中心 ----


def test_registry_resolves_hdsky():
    from app.adapters.sites import resolve
    cls = resolve("hdsky")
    assert cls is HdskyAdapter


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
