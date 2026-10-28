"""辅种孤儿文件**删除** service。

**不可逆**——删的是 NAS 上的实际数据。本模块把风险控制放在代码里，
而不是只靠前端弹框。

安全设计（三道闸）：

1. **只在配置的扫描目录内删**
   路径必须落在 :func:`app.services.orphan_scanner.parse_scan_dirs`
   解析出的目录里，且必须是其**直接子项**——不能是任意路径。
   防止「路径穿越」把 NAS 上的其它目录删掉。

2. **二次扫描复核**（最重要）
   删除前**重新拉一遍**所有下载器的任务 + 重新扫盘，确认这些条目
   **此刻**仍然无主。快照可能是几小时前的（作业每天一次）——期间
   用户可能已经重新添加了任务、或别的下载器接管了这些数据。
   复核不通过 → 跳过该条目并记原因。

3. **审计日志**
   删磁盘数据不可逆 → 必须能回答「删了什么」。逐条记
   路径 + 大小 + 文件数 + 时间。

**逐条删除，单条失败不中断其余条目**——尽可能多删，失败数计入结果。

**权限失败单独识别**（2026-10-28 加入）：删磁盘数据时若 NAS 端目录属主
非挂载账号、权限位不含写，会报 ``Permission denied``（errno 13）。这种
失败**重试无意义**——需用户在 NAS 端改属主（chown）或权限（chmod g+w）
或更换挂载账号。本模块把这类失败单独计入 ``permission_denied``，
``error`` 文案给出可行动提示，与普通失败（文件被占用、临时不可写等）
区分开。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.downloaders.base import TorrentFileNames
from app.core.logging import get_logger
from app.services.orphan_scanner import match_dir, top_names_by_dir

logger = get_logger(__name__)

__all__ = [
    "OrphanDeleteResult",
    "delete_orphans",
    "verify_deletable",
]


@dataclass(slots=True)
class OrphanDeleteResult:
    """一次孤儿删除的结果。"""

    #: 请求删除的条目数
    requested: int = 0
    #: 成功删除
    succeeded: int = 0
    #: 失败（权限 / 文件被占用等）
    failed: int = 0
    #: **复核不通过而跳过**（期间已不再是无主条目）
    skipped: int = 0
    #: 释放的字节数
    freed_bytes: int = 0
    #: 逐条结果明细
    items: list[dict[str, Any]] = field(default_factory=list)
    #: 整体错误（如目录不可写）
    error: str | None = None
    #: **因权限不足**失败的条目数（``Permission denied``，errno 13）。
    #:
    #: 单独计数是因为权限问题与普通失败的处理方式不同——前者需要用户
    #: 去 NAS 端改属主或换挂载账号，重试无意义；后者可能只是临时占用，
    #: 重试可能成功。前端据此给出不同的提示文案。
    permission_denied: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "freed_bytes": self.freed_bytes,
            "items": self.items,
            "error": self.error,
            "permission_denied": self.permission_denied,
        }


def _is_child_of(path: Path, root: Path) -> bool:
    """``path`` 是否是 ``root`` 的**直接子项**（含自身）。

    只允许直接子项——不能删到子目录内部去（那是「删一部分文件」，
    语义不明确且极易出错）。
    """
    try:
        if path == root:
            return False
        return path.parent == root
    except OSError:
        return False


def verify_deletable(
    paths: list[str],
    owned: list[TorrentFileNames],
    scan_dirs: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """复核哪些路径**此刻**仍可删。

    :param paths: 待删的绝对路径。
    :param owned: 跨下载器汇总的顶层名（含各自的 downloadDir）。
    :param scan_dirs: 配置的扫描目录列表。
    :returns: ``(可删的路径, [(路径, 跳过原因)])``。
    """
    names_by_dir = top_names_by_dir(owned)
    ok: list[str] = []
    skipped: list[tuple[str, str]] = []

    for raw in paths:
        p = Path(raw)
        # 闸 1：必须在配置的扫描目录内，且是直接子项
        target_root: Path | None = None
        for d in scan_dirs:
            root = Path(d)
            if _is_child_of(p, root):
                target_root = root
                break
        if target_root is None:
            skipped.append((raw, "不在配置的扫描目录内（或不是其直接子项）"))
            continue

        # 闸 2：此刻仍无主
        entry_name = p.name
        owned_names: set[str] = set()
        for d, names in names_by_dir.items():
            if match_dir(d, str(target_root)):
                owned_names |= names
        if entry_name in owned_names:
            skipped.append((raw, "复核时发现已有对应任务（已不再是无主条目）"))
            continue

        # 存在性检查
        if not p.exists():
            skipped.append((raw, "路径已不存在"))
            continue

        ok.append(raw)

    return ok, skipped


def _remove(path: Path) -> tuple[bool, int, str | None, bool]:
    """删除一个条目（目录递归 / 单文件）。

    :returns: ``(成功, 字节数, 错误描述, 是否权限错误)``
        - 权限错误（errno 13 / ``Permission denied``）单独标记，供上层
          给出可行动提示（NAS 端属主/权限问题，需改属主或换挂载账号）
    """
    try:
        if path.is_dir() and not path.is_symlink():
            size = _dir_size(path)
            shutil.rmtree(path)
            return True, size, None, False
        size = path.stat().st_size
        path.unlink()
        return True, size, None, False
    except OSError as exc:
        # errno 13 = EACCES（Permission denied）。CIFS 侧报 Permission denied
        # 也走这里（OSError 子类）。某些 CIFS 错误是 errno 1（EPERM）也归为
        # 权限问题——服务端拒绝，与本地权限位无关。
        is_perm = exc.errno in (13, 1) or "permission denied" in str(exc).lower()
        return False, 0, str(exc), is_perm


def _dir_size(path: Path) -> int:
    """目录的总字节数（删除前算，用于审计与「释放了多少」）。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
    except OSError:
        return total
    return total


