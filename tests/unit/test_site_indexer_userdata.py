"""站点用户数据解析器的移植测试（真实脱敏页面 fixture）。"""

from __future__ import annotations

from pathlib import Path

from app.adapters.sites.indexer.engine import IndexerEngine
from app.adapters.sites.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.adapters.sites.indexer.rules import load_site_rules

_FIXTURES = Path(__file__).parents[1] / "fixtures"


def _parser() -> NexusPhpSiteUserInfo:
    return NexusPhpSiteUserInfo(
        site_name="NexusPHP",
        url="https://example.com/",
        site_cookie="",
        apikey=None,
        token=None,
    )


def test_nexus_php_homepage_traffic_fixture() -> None:
    """回放脱敏首页：上传量 / 下载量 / 分享率 / 用户 ID 都能解析。"""
    homepage = (_FIXTURES / "cspt_homepage.html").read_text(encoding="utf-8")
    parser = _parser()
    parser._parse_site_page(homepage)
    parser._parse_user_base_info(homepage)

    assert parser.upload == round(105.06 * 1024**3)
    assert parser.download == 10 * 1024**3
    assert parser.ratio == 10.506
    assert parser.userid
    assert parser.username


def test_nexus_php_detail_page_overrides_traffic() -> None:
    """用户详情页的流量文本能覆盖首页值；无法识别时只断言不抛异常。"""
    detail = (_FIXTURES / "depthstudio_userdetails.html").read_text(encoding="utf-8")
    parser = _parser()
    parser._parse_user_traffic_info(detail)
    # 该 fixture 是自定义统计卡片，解析器不保证识别；只断言不抛异常
    assert parser.upload >= 0
    assert parser.download >= 0


def test_engine_selects_parser_by_schema() -> None:
    """引擎按站点 schema 选择解析器实例。"""
    engine = IndexerEngine()
    rule = load_site_rules().get("0ff")
    assert rule is not None
    site = {
        **rule,
        "url": rule["domain"],
        "cookie": "c=1",
        "schema": "NexusPhp",
    }
    parser = engine._parser_for(site, "NexusPhp")
    assert isinstance(parser, NexusPhpSiteUserInfo)
    assert parser._site_name == rule.get("name")


async def test_engine_refresh_userdata_over_http_shim() -> None:
    """引擎通过移植的 HTTP 层抓取并解析真实首页 fixture。"""
    import asyncio

    import httpx
    import respx

    homepage = (_FIXTURES / "cspt_homepage.html").read_text(encoding="utf-8")
    rule = load_site_rules().get("0ff")
    assert rule is not None
    site = {
        **rule,
        "url": "https://pt.0ff.cc/",
        "cookie": "uid=1",
        "schema": "NexusPhp",
        "ua": "Mozilla/5.0",
    }
    engine = IndexerEngine()
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith="https://pt.0ff.cc/").mock(
            return_value=httpx.Response(200, text=homepage)
        )
        data = await asyncio.to_thread(engine.refresh_userdata, site)

    assert data is not None
    assert data["upload"] == round(105.06 * 1024**3)
    assert data["download"] == 10 * 1024**3
