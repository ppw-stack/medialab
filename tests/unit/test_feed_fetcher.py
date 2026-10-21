"""FeedFetcher 测试（Phase 1）。

``app/services/feed_fetcher.py`` 此前**零测试覆盖**（0%），而它承载的
是 PT 站的安全约束（FR-2.2 指数退避重试）：退避策略改错会直接导致
账号被封。故本文件先把既有行为钉死，再改它。

关键契约：

- 网络类故障（DNS / 连接 / HTTP）→ **可重试**
- 解析类故障（站点改版）→ **不重试**（重试无意义，且加剧限流）
- ``BreakerTripped``（429/403 限流）→ **立即放弃**，不重试
- 站点确实没种子（``[]``）→ **不是失败**
"""

from __future__ import annotations

import httpx
from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    SiteAdapter,
    SiteFetchError,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)
from app.services.feed_fetcher import (
    MAX_RETRIES,
    FeedFetcher,
    FeedParseError,
    PullStatus,
)


class _StubAdapter(SiteAdapter):
    """可控的假适配器：按预设脚本抛异常或返回结果。"""

    adapter_type = "stub"

    def __init__(self, script: list[object]) -> None:
        super().__init__(SiteProfile(name="stub", domain="stub.test"))
        self._script = list(script)
        self.calls = 0

    async def search_torrents(self, query: dict[str, object], **kw: object) -> list[FeedItem]:
        self.calls += 1
        if not self._script:
            return []
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        assert isinstance(action, list)
        return action

    async def check_reachability(self) -> SiteHealth:
        return SiteHealth(reachable=True)

    async def download_torrent(self, item_id: str) -> bytes:
        return b""

    async def get_torrent_url(self, item_id: str) -> str | None:
        return None

    async def check_status(self, item_id: str) -> TorrentBrief:
        return TorrentBrief.UNKNOWN

    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        return TorrentDetail(item_id=item_id)


def _items(n: int = 2) -> list[FeedItem]:
    return [FeedItem(title=f"t{i}", link="", item_id=str(i)) for i in range(n)]


# ------------------------------------------------------------------ 退避计算

def test_backoff_grows_exponentially() -> None:
    f = FeedFetcher("s", jitter=False)
    assert f.compute_backoff(0) == 1.0
    assert f.compute_backoff(1) == 2.0
    assert f.compute_backoff(2) == 4.0
    assert f.compute_backoff(3) == 8.0


def test_backoff_capped_at_30_seconds() -> None:
    f = FeedFetcher("s", jitter=False)
    assert f.compute_backoff(10) == 30.0
    assert f.compute_backoff(20) == 30.0


def test_backoff_jitter_within_half_range() -> None:
    """抖动让延迟落在 [base*0.5, base*1.0]——避免多站同时重试形成尖峰。"""
    f = FeedFetcher("s", jitter=True)
    for attempt in range(5):
        base = min(2.0**attempt, 30.0)
        for _ in range(20):
            got = f.compute_backoff(attempt)
            assert base * 0.5 <= got <= base


def test_max_retries_is_3() -> None:
    assert MAX_RETRIES == 3


# ------------------------------------------------------------------ 正常路径

async def test_fetch_success() -> None:
    adapter = _StubAdapter([_items(3)])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.OK
    assert result.ok is True
    assert len(result.items) == 3
    assert result.error == ""


async def test_fetch_empty_is_not_a_failure() -> None:
    """站点确实没种子 → EMPTY，不是失败。"""
    adapter = _StubAdapter([[]])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.EMPTY
    assert result.ok is True
    assert result.items == ()


async def test_fetch_records_duration() -> None:
    adapter = _StubAdapter([_items(1)])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.duration_ms >= 0


# ------------------------------------------------------------------ 熔断

async def test_breaker_tripped_does_not_retry() -> None:
    """429/403 熔断必须**立即放弃**——继续请求只会加速账号暴露。"""
    adapter = _StubAdapter([BreakerTripped("429 限流")])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.BREAKER_TRIPPED
    assert result.ok is False
    assert adapter.calls == 1, "熔断后不应重试"
    assert "429" in result.error


async def test_breaker_tripped_error_preserved() -> None:
    adapter = _StubAdapter([BreakerTripped("站点封了")])
    result = await FeedFetcher("s").fetch(adapter)
    assert "站点封了" in result.error


# ------------------------------------------------------------------ 可重试故障

async def test_network_error_retries_then_succeeds() -> None:
    """第一次失败、第二次成功 → 整体成功（这正是接回主链路的目的）。"""
    adapter = _StubAdapter([httpx.ConnectError("DNS 挂了"), _items(2)])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.OK
    assert adapter.calls == 2


async def test_site_fetch_error_is_retryable() -> None:
    """SiteFetchError 包装的是暂时性故障，必须归入可重试一类。

    若被误归到解析类，一次网络抖动就会让整站被判 PARSE_ERROR 放弃。
    """
    adapter = _StubAdapter([SiteFetchError("连接被重置"), _items(1)])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.OK
    assert adapter.calls == 2


async def test_network_error_exhausted_retries() -> None:
    """重试耗尽 → HTTP_ERROR。"""
    adapter = _StubAdapter([httpx.ConnectError("x")] * 5)
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.HTTP_ERROR
    assert result.ok is False
    assert adapter.calls == MAX_RETRIES
    assert result.error


