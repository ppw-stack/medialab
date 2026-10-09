"""m-team 适配器测试（FR-1.5 站点特化）。

覆盖：
- ``MteamAdapter.parse_feed`` 解析 m-team RSS 2.0 XML 响应
- ``MteamAdapter._classify_free`` 各 discount/free/free_deadline 组合
- ``MteamAdapter.capabilities`` 能力声明
- 联调：``@pytest.mark.integration`` 真实拉取 m-team RSS API（需要未过期 sign/t）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.mteam import MteamAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType


# ---- RSS 2.0 XML 响应快照（m-team 实际格式） ----

#: m-team RSS XML 样例：3 条标准 RSS2 格式
MTEAM_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>M-Team - TP</title>
    <item>
      <guid isPermaLink="false">100001</guid>
      <title>Ubuntu.24.04.Desktop amd64</title>
      <link>https://kp.m-team.cc/detail/100001</link>
      <description>官方长期支持版本</description>
      <pubDate>Fri, 09 Oct 2026 13:00:00 GMT</pubDate>
      <category domain="https://kp.m-team.cc/browse?cat=423">PCGame</category>
      <enclosure length="5368709120" type="application/x-bittorrent" url="https://rss.m-team.cc/dl/100001"/>
    </item>
    <item>
      <guid isPermaLink="false">100002</guid>
      <title>Debian.12.iso</title>
      <link>https://kp.m-team.cc/detail/100002</link>
      <description>限时免费</description>
      <pubDate>Fri, 09 Oct 2026 12:30:00 GMT</pubDate>
      <category domain="https://kp.m-team.cc/browse?cat=423">PCGame</category>
      <enclosure length="1073741824" type="application/x-bittorrent" url="https://rss.m-team.cc/dl/100002"/>
    </item>
    <item>
      <guid isPermaLink="false">100003</guid>
      <title>Old.Movie.1980.avi</title>
      <link>https://kp.m-team.cc/detail/100003</link>
      <description>普通种子</description>
      <pubDate>Fri, 09 Oct 2026 12:00:00 GMT</pubDate>
      <category domain="https://kp.m-team.cc/browse?cat=419">Movie/HD</category>
      <enclosure length="700000000" type="application/x-bittorrent" url="https://rss.m-team.cc/dl/100003"/>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def adapter() -> MteamAdapter:
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    return MteamAdapter(profile, clock_offset=ClockOffset.zero())


# ---- parse_feed 单元测试（XML） ----


def test_parse_feed_decodes_mteam_xml(adapter: MteamAdapter) -> None:
    items = adapter.parse_feed(MTEAM_RSS_XML)
    assert len(items) == 3
    titles = [it.title for it in items]
    assert "Ubuntu.24.04.Desktop amd64" in titles
    assert "Debian.12.iso" in titles


def test_parse_feed_xml_extracts_size_from_enclosure(adapter: MteamAdapter) -> None:
    """size 来自 enclosure.length。"""
    items = adapter.parse_feed(MTEAM_RSS_XML)
    sizes = {it.title: it.size for it in items}
    assert sizes["Ubuntu.24.04.Desktop amd64"] == 5_368_709_120
    assert sizes["Debian.12.iso"] == 1_073_741_824


def test_parse_feed_xml_extracts_category(adapter: MteamAdapter) -> None:
    items = adapter.parse_feed(MTEAM_RSS_XML)
    cats = {it.title: it.category for it in items}
    assert cats["Ubuntu.24.04.Desktop amd64"] == "PCGame"
    assert cats["Old.Movie.1980.avi"] == "Movie/HD"


def test_parse_feed_xml_no_infohash(adapter: MteamAdapter) -> None:
    """m-team RSS 不带 InfoHash（只有数字 guid），列表层无法获取。"""
    items = adapter.parse_feed(MTEAM_RSS_XML)
    assert all(it.info_hash is None for it in items)


def test_parse_feed_xml_uses_enclosure_url_as_torrent_url(adapter: MteamAdapter) -> None:
    items = adapter.parse_feed(MTEAM_RSS_XML)
    assert items[0].torrent_url == "https://rss.m-team.cc/dl/100001"


def test_parse_feed_handles_invalid_xml(adapter: MteamAdapter) -> None:
    assert adapter.parse_feed("<not-xml>") == []
    assert adapter.parse_feed("") == []
    assert adapter.parse_feed("not xml at all") == []


def test_parse_feed_handles_xml_without_items(adapter: MteamAdapter) -> None:
    xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    assert adapter.parse_feed(xml) == []


def test_parse_feed_caps_items(adapter: MteamAdapter) -> None:
    """1000 条 RSS item → 仅取前 500。"""
    items_xml = "".join(
        f"<item><title>t{i}</title><link>https://x/{i}</link></item>"
        for i in range(1000)
    )
    xml = f'<?xml version="1.0"?><rss version="2.0"><channel>{items_xml}</channel></rss>'
    items = adapter.parse_feed(xml)
    # _MAX_ITEMS_PER_PULL = 500
    assert len(items) == 500


# ---- _classify_free 单元测试（字典路径，模拟 JSON 详情 API 场景） ----


def test_classify_discount_100_permanent(adapter: MteamAdapter) -> None:
    assert adapter._classify_free({"discount": 100}) == (FreeType.PERMANENT, None)


def test_classify_discount_50_neutral(adapter: MteamAdapter) -> None:
    assert adapter._classify_free({"discount": -50}) == (FreeType.NEUTRAL, None)


def test_classify_discount_100_with_deadline_drops_to_permanent(adapter: MteamAdapter) -> None:
    """m-team 行为：discount=100 优先 → 永久，截止时刻被忽略。"""
    ft, end = adapter._classify_free(
        {"discount": 100, "free_deadline": "2030-01-01T00:00:00Z"}
    )
    assert ft == FreeType.PERMANENT
    assert end is None


def test_classify_discount_100_no_deadline_permanent(adapter: MteamAdapter) -> None:
    """discount=100 + 无 deadline = 永久。"""
    assert adapter._classify_free({"discount": 100}) == (FreeType.PERMANENT, None)


def test_classify_discount_100_ignores_deadline_field(adapter: MteamAdapter) -> None:
    """discount=100 时 deadline 字段完全被忽略（避免 P2：m-team 偶尔有脏数据）。"""
    ft, end = adapter._classify_free(
        {"discount": 100, "free_deadline": "2030-01-01T00:00:00Z", "free": True}
    )
    assert ft == FreeType.PERMANENT
    assert end is None


def test_classify_discount_0_with_deadline_timed(adapter: MteamAdapter) -> None:
    ft, end = adapter._classify_free(
        {"discount": 0, "free_deadline": "2030-01-01T00:00:00Z"}
    )
    assert ft == FreeType.TIMED
    assert isinstance(end, datetime)


def test_classify_no_fields_unknown(adapter: MteamAdapter) -> None:
    assert adapter._classify_free({}) == (FreeType.UNKNOWN, None)


def test_classify_free_flag_true_no_deadline_permanent(adapter: MteamAdapter) -> None:
    assert adapter._classify_free({"free": True, "discount": 0}) == (FreeType.PERMANENT, None)


def test_classify_free_flag_true_with_deadline_timed(adapter: MteamAdapter) -> None:
    """free=true + 有截止 = 限时（不视作永久）。"""
    ft, end = adapter._classify_free(
        {"free": True, "discount": 0, "free_deadline": "2030-01-01T00:00:00Z"}
    )
    assert ft == FreeType.TIMED


# ---- parse_window 字典路径（基于 sample） ----


def test_parse_window_permanent_from_dict(adapter: MteamAdapter) -> None:
    import asyncio
    win = asyncio.run(adapter.parse_window({"discount": 100}))
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 1.0
    assert win.free_source == FreeSource.API


def test_parse_window_timed_from_dict(adapter: MteamAdapter) -> None:
    import asyncio
    win = asyncio.run(adapter.parse_window({"discount": 0, "free_deadline": "2030-01-01T00:00:00Z"}))
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.free_source == FreeSource.API


def test_parse_window_unknown_from_dict(adapter: MteamAdapter) -> None:
    import asyncio
    win = asyncio.run(adapter.parse_window({}))
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 能力声明 ----


def test_capabilities_includes_rss_clock(adapter: MteamAdapter) -> None:
    """m-team 列表 RSS + 时钟探测；不宣称 DETAIL_API（列表不含免费字段）。"""
    from app.adapters.sites.base import SiteFeature
    caps = adapter.capabilities()
    assert SiteFeature.RSS in caps
    assert SiteFeature.CLOCK_API in caps
    assert SiteFeature.DETAIL_API not in caps


def test_xml_categories_are_main_only_no_free(adapter: MteamAdapter) -> None:
    """m-team RSS category 只有主分类（9 个），不含 free 字段。

    这解释了列表层免费识别率 = 0：免费信息只在详情页。
    """
    items = adapter.parse_feed(MTEAM_RSS_XML)
    cats = {it.category for it in items}
    assert cats == {"PCGame", "Movie/HD"}  # sample 中只有这两个
    # 任何一条都不应在 category 中含 "free" / "限免"
    for it in items:
        assert it.category is None or "free" not in it.category.lower()
        assert "免" not in (it.category or "")


# ---- 注册中心 ----


def test_registry_resolves_mteam():
    from app.adapters.sites import resolve
    cls = resolve("mteam")
    assert cls is MteamAdapter


def test_registry_rejects_unknown():
    from app.adapters.sites import resolve
    with pytest.raises(ValueError, match="未注册"):
        resolve("nonexistent")


# ---- 联调：真实 m-team JSON API（需用户提供未过期 sign/t） ----

#: 联调 URL（用户提供，2026-10-09 21:09 UTC 之前有效）
MTEAM_LIVE_URL = (
    "https://rss.m-team.cc/api/rss/fetch"
    "?dl=1&pageSize=100&sign=a7500dce018f714fab62e8b5d2890127&t=1791553775&tkeys=ttitle&uid=291372"
)


@pytest.mark.integration
def test_live_mteam_pull_confirms_rss_parsing(adapter: MteamAdapter) -> None:
    """真实拉取 m-team RSS。

    诚实预期：m-team RSS 列表 category 只有主分类，enclosure URL 也不带
    discount / free 字段 → 列表层全部识别为 UNKNOWN。免费信息只在详情页。
    """
    import asyncio
    items = asyncio.run(adapter.fetch_feed(MTEAM_LIVE_URL))
    if not items:
        pytest.skip("联调 URL 无效或无数据；请更新 MTEAM_LIVE_URL")
    assert len(items) > 0, "RSS 应有至少一条 item"
    # 列表无 discount/free 字段 → parse_window(dict) 全部走 UNKNOWN 分支
    import asyncio as _aio
    windows = _aio.run(_classify_all(adapter, items))
    types = {w.free_type for w in windows}
    assert types == {FreeType.UNKNOWN}, (
        f"预期全部 UNKNOWN（m-team 列表无免费标记），实际={types}"
    )


async def _classify_all(adapter: MteamAdapter, items):
    """联调 helper：把 FeedItem 字段映射成 dict，传给 parse_window(dict 路径)。"""
    from dataclasses import asdict
    return [await adapter.parse_window(asdict(it)) for it in items]
