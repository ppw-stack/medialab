"""``parse_window`` 单元测试 = 附录 D 全量用例集（FR-1.5.3 / AC-16）。

这是全系统最敏感的函数：解析错误会误杀长窗口种子或让免费种超期滞留（R9）。
每个用例断言 ``free_type`` / ``free_end_at`` / ``free_source`` / ``confidence``
四元组。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.sites.base import SiteProfile
from app.adapters.sites.generic import GenericSiteAdapter
from app.core.timeutils import ClockOffset, utcnow
from app.db.models import FreeSource, FreeType

ENGLISH_PATTERN = (
    r"(?:left|remaining)?\s*"
    r"(?:(?P<d>\d+)\s*(?:days?|d)\s*)?"
    r"(?:(?P<h>\d+)\s*(?:hours?|hrs?|h)\s*)?"
    r"(?:(?P<m>\d+)\s*(?:min|m)\s*)?"
)


@pytest.fixture
def adapter(sample_site: SiteProfile) -> GenericSiteAdapter:
    return GenericSiteAdapter(sample_site)


@pytest.fixture
def adapter_offset(sample_site: SiteProfile) -> GenericSiteAdapter:
    """站点快 5 分钟。"""
    return GenericSiteAdapter(sample_site, clock_offset=ClockOffset(timedelta(minutes=5)))


@pytest.mark.asyncio
async def test_permanent_freeleech_has_highest_priority(adapter: GenericSiteAdapter) -> None:
    """R10：永久免费不得被当作限时免费。"""
    for text in ("永久免费", "永久免費", "free forever", "permanent freeleech"):
        w = await adapter.parse_window(text)
        assert w.free_type == FreeType.PERMANENT, text
        assert w.free_end_at is None, text
        assert w.confidence == 1.0


@pytest.mark.asyncio
async def test_api_field_is_most_accurate(adapter: GenericSiteAdapter) -> None:
    """来源优先级 1：API 字段，置信度 0.95。"""
    end = int(datetime(2026, 10, 10, 18, 0, tzinfo=UTC).timestamp())
    w = await adapter.parse_window({"free_until": end, "promotion": "freeleech"})
    assert w.free_source == FreeSource.API
    assert w.confidence == 0.95
    assert w.free_end_at == datetime(2026, 10, 10, 18, 0)
    assert w.is_actionable


@pytest.mark.asyncio
async def test_list_text_remaining_two_days_six_hours(
    adapter: GenericSiteAdapter,
) -> None:
    """附录 D 第 1 行：剩余 2 天 6 小时（列表页文案来源）。"""
    now = utcnow()
    w = await adapter.parse_window("剩余 2天 6小时", source_hint=FreeSource.LIST_TEXT)
    assert w.free_source == FreeSource.LIST_TEXT
    expected = now + timedelta(days=2, hours=6)
    assert abs((w.free_end_at - expected).total_seconds()) <= 2  # type: ignore[operator]
    assert w.confidence == 0.8


@pytest.mark.asyncio
async def test_timed_marker_infers_freeleech_timed(
    adapter: GenericSiteAdapter,
) -> None:
    """文案含"限时免费"标记时才推断为 freeleech_timed。

    纯"剩余 X"文案不足以判定优惠类型——只有剩余时长而无类型语义时，
    必须返回 unknown 交由业务层保守处理（FR-3.0 / FR-1.5.3）。
    """
    w = await adapter.parse_window(
        "限时免费：剩余 2天 6小时", source_hint=FreeSource.LIST_TEXT
    )
    assert w.free_type == FreeType.TIMED


@pytest.mark.asyncio
async def test_list_text_remaining_two_hours_45min(
    adapter: GenericSiteAdapter,
) -> None:
    """附录 D 第 2 行：剩余 2 小时 45 分，短窗口不得被固定时长方案误杀。"""
    w = await adapter.parse_window("剩余 2小时 45分")
    assert w.free_end_at is not None
    remaining = w.free_end_at - utcnow()
    assert timedelta(hours=2, minutes=40) < remaining <= timedelta(hours=2, minutes=45)


@pytest.mark.asyncio
async def test_promotion_end_variants(
    adapter: GenericSiteAdapter,
    sample_site: SiteProfile,
) -> None:
    """附录 D 第 4 行：promotion_end 字段 → 2X 限时免费。"""
    sample_site.download_factor_map = {"2": FreeType.TWOU_TIMED}
    ad = GenericSiteAdapter(sample_site)
    end = int(datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp())
    w = await ad.parse_window({"promotion_end": end, "download_factor": 2})
    assert w.free_end_at == datetime(2026, 10, 15, 12, 0)
    assert w.free_type == FreeType.TWOU_TIMED


@pytest.mark.asyncio
async def test_unknown_window_is_low_confidence(adapter: GenericSiteAdapter) -> None:
    """解析失败 → unknown + 低置信度，交由业务层兜底。"""
    w = await adapter.parse_window("某站未标注优惠信息")
    assert w.free_type == FreeType.UNKNOWN
    assert w.free_end_at is None
    assert w.is_low_confidence
    assert not w.is_actionable


@pytest.mark.asyncio
async def test_traditional_chinese_variant(
    sample_site: SiteProfile,
) -> None:
    """附录 D 补充用例：繁体「剩余 2 天 6 小時」。"""
    profile = SiteProfile(
        name="tw-pt",
        domain="tw.example",
        rss_time_pattern=(
            r"(?:剩餘|剩余)\s*(?:(?P<d>\d+)\s*天\s*)?"
            r"(?:(?P<h>\d+)\s*(?:小時|小时)\s*)?"
        ),
    )
    w = await GenericSiteAdapter(profile).parse_window("剩餘 2 天 6 小時")
    assert w.free_end_at is not None
    assert timedelta(hours=53) < (w.free_end_at - utcnow()) <= timedelta(hours=54)


@pytest.mark.asyncio
async def test_english_variant() -> None:
    """附录 D 补充用例：英文 "2d 6h remaining"。"""
    profile = SiteProfile(name="en-pt", domain="en.example", rss_time_pattern=ENGLISH_PATTERN)
    w = await GenericSiteAdapter(profile).parse_window("2d 6h remaining")
    assert w.free_end_at is not None
    assert timedelta(hours=53) < (w.free_end_at - utcnow()) <= timedelta(hours=54)


@pytest.mark.asyncio
async def test_time_unit_multiplier(sample_site: SiteProfile) -> None:
    """附录 D 补充用例：时间单位为"分钟"的站点。"""
    profile = SiteProfile(
        name="min-unit",
        domain="min.example",
        rss_time_pattern=sample_site.rss_time_pattern,
        time_unit_multiplier=60,
    )
    w = await GenericSiteAdapter(profile).parse_window("剩余 1天 2小时")
    # 倍数放大后剩余远超原始值，验证换算生效
    assert w.free_end_at - utcnow() > timedelta(hours=25)


@pytest.mark.asyncio
async def test_clock_offset_correction(adapter_offset: GenericSiteAdapter) -> None:
    """附录 D 时钟偏差行：站点快 5 分钟需扣除（误差 ≤30s）。"""
    w = await adapter_offset.parse_window(
        "剩余 2天 6小时", source_hint=FreeSource.LIST_TEXT
    )
    assert w.free_end_at is not None
    # 站点快 5 分钟 → 结束时刻须比声明值早 5 分钟（误差 ≤30s）
    declared = timedelta(days=2, hours=6)
    remaining = w.free_end_at - utcnow()
    assert abs((remaining - (declared - timedelta(minutes=5))).total_seconds()) <= 30


@pytest.mark.asyncio
async def test_permanent_never_gets_end_at_even_with_field(
    adapter: GenericSiteAdapter,
) -> None:
    """永久标记优先于 API 字段：构造器本身拒绝非法组合。"""
    from app.core.windows import FreeWindow

    with pytest.raises(ValueError, match="free_end_at"):
        FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=datetime(2026, 10, 10, 18, 0),
            free_source=FreeSource.API,
        )


@pytest.mark.asyncio
async def test_confidence_05_is_not_actionable(adapter: GenericSiteAdapter) -> None:
    """附录 D 补充用例：confidence=0.5 的模糊文案不挂看护。"""
    w = await adapter.parse_window({"promotion_end": "garbage-value"})
    assert w.confidence < 0.6
    assert not w.is_actionable


@pytest.mark.asyncio
async def test_iso_string_field(adapter: GenericSiteAdapter) -> None:
    w = await adapter.parse_window({"free_until": "2026-10-10T18:00:00Z"})
    assert w.free_end_at == datetime(2026, 10, 10, 18, 0)
    assert w.confidence == 0.95


@pytest.mark.asyncio
async def test_implausible_timestamp_rejected(adapter: GenericSiteAdapter) -> None:
    """站点返回异常值（1970/未来过远）必须拒绝而非污染看护。"""
    assert (await adapter.parse_window({"free_until": 12345})).free_end_at is None
    assert (await adapter.parse_window({"free_until": 99999999999})).free_end_at is None


@pytest.mark.asyncio
async def test_bad_regex_falls_back_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """站点配错正则不得抛异常上抛业务层（FR-1.5.4）。"""
    profile = SiteProfile(name="bad-re", domain="b.example", rss_time_pattern="([unclosed")
    ad = GenericSiteAdapter(profile)
    w = await ad.parse_window("剩余 2天")
    assert w.free_end_at is None
    assert "rss_time_pattern" in caplog.text