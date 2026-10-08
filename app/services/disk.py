"""磁盘空间检查（FR-2.3 / FR-4.1 / FR-5.3）。

辅种前必须确认目标分区有足够空间（条目大小 × 1.1），否则磁盘写满会连带
拖垮做种——这是 §1.3 硬性红线的直接体现：系统自身行为不得影响下载器做种。

**跨挂载点的陷阱**：``os.statvfs(save_path)`` 要求路径已存在。候选路径
通常是"将要创建"的，因此这里逐级向上找到最近的已存在父目录再查询。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: FR-2.3 要求的余量倍数（条目大小 × 1.1）
DISK_HEADROOM_RATIO = 1.1

#: 低于此值触发 🔴 磁盘告警（FR-4.1）
LOW_WATER_DEFAULT = 50 * 1024**3
#: 清理回升目标（FR-5.3）
HIGH_WATER_DEFAULT = 100 * 1024**3


@dataclass(frozen=True, slots=True)
class DiskInfo:
    """分区空间快照。"""

    path: str
    total: int
    free: int
    #: 实际查询的目录（可能是路径的某个已存在父目录）
    probed: str

    @property
    def used_ratio(self) -> float:
        if self.total <= 0:
            return 1.0
        return (self.total - self.free) / self.total


@dataclass(frozen=True, slots=True)
class SpaceVerdict:
    """空间判定。"""

    ok: bool
    required: int
    free: int
    #: 是否因无法探测而放行（保守策略：宁可少辅种也不写满磁盘）
    probed: str | None = None

    @property
    def reason(self) -> str | None:
        if self.ok:
            return None
        if self.probed is None:
            return "disk_unprobeable"
        return "disk_full"


def _nearest_existing(path: str) -> Path | None:
    """逐级向上找最近的已存在目录。"""
    p = Path(path)
    for candidate in (p, *p.parents):
        if candidate.is_dir():
            return candidate
    return None


def probe(path: str) -> DiskInfo | None:
    """探测路径所在分区空间。路径不存在时向上找到已存在父目录。"""
    resolved = _nearest_existing(path)
    if resolved is None:
        return None
    try:
        st = os.statvfs(resolved)
    except OSError:
        return None
    # f_frsize 比 f_bsize 更可靠：前者是实际分配块大小
    block = st.f_frsize or st.f_bsize
    return DiskInfo(
        path=path,
        total=st.f_blocks * block,
        free=st.f_bavail * block,
        probed=str(resolved),
    )


def check_space(save_path: str, entry_size: int) -> SpaceVerdict:
    """FR-2.3 磁盘校验：可用空间 ≥ 条目大小 × 1.1。

    无法探测时**拒绝**该条目并记录——保守优先于激进，避免把磁盘写满。
    """
    required = int(entry_size * DISK_HEADROOM_RATIO)
    info = probe(save_path)
    if info is None:
        return SpaceVerdict(ok=False, required=required, free=0, probed=None)
    return SpaceVerdict(
        ok=info.free >= required, required=required, free=info.free, probed=info.probed
    )


def is_low_water(path: str, threshold: int = LOW_WATER_DEFAULT) -> bool:
    """是否低于低水位（FR-5.3），低于则告警升级 🔴 并触发清理。"""
    info = probe(path)
    return info is not None and info.free < threshold


def gap_to(
    path: str, low: int = LOW_WATER_DEFAULT, high: int = HIGH_WATER_DEFAULT
) -> int:
    """从低水位回升到高水位所需释放的字节数（FR-5.3）。

    未低于低水位时返回 0。
    """
    info = probe(path)
    if info is None or info.free >= low:
        return 0
    return max(high - info.free, 0)