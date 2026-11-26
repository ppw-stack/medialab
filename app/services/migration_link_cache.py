"""迁移直链预取缓存（处理光鸭直链 6 小时过期自动重取）。

光鸭 CDN 直链取链免费且不消耗下载流量，但服务端 ``urlDuration=21600``（6 小时）
后失效。本缓存默认以 5.5 小时（预留 30 分钟余量）保存直链：
- 有效期内直接复用，避免重复取链；
- 超过 TTL 自动失效并重新向源驱动取链；
- 提交异常时可主动 :meth:`invalidate` 清除脏链。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

#: 默认 TTL：5.5 小时（光鸭直链 6h 过期，留 30 分钟余量）
DEFAULT_LINK_TTL_SEC = 5 * 3600 + 1800

#: 最大缓存条目数（防止长跑无限增长）
MAX_CACHE_ENTRIES = 2000


@dataclass(frozen=True, slots=True)
class _CachedLink:
    url: str
    expires_at: float


class PrefetchedLinkCache:
    """进程内迁移直链缓存（支持单调/注入时钟，便于确定性单测）。"""

    def __init__(
        self,
        *,
        ttl_sec: float = DEFAULT_LINK_TTL_SEC,
        max_entries: int = MAX_CACHE_ENTRIES,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_sec = max(1.0, float(ttl_sec))
        self._max_entries = max(1, int(max_entries))
        self._now = now_fn or time.monotonic
        self._store: dict[tuple[int, str], _CachedLink] = {}

    def get_valid(self, source_driver_id: int, remote_id: str) -> str | None:
        """返回未过期的直链；已过期则顺手淘汰并返回 ``None``。"""
        key = (source_driver_id, remote_id)
        item = self._store.get(key)
        if item is None:
            return None
        if self._now() >= item.expires_at:
            self._store.pop(key, None)
            return None
        return item.url

    def put(
        self,
        source_driver_id: int,
        remote_id: str,
        url: str,
        *,
        ttl_sec: float | None = None,
    ) -> None:
        """写入一条直链缓存（超上限时淘汰最旧一条）。"""
        if not url:
            return
        key = (source_driver_id, remote_id)
        if key not in self._store and len(self._store) >= self._max_entries:
            oldest = next(iter(self._store))
            self._store.pop(oldest, None)
        ttl = self._ttl_sec if ttl_sec is None else max(1.0, float(ttl_sec))
        self._store[key] = _CachedLink(url=url, expires_at=self._now() + ttl)

    def invalidate(self, source_driver_id: int, remote_id: str) -> None:
        """主动失效指定条目的缓存直链。"""
        self._store.pop((source_driver_id, remote_id), None)

    async def get_or_fetch(
        self,
        source_driver_id: int,
        remote_id: str,
        fetch_fn: Callable[[], Awaitable[str]],
        *,
        force_refresh: bool = False,
    ) -> tuple[str, bool]:
        """获取有效直链，返回 ``(url, from_cache)``。"""
        if not force_refresh:
            cached = self.get_valid(source_driver_id, remote_id)
            if cached is not None:
                return cached, True
        url = await fetch_fn()
        if url:
            self.put(source_driver_id, remote_id, url)
        return url, False

    def clear(self) -> None:
        """清空全部缓存（测试用）。"""
        self._store.clear()

    def stats(self) -> dict[str, int]:
        """返回当前缓存条目数与有效条目数。"""
        now = self._now()
        valid = sum(1 for item in self._store.values() if now < item.expires_at)
        return {"total": len(self._store), "valid": valid}


#: 进程内全局迁移直链缓存单例
migration_link_cache = PrefetchedLinkCache()
