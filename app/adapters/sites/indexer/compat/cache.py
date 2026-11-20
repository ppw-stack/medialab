"""进程内小缓存（移植 spider 的 ``cached`` / ``MemoryBackend`` 依赖）。"""

from __future__ import annotations

import functools
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
        return self._ttl is not None and (time.time() - created) > self._ttl

    def get(self, key: Any) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            created, value = item
            if self._expired(created):
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._maxsize and key not in self._data:
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
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
    """
    backend = MemoryBackend(maxsize=maxsize, ttl=ttl)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                key: Any = (shared_key, args, tuple(sorted(kwargs.items())))
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
