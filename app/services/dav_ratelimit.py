"""按 IP / 按用户的**入站**请求数限流。

与 :mod:`app.core.rate_limit` 的区别：那个管**出站**（调网盘 API 的
频率，保护网盘账号）；这个管**入站**（保护本服务不被刷爆）。

**为什么需要**：WebDAV 暴露后，攻击者可以：

- 用字典爆破 DAV 密码
- 疯狂发 PROPFIND 把磁盘 IO 打满
- 反复请求大文件把带宽打满

**令牌桶**：允许突发（Infuse 扫库会连发几百个 PROPFIND），但长期速率受限。

**按维度分桶**：``(维度, 键)`` 各一个桶 —— 面板与 DAV 分开计，DAV 与用户
分开计。面板是人点（60/min 够），DAV 是客户端扫库（需要 300/min 的突发
额度）。

**不限流**：配额为 0 时直接放行（配置项允许关掉）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    """单个键的令牌桶。"""

    #: 当前令牌数
    tokens: float
    #: 上次补充令牌的时刻
    last_refill: float = field(default_factory=time.monotonic)

    def refill(self, rate_per_sec: float, capacity: float, now: float) -> None:
        """按时间补充令牌。"""
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(capacity, self.tokens + elapsed * rate_per_sec)
            self.last_refill = now

    def take(self, amount: float = 1.0) -> bool:
        """取令牌；够则返回 True。"""
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class DavRateLimiter:
    """按 ``(维度, 键)`` 的令牌桶限流器。

    **进程内**（与认证器一致）—— 重启即清空，符合预期。
    """

    #: 维度常量
    DIM_IP = "ip"
    DIM_USER = "user"

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        #: 被限流拒绝的次数（监控用）
        self.rejected = 0

    def allow(
        self,
        dimension: str,
        key: str,
        per_minute: int,
        *,
        now: float | None = None,
    ) -> bool:
        """检查是否放行。

        本方法是**同步**的 —— 令牌桶不需要 await，且在 event loop 里
        执行只是几次算术运算（无 IO），不会造成阻塞。

        Args:
            dimension: 维度（``ip`` / ``user``）。不同维度独立计数。
            key: 该维度的键（IP 地址 / 用户名）。
            per_minute: 每分钟配额；0 = 不限流。
            now: 当前时刻（测试打桩用）。

        Returns:
            True = 放行；False = 超出配额（应返回 429）。
        """
        if per_minute <= 0:
            return True

        current = time.monotonic() if now is None else now
        rate = per_minute / 60.0
        capacity = float(per_minute)

        bucket_key = (dimension, key)
        bucket = self._buckets.get(bucket_key)
        if bucket is None:
            bucket = _Bucket(tokens=capacity, last_refill=current)
            self._buckets[bucket_key] = bucket

        bucket.refill(rate, capacity, current)
        if bucket.take():
            return True
        self.rejected += 1
        return False

    def purge_idle(self, idle_seconds: float = 600.0) -> int:
        """清理长时间未活动的桶，返回清理条数。

        防止长期运行后桶字典无限增长（每个来过的 IP / 用户名一个桶）。
        """
        cutoff = time.monotonic() - idle_seconds
        stale = [key for key, bucket in self._buckets.items() if bucket.last_refill < cutoff]
        for key in stale:
            del self._buckets[key]
        return len(stale)

    def reset(self) -> None:
        """清空所有桶（测试用）。"""
        self._buckets.clear()
        self.rejected = 0

    def stats(self) -> dict[str, int]:
        """限流器状态（监控用）。"""
        return {
            "buckets": len(self._buckets),
            "rejected": self.rejected,
        }


#: 进程级限流器
dav_rate_limiter = DavRateLimiter()
