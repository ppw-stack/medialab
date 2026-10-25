"""辅种孤儿文件统计 service（文件在、任务没了）。

**孤儿** = 磁盘上有、但下载器里已经没有对应任务的文件/目录。

典型来源：用「仅删任务」的方式清理种子时，磁盘文件会保留下来——
它们不再被做种，却一直占着空间（辅种站实测 21 个孤儿占 527 GB）。

为什么需要挂载磁盘：
    下载器**没有**浏览文件系统的接口——Transmission 的 RPC spec 不提供
    filesystem 浏览，qBittorrent 也没有列目录的 API。所以只能靠把辅种站
    的下载目录**只读挂载**进容器，然后扫盘 + 与下载器任务做顶层名匹配。

匹配算法：
    下载器任务的 ``files[].name``（TR）或 ``content_path`` 相对
    ``save_path`` 的首段（qB）都是**相对下载目录的顶层名**：

    - 磁盘有、任务无 → **孤儿**
    - 任务有、磁盘无 → 文件丢失（现有的异常检测已覆盖，不在此列）

**口径是保守下界**：同名但不同种子的目录会漏判（磁盘有、任务无，但同名
任务存在 → 不算孤儿）。数字只会偏少，不会偏多。

**只统计不删除**——527 GB 的误删代价太高，清理由用户自己决定。
"""
from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "OrphanEntry",
    "OrphanScanResult",
    "failed_scan",
    "scan_orphans",
]


@dataclass(frozen=True, slots=True)
class OrphanEntry:
    """一个孤儿条目（顶层目录或文件）。"""

    #: 条目名（顶层）
    name: str
    #: 绝对路径
    path: str
    #: 占用字节数
    bytes: int
    #: 包含的文件数
    file_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "bytes": self.bytes,
            "file_count": self.file_count,
        }


@dataclass(slots=True)
class OrphanScanResult:
    """一次扫描的结果。"""

    #: 孤儿条目
    orphans: list[OrphanEntry] = field(default_factory=list)
    #: 扫描目录里的顶层条目总数
    total_count: int = 0
    #: 扫描目录的总字节数
    total_bytes: int = 0
    #: 扫描失败的原因（None = 成功）
    error: str | None = None

    @property
    def orphan_count(self) -> int:
        return len(self.orphans)

    @property
    def orphan_bytes(self) -> int:
        return sum(o.bytes for o in self.orphans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orphans": [o.to_dict() for o in self.orphans],
            "orphan_count": self.orphan_count,
            "orphan_bytes": self.orphan_bytes,
            "total_count": self.total_count,
            "total_bytes": self.total_bytes,
            "error": self.error,
        }


def failed_scan(reason: str) -> OrphanScanResult:
    """构造一个「扫描失败」的结果（结构与成功时一致）。"""
    return OrphanScanResult(error=reason)


def _dir_size(path: Path) -> tuple[int, int]:
    """目录的总字节数与文件数。

    不可读的子项**跳过**（不抛）——NAS 上偶尔有权限受限的目录，
    一个读不到的文件不该让整次扫描失败。
    """
    total = 0
    count = 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    count += 1
                except OSError:
                    continue
    except OSError:
        return total, count
    return total, count


def _top_level_names(torrents: list[dict[str, Any]]) -> set[str]:
    """下载器任务占用的**顶层**名字集合。"""
    names: set[str] = set()
    for t in torrents:
        for name in t.get("top_names") or []:
            if name:
                names.add(str(name))
    return names


def scan_orphans(
    torrents: list[dict[str, Any]],
    scan_dir: str | None,
) -> OrphanScanResult:
    """扫描目录，找出「磁盘有、任务无」的孤儿条目。

    :param torrents: :meth:`DownloaderAdapter.list_torrents_with_errors` 的
        返回值（需含 ``top_names`` 字段）。
    :param scan_dir: 要扫的目录（容器内路径）。None / 不存在 / 不可读
        → 返回带 ``error`` 的结果（**不抛**——扫描失败不该让作业变红）。

    :returns: :class:`OrphanScanResult`
    """
    if not scan_dir:
        return OrphanScanResult(
            error="未配置扫描目录（辅种下载目录未挂载到容器）"
        )
    root = Path(scan_dir)
    if not root.is_dir():
        return OrphanScanResult(
            error=f"扫描目录不存在或不可读：{scan_dir}"
        )

    task_names = _top_level_names(torrents)

    try:
        entries = sorted(p for p in root.iterdir())
    except OSError as exc:
        return OrphanScanResult(error=f"读取目录失败：{exc}")

    orphans: list[OrphanEntry] = []
    total_bytes = 0
    total_count = 0
    for entry in entries:
        total_count += 1
        if entry.name in task_names:
            # 任务还在 —— 不算孤儿（但它的大小要计入总量）
            if entry.is_dir():
                size, _cnt = _dir_size(entry)
                total_bytes += size
            else:
                with suppress(OSError):
                    total_bytes += entry.stat().st_size
            continue
        # 磁盘有、任务无 → 孤儿
        if entry.is_dir():
            size, count = _dir_size(entry)
        else:
            try:
                size = entry.stat().st_size
                count = 1
            except OSError:
                size, count = 0, 0
        total_bytes += size
        orphans.append(
            OrphanEntry(
                name=entry.name,
                path=str(entry),
                bytes=size,
                file_count=count,
            )
        )

    orphans.sort(key=lambda o: -o.bytes)
    logger.info(
        "孤儿扫描完成：%s —— %d 个顶层条目，%d 个孤儿（%.2f GB / 共 %.2f GB）",
        scan_dir,
        total_count,
        len(orphans),
        sum(o.bytes for o in orphans) / 1024**3,
        total_bytes / 1024**3,
    )
    return OrphanScanResult(
        orphans=orphans,
        total_count=total_count,
        total_bytes=total_bytes,
    )
