"""单位解析。

限速配置在文档与界面里写作 ``20MB`` / ``100M`` / ``1.5GB``，而下载器 API
要的是字节/秒。这类转换散落各处极易出错（十进制 vs 二进制），故集中在此。
"""

from __future__ import annotations

import re

_UNITS: dict[str, int] = {
    "": 1,
    "B": 1,
    "K": 1000,
    "KB": 1000,
    "M": 1000**2,
    "MB": 1000**2,
    "G": 1000**3,
    "GB": 1000**3,
    "T": 1000**4,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}

_PATTERN = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]*)\s*$")


def parse_speed(value: str | int | None) -> int | None:
    """``"20MB"`` → ``20000000``。解析失败返回 ``None``（调用方跳过比较）。"""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = _PATTERN.match(str(value))
    if not m:
        return None
    num, unit = m.group(1), m.group(2).upper()
    try:
        base = float(num)
    except ValueError:
        return None
    factor = _UNITS.get(unit)
    if factor is None:
        return None
    return int(base * factor)


def format_speed(bps: int | None) -> str:
    """字节/秒 → 人类可读。"""
    if not bps:
        return "0"
    for unit, factor in (
        ("GB/s", 1000**3),
        ("MB/s", 1000**2),
        ("KB/s", 1000),
    ):
        if bps >= factor:
            return f"{bps / factor:.1f}{unit}"
    return f"{bps}B/s"


def format_size(num_bytes: int | None) -> str:
    """字节 → 人类可读（二进制，用于磁盘/体积展示）。"""
    if not num_bytes:
        return "0B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"
