"""m-team 适配器测试（FR-1.5 站点特化）。

覆盖：
- ``MteamAdapter.parse_feed`` 解析 m-team JSON 响应
- ``MteamAdapter._classify_free`` 各 discount/free/free_deadline 组合
- ``MteamAdapter.capabilities`` 能力声明
- 联调：``@pytest.mark.integration`` 真实拉取 m-team JSON API（需要未过期 sign/t）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.mteam import MteamAdapter
from app.core.timeutils import ClockOffset
from app.db.models import FreeSource, FreeType


# ---- JSON 响应快照（联调无法跑时使用） ----

#: m-team JSON 响应样例：3 条混合（永久、限时、未知）
MTEAM_JSON_SAMPLE = {
    "code": 0,
    "message": "success",
    "data": [
        {
            "id": "100001",
            "title": "Ubuntu.24.04.Desktop amd64",
            "small_descr": "官方长期支持版本",
            "size": 5_368_709_120,
            "category": "Linux",
            "link": "https://kp.m-team.cc/details.php?id=100001",
            "download": "https://kp.m-team.cc/download.php?id=100001",
            "infohash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "discount": 100,  # 永久免费
            "free": True,
        },
        {
            "id": "100002",
            "title": "Debian.12.iso",
            "small_descr": "限时免费",
            "size": 1_073_741_824,
            "category": "Linux",
            "link": "https://kp.m-team.cc/details.php?id=100002",
            "infohash": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
            "discount": 0,
            "free_deadline": "2027-01-15T10:00:00Z",  # 限时
        },
        {
            "id": "100003",
            "title": "Old.Movie.1980.avi",
            "small_descr": "普通种子",
            "size": 700_000_000,
            "category": "Movies",
            "link": "https://kp.m-team.cc/details.php?id=100003",
            "infohash": "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
            # 无 discount / free 字段 → 未知
        },
    ],
}


@pytest.fixture
def adapter() -> MteamAdapter:
    profile = SiteProfile(name="m-team", domain="kp.m-team.cc")
    return MteamAdapter(profile, clock_offset=ClockOffset.zero())


# ---- parse_feed 单元测试 ----


def test_parse_feed_decodes_mteam_json(adapter: MteamAdapter) -> None:
    items = adapter.parse_feed(json.dumps(MTEAM_JSON_SAMPLE))
    assert len(items) == 3
    titles = [it.title for it in items]
    assert "Ubuntu.24.04.Desktop amd64" in titles


def test_parse_feed_extracts_infohash(adapter: MteamAdapter) -> None:
    items = adapter.parse_feed(json.dumps(MTEAM_JSON_SAMPLE))
    hashes = {it.title: it.info_hash for it in items}
    assert hashes["Ubuntu.24.04.Desktop amd64"] is not None
    assert len(hashes["Ubuntu.24.04.Desktop amd64"]) == 40


def test_parse_feed_handles_invalid_json(adapter: MteamAdapter) -> None:
    assert adapter.parse_feed("<not-json>") == []
    assert adapter.parse_feed("") == []


def test_parse_feed_rejects_non_zero_code(adapter: MteamAdapter) -> None:
    bad = {"code": 401, "message": "unauthorized", "data": []}
    assert adapter.parse_feed(json.dumps(bad)) == []


def test_parse_feed_ignores_non_dict_items(adapter: MteamAdapter) -> None:
    payload = {"code": 0, "data": [{"title": "ok", "id": "1"}, "not-dict", None, 42]}
    items = adapter.parse_feed(json.dumps(payload))
    assert len(items) == 1


def test_parse_feed_caps_items(adapter: MteamAdapter) -> None:
    payload = {"code": 0, "data": [{"id": str(i), "title": f"t{i}"} for i in range(1000)]}
    items = adapter.parse_feed(json.dumps(payload))
    # _MAX_ITEMS_PER_PULL = 500
    assert len(items) == 500


# ---- _classify_free 单元测试（字典路径） ----


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
    win = asyncio.run(adapter.parse_window(MTEAM_JSON_SAMPLE["data"][0]))
    assert win.free_type == FreeType.PERMANENT
    assert win.free_end_at is None
    assert win.confidence == 1.0
    assert win.free_source == FreeSource.API


def test_parse_window_timed_from_dict(adapter: MteamAdapter) -> None:
    import asyncio
    win = asyncio.run(adapter.parse_window(MTEAM_JSON_SAMPLE["data"][1]))
    assert win.free_type == FreeType.TIMED
    assert win.free_end_at is not None
    assert win.free_source == FreeSource.API


def test_parse_window_unknown_from_dict(adapter: MteamAdapter) -> None:
    import asyncio
    win = asyncio.run(adapter.parse_window(MTEAM_JSON_SAMPLE["data"][2]))
    assert win.free_type == FreeType.UNKNOWN
    assert win.free_end_at is None


# ---- 能力声明 ----


def test_capabilities_includes_rss_detail_clock(adapter: MteamAdapter) -> None:
    from app.adapters.sites.base import SiteFeature
    caps = adapter.capabilities()
    assert SiteFeature.RSS in caps
    assert SiteFeature.DETAIL_API in caps
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


# ---- 联调：真实 m-team JSON API（需用户提供未过期 sign/t） ----

#: 联调 URL（运行前请替换为有效的 sign/t，否则会 401/403）
MTEAM_LIVE_URL = (
    "https://rss.m-team.cc/api/rss/fetch"
    "?pageSize=20&sign=REPLACE_ME&t=REPLACE_ME&tkeys=ttitle&uid=REPLACE_ME"
)


@pytest.mark.integration
def test_live_mteam_pull_classifies_free_types(adapter: MteamAdapter) -> None:
    """真实拉取 m-team RSS 并统计 free_type 分布。

    期望：至少出现 ``freeleech_timed`` 或 ``freeleech_permanent`` 之一，
    否则适配器可能没读到 free 字段。
    """
    import asyncio
    items = asyncio.run(adapter.fetch_feed(MTEAM_LIVE_URL))
    if not items:
        pytest.skip("联调 URL 无效或无数据；请更新 MTEAM_LIVE_URL")
    import asyncio as _aio
    classified = _aio.run(_classify_all(adapter, items))
    types = {ft for ft, _ in classified}
    # 至少识别到一种（说明字段映射生效）
    assert types & {FreeType.PERMANENT, FreeType.TIMED, FreeType.NEUTRAL, FreeType.TWOU_TIMED}, (
        f"未识别任何免费类型，分布={types}"
    )


async def _classify_all(adapter: MteamAdapter, items):
    return [await adapter.parse_window({k: v for k, v in vars(it).items() if not k.startswith('_')}) for it in items]
