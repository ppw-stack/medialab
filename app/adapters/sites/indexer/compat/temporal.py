"""日期时间工具（MoviePilot ``app.foundation.temporal`` 的精简移植）。

只保留移植解析器实际使用的三个入口：
``normalize_datetime`` / ``parse_datetime`` / ``format_timestamp``。
"""

from __future__ import annotations

import datetime
from typing import Any, Optional, Union

try:  # pragma: no cover - 环境差异
    from dateutil import parser as _date_parser
except Exception:  # pragma: no cover
    _date_parser = None

#: 站点常见的绝对时间格式
_COMMON_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)


def parse_datetime(value: Any) -> Optional[datetime.datetime]:
    """把常见日期文本解析为 ``datetime``；无法解析返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if _date_parser is not None:
        try:
            return _date_parser.parse(text)
        except (TypeError, ValueError, OverflowError):
            return None
    for fmt in _COMMON_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_datetime(value: Optional[str]) -> Optional[str]:
    """把绝对/相对日期文本统一为 ``YYYY-MM-DD HH:MM:SS``。

    解析失败时**原样返回**（与 MoviePilot 行为一致）——调用方按
    :func:`app.adapters.sites.base.parse_abs_datetime` 再校验一次。
    """
    if not value:
        return value
    parsed = parse_datetime(value)
    if parsed is None:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def format_timestamp(
    timestamp: Optional[Union[str, int, float]],
    date_format: str = "%Y-%m-%d %H:%M:%S",
) -> Optional[str]:
    """把 unix 时间戳格式化为字符串；非数字原样返回。"""
    if timestamp is None:
        return None
    if isinstance(timestamp, str):
        return timestamp
    try:
        return datetime.datetime.fromtimestamp(float(timestamp)).strftime(date_format)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
