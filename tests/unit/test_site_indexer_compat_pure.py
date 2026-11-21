"""站点索引移植层的纯函数契约测试（无网络、无 DB）。

这些模块（``compat/*`` 与 ``download.py``）是规则包适配的地基：容量解析、
时间归一、URL 处理、RSS 续期、下载配方解释。全部是纯函数，直接测最划算。
"""

from __future__ import annotations

import base64
import json
import os
from xml.dom import minidom

import pytest
from app.adapters.sites.indexer.compat import site_rules, torrent_rules
from app.adapters.sites.indexer.compat import size as size_tools
from app.adapters.sites.indexer.compat import temporal as time_tools
from app.adapters.sites.indexer.compat import text as text_tools
from app.adapters.sites.indexer.compat import url as url_tools
from app.adapters.sites.indexer.compat.crypto import HashUtils
from app.adapters.sites.indexer.compat.dom import DomUtils
from app.adapters.sites.indexer.compat.exceptions import RateLimitExceededException
from app.adapters.sites.indexer.compat.types import MediaSource, MediaType
from app.adapters.sites.indexer.download import (
    encrypt_site_payload,
    is_download_confirmation,
    parse_download_confirmation,
    parse_download_response,
)
from lxml import etree


class TestSizeTools:
    """容量文本与字节数的互转。"""

    def test_parse_size_with_units(self) -> None:
        assert size_tools.parse_size("1 KB") == 1024
        assert size_tools.parse_size("1MB") == 1024**2
        assert size_tools.parse_size("1.5 GB") == round(1.5 * 1024**3)
        assert size_tools.parse_size("1 TB") == 1024**4
        assert size_tools.parse_size("1 PB") == 1024**5
        assert size_tools.parse_size("1 KiB") == 1024
        assert size_tools.parse_size("1 GiB") == 1024**3

    def test_parse_size_plain_and_separators(self) -> None:
        assert size_tools.parse_size("1024") == 1024
        assert size_tools.parse_size("1,024") == 1024
        assert size_tools.parse_size(2048) == 2048

    def test_parse_size_invalid(self) -> None:
        assert size_tools.parse_size("") == 0
        assert size_tools.parse_size(None) == 0  # type: ignore[arg-type]
        assert size_tools.parse_size("abc") == 0

    def test_format_compact_size(self) -> None:
        assert size_tools.format_compact_size(512) == "512.0B"
        assert size_tools.format_compact_size(1024) == "1.0K"
        assert size_tools.format_compact_size(1024**2) == "1.0M"
        assert size_tools.format_compact_size(1024**3) == "1.0G"
        assert size_tools.format_compact_size(1024**4) == "1.0T"

    def test_format_compact_size_passthrough(self) -> None:
        # 已带单位的文本只去掉 B / iB 与空白后原样返回
        assert size_tools.format_compact_size("1.5GB") == "1.5G"
        assert size_tools.format_compact_size(None) == ""
        assert size_tools.format_compact_size("abc") == "abcB"

    def test_format_size(self) -> None:
        assert size_tools.format_size(0) == "0 B"
        assert size_tools.format_size(512) == "512 B"
        assert size_tools.format_size(1024) == "1.00 KB"
        assert size_tools.format_size(1024**3) == "1.00 GB"


class TestTextTools:
    """文本与数值解析。"""

    def test_contains_chinese(self) -> None:
        assert text_tools.contains_chinese("测试")
        assert text_tools.contains_chinese(["abc", "测试"])
        assert not text_tools.contains_chinese("abc")
        assert not text_tools.contains_chinese("")
        assert not text_tools.contains_chinese([])

    def test_parse_int(self) -> None:
        assert text_tools.parse_int("1,024") == 1024
        assert text_tools.parse_int(" 42 ") == 42
        assert text_tools.parse_int("abc") == 0
        assert text_tools.parse_int("") == 0

    def test_parse_float(self) -> None:
        assert text_tools.parse_float("1,024.5") == 1024.5
        assert text_tools.parse_float(" 3.5 ") == 3.5
        assert text_tools.parse_float("abc") == 0.0
        assert text_tools.parse_float("") == 0.0

    def test_parse_bool(self) -> None:
        assert text_tools.parse_bool(True) is True
        assert text_tools.parse_bool(0) is False
        assert text_tools.parse_bool(1) is True
        assert text_tools.parse_bool("yes") is True
        assert text_tools.parse_bool("否") is False
        assert text_tools.parse_bool("", True) is True
        assert text_tools.parse_bool("unknown", True) is True
        assert text_tools.parse_bool("unknown") is False

    def test_remove_punctuation(self) -> None:
        assert text_tools.remove_punctuation("a.b") == "ab"
        assert text_tools.remove_punctuation("a b") == "ab"
        assert text_tools.remove_punctuation("a b", allow_space=True) == "a b"
        assert text_tools.remove_punctuation(["a.b", "c!"]) == ["ab", "c"]
        assert text_tools.remove_punctuation("") == ""


