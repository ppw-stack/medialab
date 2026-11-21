"""站点规则包与通用解析引擎的单元测试（无网络）。"""

from __future__ import annotations

from app.adapters.sites._registry import resolve
from app.adapters.sites.generic import GenericAdapter, _promo_payload, _to_feed_item
from app.adapters.sites.indexer.rules import load_site_rules
from app.adapters.sites.indexer.spider import SiteSpider, normalize_selector

#: 一段 NexusPHP 列表页（含免费标记 / 下载链接 / 分类 / 体积）
_NEXUS_ROW = """
<table class="torrents">
<tr><td>
  <table class="torrentname"><tr><td>
    <a title="测试.2024.1080p.BluRay" href="details.php?id=12345&hit=1"><b>测试.2024.1080p</b></a>
    <a href="download.php?id=12345&passkey=abc"><img src="/pic/dl.png"></a>
    <span class="pro_free"><img class="pro_free" src="free.png"></span>
    <span title="2024-05-01 12:00:00">2024-05-01 12:00:00</span>
  </td>
  <td class="row">1.5 GB</td>
  <td class="row"><a href="?cat=401">Movies</a></td>
  <td class="row">12 / 3</td>
  <td class="row">7</td>
  </table>
</td></tr>
</table>
"""


def test_rules_pack_is_loaded() -> None:
    """规则包应能加载到完整站点目录。"""
    rules = load_site_rules()
    assert rules.version
    assert len(rules.indexers) >= 180


def test_find_rule_by_domain_and_id() -> None:
    """按域名与按 ID 都能定位到同一份规则。"""
    rules = load_site_rules()
    by_domain = rules.find_by_domain("https://pt.0ff.cc/")
    assert by_domain is not None
    assert by_domain["id"] == "0ff"
    assert rules.get("0ff") is by_domain


def test_mteam_rule_present_with_domain_aliases() -> None:
    """规则包包含 m-team，且主域/子域都能匹配。"""
    rules = load_site_rules()
    rule = rules.find_by_domain("m-team.io")
    assert rule is not None
    assert rule["id"] == "mteam"
    assert rule["schema"] == "MTorrent"
    assert rule["parser"] == "mTorrent"
    assert rules.find_by_domain("xp.m-team.io") is rule


def test_mteam_spider_provides_download_url() -> None:
    """API 站的下载配方由专站 spider 提供（generic 只负责调用）。"""
    from app.adapters.sites.indexer.engine import SPIDER_PARSER_CLASSES

    assert callable(getattr(SPIDER_PARSER_CLASSES["mTorrent"], "get_download_url", None))


def test_selector_normalization_removes_quotes() -> None:
    """规则里的 ``:has("x")`` 必须被规范化，否则 cssselect 直接报错。"""
    assert normalize_selector('tr:has("table.torrentname")') == "tr:has(table.torrentname)"


def test_search_url_uses_site_rule() -> None:
    """通用爬虫按规则生成搜索 URL（路径 / 参数 / 分类）。"""
    rule = load_site_rules().get("0ff")
    assert rule is not None
    spider = SiteSpider(indexer={**rule, "domain": "https://pt.0ff.cc/"}, keyword="测试")
    url = spider._SiteSpider__get_search_url()
    assert url.startswith("https://pt.0ff.cc/torrents.php?")
    assert "search=%E6%B5%8B%E8%AF%95" in url
    assert "notnewword=1" in url


def test_parse_nexusphp_row_with_real_rule() -> None:
    """真实规则能解析出标题、下载地址、倍率与分类。"""
    rule = load_site_rules().get("0ff")
    assert rule is not None
    spider = SiteSpider(indexer={**rule, "domain": "https://pt.0ff.cc/"})
    rows = spider.parse(_NEXUS_ROW)
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "测试.2024.1080p.BluRay"
    assert row["enclosure"] == "https://pt.0ff.cc/download.php?id=12345&passkey=abc"
    assert row["downloadvolumefactor"] == 0
    assert row["category"] == "电影"


def test_feed_item_mapping() -> None:
    """引擎结果映射到本项目 FeedItem，并生成免费窗口输入。"""
    row = {
        "title": "测试.2024.1080p",
        "id": "12345",
        "size": 1610612736,
        "category": "电影",
        "pubdate": "2024-05-01 12:00:00",
        "downloadvolumefactor": 0,
        "freedate": "2天18时",
    }
    item = _to_feed_item(row, {})
    assert item.title == "测试.2024.1080p"
    assert item.item_id == "12345"
    assert item.size == 1610612736
    assert item.download_factor == 0
    assert item.published_at == "2024-05-01 12:00:00"
    payload = item.free_payload or {}
    assert "free" in payload["free_classes"]
    assert payload["remaining_text"] == "2天18时"


def test_promo_payload_none_for_normal_torrent() -> None:
    """普通种子不产生免费窗口输入（避免被误判成免费）。"""
    assert _promo_payload({"downloadvolumefactor": 1}, {}) is None


def test_registry_resolves_generic_adapter() -> None:
    """通用适配器必须注册到 adapter 注册表。"""
    assert resolve("generic") is GenericAdapter
