"""RSS 解析测试（FR-2.2 / FR-1.5.1 归一化）。

覆盖：命名空间、InfoHash 提取、size 单位、容错。
"""

from __future__ import annotations

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.generic import GenericSiteAdapter

HASH = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"

NEXUSPHP_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Test PT</title>
  <item>
    <title>Ubuntu.24.04.iso</title>
    <link>https://pt.invalid/rss.php?feedkey=K&amp;id=1</link>
    <guid isPermaLink="false">{HASH}</guid>
    <category>linux</category>
    <size>5368709120</size>
  </item>
</channel>
</rss>
"""

ATOM_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom PT</title>
  <entry>
    <title>Debian.12.iso</title>
    <link href="https://pt.invalid/details.php?id=2"/>
    <id>{HASH}</id>
    <category term="linux"/>
    <content type="text">1.2 GB</content>
  </entry>
</feed>
"""

BROKEN_XML = "<rss><channel><item><title>unclosed"


@pytest.fixture
def adapter() -> GenericSiteAdapter:
    return GenericSiteAdapter(SiteProfile(name="pt", domain="pt.invalid"))


def test_parses_nexusphp_item(adapter: GenericSiteAdapter) -> None:
    items = adapter.parse_feed(NEXUSPHP_RSS)
    assert len(items) == 1
    it = items[0]
    assert it.title == "Ubuntu.24.04.iso"
    assert it.info_hash == HASH, "guid 中的 InfoHash 必须被提取（FR-2.3 匹配前提）"
    assert it.size == 5368709120
    assert it.category == "linux"


def test_namespace_stripped(adapter: GenericSiteAdapter) -> None:
    """带 XML 命名空间的 Atom 也能解析出 title/link/id。"""
    items = adapter.parse_feed(ATOM_RSS)
    assert len(items) == 1
    assert items[0].title == "Debian.12.iso"
    assert items[0].info_hash == HASH


def test_broken_xml_returns_empty(adapter: GenericSiteAdapter) -> None:
    """损坏的 XML 不得抛异常上抛业务层（FR-1.5.4 降级）。"""
    assert adapter.parse_feed(BROKEN_XML) == []


def test_empty_channel(adapter: GenericSiteAdapter) -> None:
    assert adapter.parse_feed("<rss><channel></channel></rss>") == []


def test_item_without_hash_is_still_returned(adapter: GenericSiteAdapter) -> None:
    """无 hash 的条目仍要返回，由 matcher 判 no_local_match。

    丢弃条目会让人无法排查"为什么这个种没被辅种"。
    """
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>X</title><link>https://p/details.php?id=9</link></item>
    </channel></rss>"""
    items = adapter.parse_feed(xml)
    assert len(items) == 1
    assert items[0].info_hash is None


def test_magnet_enclosure_used_as_torrent_url(adapter: GenericSiteAdapter) -> None:
    xml = f"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Y</title>
    <enclosure url="magnet:?xt=urn:btih:{HASH}"/>
    <guid isPermaLink="false">{HASH}</guid>
    </item></channel></rss>"""
    items = adapter.parse_feed(xml)
    assert items[0].torrent_url
    assert "magnet:" in items[0].torrent_url


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1024", 1024),
        ("1KB", 1024),
        ("1 KiB", 1024),
        ("1MB", 1024**2),
        ("1.5 GB", int(1.5 * 1024**3)),
        ("2TB", 2 * 1024**4),
        ("", 0),
        ("garbage", 0),
    ],
)
def test_size_parsing(text: str, expected: int) -> None:
    from app.adapters.sites.generic import _parse_size

    assert _parse_size(text) == expected