"""时间工具。

免费窗口看护（FR-3.0 / FR-3.4）全部依赖时间计算，任何隐式时区转换都是
事故来源，因此本模块强制 **naive UTC 内部表示 + 显式站点偏移**：

- 数据库与业务逻辑内部一律使用 **UTC**（naive，由 SQLite 约定承载）。
- 展示层在 FR-6.3 按配置时区渲染。
- 站点时钟偏差以 :class:`ClockOffset` 显式建模（FR-3.0.2），绝不隐式猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: 系统内部统一使用 UTC
UTC_TZ = UTC


def utcnow() -> datetime:
    """当前 UTC 时间（naive，秒级精度足够看护场景）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc(dt: datetime) -> datetime:
    """任意 datetime → naive UTC。naive 输入视为已是 UTC。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class ClockOffset:
    """站点与本机的时钟偏差（FR-3.0.2）。

    定义为 ``clock_offset = 站点时间 - 本地时间``。因此站点声明的剩余
    时长换算为绝对时刻时：

    ``free_end_at = utcnow() + declared_remaining - clock_offset``

    正值表示站点时钟偏快，需减掉。
    """

    offset: timedelta = timedelta(0)
    probed_at: datetime | None = None

    @classmethod
    def zero(cls) -> ClockOffset:
        return cls()

    def apply(self, local_now: datetime | None = None) -> datetime:
        """站点当前时间。"""
        base = local_now or utcnow()
        return base + self.offset

    def remaining_to_absolute(
        self, declared: timedelta, *, local_now: datetime | None = None
    ) -> datetime:
        """站点声明的"剩余时长" → 绝对 UTC 结束时刻。"""
        base = local_now or utcnow()
        return base + declared - self.offset

    @property
    def seconds(self) -> int:
        return int(self.offset.total_seconds())

    @classmethod
    def from_probe(
        cls, site_time: datetime, local_time: datetime
    ) -> ClockOffset:
        """由"站点响应时间 - 本地时间"构造（FR-3.0.2 第 1 条）。"""
        return cls(
            offset=to_utc(site_time) - to_utc(local_time),
            probed_at=to_utc(local_time),
        )

    def manual_adjust(self, delta: timedelta) -> ClockOffset:
        """站点级手动校正（FR-3.0.2 第 2 条）。"""
        return ClockOffset(offset=self.offset + delta, probed_at=utcnow())


def remaining_until(end_at: datetime | None, *, now: datetime | None = None) -> timedelta | None:
    """剩余时长；负值表示已过期。永久免费（``None``）返回 ``None``。"""
    if end_at is None:
        return None
    return to_utc(end_at) - (now or utcnow())


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