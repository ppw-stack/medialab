"""请求限速器。

网盘对高频请求会限流（光鸭实测：列目录 5 次/秒、换直链 2 次/秒）。
递归扫描媒体库时请求很密——一棵树几百个目录，不限速必然被打。

实现用「**全局最小间隔**」而非令牌桶：

- 令牌桶允许突发（桶满时能连发），但风控看的是**平均速率**
- 「两次请求至少间隔 N 秒」更贴合风控的判定方式，且行为可预测

**按驱动分桶**：不同网盘的限频不同（光鸭列目录与换直链也各一个桶）。
用同一个间隔会浪费额度，也会打出软限频。

限速是**进程级**的（同一驱动的所有实例共享一个桶）——否则多个存储源
各自限速，合计仍会超。

**间隔的单一真源**是 :meth:`RateLimiter.get_interval`：

- 显式设过某个 provider → 用它的专属值
- 没设过 → 用默认值

桶本身只记录「上次请求时间」。这样「改了默认值」和「改了某个 provider」
不会互相打架。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    """单个驱动的限速桶（只记上次请求时间）。"""

    last_request: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    """按驱动分桶的限速器。

    Attributes:
        default_interval: 未显式设置的驱动使用的间隔（秒）。
    """

    def __init__(self, default_interval: float = 1.0) -> None:
        self.default_interval = default_interval
        self._buckets: dict[str, _Bucket] = {}
        #: provider → 显式设置的间隔
        self._overrides: dict[str, float] = {}
        self._registry_lock = asyncio.Lock()

    async def acquire(self, provider: str = "default") -> None:
        """等待到该驱动允许发起下一次请求为止。

        Args:
            provider: 驱动标识（``guangya`` / ``guangya_link`` …）。不同 provider
                各用一个独立的桶——互不干扰。
        """
        interval = self.get_interval(provider)
        if interval <= 0:
            return
        bucket = await self._get_bucket(provider)
        async with bucket.lock:
            now = time.monotonic()
            elapsed = now - bucket.last_request
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            bucket.last_request = time.monotonic()

    async def _get_bucket(self, provider: str) -> _Bucket:
        """取（或建）某个驱动的限速桶。"""
        async with self._registry_lock:
            bucket = self._buckets.get(provider)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[provider] = bucket
            return bucket

    def set_interval(self, seconds: float, provider: str | None = None) -> None:
        """运行时调整间隔（改配置后无需重启）。

        Args:
            seconds: 新的最小间隔（秒）。0 = 不限速。
            provider: 只改这个驱动的间隔；``None`` = 改**默认值**
                （已显式设过专属值的 provider **不受影响**）。
        """
        seconds = max(float(seconds), 0.0)
        if provider is None:
            self.default_interval = seconds
            return
        self._overrides[provider] = seconds

    def get_interval(self, provider: str) -> float:
        """取某个驱动当前的间隔（单一真源）。"""
        override = self._overrides.get(provider)
        if override is not None:
            return override
        return self.default_interval


#: 进程级限速器（所有驱动共享，按 provider 分桶）
#:
#: 兜底默认 1.0 秒（未显式配置的驱动）。生产由启动期按驱动覆盖。
rate_limiter = RateLimiter()


def configure_rate_limit(seconds: float) -> None:
    """设置全局默认间隔（秒）。0 = 不限速。"""
    rate_limiter.set_interval(seconds)


def configure_provider_rate_limit(provider: str, seconds: float) -> None:
    """设置某个驱动的专属间隔（秒）。0 = 不限速。"""
    rate_limiter.set_interval(seconds, provider)
