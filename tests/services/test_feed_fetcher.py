"""RSS 拉取器测试（FR-2.2）。

覆盖：频率下限强制、熔断状态、退避重试、空结果、条目计数。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from app.adapters.sites.base import FeedItem
from app.core.timeutils import utcnow
from app.db.models import Feed
from app.services.feed_fetcher import (
    BREAKER_SECONDS,
    MAX_RETRIES,
    MIN_INTERVAL_MINUTES,
    BreakerTripped,
    FeedFetcher,
    PullStatus,
    enforce_interval,
)


class _StubAdapter:
    """可编程的假适配器。"""

    def __init__(self, *, items=None, fail_times=0, trip=False):
        self.items = items or []
        self.fail_times = fail_times
        self.trip = trip
        self.calls = 0

    async def fetch_feed(self, url: str) -> list[FeedItem]:
        self.calls += 1
        if self.trip:
            raise BreakerTripped("HTTP 429")
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        return list(self.items)


def feed_row(**kw):
    defaults = {
        "id": 1,
        "site_id": 1,
        "kind": "site_rss",
        "interval_minutes": 60,
        "last_pulled_at": None,
        "last_status": None,
        "last_item_count": 0,
        "last_duration_ms": 0,
        "breaker_until": None,
        "enabled": True,
    }
    defaults.update(kw)
    return Feed(**defaults)


# ------------------------------------------------------------ 频率下限
@pytest.mark.parametrize(
    ("configured", "expected"),
    [(1, 15), (10, 15), (15, 15), (30, 30), (60, 60), (0, 15), (-5, 15)],
)
def test_enforce_interval_floor(configured: int, expected: int) -> None:
    """FR-2.2 强制下限 15 分钟，配置再小也被夹住（R2 账号保护）。"""
    assert enforce_interval(configured) == expected


def test_should_pull_when_never_pulled() -> None:
    fetcher = FeedFetcher("pt")
    ok, why = fetcher.should_pull(feed_row(), interval_minutes=60)
    assert ok is True
    assert why == ""


def test_should_not_pull_before_interval() -> None:
    fetcher = FeedFetcher("pt")
    feed = feed_row(last_pulled_at=utcnow() - timedelta(minutes=5))
    ok, why = fetcher.should_pull(feed, interval_minutes=60)
    assert ok is False
    assert "未到间隔" in why


def test_should_pull_after_interval() -> None:
    fetcher = FeedFetcher("pt")
    feed = feed_row(last_pulled_at=utcnow() - timedelta(minutes=61))
    assert fetcher.should_pull(feed, interval_minutes=60)[0] is True


def test_interval_floor_applies_even_if_configured_low() -> None:
    """配置 1 分钟时，实际等待至少 15 分钟。"""
    fetcher = FeedFetcher("pt")
    feed = feed_row(last_pulled_at=utcnow() - timedelta(minutes=5))
    ok, why = fetcher.should_pull(feed, interval_minutes=1)
    assert ok is False
    assert str(MIN_INTERVAL_MINUTES) in why


# ------------------------------------------------------------ 熔断
def test_breaker_blocks_pull() -> None:
    fetcher = FeedFetcher("pt")
    feed = feed_row(breaker_until=utcnow() + timedelta(seconds=600))
    ok, why = fetcher.should_pull(feed, interval_minutes=60)
    assert ok is False
    assert "熔断" in why


def test_expired_breaker_allows_pull() -> None:
    fetcher = FeedFetcher("pt")
    feed = feed_row(
        breaker_until=utcnow() - timedelta(seconds=1),
        last_pulled_at=utcnow() - timedelta(hours=2),
    )
    assert fetcher.should_pull(feed, interval_minutes=60)[0] is True


def test_breaker_takes_priority_over_interval() -> None:
    """熔断优先：即使间隔已过也不能拉（避免重启即绕过熔断）。"""
    fetcher = FeedFetcher("pt")
    feed = feed_row(breaker_until=utcnow() + timedelta(seconds=60))
    ok, why = fetcher.should_pull(feed, interval_minutes=15)
    assert ok is False
    assert "熔断" in why


def test_breaker_duration_is_one_hour() -> None:
    assert BREAKER_SECONDS == 3600


async def test_fetch_tripped_breaker() -> None:
    fetcher = FeedFetcher("pt")
    adapter = _StubAdapter(trip=True)
    result = await fetcher.fetch(adapter, "https://x/rss")
    assert result.status is PullStatus.BREAKER_TRIPPED
    assert result.ok is False
    assert adapter.calls == 1, "熔断后不得重试"


# ------------------------------------------------------------ 重试
async def test_retries_then_succeeds() -> None:
    fetcher = FeedFetcher("pt", jitter=False)
    adapter = _StubAdapter(fail_times=2, items=[FeedItem(title="a", link="l")])
    result = await fetcher.fetch(adapter, "https://x/rss")
    assert result.status is PullStatus.OK
    assert adapter.calls == 3


async def test_gives_up_after_max_retries() -> None:
    fetcher = FeedFetcher("pt", jitter=False)
    adapter = _StubAdapter(fail_times=99)
    result = await fetcher.fetch(adapter, "https://x/rss")
    assert result.status is PullStatus.HTTP_ERROR
    assert adapter.calls == MAX_RETRIES
    assert result.error


async def test_no_retry_when_disabled() -> None:
    fetcher = FeedFetcher("pt", jitter=False)
    adapter = _StubAdapter(fail_times=99)
    await fetcher.fetch(adapter, "https://x/rss", retry=False)
    assert adapter.calls == 1


def test_backoff_is_exponential_and_bounded() -> None:
    fetcher = FeedFetcher("pt", jitter=False)
    waits = [fetcher.compute_backoff(i) for i in range(8)]
    assert waits == sorted(waits), "退避应单调递增"
    assert waits[0] < waits[1] < waits[2]
    assert max(waits) <= 30.0


def test_backoff_has_jitter_by_default() -> None:
    fetcher = FeedFetcher("pt", jitter=True)
    samples = {fetcher.compute_backoff(3) for _ in range(10)}
    assert len(samples) > 1, "抖动应产生不同延迟，避免多站点同步重试"


# ------------------------------------------------------------ 结果
async def test_empty_feed_is_ok_status() -> None:
    """空结果不算失败（站点只是没放新种）。"""
    fetcher = FeedFetcher("pt")
    result = await fetcher.fetch(_StubAdapter(items=[]), "https://x/rss")
    assert result.status is PullStatus.EMPTY
    assert result.ok is True
    assert result.items == ()


async def test_duration_recorded() -> None:
    fetcher = FeedFetcher("pt")
    result = await fetcher.fetch(
        _StubAdapter(items=[FeedItem(title="a", link="l")]), "https://x/rss"
    )
    assert result.duration_ms >= 0