"""RSS 拉取器（FR-2.2）。

PT 站对高频请求极其敏感（R2：被封 = 账号风险），因此本模块的三条约束都是
**安全要求**而非性能优化：

1. **频率下限 15 分钟**（FR-2.2 强制下限），配置再小也会被夹住。
2. **指数退避重试 3 次**，而不是无限重试。
3. **429/403 立即熔断该站 1 小时**——这两种状态说明站点已把我们标记为
   异常流量，继续请求只会加速账号暴露。

熔断状态持久化在 ``feeds.breaker_until``，进程重启不会遗忘（避免重启即
绕过熔断）。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

import httpx

from app.adapters.sites.base import FeedItem, SiteAdapter
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import Feed

logger = get_logger(__name__)

#: 频率下限（分钟，FR-2.2 强制）
MIN_INTERVAL_MINUTES = 15
#: 重试次数（FR-2.2）
MAX_RETRIES = 3
#: 熔断时长（秒，FR-2.2：429/403 熔断 1 小时）
BREAKER_SECONDS = 3600
#: 单次请求超时
REQUEST_TIMEOUT = 30.0
#: 反爬合规：仅常规浏览器标识，绝不做 UA 伪装对抗（R2）
USER_AGENT = "SeedHelper/0.1 (+https://github.com/seedhelper)"


class PullStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    BREAKER_TRIPPED = "breaker_tripped"
    INTERVAL_NOT_MET = "interval_not_met"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"


class BreakerTripped(RuntimeError):
    """触发熔断（429/403）。调用方应记入 feeds.breaker_until。"""


@dataclass(frozen=True, slots=True)
class PullResult:
    """单次拉取结果。"""

    status: PullStatus
    items: tuple[FeedItem, ...] = ()
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {PullStatus.OK, PullStatus.EMPTY}


@dataclass(frozen=True, slots=True)
class BreakerState:
    """熔断状态。"""

    tripped: bool
    until: object | None = None

    @classmethod
    def clear(cls) -> BreakerState:
        return cls(tripped=False, until=None)


def enforce_interval(configured_minutes: int) -> int:
    """夹住拉取间隔下限（FR-2.2）。"""
    return max(int(configured_minutes), MIN_INTERVAL_MINUTES)


class FeedFetcher:
    """RSS 拉取器。

    **退避 + 熔断**都在这里，站点适配层只负责解析（分层约束，§7.1）。
    """

    def __init__(
        self,
        site_name: str,
        *,
        timeout: float = REQUEST_TIMEOUT,
        jitter: bool = True,
    ) -> None:
        self._site = site_name
        self._timeout = timeout
        self._jitter = jitter

    def should_pull(self, feed: Feed, *, interval_minutes: int) -> tuple[bool, str]:
        """是否可以拉取：熔断优先，其次频率下限。"""
        now = utcnow()
        if feed.breaker_until and feed.breaker_until > now:
            remain = int((feed.breaker_until - now).total_seconds())
            return False, f"熔断中，剩余 {remain}s"
        if feed.last_pulled_at:
            interval = enforce_interval(interval_minutes)
            next_at = feed.last_pulled_at + timedelta(minutes=interval)
            if now < next_at:
                remain = int((next_at - now).total_seconds())
                return False, f"未到间隔，剩余 {remain}s（间隔下限 {interval}min）"
        return True, ""

    def compute_backoff(self, attempt: int) -> float:
        """指数退避 + 抖动（FR-2.2）。

        抖动避免多站点同时重试形成新的尖峰——这正是触发 429 的原因。
        """
        base = min(2.0**attempt, 30.0)
        return base * (0.5 + random.random() * 0.5) if self._jitter else base

    async def fetch(
        self, adapter: SiteAdapter, url: str, *, retry: bool = True
    ) -> PullResult:
        """拉取并解析 RSS。"""
        started = time.monotonic()
        attempts = MAX_RETRIES if retry else 1
        last_error = ""

        for attempt in range(attempts):
            try:
                items = await self._fetch_once(adapter, url)
            except BreakerTripped as exc:
                logger.warning("站点 %s 触发熔断：%s", self._site, exc)
                return PullResult(
                    status=PullStatus.BREAKER_TRIPPED,
                    duration_ms=self._elapsed_ms(started),
                    error=str(exc),
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "站点 %s 第 %d/%d 次拉取失败：%s",
                    self._site,
                    attempt + 1,
                    attempts,
                    exc,
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.compute_backoff(attempt))
                continue
            except Exception as exc:
                # 解析类异常（XML ParseError 等）：重试无意义，直接标记
                logger.warning("站点 %s 解析失败：%s", self._site, exc)
                return PullResult(
                    status=PullStatus.PARSE_ERROR,
                    duration_ms=self._elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                duration = self._elapsed_ms(started)
                if not items:
                    logger.info("站点 %s RSS 无新条目（%dms）", self._site, duration)
                    return PullResult(
                        status=PullStatus.EMPTY, duration_ms=duration
                    )
                logger.info(
                    "站点 %s 拉取 %d 条（%dms）", self._site, len(items), duration
                )
                return PullResult(
                    status=PullStatus.OK, items=tuple(items), duration_ms=duration
                )

        return PullResult(
            status=PullStatus.HTTP_ERROR,
            duration_ms=self._elapsed_ms(started),
            error=last_error,
        )

    async def _fetch_once(self, adapter: SiteAdapter, url: str) -> list[FeedItem]:
        """单次拉取。返回任务列表（RSS 拉取路径已废弃）。"""
        _ = url  # RSS 路径不再使用
        try:
            items = await adapter.search_torrents({})
        except Exception as exc:
            raise httpx.HTTPError(f"search_torrents 失败：{exc}") from exc
        if items is None:
            raise httpx.HTTPError("适配器返回 None")
        return items

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)


async def fetch_with_own_client(
    url: str,
    *,
    cookies: str | None = None,
    timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109
) -> httpx.Response:
    """直接用 httpx 拉取（供站点适配层内部复用）。

    显式携带固定 UA（R2：仅常规浏览器标识，不做伪装对抗）。
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"}
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as client:
        resp = await client.get(url)
        if resp.status_code in {403, 429}:
            raise BreakerTripped(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp