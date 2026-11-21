"""移植代码抛出的限流异常。"""

from __future__ import annotations


class RateLimitExceededException(RuntimeError):
    """站点请求额度耗尽（本地或远端冷却）。"""

    def __init__(self, message: str = "站点请求过于频繁") -> None:
        super().__init__(message)
        self.message = message
