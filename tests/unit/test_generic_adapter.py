"""通用适配器的端到端（mock HTTP）测试。"""

from __future__ import annotations

import httpx
import respx
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.generic import GenericAdapter

_LIST_HTML = """
<table class="torrents">
<tr><td>
  <table class="torrentname"><tr><td>
    <a title="测试.2024.1080p.BluRay" href="details.php?id=12345&amp;hit=1"><b>测试.2024.1080p</b></a>
    <a href="download.php?id=12345&amp;passkey=abc"><img src="/pic/dl.png"></a>
    <span class="pro_free"><img class="pro_free" src="free.png"></span>
    <span title="2024-05-01 12:00:00">2024-05-01 12:00:00</span>
  </td>
  <td class="row">1.5 GB</td>
  <td class="row"><a href="?cat=401">Movies</a></td>
  </table>
</td></tr>
</table>
"""

_DETAIL_HTML = """
<html><head><title>测试.2024.1080p</title></head><body>
<table class="torrents"><tr><td>
  <a href="download.php?id=12345&amp;passkey=abc">下载种子文件</a>
</td></tr></table>
</body></html>
"""

_INDEX_HTML = """
<html><body>
<a href="userdetails.php?id=1">测试用户</a>
<a href="logout.php">退出</a>
</body></html>
"""


async def test_generic_search_uses_rule_pack() -> None:
    """通用适配器按规则包拼 URL 并解析列表。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/torrents.php").mock(
            return_value=httpx.Response(200, text=_LIST_HTML)
        )
        items = await adapter.search_torrents({"keyword": "测试"})
    await adapter.close()

    assert len(items) == 1
    item = items[0]
    assert item.title == "测试.2024.1080p.BluRay"
    assert item.item_id == "12345"
    assert item.download_factor == 0
    assert (item.free_payload or {}).get("free_classes") == ["free"]


async def test_generic_check_reachability_detects_login() -> None:
    """首页出现 userdetails/logout 时判定为已登录。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get("https://pt.0ff.cc/").mock(
            return_value=httpx.Response(200, text=_INDEX_HTML)
        )
        health = await adapter.check_reachability()
    await adapter.close()

    assert health.reachable is True
    assert health.logged_in is True


async def test_generic_torrent_url_from_detail_page() -> None:
    """详情页里的下载锚点被补成绝对 URL。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/details.php?id=12345").mock(
            return_value=httpx.Response(200, text=_DETAIL_HTML)
        )
        url = await adapter.get_torrent_url("12345")
    await adapter.close()

    assert url == "https://pt.0ff.cc/download.php?id=12345&passkey=abc"


async def test_mteam_connectivity_uses_api_tester() -> None:
    """API 站连通性走专站 test_connection（不看首页登录标记）。"""
    adapter = GenericAdapter(
        SiteProfile(name="馒头", domain="m-team.io"),
        apikey="test-key",
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith="https://api.m-team.io/api/member/profile").mock(
            return_value=httpx.Response(200, json={"code": "0", "data": {"id": 1}})
        )
        health = await adapter.check_reachability()
    await adapter.close()

    assert health.reachable is True
    assert health.logged_in is True
    assert "成功" in health.detail


async def test_mteam_connectivity_reports_auth_failure() -> None:
    """鉴权失败：可达但未登录（不是网络故障）。"""
    adapter = GenericAdapter(
        SiteProfile(name="馒头", domain="m-team.io"),
        apikey="bad-key",
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith="https://api.m-team.io/api/member/profile").mock(
            return_value=httpx.Response(
                200, json={"code": "1", "message": "鉴权已过期或无效"}
            )
        )
        health = await adapter.check_reachability()
    await adapter.close()

    assert health.reachable is True
    assert health.logged_in is False
    assert "鉴权" in health.detail


_SUBTITLE_HTML = """
<table>
<tr>
  <td class="rowfollow"><img title="简体中文" src="/lang/schinese.png"></td>
  <td class="rowfollow"><a href="download.php?id=123&amp;subid=456">测试字幕.ass</a></td>
  <td class="rowfollow"><span title="2024-05-01 12:00:00">2024-05-01</span></td>
  <td class="rowfollow">12.5 KB</td>
  <td class="rowfollow">3</td>
  <td class="rowfollow">uploader1</td>
  <td class="rowfollow"><a href="report.php?id=123">举报</a></td>
</tr>
</table>
"""


async def test_generic_subtitle_search_uses_rule() -> None:
    """规则包的 subtitles 配置能解析字幕列表。"""
    adapter = GenericAdapter(
        SiteProfile(name="自由农场", domain="pt.0ff.cc"),
        cookies="uid=1",
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://pt.0ff.cc/subtitles.php").mock(
            return_value=httpx.Response(200, text=_SUBTITLE_HTML)
        )
        items = await adapter.search_subtitles({"keyword": "测试"})
    await adapter.close()

    assert len(items) == 1
    item = items[0]
    assert item.title == "测试字幕.ass"
    assert item.language == "简体中文"
    assert item.download_url == "https://pt.0ff.cc/download.php?id=123&subid=456"
    assert item.size == 12800
    assert item.subtitle_id == "456"
    assert item.published_at == "2024-05-01 12:00:00"


async def test_generic_subtitle_search_without_config_returns_empty() -> None:
    """没有 subtitles 配置的站点返回 []（不是故障）。"""
    from app.adapters.sites.indexer.rules import load_site_rules

    rule = next(r for r in load_site_rules().indexers.values() if not r.get("subtitles"))
    adapter = GenericAdapter(
        SiteProfile(name=str(rule.get("name")), domain=str(rule.get("domain"))),
        apikey="k",
    )
    assert await adapter.search_subtitles({"keyword": "测试"}) == []
    await adapter.close()


def test_torrent_detail_is_a_frozen_dataclass() -> None:
    """契约结构必须是 dataclass——否则 get_torrent_detail 一调就 TypeError。

    曾因漏掉 ``@dataclass`` 装饰器，``TorrentDetail(item_id=...)`` 在运行时
    抛 ``TypeError: takes no arguments``（静态检查同样报「unexpected keyword」）。
    """
    from dataclasses import fields, is_dataclass

    from app.adapters.sites.base import TorrentDetail

    assert is_dataclass(TorrentDetail)
    assert {f.name for f in fields(TorrentDetail)} >= {
        "item_id",
        "title",
        "size",
        "download_url",
        "free_payload",
        "raw_html",
        "error",
    }
    detail = TorrentDetail(item_id="123", title="片名", size=1024)
    assert detail.item_id == "123"
    assert detail.error is None
