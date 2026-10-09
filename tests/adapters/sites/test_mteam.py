"""m-team 适配器测试（FR-1.5 站点特化 + MoviePilot 风格的 x-api-key API）。

覆盖：
- ``MteamAdapter._classify_from_status`` discount / discountEndTime / mallSingleFree / promotionRule
- ``MteamAdapter._fetch_via_api`` JSON 响应解析
- ``MteamAdapter._parse_rss`` RSS 兜底
- ``MteamAdapter.torrent_detail`` 调 genDlToken
- ``MteamAdapter.capabilities`` 能力声明
- 注册中心 resolve / 拒绝未知
- 联调：``@pytest.mark.integration`` 真实拉取（需用户提供 apikey + sign/t）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.mteam import MteamAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType


# ---- m-team search API 响应快照（MoviePilot 风格） ----

MTEAM_SEARCH_RESPONSE: dict[str, Any] = {
    "code": 0,
    "message": "success",
    "data": {
        "pageNumber": 1,
        "totalPages": 1,
        "total": 3,
        "data": [
            # 永久免费（discount=100）
            {
                "id": "100001",
                "name": "Ubuntu.24.04 amd64",
                "smallDescr": "官方长期支持",
                "category": "423",
                "size": 5_368_709_120,
                "createdDate": "2026-10-09T10:00:00Z",
                "imdb": None,
                "labelsNew": ["1"],
                "status": {
                    "seeders": 10,
                    "leechers": 2,
                    "timesCompleted": 5,
                    "discount": 100,
                    "discountEndTime": None,
                    "mallSingleFree": None,
                },
                "promotionRule": None,
            },
            # 限时免费（discountEndTime 有值）
            {
                "id": "100002",
                "name": "Debian.12.iso",
                "smallDescr": "限时免费",
                "category": "423",
                "size": 1_073_741_824,
                "createdDate": "2026-10-09T11:00:00Z",
                "imdb": None,
                "labelsNew": [],
                "status": {
                    "seeders": 5,
                    "leechers": 1,
                    "timesCompleted": 3,
                    "discount": 0,
                    "discountEndTime": 1793000000,
                    "mallSingleFree": None,
                },
                "promotionRule": None,
            },
            # 单种特别免费
            {
                "id": "100003",
                "name": "Free.Movie.2026",
                "smallDescr": "站内单种免费",
                "category": "419",
                "size": 2_147_483_648,
                "createdDate": "2026-10-09T12:00:00Z",
                "imdb": "tt1234567",
                "labelsNew": ["1", "4"],
                "status": {
                    "seeders": 0,
                    "leechers": 0,
                    "timesCompleted": 0,
                    "discount": 0,
                    "discountEndTime": None,
                    "mallSingleFree": {
                        "status": "ONGOING",
                        "endDate": "2026-12-31T23:59:59Z",
                    },
                },
                "promotionRule": None,
            },
        ],
    },
}


MTEAM_DOWNLOAD_RESPONSE: dict[str, Any] = {
    "code": 0,
    "message": "success",
    "data": {
        "url": "https://rss.m-team.cc/api/rss/dlv2?sign=abc123&t=1791553775&tid=100001&uid=291372"
    },
}


@pytest.fixture
def adapter() -> MteamAdapter:
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    return MteamAdapter(profile, apikey="test-api-key-xxx", clock_offset=ClockOffset.zero())


# ---- _classify_from_status 单元测试 ----


def test_classify_discount_100_permanent(adapter: MteamAdapter) -> None:
    ft, end = adapter._classify_from_status({"discount": 100})
    assert ft == FreeType.PERMANENT
    assert end is None


def test_classify_discount_50_neutral(adapter: MteamAdapter) -> None:
    """m-team 上传乘数：-50 = 中性种（2x upload / 0.5x download）。"""
    ft, end = adapter._classify_from_status({"discount": -50})
    assert ft == FreeType.NEUTRAL
    assert end is None


def test_classify_discount_string_percent_50_neutral(adapter: MteamAdapter) -> None:
    """m-team 实际返回字符串 'PERCENT_50' = 50% 折扣（中性种）。"""
    ft, end = adapter._classify_from_status({"discount": "PERCENT_50"})
    assert ft == FreeType.NEUTRAL
    assert end is None


def test_classify_discount_string_free_permanent(adapter: MteamAdapter) -> None:
    """m-team 实际返回字符串 'FREE' = 永久免费。"""
    ft, end = adapter._classify_from_status({"discount": "FREE"})
    assert ft == FreeType.PERMANENT
    assert end is None


def test_classify_discount_string_normal_unknown(adapter: MteamAdapter) -> None:
    """m-team 字符串 'NORMAL' = 无折扣。"""
    ft, end = adapter._classify_from_status({"discount": "NORMAL"})
    assert ft == FreeType.UNKNOWN
    assert end is None


def test_classify_discount_string_percent_100_permanent(adapter: MteamAdapter) -> None:
    """m-team 字符串 'PERCENT_100' = 100% 折扣 = 永久免费。"""
    ft, end = adapter._classify_from_status({"discount": "PERCENT_100"})
    assert ft == FreeType.PERMANENT
    assert end is None


def test_classify_discount_100_with_deadline_permanent(adapter: MteamAdapter) -> None:
    """discount=100 永远视作永久，截止时刻被忽略。"""
    ft, end = adapter._classify_from_status({"discount": 100, "discountEndTime": 1793000000})
    assert ft == FreeType.PERMANENT
    assert end is None


def test_classify_discount_0_with_deadline_timed(adapter: MteamAdapter) -> None:
    ft, end = adapter._classify_from_status({"discount": 0, "discountEndTime": 1793000000})
    assert ft == FreeType.TIMED
    assert isinstance(end, type(adapter._clock_offset.remaining_to_absolute(__import__("datetime").timedelta(seconds=0))))


def test_classify_no_fields_unknown(adapter: MteamAdapter) -> None:
    assert adapter._classify_from_status({}) == (FreeType.UNKNOWN, None)


def test_classify_mall_single_free_timed(adapter: MteamAdapter) -> None:
    """单种特别免费（ONGOING）→ TIMED + endDate。"""
    ft, end = adapter._classify_from_status({
        "discount": 0,
        "discountEndTime": None,
        "mallSingleFree": {"status": "ONGOING", "endDate": "2026-12-31T23:59:59Z"},
    })
    assert ft == FreeType.TIMED
    assert end is not None


def test_classify_mall_single_free_overrides_discount(adapter: MteamAdapter) -> None:
    """mallSingleFree 优先于 discount 字段。"""
    ft, end = adapter._classify_from_status({
        "discount": 0,  # 没免费
        "mallSingleFree": {"status": "ONGOING", "endDate": "2026-12-31T23:59:59Z"},
    })
    assert ft == FreeType.TIMED


def test_classify_promotion_rule_free(adapter: MteamAdapter) -> None:
    """全站促销：discount='FREE' → 永久。"""
    ft, end = adapter._classify_from_status({
        "discount": 0,
        "promotionRule": {"discount": "FREE", "endTime": 1793000000},
    })
    assert ft == FreeType.PERMANENT


def test_classify_promotion_rule_normal_with_endtime(adapter: MteamAdapter) -> None:
    ft, end = adapter._classify_from_status({
        "promotionRule": {"discount": "NORMAL", "endTime": 1793000000},
    })
    assert ft == FreeType.TIMED


# ---- _fetch_via_api 解析测试（用 httpx mock） ----


async def test_fetch_via_api_parses_three_items(adapter: MteamAdapter) -> None:
    """从 mock 响应解析 3 条种子。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = MTEAM_SEARCH_RESPONSE
    adapter._client.post = AsyncMock(return_value=mock_resp)

    items = await adapter._fetch_via_api()
    assert len(items) == 3
    assert items[0].title == "Ubuntu.24.04 amd64"
    assert items[0].size == 5_368_709_120
    assert items[0].category == "423"
    assert items[0].info_hash is None  # m-team search API 不返回 infohash
    assert items[0].link == "https://kp.m-team.cc/detail/100001"