async def test_http_status_error_is_retryable() -> None:
    """HTTP 状态错误属网络类 → 可重试，重试耗尽才是 HTTP_ERROR。"""
    err = httpx.HTTPStatusError("500", request=None, response=None)  # type: ignore[arg-type]
    adapter = _StubAdapter([err, err, err])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.HTTP_ERROR
    assert adapter.calls == MAX_RETRIES

async def test_retry_disabled_makes_single_attempt() -> None:
    adapter = _StubAdapter([httpx.ConnectError("x")] * 5)
    result = await FeedFetcher("s").fetch(adapter, retry=False)
    assert result.status == PullStatus.HTTP_ERROR
    assert adapter.calls == 1


# ------------------------------------------------------------------ 不可重试故障

async def test_parse_error_does_not_retry() -> None:
    """解析类异常重试无意义（站点改版），且会加剧限流。"""
    adapter = _StubAdapter([ValueError("HTML 结构变了"), _items(1)])
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.PARSE_ERROR
    assert result.ok is False
    assert adapter.calls == 1, "解析错误不应重试"


async def test_adapter_returning_none_is_parse_error() -> None:
    """适配器返回 None 是契约违规 → PARSE_ERROR。"""
    adapter = _StubAdapter([])
    # 让 search_torrents 返回 None（违反类型契约）
    async def _none(query: dict[str, object], **kw: object) -> list[FeedItem]:
        return None  # type: ignore[return-value]

    adapter.search_torrents = _none  # type: ignore[method-assign]
    result = await FeedFetcher("s").fetch(adapter)
    assert result.status == PullStatus.PARSE_ERROR


def test_feed_parse_error_is_exception() -> None:
    assert issubclass(FeedParseError, RuntimeError)


# ------------------------------------------------------------------ 状态枚举

def test_pull_status_values() -> None:
    assert PullStatus.OK == "ok"
    assert PullStatus.EMPTY == "empty"
    assert PullStatus.BREAKER_TRIPPED == "breaker_tripped"
    assert PullStatus.HTTP_ERROR == "http_error"
    assert PullStatus.PARSE_ERROR == "parse_error"


def test_pull_result_ok_property() -> None:
    from app.services.feed_fetcher import PullResult

    assert PullResult(status=PullStatus.OK).ok is True
    assert PullResult(status=PullStatus.EMPTY).ok is True
    assert PullResult(status=PullStatus.HTTP_ERROR).ok is False
    assert PullResult(status=PullStatus.PARSE_ERROR).ok is False
    assert PullResult(status=PullStatus.BREAKER_TRIPPED).ok is False


# ------------------------------------------------------- fetch_pages（生产路径）

async def test_fetch_pages_collects_all_pages() -> None:
    """多页结果合并。"""
    adapter = _StubAdapter([_items(2), _items(2), _items(2)])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.OK
    assert len(result.items) == 6
    assert result.pages_fetched == 3


async def test_fetch_pages_stops_on_empty_page() -> None:
    """某页为空 → 停止后续页（避免无效请求）。"""
    adapter = _StubAdapter([_items(2), []])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.OK
    assert len(result.items) == 2
    assert result.pages_fetched == 2
    assert adapter.calls == 2, "空页之后不应继续请求"


async def test_fetch_pages_respects_max_pages() -> None:
    """不请求超过 max_pages 页。"""
    adapter = _StubAdapter([_items(1)] * 10)
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=2)
    assert adapter.calls == 2
    assert result.pages_fetched == 2


async def test_fetch_pages_retries_network_error() -> None:
    """某页网络故障 → 该页重试，成功则整站成功。"""
    adapter = _StubAdapter([httpx.ConnectError("DNS 抖动"), _items(2)])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.OK
    assert len(result.items) == 2


async def test_fetch_pages_keeps_earlier_pages_on_failure() -> None:
    """第 2 页失败 → 保留第 1 页数据，但整站记为失败。

    「第 3 页失败不该让前 2 页白拉」——但本站仍算失败站。
    """
    # 3 次重试都失败
    err = httpx.ConnectError("挂了")
    adapter = _StubAdapter([_items(2), err, err, err])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.HTTP_ERROR
    assert result.ok is False
    assert len(result.items) == 2, "前 1 页的数据要保留"
    assert result.pages_fetched == 1


async def test_fetch_pages_breaker_stops_immediately() -> None:
    """熔断 → 立即放弃，不重试，也不拉后续页。"""
    adapter = _StubAdapter([BreakerTripped("429"), _items(2)])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.BREAKER_TRIPPED
    assert adapter.calls == 1
    assert result.items == ()


async def test_fetch_pages_parse_error_does_not_retry() -> None:
    adapter = _StubAdapter([ValueError("HTML 变了"), _items(2)])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.PARSE_ERROR
    assert adapter.calls == 1, "解析错误不应重试"


async def test_fetch_pages_empty_result_is_empty_not_failure() -> None:
    """站点确实没种子 → EMPTY，不是失败。"""
    adapter = _StubAdapter([[]])
    result = await FeedFetcher("s").fetch_pages(adapter, max_pages=3)
    assert result.status == PullStatus.EMPTY
    assert result.ok is True


async def test_pages_result_ok_property() -> None:
    from app.services.feed_fetcher import PagesResult

    assert PagesResult(status=PullStatus.OK).ok is True
    assert PagesResult(status=PullStatus.EMPTY).ok is True
    assert PagesResult(status=PullStatus.HTTP_ERROR).ok is False
    assert PagesResult(status=PullStatus.BREAKER_TRIPPED).ok is False
