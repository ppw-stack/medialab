"""单飞（single-flight）契约测试。"""

from __future__ import annotations

import asyncio

from app.core.singleflight import SingleFlight


class TestSingleFlight:
    async def test_sequential_calls_each_execute(self) -> None:
        """串行调用不共享——每次都要真正执行。"""
        flight = SingleFlight()
        calls = 0

        async def fn() -> int:
            nonlocal calls
            calls += 1
            return calls

        assert await flight.run("k", fn) == 1
        assert await flight.run("k", fn) == 2
        assert calls == 2

    async def test_concurrent_same_key_executes_once(self) -> None:
        """同 key 并发只执行一次——这是防网盘 API 被打爆的关键。"""
        flight = SingleFlight()
        calls = 0

        async def fn() -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return "result"

        results = await asyncio.gather(*[flight.run("k", fn) for _ in range(10)])
        assert results == ["result"] * 10
        assert calls == 1

    async def test_different_keys_execute_independently(self) -> None:
        flight = SingleFlight()
        calls = 0

        async def fn() -> int:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return calls

        await asyncio.gather(flight.run("a", fn), flight.run("b", fn))
        assert calls == 2

    async def test_exception_propagates_and_locks_are_released(self) -> None:
        """执行抛错时锁必须释放，否则该 key 永久卡死。"""
        flight = SingleFlight()

        async def boom() -> None:
            raise ValueError("boom")

        try:
            await flight.run("k", boom)
        except ValueError:
            pass
        else:
            raise AssertionError("异常应向上传播")

        assert flight.in_flight == 0

        # 锁已释放，后续调用能正常执行。
        async def ok() -> str:
            return "ok"

        assert await flight.run("k", ok) == "ok"

    async def test_lock_entries_cleaned_up(self) -> None:
        """引用计数归零后条目被删除——字典不能无限增长。"""
        flight = SingleFlight()

        async def fn() -> int:
            return 1

        await flight.run("k", fn)
        assert flight.in_flight == 0

    async def test_concurrent_failure_releases_all_waiters(self) -> None:
        """并发时失败，所有等待者都应收到异常且锁被释放。"""
        flight = SingleFlight()

        async def boom() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("boom")

        results = await asyncio.gather(
            *[flight.run("k", boom) for _ in range(5)], return_exceptions=True
        )
        assert all(isinstance(r, RuntimeError) for r in results)
        assert flight.in_flight == 0
