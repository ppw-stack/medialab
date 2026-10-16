"""时间工具。

免费窗口看护（FR-3.0 / FR-3.4）全部依赖时间计算，任何隐式时区转换都是
事故来源，因此本模块强制 **北京时间字符串内部表示 + 显式站点偏移**：

- 数据库与业务逻辑内部一律使用 **北京时间字符串**（``yyyy-MM-dd HH:mm:ss``）。
- 展示层直接使用字符串，无需额外时区转换。
- 站点时钟偏差以 :class:`ClockOffset` 显式建模（FR-3.0.2），绝不隐式猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


@dataclass(frozen=True, slots=True)
class ClockOffset:
    """站点与本机的时钟偏差（FR-3.0.2）。

    定义为 ``clock_offset = 站点时间 - 本地时间``。因此站点声明的剩余
    时长换算为绝对时刻时：

    ``free_end_at = bj_now_dt() + declared_remaining - clock_offset``

    正值表示站点时钟偏快，需减掉。
    """

    offset: timedelta = timedelta(0)
    probed_at: str | None = None

    @classmethod
    def zero(cls) -> ClockOffset:
        return cls()

    def apply(self, local_now: datetime | None = None) -> datetime:
        """站点当前时间。"""
        base = local_now or bj_now_dt()
        return base + self.offset

    def remaining_to_absolute(
        self, declared: timedelta, *, local_now: datetime | None = None
    ) -> str:
        """站点声明的"剩余时长" → 北京时间字符串。"""
        base = local_now or bj_now_dt()
        return format_bj(base + declared - self.offset)

    @property
    def seconds(self) -> int:
        return int(self.offset.total_seconds())

    @classmethod
    def from_probe(
        cls, site_time: datetime, local_time: datetime
    ) -> ClockOffset:
        """由"站点响应时间 - 本地时间"构造（FR-3.0.2 第 1 条）。"""
        return cls(
            offset=_to_bj_dt(site_time) - _to_bj_dt(local_time),
            probed_at=bj_now(),
        )

    def manual_adjust(self, delta: timedelta) -> ClockOffset:
        """站点级手动校正（FR-3.0.2 第 2 条）。"""
        return ClockOffset(offset=self.offset + delta, probed_at=bj_now())


def _to_bj_dt(dt: datetime) -> datetime:
    """任意 datetime → aware 北京时间 datetime。naive 视为北京时间。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BJ_TZ)
    return dt.astimezone(BJ_TZ)


def remaining_until(end_at: str | None, *, now: str | None = None) -> timedelta | None:
    """剩余时长；负值表示已过期。永久免费（``None``）返回 ``None``。"""
    if end_at is None:
        return None
    end_dt = parse_bj(end_at)
    if end_dt is None:
        return None
    now_dt = parse_bj(now) if now else bj_now_dt()
    if now_dt is None:
        return None
    return end_dt - now_dt


def parse_duration_seconds(text: str | int | float | timedelta | None) -> timedelta | None:
    """解析时长：支持 ``72h`` / ``30m`` / ``2d`` / 秒数 / timedelta。"""
    if text is None:
        return None
    if isinstance(text, timedelta):
        return text
    if isinstance(text, bool):
        raise ValueError(f"时长格式非法：{text}")
    if isinstance(text, int | float):
        return timedelta(seconds=float(text))
    raw = str(text).strip().lower()
    if not raw:
        return None
    total = 0.0
    num = ""
    seen = False
    for ch in raw:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in {"h", "m", "s", "d", "w"}:
            if not num:
                raise ValueError(f"时长格式非法：{text}")
            value = float(num)
            factor = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[ch]
            total += value * factor
            num = ""
            seen = True
        else:
            raise ValueError(f"时长格式非法：{text}")
    if num:
        total += float(num)
        seen = True
    if not seen:
        raise ValueError(f"时长格式非法：{text}")
    return timedelta(seconds=total)
