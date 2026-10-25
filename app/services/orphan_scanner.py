"""辅种孤儿文件统计 service（文件在、任务没了）。

**孤儿** = 磁盘上有、但下载器里已经没有对应任务的文件/目录。

典型来源：用「仅删任务」的方式清理种子时，磁盘文件会保留下来——
它们不再被做种，却一直占着空间。

为什么需要挂载磁盘：
    下载器**没有**浏览文件系统的接口——Transmission 的 RPC spec 不提供
    filesystem 浏览，qBittorrent 也没有列目录的 API。所以只能靠把辅种站
    的下载目录**只读挂载**进容器，然后扫盘 + 与下载器任务做名称匹配。

匹配算法（按 downloadDir 分组）：
    辅种站的 Transmission 有多个 ``downloadDir``（实测：``辅种`` 4639 个
    种子 / 父目录 5 个 / ``game`` 1 个）。每个种子的 ``files[].name`` 是
    **相对它自己 downloadDir** 的路径，所以：

    - 顶层名（``files[].name`` 的第一段 / qB ``content_path`` 相对
      ``save_path`` 的首段）只在**同一个 downloadDir 内**才有意义
    - 扫描某个配置目录时，**只把 downloadDir 等于该目录的任务**算作
      「有主」——否则父目录的任务会把辅种目录里的同名条目误认有主

    对每个配置目录 D：
        有主 = { 顶层名(t) | t 的 downloadDir == D }
        孤儿 = { D 下的顶层条目 } - 有主

    **空目录也算孤儿**（0 字节）——它们是「任务没了的残留」，清理掉能
    让目录干净；只是**不占空间**，故占用统计里是 0。

**只统计不删除**——孤儿占用的空间很大，误删代价太高，清理由用户自己决定。
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
    "parse_scan_dirs",
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
    #: 包含的文件数（0 = 空目录）
    file_count: int
    #: 该条目属于哪个扫描目录（downloadDir）
    scan_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "bytes": self.bytes,
            "file_count": self.file_count,
            "scan_dir": self.scan_dir,
        }


@dataclass(slots=True)
class OrphanScanResult:
    """一次扫描的结果（跨多个配置目录）。"""

    #: 孤儿条目
    orphans: list[OrphanEntry] = field(default_factory=list)
    #: 扫描目录里的顶层条目总数
    total_count: int = 0
    #: 扫描目录的总字节数
    total_bytes: int = 0
    #: 空目录（0 文件）孤儿数——它们是残留但不占空间
    empty_dir_count: int = 0
    #: 实际扫描的目录（逐个的统计）
    scanned_dirs: list[dict[str, Any]] = field(default_factory=list)
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
            "empty_dir_count": self.empty_dir_count,
            "total_count": self.total_count,
            "total_bytes": self.total_bytes,
            "scanned_dirs": self.scanned_dirs,
            "error": self.error,
        }


def failed_scan(reason: str) -> OrphanScanResult:
    """构造一个「扫描失败」的结果（结构与成功时一致）。"""
    return OrphanScanResult(error=reason)


def parse_scan_dirs(raw: str) -> list[str]:
    """把逗号分隔的配置串解析成目录列表（去掉空项与重复）。

    支持带空格的 ``"/a, /b"``（用户手写时容易加空格）。
    """
    out: list[str] = []
    for part in (raw or "").split(","):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


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


def _top_names_by_dir(torrents: list[dict[str, Any]]) -> dict[str, set[str]]:
    """按 downloadDir 分组，收集每组占用的**顶层名**。

    分组键用 ``save_path``（= TR 的 ``downloadDir`` / qB 的 ``save_path``）
    —— 顶层名只在同一个 downloadDir 内才有意义。
    """
    out: dict[str, set[str]] = {}
    for t in torrents:
        save_path = str(t.get("save_path") or "")
        names = out.setdefault(save_path, set())
        for name in t.get("top_names") or []:
            if name:
                names.add(str(name))
    return out


def _match_dir(a: str, b: str) -> bool:
    """两个 downloadDir 是否指向同一个目录（忽略尾部斜杠）。

    路径**必须完全一致**才算同一个——不做前缀匹配（前缀匹配会把
    ``/downloads`` 和 ``/downloads/辅种`` 混为一谈，导致辅种目录里的
    条目被父目录的任务误认有主）。
    """
    return a.rstrip("/") == b.rstrip("/")


def scan_orphans(
    torrents: list[dict[str, Any]],
    scan_dirs: list[str],
) -> OrphanScanResult:
    """扫描配置的目录，找出「磁盘有、任务无」的孤儿条目。

    :param torrents: :meth:`DownloaderAdapter.list_torrents_with_errors` 的
        返回值（需含 ``save_path`` 与 ``top_names`` 字段）。
    :param scan_dirs: 要扫描的目录列表（= 下载器的 downloadDir 列表）。
        空 / 不存在 / 不可读 → 记 error（**不抛**——扫描失败不该让作业
        变红）。

    :returns: :class:`OrphanScanResult`
    """
    if not scan_dirs:
        return OrphanScanResult(
            error="未配置扫描目录（辅种下载目录未挂载到容器）"
        )

    names_by_dir = _top_names_by_dir(torrents)

    orphans: list[OrphanEntry] = []
    scanned_dirs: list[dict[str, Any]] = []
    total_bytes = 0
    total_count = 0
    empty_dir_count = 0
    errors: list[str] = []

    for scan_dir in scan_dirs:
        root = Path(scan_dir)
        if not root.is_dir():
            errors.append(f"扫描目录不存在或不可读：{scan_dir}")
            continue

        # 只把 downloadDir 等于该目录的任务算作「有主」
        owned: set[str] = set()
        for d, names in names_by_dir.items():
            if _match_dir(d, scan_dir):
                owned |= names

        try:
            entries = sorted(p for p in root.iterdir())
        except OSError as exc:
            errors.append(f"读取目录失败（{scan_dir}）：{exc}")
            continue

        dir_orphan_count = 0
        dir_orphan_bytes = 0
        dir_empty_count = 0
        dir_total_count = 0
        for entry in entries:
            total_count += 1
            dir_total_count += 1
            if entry.name in owned:
                # 任务还在 —— 不算孤儿（但它的大小要计入总量）
                if entry.is_dir():
                    size, _cnt = _dir_size(entry)
                    total_bytes += size
                else:
                    with suppress(OSError):
                        total_bytes += entry.stat().st_size
                continue
            # 磁盘有、任务无 → 孤儿（**空目录也算**）
            if entry.is_dir():
                size, count = _dir_size(entry)
            else:
                try:
                    size = entry.stat().st_size
                    count = 1
                except OSError:
                    size, count = 0, 0
            total_bytes += size
            dir_orphan_count += 1
            dir_orphan_bytes += size
            if count == 0:
                # 空目录（0 文件）——残留但**不占空间**
                empty_dir_count += 1
                dir_empty_count += 1
            orphans.append(
                OrphanEntry(
                    name=entry.name,
                    path=str(entry),
                    bytes=size,
                    file_count=count,
                    scan_dir=scan_dir,
                )
            )

        scanned_dirs.append({
            "scan_dir": scan_dir,
            "total_count": dir_total_count,
            "orphan_count": dir_orphan_count,
            "orphan_bytes": dir_orphan_bytes,
            "empty_dir_count": dir_empty_count,
            "owned_count": len(owned),
        })

    orphans.sort(key=lambda o: -o.bytes)
    logger.info(
        "孤儿扫描完成：%d 个目录 —— %d 个顶层条目，%d 个孤儿"
        "（%d 个空目录，%.2f GB / 共 %.2f GB）",
        len(scanned_dirs),
        total_count,
        len(orphans),
        empty_dir_count,
        sum(o.bytes for o in orphans) / 1024**3,
        total_bytes / 1024**3,
    )
    result = OrphanScanResult(
        orphans=orphans,
        total_count=total_count,
        total_bytes=total_bytes,
        empty_dir_count=empty_dir_count,
        scanned_dirs=scanned_dirs,
    )
    if errors and not scanned_dirs:
        # 一个目录都没扫成 → 整体失败（前端据此显示原因）
        result.error = "；".join(errors)
    elif errors:
        # 部分目录失败 → 记下来但不阻断
        result.error = "；".join(errors)
    return result
