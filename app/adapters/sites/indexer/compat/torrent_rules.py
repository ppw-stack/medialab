"""限流提示识别（hdarea parser 依赖）。"""

from __future__ import annotations

import re
from typing import Optional

_RATE_RE = re.compile(
    r"(429|too many requests|rate limit|频繁|请稍后|稍后再试|403 forbidden)",
    re.IGNORECASE,
)


def rate_limit_cooldown(message: str | None) -> Optional[int]:
    """从错误文案识别限流，返回建议冷却秒数；未命中返回 ``None``。"""
    if not message:
        return None
    return 3600 if _RATE_RE.search(str(message)) else None