async def test_fetch_via_api_returns_empty_on_401(adapter: MteamAdapter) -> None:
    """401 = 鉴权失败（apikey 无效），归类为正常错误，返回空列表（不熔断）。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    adapter._client.post = AsyncMock(return_value=mock_resp)
    items = await adapter._fetch_via_api()
    assert items == []


async def test_fetch_via_api_returns_empty_on_500(adapter: MteamAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    adapter._client.post = AsyncMock(return_value=mock_resp)
    items = await adapter._fetch_via_api()
    assert items == []


async def test_fetch_via_api_returns_empty_on_non_zero_code(adapter: MteamAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"code": 401, "message": "鉴权已过期", "data": None}
    adapter._client.post = AsyncMock(return_value=mock_resp)
    items = await adapter._fetch_via_api()
    assert items == []


# ---- torrent_detail 调 genDlToken ----


async def test_torrent_detail_calls_genDlToken(adapter: MteamAdapter) -> None:
    """torrent_detail 应 POST /api/torrent/genDlToken，body={id}。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = MTEAM_DOWNLOAD_RESPONSE
    adapter._client.post = AsyncMock(return_value=mock_resp)

    result = await adapter.torrent_detail("100001")
    assert result["id"] == "100001"
    assert "download_url" in result
    assert "rss.m-team.cc" in result["download_url"]


