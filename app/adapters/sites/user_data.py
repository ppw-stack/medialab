"""站点用户数据契约（FR-1.6，精简版）。

只保留 4 业务字段：

- **上传量** (upload, bytes)
- **下载量** (download, bytes)
- **做种数** (seeding_count, 当前做种)
- **下载中数** (leeching_count, 当前下载/未完成)

其他字段（username/user_level/bonus/H&R/消息/做种积分）已移除：
适配层 4 方法合并为单方法 :meth:`SiteAdapter.get_user_snapshot`，
service 只 upsert 这 4 列。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SiteUserSnapshot"]


@dataclass(frozen=True, slots=True)
class SiteUserSnapshot:
    """单次 refresh 拉到的 4 业务字段（FR-1.6 精简版）。"""

    upload: int = 0
    download: int = 0
    seeding_count: int = 0
    leeching_count: int = 0