class TestTemporalTools:
    """时间归一。"""

    def test_parse_datetime_formats(self) -> None:
        assert time_tools.parse_datetime("2024-05-01 12:00:00") is not None
        assert time_tools.parse_datetime("2024/05/01") is not None
        assert time_tools.parse_datetime("2024-05-01T12:00:00") is not None

    def test_parse_datetime_invalid(self) -> None:
        assert time_tools.parse_datetime(None) is None
        assert time_tools.parse_datetime("") is None
        assert time_tools.parse_datetime("not a date") is None

    def test_normalize_datetime(self) -> None:
        assert time_tools.normalize_datetime("2024-05-01 12:00:00") == "2024-05-01 12:00:00"
        assert time_tools.normalize_datetime("2024-05-01") == "2024-05-01 00:00:00"
        # 无法解析时原样返回
        assert time_tools.normalize_datetime("乱码") == "乱码"
        assert time_tools.normalize_datetime(None) is None

    def test_format_timestamp(self) -> None:
        assert time_tools.format_timestamp(0) is not None
        assert time_tools.format_timestamp("2024-05-01 12:00:00") == "2024-05-01 12:00:00"
        assert time_tools.format_timestamp(None) is None


class TestHashUtils:
    """摘要算法。"""

    def test_md5(self) -> None:
        assert HashUtils.md5("abc") == "900150983cd24fb0d6963f7d28e17f72"
        assert HashUtils.md5(b"abc") == "900150983cd24fb0d6963f7d28e17f72"

    def test_sha1(self) -> None:
        assert HashUtils.sha1("abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"
        assert HashUtils.sha1(b"abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"

    def test_sha256(self) -> None:
        assert HashUtils.sha256("abc") == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
        assert HashUtils.sha256(b"abc") == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )


class TestDomUtils:
    """XML DOM 读取与构造。"""

    @staticmethod
    def _doc() -> minidom.Document:
        return minidom.parseString(
            "<root><item id='7'>文本</item><item id='8'>另一段</item></root>"
        )

    def test_has_child_elements(self) -> None:
        # 调用方传的是 lxml 元素（支持 len()），不是 minidom 节点
        element = etree.fromstring("<root><item/></root>")
        assert DomUtils.has_child_elements(element) is True
        assert DomUtils.has_child_elements(etree.fromstring("<root/>")) is False
        assert DomUtils.has_child_elements(None) is False

    def test_tag_value_text(self) -> None:
        doc = self._doc()
        assert DomUtils.tag_value(doc, "item") == "文本"

    def test_tag_value_attribute(self) -> None:
        doc = self._doc()
        assert DomUtils.tag_value(doc, "item", "id") == "7"

    def test_tag_value_default(self) -> None:
        doc = self._doc()
        assert DomUtils.tag_value(doc, "missing") is None
        assert DomUtils.tag_value(doc, "missing", default="兜底") == "兜底"
        assert DomUtils.tag_value(doc, "item", "missing-attr", default="兜底") == "兜底"

    def test_add_node(self) -> None:
        doc = self._doc()
        root = doc.documentElement
        node = DomUtils.add_node(doc, root, "child", "值")
        assert node.tagName == "child"
        assert node.firstChild is not None
        assert node.firstChild.data == "值"
        empty = DomUtils.add_node(doc, root, "empty")
        assert empty.firstChild is None


class TestUrlTools:
    """URL 解析、拼接与脱敏。"""

    def test_normalize_http_url(self) -> None:
        assert url_tools.UrlUtils.normalize_http_url("https://a.com/x") == "https://a.com/x"
        assert url_tools.UrlUtils.normalize_http_url("ftp://a.com") is None
        assert url_tools.UrlUtils.normalize_http_url("https://a.com/ x") is None
        assert url_tools.UrlUtils.normalize_http_url(None) is None
        assert url_tools.UrlUtils.normalize_http_url("") is None

    def test_same_origin(self) -> None:
        assert url_tools.UrlUtils.same_origin("https://a.com/x", "https://a.com/y") is True
        assert url_tools.UrlUtils.same_origin("https://a.com/x", "https://b.com/y") is False
        assert url_tools.UrlUtils.same_origin("https://a.com/x", "http://a.com/y") is False
        assert url_tools.UrlUtils.same_origin("https://u:p@a.com/x", "https://a.com/y") is False

    def test_standardize_and_adapt(self) -> None:
        assert url_tools.UrlUtils.standardize_base_url("a.com") == "http://a.com/"
        assert url_tools.UrlUtils.standardize_base_url("") == ""
        assert url_tools.UrlUtils.adapt_request_url("", "https://a.com/x") == "https://a.com/x"
        assert url_tools.UrlUtils.adapt_request_url("a.com", "/x") == "http://a.com/x"

    def test_combine_url(self) -> None:
        combined = url_tools.UrlUtils.combine_url("https://a.com", "/x", {"k": "v"})
        assert combined is not None
        assert "k=v" in combined

    def test_get_mime_type(self) -> None:
        assert url_tools.UrlUtils.get_mime_type("a.mp4") == "video/mp4"
        assert url_tools.UrlUtils.get_mime_type("a.unknown") == "application/octet-stream"
        assert url_tools.UrlUtils.get_mime_type("a.mp4", "text/plain") == "video/mp4"

    def test_quote_and_parse_url_params(self) -> None:
        assert url_tools.UrlUtils.quote("a b") == "a%20b"
        assert url_tools.UrlUtils.parse_url_params("https://a.com:8443/x") == (
            "https", "a.com", 8443, "/x/",
        )
        assert url_tools.UrlUtils.parse_url_params("a.com") == ("http", "a.com", 80, "/")
        assert url_tools.UrlUtils.parse_url_params("") is None

    def test_url_matches_trusted_hosts(self) -> None:
        assert url_tools.url_matches_trusted_hosts("https://a.com/x", ["a.com"]) is True
        assert url_tools.url_matches_trusted_hosts("https://a.com:8443/x", ["a.com:8443"]) is True
        assert url_tools.url_matches_trusted_hosts("https://b.com/x", ["a.com"]) is False
        assert url_tools.url_matches_trusted_hosts("ftp://a.com/x", ["a.com"]) is False

    def test_split_netloc_and_labels(self) -> None:
        assert url_tools.split_netloc("https://a.com/x") == ("https", "a.com")
        assert url_tools.split_netloc("a.com") == ("http", "a.com")
        assert url_tools.split_netloc("") == ("", "")
        assert url_tools.second_level_label("https://www.a.com") == "a"
        assert url_tools.second_level_label("localhost") == "localhost"
        assert url_tools.second_level_label("") == ""
        assert url_tools.host_label("https://www.a.com") == "a"
        assert url_tools.host_label("") == ""
        assert url_tools.base_url("https://a.com/x") == "https://a.com"
        assert url_tools.base_url("") == ""

    def test_mask_url_credentials(self) -> None:
        masked = url_tools.mask_url_credentials("https://a.com/x?passkey=secret&page=1")
        assert "secret" not in masked
        assert "page=1" in masked
        assert url_tools.mask_url_credentials("") == ""
        userinfo = url_tools.mask_url_credentials("https://user:pw@a.com/x")
        assert "pw" not in userinfo

    def test_url_origin_for_log(self) -> None:
        assert url_tools.url_origin_for_log("https://a.com:8443/x?k=v") == "https://a.com:8443"
        assert url_tools.url_origin_for_log("") == "://"

    def test_parse_address(self) -> None:
        assert url_tools.parse_address("a.com:8080") == ("http://a.com", 8080)
        assert url_tools.parse_address("https://a.com") == ("https://a.com", 443)
        assert url_tools.parse_address("a.com:8080", include_scheme=False) == ("a.com:8080", 80)
        assert url_tools.parse_address("") == (None, None)
        assert url_tools.parse_address("a:b:c:d") == (None, None)
        assert url_tools.parse_address("a.com") == ("http://a.com", 80)
        assert url_tools.parse_address("a.com:8080:extra") == (None, None)

    def test_is_link(self) -> None:
        assert url_tools.is_link("https://a.com") is True
        assert url_tools.is_link("a.com") is True
        assert url_tools.is_link("not a link") is False
        assert url_tools.is_link("") is False


class TestSiteRules:
    """站点地址匹配与 RSS 续期。"""

    def test_normalize_rss_link(self) -> None:
        base = "https://a.com/"
        assert site_rules.normalize_rss_link("rss.php?key=1", base) == "https://a.com/rss.php?key=1"
        assert site_rules.normalize_rss_link("#", base) is None
        assert site_rules.normalize_rss_link("", base) is None
        assert site_rules.normalize_rss_link(None, base) is None
        assert site_rules.normalize_rss_link("https://u:p@a.com/x", base) is None

    def test_normalize_rss_link_broad(self) -> None:
        base = "https://a.com/"
        assert site_rules.normalize_rss_link("feed.php", base, broad=True) is not None
        assert site_rules.normalize_rss_link("other.php", base, broad=True) is None
        assert site_rules.normalize_rss_link("other.php?passkey=1", base, broad=True) is not None

    def test_renew_rss_link(self) -> None:
        renewed = site_rules.renew_rss_link(
            "https://a.com/rss.php?passkey=old&rows=50",
            "https://b.com/rss.php?passkey=new",
        )
        assert "passkey=new" in renewed
        assert "rows=50" in renewed
        assert "b.com" in renewed
        # 旧链接无效时直接用新链接
        assert site_rules.renew_rss_link(None, "https://b.com/x") == "https://b.com/x"

    def test_is_rss_document(self) -> None:
        assert site_rules.is_rss_document("<rss><channel></channel></rss>") is True
        assert site_rules.is_rss_document("<feed></feed>") is True
        assert site_rules.is_rss_document("<html><body>登录</body></html>") is False
        assert site_rules.is_rss_document("") is False

    def test_urls_match(self) -> None:
        assert site_rules.urls_match("https://www.a.com/x", "a.com") is True
        assert site_rules.urls_match("https://a.com", "https://b.com") is False
        assert site_rules.urls_match("", "a.com") is False

    def test_extract_domain(self) -> None:
        assert site_rules.extract_domain("https://www.a.com/x") == "a.com"
        assert site_rules.extract_domain("https://a.b.c.com/x") == "a.b.c.com"
        assert site_rules.extract_domain("https://.a.com/x") == "a.com"
        assert site_rules.extract_domain("") == ""
        assert site_rules.extract_domain(None) == ""

    def test_resolve_page_url(self) -> None:
        assert site_rules.resolve_page_url("https://a.com", "x.php") == "https://a.com/x.php"
        assert site_rules.resolve_page_url("https://a.com", "https://b.com/x") == "https://b.com/x"
        assert site_rules.resolve_page_url("https://a.com", None) == "https://a.com"

    def test_site_utils_login_and_checkin(self) -> None:
        assert site_rules.SiteUtils.is_logged_in("<html><body>x</body></html>") is False
        assert site_rules.SiteUtils.is_logged_in(
            "<html><body><a href='logout.php'>退出</a></body></html>"
        ) is True
        assert site_rules.SiteUtils.is_logged_in(
            "<html><body><input type='password'></body></html>"
        ) is False
        assert site_rules.SiteUtils.is_checkin("<html><body>x</body></html>") is True
        assert site_rules.SiteUtils.is_checkin(
            "<html><body><a href='attendance.php'>签到</a></body></html>"
        ) is False


class TestTorrentRules:
    """限流提示识别。"""

    def test_rate_limit_cooldown(self) -> None:
        assert torrent_rules.rate_limit_cooldown("429 Too Many Requests") == 3600
        assert torrent_rules.rate_limit_cooldown("请求过于频繁") == 3600
        assert torrent_rules.rate_limit_cooldown("普通失败") is None
        assert torrent_rules.rate_limit_cooldown(None) is None


class TestMediaTypes:
    """媒体类型枚举。"""

    def test_from_agent(self) -> None:
        assert MediaType.from_agent("movie") is MediaType.MOVIE
        assert MediaType.from_agent("电影") is MediaType.MOVIE
        assert MediaType.from_agent("tv") is MediaType.TV
        assert MediaType.from_agent("music") is MediaType.MUSIC
        assert MediaType.from_agent("collection") is MediaType.COLLECTION
        assert MediaType.from_agent("未知类型") is MediaType.UNKNOWN
        assert MediaType.from_agent(None) is MediaType.UNKNOWN

    def test_media_source_values(self) -> None:
        assert MediaSource.IMDb.value == "imdb"
        assert MediaSource.TMDB.value == "tmdb"


class TestRateLimitException:
    """限流异常。"""

    def test_message(self) -> None:
        exc = RateLimitExceededException("慢一点")
        assert exc.message == "慢一点"
        assert str(exc) == "慢一点"
        assert isinstance(exc, RuntimeError)


class TestDownloadProtocol:
    """下载配方与确认页协议。"""

    def test_is_download_confirmation(self) -> None:
        assert is_download_confirmation("下载种子文件".encode()) is True
        assert is_download_confirmation("下載種子檔案".encode()) is True
        assert is_download_confirmation(b"d8:announce") is False

    def test_parse_download_confirmation(self) -> None:
        page = (
            "<html><body><form action='' method='post'>"
            "<input type='hidden' name='csrf' value='abc'>"
            "<input type='submit' name='submit' value='下载种子文件'>"
            "</form></body></html>"
        )
        parsed = parse_download_confirmation(page, "https://a.com/download.php?id=1")
        assert parsed is not None
        target, data = parsed
        assert target == "https://a.com/download.php?id=1"
        assert data["csrf"] == "abc"
        assert data["submit"] == "下载种子文件"

    def test_parse_download_confirmation_rejects_cross_origin(self) -> None:
        page = (
            "<html><body><form action='https://evil.com/x' method='post'>"
            "<input type='hidden' name='csrf' value='abc'></form></body></html>"
        )
        assert parse_download_confirmation(page, "https://a.com/download.php") is None

    def test_parse_download_confirmation_without_form(self) -> None:
        assert parse_download_confirmation("<html></html>", "https://a.com/x") is None

    def test_parse_download_response_plain(self) -> None:
        params = {"result": "data.url", "result_base_url": "https://a.com"}
        url = parse_download_response(params, 200, {"data": {"url": "https://a.com/t.torrent"}}, None)
        assert url == "https://a.com/t.torrent"

    def test_parse_download_response_relative(self) -> None:
        params = {"result": "data.url", "result_base_url": "https://a.com"}
        url = parse_download_response(params, 200, {"data": {"url": "/t.torrent"}}, None)
        assert url == "https://a.com/t.torrent"

    def test_parse_download_response_query_param(self) -> None:
        params = {
            "result": "data.token",
            "result_path": "download.php",
            "result_query_param": "token",
            "result_base_url": "https://a.com",
        }
        url = parse_download_response(params, 200, {"data": {"token": "abc"}}, None)
        assert url is not None
        assert "token=abc" in url

    def test_parse_download_response_failures(self) -> None:
        params = {"result": "data.url"}
        assert parse_download_response(params, None, {}, None) is None
        assert parse_download_response(params, 500, {}, None) is None
        assert parse_download_response(params, 200, {"data": {}}, None) is None
        assert parse_download_response(params, 200, {"data": {"url": 1}}, None) is None
        # 没声明 result 字段时直接返回正文
        assert parse_download_response({}, 200, {}, "body") == "body"

    def test_parse_download_response_success_flag(self) -> None:
        params = {"result": "data.url", "success": "success"}
        assert parse_download_response(
            params, 200, {"success": True, "data": {"url": "https://a.com/t"}}, None
        ) == "https://a.com/t"
        assert parse_download_response(params, 200, {"success": False}, None) is None

    def test_encrypt_site_payload(self) -> None:
        # 密钥运行时随机生成——测试里也不该出现凭据字面量
        key = os.urandom(16)
        payload = encrypt_site_payload("hello", key)
        decoded = json.loads(base64.b64decode(payload).decode())
        assert set(decoded) == {"iv", "value", "mac", "tag"}
        assert decoded["tag"] == ""
        assert decoded["iv"]
        assert decoded["value"]


@pytest.mark.parametrize(
    "value",
    ["1 KB", "1MB", "1.5 GB", "1024", "1,024", "abc", ""],
)
def test_parse_size_never_raises(value: str) -> None:
    """容量解析对任何输入都不抛异常（解析器在站点页面里跑）。"""
    assert size_tools.parse_size(value) >= 0
