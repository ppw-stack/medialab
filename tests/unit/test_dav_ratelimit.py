"""WebDAV 限流器的单测。

覆盖：令牌桶补充、突发额度、维度独立、0 = 不限流、闲置清理。
"""

from __future__ import annotations

from app.services.dav_ratelimit import DavRateLimiter


def test_allows_within_quota():
    """配额内全部放行。"""
    rl = DavRateLimiter()
    for _ in range(10):
        assert rl.allow("ip", "1.2.3.4", 10)


def test_blocks_over_quota():
    """超出配额被拒。"""
    rl = DavRateLimiter()
    for _ in range(10):
        assert rl.allow("ip", "1.2.3.4", 10)
    assert not rl.allow("ip", "1.2.3.4", 10)
    assert rl.stats()["rejected"] == 1


def test_zero_quota_means_unlimited():
    """配额 0 = 不限流。"""
    rl = DavRateLimiter()
    for _ in range(1000):
        assert rl.allow("ip", "1.2.3.4", 0)
    assert rl.stats()["buckets"] == 0  # 不限流时根本不建桶


def test_dimensions_are_independent():
    """不同维度独立计数 —— IP 限额不影响用户维度。"""
    rl = DavRateLimiter()
    for _ in range(5):
        assert rl.allow("ip", "1.2.3.4", 5)
    assert not rl.allow("ip", "1.2.3.4", 5)

    # 用户维度还有自己的额度
    assert rl.allow("user", "alice", 5)


def test_keys_are_independent():
    """同一维度不同键独立计数。"""
    rl = DavRateLimiter()
    for _ in range(5):
        assert rl.allow("ip", "1.2.3.4", 5)
    assert rl.allow("ip", "5.6.7.8", 5)


def test_tokens_refill_over_time():
    """时间流逝后令牌补充回来。"""
    rl = DavRateLimiter()
    now = 1000.0
    for _ in range(10):
        assert rl.allow("ip", "1.2.3.4", 10, now=now)
    assert not rl.allow("ip", "1.2.3.4", 10, now=now)

    # 60 秒后（1 分钟）应补满 10 个令牌
    assert rl.allow("ip", "1.2.3.4", 10, now=now + 60)


def test_partial_refill():
    """部分补充 —— 30 秒补回一半的令牌（10/min → 5 个）。"""
    rl = DavRateLimiter()
    now = 1000.0
    for _ in range(10):
        rl.allow("ip", "1.2.3.4", 10, now=now)
    assert not rl.allow("ip", "1.2.3.4", 10, now=now)

    # 30 秒 → 补 5 个令牌 → 还能放行 5 次
    for _ in range(5):
        assert rl.allow("ip", "1.2.3.4", 10, now=now + 30)
    # 第 6 次没有令牌了
    assert not rl.allow("ip", "1.2.3.4", 10, now=now + 30)


def test_purge_idle():
    """闲置的桶被清理。"""
    rl = DavRateLimiter()
    rl.allow("ip", "1.2.3.4", 10)
    assert rl.stats()["buckets"] == 1
    assert rl.purge_idle(idle_seconds=0.0) == 1
    assert rl.stats()["buckets"] == 0


def test_reset():
    """reset 清空一切。"""
    rl = DavRateLimiter()
    rl.allow("ip", "1.2.3.4", 1)
    rl.reset()
    assert rl.stats() == {"buckets": 0, "rejected": 0}
    assert rl.allow("ip", "1.2.3.4", 1)
