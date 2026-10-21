"""按下载器实例的写操作串行化（非功能·并发安全 + FR-1.2）。

R5 应对措施："单实例操作锁 + 断线重连 + 写操作前二次校验 hash 存在性"。

与 :mod:`app.db.session` 的短事务约定的区别：后者约束的是**数据库**写，
本模块串行化**下载器 API** 调用——两把锁互不替代，必须同时存在。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.logging import get_logger

logger = get_logger(__name__)


class DownloaderLocks:
    """按实例名维护的锁表。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        """串行化对同一下载器的写操作。

        R5：下载器 API 本身不支持并发写（如 qBittorrent 会话、Deluge daemon），
        并发调用会导致状态错乱。
        """
        lock = self._lock_for(key)
        if lock.locked():
            logger.debug("等待下载器锁：%s", key)
        async with lock:
            yield


#: 全局锁表
locks = DownloaderLocks()