async def test_torrent_detail_returns_error_on_401(adapter: MteamAdapter) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    adapter._client.post = AsyncMock(return_value=mock_resp)
    result = await adapter.torrent_detail("100001")
    assert result["id"] == "100001"
    assert "鉴权失败" in result.get("_error", "")


# ---- RSS 兜底（无 apikey 时） ----


def test_adapter_no_apikey_uses_rss_fallback() -> None:
    """无 apikey → fetch_feed 走 RSS 解析（向后兼容）。"""
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    a = MteamAdapter(profile, apikey=None, clock_offset=ClockOffset.zero())
    assert a._apikey is None
    # 不调 API，只验证 _fetch_via_rss 仍可用
    sample_rss = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><guid isPermaLink="false">1</guid>
<title>Old RSS Item</title>
<link>https://kp.m-team.cc/detail/1</link>
<description>old style</description>
<enclosure length="100" type="application/x-bittorrent" url="https://rss.m-team.cc/dl/1"/>
<category>TV Series/HD</category>
</item></channel></rss>"""
    items = MteamAdapter._parse_rss(sample_rss.decode())
    assert len(items) == 1
    assert items[0].info_hash is None
    assert items[0].torrent_url == "https://rss.m-team.cc/dl/1"


# ---- parse_window 字典路径（基于 MTEAM_SEARCH_RESPONSE 样本） ----


async def test_parse_window_permanent_from_dict(adapter: MteamAdapter) -> None:
    item = MTEAM_SEARCH_RESPONSE["data"]["data"][0]  # discount=100
    win = await adapter.parse_window(item)
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 1.0
    assert win.free_source == FreeSource.API


async def test_parse_window_timed_from_dict(adapter: MteamAdapter) -> None:
    item = MTEAM_SEARCH_RESPONSE["data"]["data"][1]  # discountEndTime
    win = await adapter.parse_window(item)
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95
    assert win.free_source == FreeSource.API


async def test_parse_window_mall_single_free_from_dict(adapter: MteamAdapter) -> None:
    item = MTEAM_SEARCH_RESPONSE["data"]["data"][2]  # mallSingleFree
    win = await adapter.parse_window(item)
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.confidence == 0.95


async def test_parse_window_unknown_from_empty_dict(adapter: MteamAdapter) -> None:
    win = await adapter.parse_window({})
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 能力声明 ----


def test_capabilities_includes_rss_detail_clock(adapter: MteamAdapter) -> None:
    from app.adapters.sites.base import SiteFeature
    caps = adapter.capabilities()
    assert SiteFeature.RSS in caps
    assert SiteFeature.DETAIL_API in caps  # 因为有 genDlToken 能力
    assert SiteFeature.CLOCK_API in caps


# ---- 注册中心 ----


def test_registry_resolves_mteam():
    from app.adapters.sites import resolve
    cls = resolve("mteam")
    assert cls is MteamAdapter


def test_registry_rejects_unknown():
    from app.adapters.sites import resolve
    with pytest.raises(ValueError, match="未注册"):
        resolve("nonexistent")


# ---- 联调：真实 m-team search API（需用户提供 apikey + sign/t） ----

#: 联调占位符——用户提供有效 apikey 后替换
INTEGRATION_APIKEY = "ebabd78f-9c78-463b-a91f-690a70091049"
INTEGRATION_DOMAIN = "m-team.io"  # 联调用 .io 域名（当前环境 .cc DNS 不可达）


@pytest.mark.integration
async def test_live_mteam_search_with_apikey() -> None:
    """真实拉取 m-team search API，验证识别率。

    预期：100 条中应出现 PERMANENT / TIMED / NEUTRAL / TWOU_TIMED 至少一种。
    提供真实 apikey + 可用 sign/t 后取消 skip。
    """
    if INTEGRATION_APIKEY == "REPLACE_ME":
        pytest.skip("请在 tests/adapters/sites/test_mteam.py 顶部设置 INTEGRATION_APIKEY")
    profile = SiteProfile(name="m-team", domain=INTEGRATION_DOMAIN)
    a = MteamAdapter(profile, apikey=INTEGRATION_APIKEY, clock_offset=ClockOffset.zero())
    try:
        items = await a._fetch_via_api(page_size=100)
        assert len(items) > 0
        from collections import Counter
        type_counter: Counter[str] = Counter()
        for it in items:
            # free_payload 是适配层已填充的 status dict
            win = await a.parse_window(it.free_payload or {"id": it.link.rsplit("/", 1)[-1]})
            # FreeType 是 StrEnum，win.free_type 已是 str
            type_counter[str(win.free_type)] += 1
        # 至少识别出 1 条免费种
        free_count = sum(c for ft, c in type_counter.items() if str(ft) != str(FreeType.UNKNOWN))
        assert free_count > 0, f"未识别任何免费种，分布={dict(type_counter)}"
        print(f"\n联调统计：{dict(type_counter)}")
    finally:
        await a.close()
