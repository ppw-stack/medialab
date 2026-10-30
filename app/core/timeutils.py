"""时间工具。

免费窗口看护（FR-3.0 / FR-3.4）全部依赖时间计算，任何隐式时区转换都是
事故来源，因此本模块强制 **北京时间字符串内部表示**：

- 数据库与业务逻辑内部一律使用 **北京时间字符串**（``yyyy-MM-dd HH:mm:ss``）。
  DB 时间列（``created_at`` / ``updated_at`` / ``last_*_at`` / ``probed_at`` /
  ``mtime`` 等）一律用 :func:`bj_now` 写入；协议侧时刻（``link_cache.expires_at``
  等）保留 unix 秒，在 service 层消费时用 :func:`format_bj` 转换。
- 展示层直接使用字符串，无需额外时区转换。
- naive datetime 一律视为北京时间（见 :func:`to_bj`），绝不隐式猜测。

历史：FR-3.0.2 的站点时钟偏差 :class:`ClockOffset` 随 FR-3.0 截止时间模型
一起下线（实现见 :mod:`app.services.monitor`，走 ``torrent_index.free_end_at``）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

#: 北京时区
BJ_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

#: 时间字符串格式
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def bj_now() -> str:
    """当前北京时间字符串（``yyyy-MM-dd HH:mm:ss``）。"""
    return datetime.now(BJ_TZ).strftime(_TIME_FORMAT)


def bj_now_dt() -> datetime:
    """当前北京时间 aware datetime（用于 timedelta 运算）。"""
    return datetime.now(BJ_TZ)


def parse_bj(s: str | None) -> datetime | None:
    """北京时间字符串 → aware 北京时间 datetime。"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), _TIME_FORMAT).replace(tzinfo=BJ_TZ)
    except ValueError:
        return None


def format_bj(dt: datetime) -> str:
    """aware datetime → 北京时间字符串。"""
    return dt.astimezone(BJ_TZ).strftime(_TIME_FORMAT)


def to_bj(dt: datetime) -> str:
    """任意 datetime → 北京时间字符串。naive 输入视为北京时间。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJ_TZ)
    return dt.astimezone(BJ_TZ).strftime(_TIME_FORMAT)
