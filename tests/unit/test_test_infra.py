"""测试基础设施自身的约束（Phase 2 批次 3）。

``tests/conftest.py`` 里的 fixture 会影响**所有**用例——它们出问题时
不会报错，只会让别的用例莫名其妙地失效。本文件把关键约束写成测试。

背景：``fast_backoff`` fixture 曾把
``app.services.feed_fetcher.asyncio.sleep`` 换成瞬时版。但
``app.services.feed_fetcher.asyncio`` **就是**全局 ``asyncio`` 模块
（模块属性），于是整个测试进程的 ``asyncio.sleep`` 都变成了 0——
连用例自己写的 ``await asyncio.sleep(1.1)`` 也不睡。

后果：一个断言「跨秒」的用例（``updated_at`` 必须是提交后的新值）
**永远通过**，看起来是绿的，实际什么也没验证。
"""

from __future__ import annotations

import asyncio
import time


async def test_global_asyncio_sleep_is_not_patched() -> None:
    """普通 sleep 必须真的睡（不能被 fast_backoff 全局改掉）。"""
    t0 = time.monotonic()
    await asyncio.sleep(0.05)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.04, f"全局 asyncio.sleep 被改掉了（{elapsed:.3f}s）"


async def test_feed_fetcher_sleep_is_patched() -> None:
    """退避 sleep 仍被压成 0（fast_backoff 本身要生效）。"""
    import app.services.feed_fetcher as ff

    t0 = time.monotonic()
    await ff.asyncio.sleep(1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"feed_fetcher 的退避 sleep 没被压掉（{elapsed:.3f}s）"


async def test_feed_fetcher_asyncio_proxy_forwards_other_attrs() -> None:
    """代理模块对象只换 sleep，其余属性照常转发。"""
    import app.services.feed_fetcher as ff

    # 代理必须保留 create_task / wait_for 等（feed_fetcher 可能用到）
    assert hasattr(ff.asyncio, "create_task")
    assert hasattr(ff.asyncio, "wait_for")
    # 且代理的 sleep 与全局的不是一个
    assert ff.asyncio.sleep is not asyncio.sleep
