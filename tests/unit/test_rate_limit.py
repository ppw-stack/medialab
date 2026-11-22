"""请求限速器的契约测试。

网盘对高频请求会限流。递归扫描一棵大树
要发几百个请求，不限速必然被打。

**按驱动分桶**：不同网盘 / 不同接口的限频不同（光鸭列目录 5 次/秒、
换直链 2 次/秒），用同一个间隔会浪费配额也会打出软限频。
"""

from __future__ import annotations

import asyncio
import time

from app.core.rate_limit import RateLimiter


class TestRateLimiter:
    async def test_first_request_is_immediate(self) -> None:
        """首次请求不等。"""
        limiter = RateLimiter(default_interval=1.0)
        t0 = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - t0 < 0.2

    async def test_second_request_waits(self) -> None:
        """第二次请求至少间隔 min_interval。"""
        limiter = RateLimiter(default_interval=0.5)
        await limiter.acquire()
        t0 = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.45, f"间隔 {elapsed:.3f}s < 0.5s"

    async def test_concurrent_requests_are_serialized(self) -> None:
        """并发请求被串行化——总耗时应约等于 N × interval。"""
        limiter = RateLimiter(default_interval=0.2)
        start = time.monotonic()
        await asyncio.gather(*[limiter.acquire() for _ in range(4)])
        elapsed = time.monotonic() - start
        # 4 次请求至少要 3 个间隔
        assert elapsed >= 0.55, f"并发未被正确限速：{elapsed:.3f}s"

    async def test_zero_means_no_limit(self) -> None:
        """0 = 不限速。"""
        limiter = RateLimiter(default_interval=0)
        start = time.monotonic()
        await asyncio.gather(*[limiter.acquire() for _ in range(10)])
        assert time.monotonic() - start < 0.2

    async def test_set_interval_at_runtime(self) -> None:
        """运行时可调间隔（改配置无需重启）。"""
        limiter = RateLimiter(default_interval=10.0)
        limiter.set_interval(0.0)
        start = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - start < 0.2

    async def test_negative_interval_treated_as_zero(self) -> None:
        """负值按 0 处理（不限速），不报错。"""
        limiter = RateLimiter(default_interval=-5.0)
        start = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - start < 0.2


class TestProviderBuckets:
    async def test_providers_are_independent(self) -> None:
        """不同 provider 互不干扰——各自的桶相互独立。"""
        limiter = RateLimiter(default_interval=1.0)
        await limiter.acquire("other")
        # 光鸭的首次请求应当立即返回（自己的桶还没发过请求）
        t0 = time.monotonic()
        await limiter.acquire("guangya")
        assert time.monotonic() - t0 < 0.2

    async def test_same_provider_shares_bucket(self) -> None:
        """同一 provider 的多次请求共享一个桶。"""
        limiter = RateLimiter(default_interval=0.5)
        await limiter.acquire("other")
        t0 = time.monotonic()
        await limiter.acquire("other")
        assert time.monotonic() - t0 >= 0.45

    async def test_set_interval_for_one_provider_only(self) -> None:
        """按 provider 设间隔不影响其它 provider。"""
        limiter = RateLimiter(default_interval=1.0)
        limiter.set_interval(0.0, "guangya")
        assert limiter.get_interval("guangya") == 0.0
        assert limiter.get_interval("other") == 1.0

    async def test_set_interval_without_provider_updates_default(self) -> None:
        """不带 provider 的设置只改**默认值**。"""
        limiter = RateLimiter(default_interval=1.0)
        limiter.set_interval(0.0)
        assert limiter.get_interval("other") == 0.0
        assert limiter.get_interval("guangya") == 0.0
        assert limiter.default_interval == 0.0

    async def test_explicit_override_survives_default_change(self) -> None:
        """显式设过的 provider **不受**改默认值影响。"""
        limiter = RateLimiter(default_interval=1.0)
        limiter.set_interval(0.2, "guangya")
        limiter.set_interval(5.0)  # 改默认值
        assert limiter.get_interval("guangya") == 0.2
        assert limiter.get_interval("other") == 5.0
