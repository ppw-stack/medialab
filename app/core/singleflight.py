"""单飞（single-flight）控制。

同一 key 并发 miss 时只让**一个**执行者真正干活，其余共享同一结果。

为什么必须有：直链缓存 miss 意味着要调网盘 API 换直链。若 10 台设备同时
播放同一文件而没有单飞，就是 10 次网盘调用 —— 直接触发网盘风控。

实现要点：每个 key 一个「槽位」，槽位里存**已完成的结果**。
后来者发现槽位已完成就直接取结果，**不再执行 fn**。

**仅用锁是不够的**——锁只保证串行，等待者醒来后仍会各执行一次 ``fn``。
必须显式记录「这个 key 已经算过了」。

槽位用**引用计数**回收：最后一个等待者离开时删除，避免字典无限增长。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


@dataclass
class _Slot[T]:
    """一个 key 的执行槽位。

    Attributes:
        lock: 串行化闸门。
        done: 是否已有成功结果。
        value: 已算出的结果。
        error: 执行抛出的异常（供后来者重抛）。
        waiters: 当前持有/等待该槽位的请求数。
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: bool = False
    value: T | None = None
    error: BaseException | None = None
    waiters: int = 0


class SingleFlight:
    """按 key 去重的并发执行器。"""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._slots: dict[str, _Slot[object]] = {}

    async def run(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        """执行 ``fn``，同一 key 的并发调用共享一次执行。

        Raises:
            异常向上传播：首个执行者抛的错，后来者收到**同一个**异常；
            但槽位会被重置，允许后续请求重试（一次瞬时故障不应让该 key 永久失败）。
        """
        async with self._guard:
            slot = self._slots.get(key)
            if slot is None:
                slot = _Slot[object]()
                self._slots[key] = slot
            slot.waiters += 1
            typed = slot

        try:
            async with typed.lock:
                # 已有别人算好的结果 → 直接取，不重复执行。
                if typed.done:
                    if typed.error is not None:
                        raise typed.error
                    return typed.value  # type: ignore[return-value]
                # 之前有人失败过：清掉错误标记，允许本次重试。
                typed.error = None

                try:
                    result = await fn()
                except BaseException as exc:
                    typed.error = exc
                    raise
                else:
                    typed.value = result
                    typed.done = True
                    return result
        finally:
            async with self._guard:
                typed.waiters -= 1
                if typed.waiters <= 0:
                    self._slots.pop(key, None)

    @property
    def in_flight(self) -> int:
        """当前持有槽位的 key 数（监控用）。"""
        return len(self._slots)
