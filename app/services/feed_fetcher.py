"""站点拉取器（FR-2.2 节奏控制）。

PT 站对高频请求极其敏感（R2：被封 = 账号风险），因此本模块的约束是
**安全要求**而非性能优化：**指数退避重试 3 次**，而不是无限重试。

429/403 由适配层抛 :class:`BreakerTripped`，本模块识别后立即放弃本次拉取
（这类状态说明站点已把我们标记为异常流量，继续请求只会加速账号暴露）。

注：本模块是兼容层——实际拉取已统一走各 adapter 的
:meth:`SiteAdapter.search_torrents`（API/HTML），不再独立发 HTTP 请求。
本类保留用于：单站连通性测试（``/sites/{id}/feed-test``）。

历史：FR-2.2 原设计还有"频率下限 15 分钟"与"熔断状态持久化到
``feeds.breaker_until``"，两者随 ``feeds`` 表一起下线（现表结构见
:mod:`app.db.models`），对应的 :meth:`FeedFetcher.should_pull` /
``BreakerState`` 已删除。
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

logger = get_logger(__name__)

#: 重试次数（FR-2.2）
MAX_RETRIES = 3
#: 单次请求超时
REQUEST_TIMEOUT = 30.0


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
class PagesResult:
    """分页拉取结果（生产路径用）。

    与 :class:`PullResult` 的区别：多一个 ``pages_fetched``
    （实际拉到几页），用于区分「站点只有 1 页」与「第 2 页就失败」。
    """

    status: PullStatus
    items: tuple[FeedItem, ...] = ()
    pages_fetched: int = 0
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {PullStatus.OK, PullStatus.EMPTY}


class FeedFetcher:
    """站点拉取器（兼容层，节奏控制）。

    **退避**在这里，站点适配层只负责解析（分层约束，§7.1）。
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

    async def fetch_pages(
        self,
        adapter: SiteAdapter,
        *,
        max_pages: int = 3,
        page_size: int = 200,
    ) -> PagesResult:
        """分页拉取（**生产路径**用这个），带指数退避重试。

        与 :meth:`fetch` 的关系：

        - ``fetch()`` 是**单页**语义，供 ``/sites/{id}/feed-test`` 诊断端点用
        - ``fetch_pages()`` 是**分页**语义，供 ``fetch_all_sites`` 作业用

        分页与重试的交互：

        1. 逐页拉取（第 1..max_pages 页），某页空 → 停止后续页
        2. 某页网络类失败 → **该页重试**（指数退避 + 抖动）
        3. 某页重试耗尽 → **保留已拉到的前几页数据**，但整站记为失败
           （第 3 页失败不该让前 2 页白拉）
        4. 某页解析类失败 → 不重试，整站记为解析失败
        5. 某页 :class:`BreakerTripped` → 立即放弃整站（熔断信号）

        耗时上界：``max_pages`` × ``MAX_RETRIES`` 次请求 + 退避 sleep。
        退避基数 1/2/4 秒，最坏单站额外约 7 秒。

        注：**不做站点级 sleep**（FR-2.2 的「频率下限 15 分钟」已在
        7ef7c68 移除，2a08bfd 进一步删掉了 min_interval_minutes 字段——
        cron 周期已是事实上的间隔）。本方法只在**失败后**重试。
        """
        started = time.monotonic()
        items: list[FeedItem] = []
        pages_fetched = 0

        for page_number in range(1, max_pages + 1):
            try:
                page_items = await self._fetch_page(
                    adapter, page_number=page_number, page_size=page_size
                )
            except BreakerTripped as exc:
                logger.warning("站点 %s 第 %d 页触发熔断：%s", self._site, page_number, exc)
                return PagesResult(
                    status=PullStatus.BREAKER_TRIPPED,
                    items=tuple(items),
                    pages_fetched=pages_fetched,
                    duration_ms=self._elapsed_ms(started),
                    error=str(exc),
                )
            except FeedParseError as exc:
                logger.warning(
                    "站点 %s 第 %d 页解析失败：%s", self._site, page_number, exc
                )
                return PagesResult(
                    status=PullStatus.PARSE_ERROR,
                    items=tuple(items),
                    pages_fetched=pages_fetched,
                    duration_ms=self._elapsed_ms(started),
                    error=str(exc),
                )
            except (httpx.HTTPError, SiteFetchError) as exc:
                # 该页重试耗尽：保留已拉到的数据，整站记为失败
                logger.warning(
                    "站点 %s 第 %d 页重试 %d 次仍失败：%s",
                    self._site, page_number, MAX_RETRIES, exc,
                )
                return PagesResult(
                    status=PullStatus.HTTP_ERROR,
                    items=tuple(items),
                    pages_fetched=pages_fetched,
                    duration_ms=self._elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}",
                )
            items.extend(page_items)
            pages_fetched += 1
            if not page_items:
                break  # 后续页无数据就停，避免无效请求

        return PagesResult(
            status=PullStatus.OK if items else PullStatus.EMPTY,
            items=tuple(items),
            pages_fetched=pages_fetched,
            duration_ms=self._elapsed_ms(started),
            error="",
        )

    async def _fetch_page(
        self, adapter: SiteAdapter, *, page_number: int, page_size: int
    ) -> list[FeedItem]:
        """拉取单页（带退避重试）。失败抛异常，由 :meth:`fetch_pages` 分类。"""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                items = await adapter.search_torrents(
                    {}, page_size=page_size, page_number=page_number
                )
            except (BreakerTripped, FeedParseError):
                raise  # 语义明确，不重试
            except (httpx.HTTPError, SiteFetchError) as exc:
                last_error = exc
                logger.warning(
                    "站点 %s 第 %d 页第 %d/%d 次拉取失败：%s",
                    self._site, page_number, attempt + 1, MAX_RETRIES, exc,
                )
                if attempt + 1 < MAX_RETRIES:
                    await asyncio.sleep(self.compute_backoff(attempt))
                continue
            except Exception as exc:
                # 解析/契约类：重试无意义
                raise FeedParseError(f"search_torrents 解析失败：{exc}") from exc
            if items is None:
                raise FeedParseError("适配器返回 None")
            return items
        # 重试耗尽
        assert last_error is not None
        raise last_error

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)