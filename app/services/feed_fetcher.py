"""站点拉取器（FR-2.2 节奏控制 + 熔断保护）。

PT 站对高频请求极其敏感（R2：被封 = 账号风险），因此本模块的三条约束都是
**安全要求**而非性能优化：

1. **频率下限 15 分钟**（FR-2.2 强制下限），配置再小也会被夹住。
2. **指数退避重试 3 次**，而不是无限重试。
3. **429/403 立即熔断该站 1 小时**——这两种状态说明站点已把我们标记为
   异常流量，继续请求只会加速账号暴露。

熔断状态持久化在 ``feeds.breaker_until``，进程重启不会遗忘（避免重启即
绕过熔断）。

注：本模块是兼容层——实际拉取已统一走各 adapter 的
:meth:`SiteAdapter.search_torrents`（API/HTML），不再独立发 HTTP 请求。
本类保留用于：单站连通性测试（``/sites/{id}/feed-test``）+ 熔断节奏控制。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    SiteAdapter,
    SiteFetchError,
)
from app.core.logging import get_logger
from app.core.timeutils import bj_now_dt, parse_bj

logger = get_logger(__name__)

#: 重试次数（FR-2.2）
MAX_RETRIES = 3
#: 熔断时长（秒，FR-2.2：429/403 熔断 1 小时）
BREAKER_SECONDS = 3600
#: 单次请求超时
REQUEST_TIMEOUT = 30.0
#: 反爬合规：仅常规浏览器标识，绝不做 UA 伪装对抗（R2）
USER_AGENT = "SeedHelper/0.1 (+https://github.com/seedhelper)"


class FeedParseError(RuntimeError):
    """解析/契约类错误：重试无意义，fetch() 直接标记 PARSE_ERROR。"""


class PullStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    BREAKER_TRIPPED = "breaker_tripped"
    INTERVAL_NOT_MET = "interval_not_met"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"


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
    until: str | None = None

    @classmethod
    def clear(cls) -> BreakerState:
        return cls(tripped=False, until=None)


class FeedFetcher:
    """站点拉取器（兼容层，节奏控制 + 熔断）。

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

    def should_pull(self, breaker_until: str | None) -> tuple[bool, str]:
        """是否应该拉取：当前仅保留熔断检查（FR-2.2 频率下限已移除）。

        :param breaker_until: 该站点的熔断结束时刻（北京时间字符串或 None）。
        """
        now = bj_now_dt()
        if breaker_until:
            until_dt = parse_bj(breaker_until)
            if until_dt is not None and until_dt > now:
                remain = int((until_dt - now).total_seconds())
                return False, f"熔断中，剩余 {remain}s"
        return True, ""

    def compute_backoff(self, attempt: int) -> float:
        """指数退避 + 抖动（FR-2.2）。

        抖动避免多站点同时重试形成新的尖峰——这正是触发 429 的原因。
        """
        base = min(2.0**attempt, 30.0)
        return base * (0.5 + random.random() * 0.5) if self._jitter else base

    async def fetch(
        self, adapter: SiteAdapter, url: str | None = None, *, retry: bool = True
    ) -> PullResult:
        """拉取并解析（兼容层，调用 adapter.search_torrents）。"""
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
            except (httpx.HTTPError, SiteFetchError) as exc:
                # 网络类故障（DNS / 连接 / HTTP 错误）：可重试
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
                # 解析类异常（HTML BS4 / API JSON 等）：重试无意义，直接标记
                logger.warning("站点 %s 解析失败：%s", self._site, exc)
                return PullResult(
                    status=PullStatus.PARSE_ERROR,
                    duration_ms=self._elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                duration = self._elapsed_ms(started)
                if not items:
                    logger.info("站点 %s 无新条目（%dms）", self._site, duration)
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

    async def _fetch_once(self, adapter: SiteAdapter, url: str | None) -> list[FeedItem]:
        """单次拉取。返回任务列表（站点拉取路径走 adapter.search_torrents）。

        异常分类：网络类（httpx.HTTPError / :class:`SiteFetchError`）原样
        上抛由 fetch() 重试；解析/契约类包装为 :class:`FeedParseError`
        不再重试（站点改版重试无意义，且避免对限流站点每轮额外请求）。

        ``SiteFetchError`` 必须在可重试一类——它包装的是 DNS / 连接 /
        HTTP 错误这类**暂时性**故障，重试有意义；若被归到解析类，
        一次网络抖动就会让整个站点被判 PARSE_ERROR 直接放弃。
        """
        _ = url  # 兼容层保留参数，路径不再直接使用
        try:
            items = await adapter.search_torrents({})
        except (BreakerTripped, httpx.HTTPError, FeedParseError, SiteFetchError):
            raise  # 各自的语义已明确，透传
        except Exception as exc:
            raise FeedParseError(f"search_torrents 解析失败：{exc}") from exc
        if items is None:
            raise FeedParseError("适配器返回 None")
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
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html, */*"}
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as client:
        resp = await client.get(url)
        if resp.status_code in {403, 429}:
            raise BreakerTripped(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp