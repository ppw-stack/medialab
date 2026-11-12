"""直链缓存契约测试。

覆盖：L1/L2 两级、UA 分桶、TTL 提前失效、单飞、过期清理。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager

import pytest
from app.services.link_cache import LinkCache, parse_expires_at
from sqlalchemy.exc import OperationalError


@pytest.fixture
def cache(settings, db_engine) -> LinkCache:
    """缓存实例（依赖已初始化的 DB——L2 读写要用）。"""
    return LinkCache(settings)


class TestParseExpiresAt:
    def test_reads_t_parameter(self) -> None:
        """直链的 ``?t=`` 是过期时间戳。"""
        expires = 2_000_000_000
        url = f"https://cdn.example.com/f?t={expires}"
        parsed = parse_expires_at(url, default_ttl_sec=600, safety_margin_sec=300)
        assert parsed == expires - 300

    def test_fallback_when_no_t_parameter(self) -> None:
        """无 ``?t=`` 时用 fallback TTL（再扣安全余量）。"""
        url = "https://cdn.example.com/f"
        before = int(time.time())
        parsed = parse_expires_at(url, default_ttl_sec=600, safety_margin_sec=0)
        after = int(time.time())
        assert before + 600 <= parsed <= after + 600

    def test_non_numeric_t_falls_back(self) -> None:
        """``?t=`` 非数字 → 走 fallback，不抛错。"""
        url = "https://cdn.example.com/f?t=abc"
        parsed = parse_expires_at(url, default_ttl_sec=600, safety_margin_sec=0)
        assert parsed > int(time.time())

    def test_safety_margin_prevents_serving_expiring_urls(self) -> None:
        """安全余量：不能把「马上要过期」的 URL 发给客户端。"""
        expires = int(time.time()) + 100
        url = f"https://cdn.example.com/f?t={expires}"
        parsed = parse_expires_at(url, default_ttl_sec=600, safety_margin_sec=300)
        assert parsed < int(time.time())

    def test_extra_query_params_are_ignored(self) -> None:
        expires = 2_000_000_000
        url = f"https://cdn.example.com/f?f=1&t={expires}&c=0"
        parsed = parse_expires_at(url, default_ttl_sec=600, safety_margin_sec=300)
        assert parsed == expires - 300


class TestL1Cache:
    async def test_put_then_get(self, cache: LinkCache) -> None:
        await cache.put(1, "ua1", "https://cdn/f", int(time.time()) + 3600)
        found = await cache.get(1, "ua1")
        assert found is not None
        assert found.url == "https://cdn/f"

    async def test_miss_returns_none(self, cache: LinkCache) -> None:
        assert await cache.get(999, "ua1") is None

    async def test_ua_buckets_are_separate(self, cache: LinkCache) -> None:
        """**核心正确性要求**：不同 UA 的直链不能混用（115 的 f=1 绑定 UA）。"""
        await cache.put(1, "ua-a", "https://cdn/for-ua-a", int(time.time()) + 3600)
        assert await cache.get(1, "ua-b") is None
        found = await cache.get(1, "ua-a")
        assert found is not None
        assert found.url == "https://cdn/for-ua-a"

    async def test_expired_entry_is_not_returned(self, cache: LinkCache) -> None:
        await cache.put(1, "ua1", "https://cdn/f", int(time.time()) - 1)
        assert await cache.get(1, "ua1") is None

    async def test_lru_evicts_oldest(self, settings, db_engine) -> None:
        """L1 超容量逐出最久未用的条目。

        注意：L1 淘汰**不等于**条目消失——L2 仍有副本，``get`` 会回填。
        故此处断言 L1 内部状态，而非 ``get`` 的返回值。
        """
        settings.link_cache_l1_size = 2
        cache = LinkCache(settings)
        expires = int(time.time()) + 3600
        await cache.put(1, "ua", "https://cdn/1", expires)
        await cache.put(2, "ua", "https://cdn/2", expires)
        await cache.put(3, "ua", "https://cdn/3", expires)
        assert len(cache._l1) == 2
        assert (1, "ua") not in cache._l1
        assert (3, "ua") in cache._l1
        # L2 仍有全部三条——L1 只是快速路径。
        assert len(cache._l1) <= settings.link_cache_l1_size


class TestL2Cache:
    async def test_l2_survives_new_cache_instance(self, cache: LinkCache, settings) -> None:
        """L2 持久化：新建 cache 实例（L1 为空）仍能命中。"""
        await cache.put(1, "ua1", "https://cdn/f", int(time.time()) + 3600)
        fresh = LinkCache(settings)
        found = await fresh.get(1, "ua1")
        assert found is not None
        assert found.url == "https://cdn/f"

    async def test_put_overwrites_existing(self, cache: LinkCache) -> None:
        expires = int(time.time()) + 3600
        await cache.put(1, "ua1", "https://cdn/old", expires)
        await cache.put(1, "ua1", "https://cdn/new", expires)
        found = await cache.get(1, "ua1")
        assert found is not None
        assert found.url == "https://cdn/new"

    async def test_cleanup_removes_expired(self, cache: LinkCache) -> None:
        await cache.put(1, "ua1", "https://cdn/old", int(time.time()) - 10)
        await cache.put(2, "ua1", "https://cdn/new", int(time.time()) + 3600)
        deleted = await cache.cleanup_expired()
        assert deleted == 1
        assert await cache.get(1, "ua1") is None
        assert await cache.get(2, "ua1") is not None


class TestGetOrFetch:
    async def test_cache_hit_does_not_call_fetch(self, cache: LinkCache) -> None:
        calls = 0

        async def fetch() -> tuple[str, int]:
            nonlocal calls
            calls += 1
            return "https://cdn/f", int(time.time()) + 3600

        await cache.get_or_fetch(1, "ua", fetch)
        await cache.get_or_fetch(1, "ua", fetch)
        assert calls == 1

    async def test_concurrent_miss_calls_fetch_once(self, cache: LinkCache) -> None:
        """并发 miss 只打一次网盘 API（单飞）——防风控的关键。"""
        calls = 0

        async def fetch() -> tuple[str, int]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return "https://cdn/f", int(time.time()) + 3600

        results = await asyncio.gather(
            *[cache.get_or_fetch(1, "ua", fetch) for _ in range(10)]
        )
        assert all(r.url == "https://cdn/f" for r in results)
        assert calls == 1

    async def test_different_ua_triggers_separate_fetch(self, cache: LinkCache) -> None:
        """不同 UA 是不同分桶 → 各自换直链。"""
        calls = 0

        async def fetch() -> tuple[str, int]:
            nonlocal calls
            calls += 1
            return f"https://cdn/{calls}", int(time.time()) + 3600

        await cache.get_or_fetch(1, "ua-a", fetch)
        await cache.get_or_fetch(1, "ua-b", fetch)
        assert calls == 2


class TestDbLockDegradation:
    """**缓存读写失败不得让播放失败**（2026-11-12 prod 事故）。

    扫描作业大批量写库时，中转请求撞上 SQLite 写锁
    （``DB_JOURNAL_MODE=DELETE`` 是**全库排他**锁），
    ``database is locked`` 冒泡出去 → **播放 500**。
    直链缓存是优化，不是播放的前提——读写失败一律降级。
    """

    @staticmethod
    def _locked() -> OperationalError:
        return OperationalError(
            "UPDATE link_cache", {}, sqlite3.OperationalError("database is locked")
        )

    @staticmethod
    def _patch_session_scope(monkeypatch: pytest.MonkeyPatch) -> None:
        """让缓存的 DB 访问整体抛「database is locked」。"""

        @asynccontextmanager
        async def boom():  # type: ignore[no-untyped-def]
            raise OperationalError(
                "SELECT link_cache", {}, sqlite3.OperationalError("database is locked")
            )
            yield  # pragma: no cover - 不可达

        monkeypatch.setattr("app.services.link_cache.session_scope", boom)

    async def test_l2_write_failure_does_not_raise(
        self, cache: LinkCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L2 写失败 → 静默降级（只进 L1），不抛异常。"""
        self._patch_session_scope(monkeypatch)

        async def fetch() -> tuple[str, int]:
            return "https://cdn/f", int(time.time()) + 3600

        link = await cache.get_or_fetch(1, "ua", fetch)
        # 直链照常返回（L1 仍可命中）
        assert link.url == "https://cdn/f"
        # L1 内存缓存仍然生效
        assert (await cache.get(1, "ua")).url == "https://cdn/f"

    async def test_l2_read_failure_falls_back_to_fetch(
        self, cache: LinkCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L2 读失败 → 当作未命中，继续换取直链（不抛）。"""
        self._patch_session_scope(monkeypatch)
        calls = 0

        async def fetch() -> tuple[str, int]:
            nonlocal calls
            calls += 1
            return "https://cdn/f", int(time.time()) + 3600

        link = await cache.get_or_fetch(1, "ua", fetch)
        assert link.url == "https://cdn/f"
        assert calls == 1


class TestStats:
    async def test_hit_rate_tracked(self, cache: LinkCache) -> None:
        await cache.get(1, "ua")  # miss
        await cache.put(1, "ua", "https://cdn/f", int(time.time()) + 3600)
        await cache.get(1, "ua")  # hit
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5
