"""线程池执行适配。"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


async def run_in_threadpool(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """在默认线程池中执行同步函数。"""
    if kwargs:
        return await asyncio.to_thread(lambda: func(*args, **kwargs))
    return await asyncio.to_thread(func, *args)
