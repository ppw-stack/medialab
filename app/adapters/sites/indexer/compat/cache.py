"""进程内小缓存（移植 spider 的 ``cached`` / ``MemoryBackend`` 依赖）。"""

from __future__ import annotations

import functools
import inspect
import threading
import time
from typing import Any, Callable


class MemoryBackend:
    """带容量与 TTL 的线程安全字典缓存。"""

    def __init__(self, maxsize: int = 128, ttl: float | None = None) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[Any, tuple[float, Any]] = {}

    def _expired(self, created: float) -> bool:
        """判断条目是否已过期。

        :param created: 条目的写入时刻（``time.time()`` 口径）
        :return: 条目是否已过期
        """
        if self._ttl is None:
            return False
        if self._ttl <= 0:
            # ttl=0 的语义是「立即过期」：条目的有效期为零，读回必须算未命中。
            # 不能靠「时钟有没有前进」判定——time.time() 在同一瞬间可能返回
            # 完全相同的浮点值，那会让本该过期的条目被当成命中（测试随机翻车）。
            return True
        # 用 >= ：条目在恰好到达 TTL 边界时就到期（与本项目 sunnypt 的
        # `time.monotonic() - cached_at >= ttl` 口径一致）。
        return (time.time() - created) >= self._ttl

    def get(self, key: Any, region: str | None = None) -> Any:
        """读取缓存值。

        :param key: 缓存键
        :param region: 兼容 MoviePilot 的分区参数；本实现只有一块缓存，忽略该值
        :return: 命中的缓存值；未命中或已过期时为 None
        """
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            created, value = item
            if self._expired(created):
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Any, value: Any, region: str | None = None) -> None:
        """写入缓存值；容量已满时淘汰最早写入的一项。

        :param key: 缓存键
        :param value: 缓存值
        :param region: 兼容 MoviePilot 的分区参数；本实现只有一块缓存，忽略该值
        """
        with self._lock:
            if len(self._data) >= self._maxsize and key not in self._data:
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            self._data[key] = (time.time(), value)

    def delete(self, key: Any, region: str | None = None) -> None:
        """删除一个缓存键；键不存在时静默通过。

        :param key: 待删除的缓存键
        :param region: 兼容 MoviePilot 的分区参数；本实现只有一块缓存，忽略该值
        """
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        """清空全部缓存项。"""
        with self._lock:
            self._data.clear()


def cached(
    _func: Callable[..., Any] | None = None,
    *,
    region: str = "default",
    maxsize: int = 128,
    ttl: float | None = None,
    skip_empty: bool = False,
    shared_key: str | None = None,
) -> Callable[..., Any]:
    """给函数加一个进程内 TTL 缓存（按参数哈希）。

    只覆盖移植代码里 ``@cached(...)`` 的用法：参数可哈希即可；``shared_key``
    仅用于同一方法的不同装饰器共享缓存，这里直接并到键里。

    异步函数走独立的 async 包装器，缓存的是 **await 之后的结果**——缓存协程
    对象本身会在第二次命中时抛「cannot reuse already awaited coroutine」。
    """
    backend = MemoryBackend(maxsize=maxsize, ttl=ttl)

    def _cache_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return (shared_key, args, tuple(sorted(kwargs.items())))

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    key: Any = _cache_key(args, kwargs)
                    hash(key)
                except TypeError:
                    return await func(*args, **kwargs)
                hit = backend.get(key)
                if hit is not None:
                    return hit
                value = await func(*args, **kwargs)
                if value is None and skip_empty:
                    return value
                backend.set(key, value)
                return value

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                key: Any = _cache_key(args, kwargs)
                hash(key)
            except TypeError:
                return func(*args, **kwargs)
            hit = backend.get(key)
            if hit is not None:
                return hit
            value = func(*args, **kwargs)
            if value is None and skip_empty:
                return value
            backend.set(key, value)
            return value

        return wrapper

    if _func is not None:
        return decorator(_func)
    return decorator