def delete_orphans(
    paths: list[str],
    owned: list[TorrentFileNames],
    scan_dirs: list[str],
) -> OrphanDeleteResult:
    """删除孤儿条目（**不可逆**）。

    :param paths: 待删的绝对路径（来自孤儿明细的 ``path`` 字段）。
    :param owned: 跨下载器汇总的顶层名——用于**二次复核**（见
        :func:`verify_deletable`）。
    :param scan_dirs: 配置的扫描目录列表。
    :returns: :class:`OrphanDeleteResult`
    """
    if not scan_dirs:
        return OrphanDeleteResult(
            requested=len(paths),
            error="未配置扫描目录（辅种下载目录未挂载到容器）",
        )

    ok, skipped = verify_deletable(paths, owned, scan_dirs)

    result = OrphanDeleteResult(
        requested=len(paths),
        skipped=len(skipped),
    )
    for raw, reason in skipped:
        result.items.append({
            "path": raw,
            "status": "skipped",
            "reason": reason,
        })

    for raw in ok:
        p = Path(raw)
        # 审计：删磁盘数据不可逆 → 记「删了什么」
        logger.warning(
            "删除孤儿文件：path=%s bytes=%d", raw, _dir_size(p) if p.is_dir() else 0
        )
        success, size, err, is_perm = _remove(p)
        if success:
            result.succeeded += 1
            result.freed_bytes += size
            result.items.append({
                "path": raw,
                "status": "deleted",
                "bytes": size,
            })
        else:
            result.failed += 1
            result.items.append({
                "path": raw,
                "status": "failed",
                "reason": err or "未知错误",
                "permission_denied": is_perm,
            })
            if is_perm:
                result.permission_denied += 1
            logger.warning(
                "删除孤儿文件失败：path=%s error=%s permission_denied=%s",
                raw, err, is_perm,
            )

    # 失败时构造 error 文案：权限失败单独给可行动提示（重试无意义，
    # 需用户在 NAS 端改属主/权限或换挂载账号）；其它失败走通用文案
    if result.permission_denied > 0:
        result.error = (
            f"{result.permission_denied} 个条目因权限不足删除失败"
            f"（NAS 端目录属主非挂载账号，权限位不含写）——"
            f"需在 NAS 上把这些目录的属主改为挂载账号（chown），"
            f"或给组加写权限（chmod g+w），或更换挂载账号为属主；"
            f"改后重试即可。"
        )
    elif result.failed > 0:
        result.error = f"{result.failed} 个条目删除失败"
    return result
