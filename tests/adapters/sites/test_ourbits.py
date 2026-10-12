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
    """走详情页提取 passkey → 下载完整 URL。

    两次 GET：details.php?id=10001 → download.php?id=...&passkey=...
    """
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = OURBITS_DETAIL_HTML_10001
    download_resp = MagicMock()
    download_resp.status_code = 200
    download_resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(side_effect=[detail_resp, download_resp])
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


async def test_check_status_redirect_to_login(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 302
    resp.headers = {"Location": "https://ourbits.club/login.php"}
    adapter._client.get = AsyncMock(return_value=resp)
    assert await adapter.check_status("10001") == TorrentBrief.UNKNOWN


# ---- 5. get_torrent_detail ----


async def test_get_torrent_detail_ok(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<h1>Test Movie</h1>" + OURBITS_DETAIL_HTML_10001
    adapter._client.get = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("10001")
    assert detail.item_id == "10001"
    assert detail.raw_html is not None
    assert detail.title == "Test Movie"
    assert detail.download_url is not None
    assert "passkey=" in detail.download_url


# ---- 2. search_torrents (parsel 解析 spstate=0) ----


async def test_search_torrents_parses_spstate7(adapter: OurbitsAdapter) -> None:
    """parsel CSS 解析 Ourbits 标准 NexusPHP 表格 + 批量抓详情填 torrent_url。

    fixture 模拟 spstate=0 页面（永久 + 限时 + 折扣混排）：
    - 永久免费：<img class="pro_free">，无剩余时间
    - 限时免费：<img class="pro_free"> + <span title="绝对时刻">
    - 永久折扣：<img class="pro_50pctdown">，无剩余时间
    """
    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.text = OURBITS_SPSTATE7_HTML
    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.text = OURBITS_DETAIL_HTML_10001
    adapter._client.get = AsyncMock(
        side_effect=[list_resp, detail_resp, detail_resp, detail_resp, detail_resp]
    )

    items = await adapter.search_torrents({}, page_size=100, page_number=1)
    # 全量拉取（含无促销标记的种子）—— fetch_all_sites 必须入库所有 spstate=0 项
    assert len(items) == 4
    # 第一条：永久免费 + torrent_url 已填
    assert items[0].item_id == "10001"
    assert items[0].title == "Movie A"
    assert items[0].free_payload is not None
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]
    assert items[0].free_payload["remaining_abs"] is None  # type: ignore[operator]
    # torrent_url 已填：Ourbits passkey 形式
    assert items[0].torrent_url is not None
    assert "passkey=" in items[0].torrent_url
    assert items[0].torrent_url and "id=10001" in items[0].torrent_url
    # 第二条：限时免费（pro_free + remaining_abs）
    assert items[1].item_id == "10002"
    assert items[1].free_payload is not None
    assert "free" in items[1].free_payload["free_classes"]  # type: ignore[operator]
    assert items[1].free_payload["remaining_abs"] == "2026-12-31 23:59:00"  # type: ignore[index]
    # 第三条：50% 折扣（pro_50pctdown → halfdown → NEUTRAL）
    assert items[2].item_id == "10003"
    assert items[2].free_payload is not None
    assert "halfdown" in items[2].free_payload["free_classes"]  # type: ignore[operator]
    assert items[2].free_payload["remaining_abs"] is None  # type: ignore[operator]
    # 第四条：spstate=0 拉全量后必须保留（无任何促销标记）
    assert items[3].item_id == "10004"
    assert items[3].title == "Movie D"
    assert items[3].free_payload is None
    assert items[3].torrent_url is not None


async def test_search_torrents_http_error(adapter: OurbitsAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 500
    adapter._client.get = AsyncMock(return_value=resp)
    items = await adapter.search_torrents({})
    assert items == []


# ---- 静态解析单元测试 ----


def test_parse_spstate7_html_extracts_items() -> None:
    items = OurbitsAdapter._parse_spstate7_html(OURBITS_SPSTATE7_HTML, limit=100)
    # 全量拉取：4 条（3 个有促销 + 1 个无）
    assert len(items) == 4
    assert items[0].item_id == "10001"
    assert "free" in items[0].free_payload["free_classes"]  # type: ignore[operator]


def test_parse_spstate7_html_empty() -> None:
    assert OurbitsAdapter._parse_spstate7_html("<html></html>") == []


def test_parse_spstate7_html_keeps_non_free() -> None:
    """spstate=0 拉全量：含普通付费种子（无 pro_* 标记）也必须保留。

    fetch_all_sites 不做免费过滤——是否免费留给 WindowParser enrich 阶段判断。
    """
    html = (
        '<html><body><table class="torrents">'
        '<tr>'
        '<td><a target="_blank" title="Free" href="details.php?id=30001&amp;hit=1">Free</a></td>'
        '<td><img class="pro_free" src="free.png"/></td>'
        '<td>1.00<br />GB</td>'
        '</tr>'
        '<tr>'
        '<td><a target="_blank" title="Paid" href="details.php?id=30002&amp;hit=1">Paid</a></td>'
        '<td>&nbsp;</td>'  # 无 pro_*
        '<td>2.00<br />GB</td>'
        '</tr>'
        '<tr>'
        '<td><a target="_blank" title="Timed" href="details.php?id=30003&amp;hit=1">Timed</a></td>'
        '<td><img class="pro_free" src="free.png"/></td>'
        '<td><span title="2026-12-31 23:59:00">1天</span></td>'
        '</tr>'
        '</table></body></html>'
    )
    items = OurbitsAdapter._parse_spstate7_html(html, limit=100)
    ids = [it.item_id for it in items]
    # 三条全保留：30002 虽无促销标记也入库
    assert ids == ["30001", "30002", "30003"]


# ---- fixture: Ourbits spstate=0 真实结构片段（2026-10 抓取）----

# 模拟 Ourbits spstate=0 列表（混排：永久 + 限时 + 50% 折扣）。
# 真实页面 DOM 例子（与 HDSky 同结构 NexusPHP 主题）：
#   <tr>
#     <td><a target="_blank" title="..." href="details.php?id=10002&amp;hit=1">Iron Man</a></td>
#     <td><img class="pro_free" src="free.png" /></td>
#     <td>5.05 GB</td>
#     <td>剩余时间：<b><span title="2026-10-12 22:19:50">7时35分</span></b></td>
#   </tr>
OURBITS_SPSTATE7_HTML = (
    '<html><body><table class="torrents">'
    # 永久免费
    '<tr>'
    '<td><a target="_blank" title="Movie A" href="details.php?id=10001&amp;hit=1">Movie A</a></td>'
    '<td><img class="pro_free" src="free.png"/></td>'
    '<td>4.95<br />GB</td>'
    '<td>&nbsp;</td>'
    '</tr>'
    # 限时免费
    '<tr>'
    '<td><a target="_blank" title="Movie B" href="details.php?id=10002&amp;hit=1">Movie B</a></td>'
    '<td><img class="pro_free" src="free.png" title="免费"/></td>'
    '<td>1.00<br />GB</td>'
    '<td>剩余时间：<b><span title="2026-12-31 23:59:00">3天2小时</span></b></td>'
    '</tr>'
    # 50% 折扣（永久）
    '<tr>'
    '<td><a target="_blank" title="Movie C" href="details.php?id=10003&amp;hit=1">Movie C</a></td>'
    '<td><img class="pro_50pctdown" src="50.png" alt="50%"/></td>'
    '<td>2.00<br />GB</td>'
    '<td>&nbsp;</td>'
    '</tr>'
    # 普通种子（spstate=0 拉全量，无任何促销标记）—— 必须保留
    '<tr>'
    '<td><a target="_blank" title="Movie D" href="details.php?id=10004&amp;hit=1">Movie D</a></td>'
    '<td>&nbsp;</td>'
    '<td>3.50<br />GB</td>'
    '<td>&nbsp;</td>'
    '</tr>'
    '</table></body></html>'
)

#: Ourbits details.php?id=10001 HTML（anchor href 含 passkey）
OURBITS_DETAIL_HTML_10001 = (
    '<html><body>'
    '<a href="/download.php?id=10001&passkey=74f6bd64e80ba32094b6f71485727f9a&https=1">下载</a>'
    '</body></html>'
)
