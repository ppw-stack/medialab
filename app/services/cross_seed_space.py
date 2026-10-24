"""辅种空间统计 service（辅种模块 · 空间维度）。

回答两个问题：

1. **辅种占了多少磁盘**——各辅种下载器的种子实际占用（``completed_bytes``
   求和）按下载目录分组
2. **磁盘还剩多少**——下载器所在分区的剩余/总量空间

设计要点：

- **复用同一次全量拉取**：辅种站种子量大（实测 4600+ 个），一次拉取要
  20s+。占用数据直接从 :meth:`DownloaderAdapter.list_torrents_with_errors`
  的返回值里算（不额外打接口）；只有磁盘空间需要**额外一次**轻量调用
  （TR ``free-space`` / qB ``sync/maindata``，毫秒级）。
- **磁盘信息拿不到不阻塞**：空间信息是**辅助**数据，取不到就降级成
  NULL——不能因为它让整个抓取失败（占用数据照样有用）。
- **口径诚实**：TR 的 ``haveValid`` 是精确的磁盘占用；qB 的
  ``completed`` 是 ``total_wanted_done``（不含 unwanted 文件与冗余块），
  会**小于**真实占用。不夸大、不折算。

字段缺失的处理：
    ``list_torrents_with_errors`` 的契约里 ``completed_bytes`` /
    ``save_path`` 是新增字段——旧适配器（未实现）可能不返回。缺失时按
    0 / None 处理，不报错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.adapters.downloaders.base import DownloaderAdapter
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "DirUsage",
    "SpaceStats",
    "collect_space",
    "summarize_spaces",
]


@dataclass(frozen=True, slots=True)
class DirUsage:
    """单个下载目录的占用。"""

    #: 下载目录（下载器视角的路径）
    dir: str
    #: 该目录下的种子数
    count: int
    #: 实际占用字节数（``completed_bytes`` 求和）
    bytes: int
    #: 种子总大小字节数（``size`` 求和，理论值）
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir": self.dir,
            "count": self.count,
            "bytes": self.bytes,
            "total_bytes": self.total_bytes,
        }


@dataclass(slots=True)
class SpaceStats:
    """单个辅种下载器的空间统计。"""

    #: 辅种实际占用（字节）
    used_bytes: int = 0
    #: 辅种总大小（字节，理论值）
    total_size_bytes: int = 0
    #: 磁盘剩余空间（字节）；None = 没取到
    disk_free_bytes: int | None = None
    #: 磁盘总量（字节）；None = 该下载器不提供（qB）
    disk_total_bytes: int | None = None
    #: 按目录分布（按占用降序）
    by_dir: list[DirUsage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_bytes": self.used_bytes,
            "total_size_bytes": self.total_size_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "by_dir": [d.to_dict() for d in self.by_dir],
        }


def _group_by_dir(torrents: list[dict[str, Any]]) -> list[DirUsage]:
    """按下载目录分组统计占用（按占用降序）。"""
    groups: dict[str, dict[str, int]] = {}
    for t in torrents:
        raw = t.get("save_path")
        path = str(raw) if raw else ""
        # 没有目录信息的种子归到「未知目录」——不丢弃（那会让占用对不上）
        key = path or "(未知目录)"
        g = groups.setdefault(key, {"count": 0, "bytes": 0, "total_bytes": 0})
        g["count"] += 1
        g["bytes"] += int(t.get("completed_bytes") or 0)
        g["total_bytes"] += int(t.get("size") or 0)
    return [
        DirUsage(
            dir=path,
            count=g["count"],
            bytes=g["bytes"],
            total_bytes=g["total_bytes"],
        )
        for path, g in sorted(
            groups.items(), key=lambda kv: (-kv[1]["bytes"], kv[0])
        )
    ]


async def collect_space(
    adapter: DownloaderAdapter,
    torrents: list[dict[str, Any]],
) -> SpaceStats:
    """从已抓取的种子列表算空间统计 + 取磁盘空间。

    :param adapter: 下载器适配器（用于查磁盘空间）。
    :param torrents: :meth:`DownloaderAdapter.list_torrents_with_errors`
        的返回值（含 ``completed_bytes`` / ``save_path``）。

    **磁盘空间取不到不影响占用统计**——降级成 NULL 并记 warning。
    """
    used = sum(int(t.get("completed_bytes") or 0) for t in torrents)
    total = sum(int(t.get("size") or 0) for t in torrents)
    by_dir = _group_by_dir(torrents)

    stats = SpaceStats(
        used_bytes=used,
        total_size_bytes=total,
        by_dir=by_dir,
    )

    # 磁盘空间：单独一次轻量调用。TR 需要一个存在的目录——用辅种数据
    # 实际所在的目录（能定位到正确的分区）。
    reference = next((d.dir for d in by_dir if d.dir and d.dir != "(未知目录)"), None)
    try:
        space = await adapter.get_disk_space(reference)
    except NotImplementedError:
        logger.info(
            "下载器 %s 不支持磁盘空间查询（空间信息留空）",
            getattr(adapter, "name", "?"),
        )
        return stats
    except Exception as exc:
        # 磁盘信息是辅助数据——拿不到不该让整个抓取失败
        logger.warning(
            "下载器 %s 磁盘空间查询失败（占用数据仍会落库）：%s",
            getattr(adapter, "name", "?"), exc,
        )
        return stats

    stats.disk_free_bytes = space.free_bytes
    stats.disk_total_bytes = space.total_bytes
    return stats


@dataclass(frozen=True, slots=True)
class SpaceSummary:
    """跨辅种下载器的空间汇总。"""

    downloaders: int = 0
    used_bytes: int = 0
    total_size_bytes: int = 0
    #: 磁盘剩余空间合计（字节）；None = 一个都没取到
    disk_free_bytes: int | None = None
    #: 磁盘总量合计（字节）；None = 一个都没取到
    disk_total_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloaders": self.downloaders,
            "used_bytes": self.used_bytes,
            "total_size_bytes": self.total_size_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
        }


def summarize_spaces(spaces: list[tuple[str, SpaceStats]]) -> SpaceSummary:
    """汇总多个辅种下载器的空间数据。

    :param spaces: ``[(下载器名, SpaceStats), ...]``。

    磁盘合计的语义：**各下载器所在分区的剩余之和**。若多个下载器在
    同一分区上，这个和会重复计算——但那也确实是「各自视角的剩余」。
    取不到磁盘信息的下载器不计入（用「有值的那些求和」，不是「有值的
    那些 + 未知当作 0」——后者会低估剩余空间）。
    """
    free_values = [s.disk_free_bytes for _, s in spaces if s.disk_free_bytes is not None]
    total_values = [s.disk_total_bytes for _, s in spaces if s.disk_total_bytes is not None]
    return SpaceSummary(
        downloaders=len(spaces),
        used_bytes=sum(s.used_bytes for _, s in spaces),
        total_size_bytes=sum(s.total_size_bytes for _, s in spaces),
        disk_free_bytes=sum(free_values) if free_values else None,
        disk_total_bytes=sum(total_values) if total_values else None,
    )
