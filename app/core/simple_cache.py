"""进程内 TTL 缓存（单值）。

用途：把**跨网络**的查询降频——例如 Dashboard 每 5 秒轮询一次，若每次都
拉下载器的全量种子列表，会持续给 qBittorrent / Transmission 打请求。

本模块只提供"单值 + 过期时间"的最小实现，不做并发去重（single-flight）：
Dashboard 的并发写入是幂等的（同样输入得到同样输出），重复计算无害。

注意：缓存是**进程内**的——多进程部署时每个进程各持一份，这是刻意的
（跨进程共享需要 IPC，成本远大于收益）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

#: 哨兵：区分"未命中"与"已缓存的 None 值"
_MISS = object()


class TTLCache[T]:
    """单值 TTL 缓存。

    用法::

        cache: TTLCache[int | None] = TTLCache(ttl_seconds=30)

        async def load() -> int | None:
            ...

        value = await cache.get_or_set(load)

    注：``None`` 是合法缓存值（加载失败时的降级结果），用哨兵与"未命中"
    区分——否则 ``None`` 会被当成未命中而每次重新加载。
    """

    __slots__ = ("_expires_at", "_lock", "_ttl", "_value")

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = float(ttl_seconds)
        self._value: object = _MISS
        self._expires_at: float = 0.0
        #: 防止并发首次加载时重复计算（Dashboard 并发请求可能同时未命中）
        self._lock = asyncio.Lock()

    def get(self) -> T | None:
        """取缓存值；未命中或已过期返回 ``None``。

        缓存值本身是 ``None`` 时也返回 ``None``——用 :meth:`has_value`
        区分两种情况。
        """
        if self._value is _MISS:
            return None
        if time.monotonic() >= self._expires_at:
            return None
        value = self._value
        return None if value is None else value  # type: ignore[return-value]

    def has_value(self) -> bool:
        """当前是否有未过期的缓存条目（含值为 ``None`` 的情形）。"""
        if self._value is _MISS:
            return False
        return time.monotonic() < self._expires_at

    def set(self, value: T | None) -> None:
        """写缓存并重置过期时间。"""
        self._value = value
        self._expires_at = time.monotonic() + self._ttl

    def invalidate(self) -> None:
        """清缓存（下次读取重新加载）。"""
        self._value = _MISS
        self._expires_at = 0.0

    async def get_or_set(self, loader: Callable[[], Awaitable[T | None]]) -> T | None:
        """命中则返回缓存；否则调用 ``loader`` 加载并缓存。

        ``loader`` 抛异常时**不写缓存**——下次请求会重试。异常向上传播，
        由调用方决定降级策略。
        """
        if self.has_value():
            return self.get()
        async with self._lock:
            # 双检：等锁期间可能已被其他请求填充
            if self.has_value():
                return self.get()
            value = await loader()
            self.set(value)
            return value


__all__ = ["TTLCache"]
