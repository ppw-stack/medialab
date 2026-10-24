"""辅种空间统计 service 单元测试。

锁住的核心契约：

- **占用求和**：``completed_bytes`` 求和 = 实际占用；``size`` 求和 = 总大小
- **按目录分组**：同一目录的种子合并；无目录信息的归到「未知目录」
  （**不丢弃**——丢弃会让占用对不上）
- **磁盘信息拿不到不阻塞**：适配器不支持 / 查询失败 → 降级成 NULL，
  占用数据照样返回
- **口径诚实**：qB 的 ``completed`` 不含 unwanted 文件，会小于真实占用
  （代码注释里写明，不在代码里折算）
"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import DiskSpace, DownloaderError
from app.services.cross_seed_space import (
    SpaceStats,
    collect_space,
    summarize_spaces,
)


class _FakeAdapter:
    """可控的假下载器适配器。"""

    def __init__(
        self,
        space: DiskSpace | None = None,
        *,
        error: Exception | None = None,
        unsupported: bool = False,
    ) -> None:
        self._space = space
        self._error = error
        self._unsupported = unsupported
        #: 记录被传入的参考目录
        self.requested_path: str | None = None

    async def get_disk_space(self, path: str | None = None) -> DiskSpace:
        self.requested_path = path
        if self._unsupported:
            raise NotImplementedError("不支持")
        if self._error is not None:
            raise self._error
        assert self._space is not None
        return self._space


def _torrent(
    name: str,
    size: int,
    completed: int,
    save_path: str | None = None,
) -> dict[str, object]:
    return {
        "id": 0,
        "hash": "a" * 40,
        "name": name,
        "size": size,
        "status": 6,
        "error_code": 0,
        "error_string": "",
        "state": "seeding",
        "completed_bytes": completed,
        "save_path": save_path,
    }


# ------------------------------------------------------------------ 占用


async def test_collect_space_sums_used_and_total() -> None:
    """占用 = completed_bytes 求和；总大小 = size 求和。"""
    adapter = _FakeAdapter(DiskSpace(free_bytes=100, total_bytes=1000))
    torrents = [
        _torrent("a", 1000, 900, "/data/x"),
        _torrent("b", 2000, 1800, "/data/x"),
    ]

    stats = await collect_space(adapter, torrents)

    assert stats.used_bytes == 2700
    assert stats.total_size_bytes == 3000
    assert stats.disk_free_bytes == 100
    assert stats.disk_total_bytes == 1000


async def test_collect_space_groups_by_dir() -> None:
    """按下载目录分组（按占用降序）。"""
    adapter = _FakeAdapter(DiskSpace(free_bytes=1, total_bytes=2))
    torrents = [
        _torrent("a", 100, 90, "/data/small"),
        _torrent("b", 200, 190, "/data/big"),
        _torrent("c", 300, 280, "/data/big"),
    ]

    stats = await collect_space(adapter, torrents)

    assert len(stats.by_dir) == 2
    top = stats.by_dir[0]
    assert top.dir == "/data/big"
    assert top.count == 2
    assert top.bytes == 470
    assert top.total_bytes == 500
    # 降序
    assert stats.by_dir[0].bytes >= stats.by_dir[1].bytes


async def test_missing_save_path_becomes_unknown_dir() -> None:
    """没有目录信息的种子归到「未知目录」——**不丢弃**（否则占用对不上）。"""
    adapter = _FakeAdapter(DiskSpace(free_bytes=1, total_bytes=2))
    torrents = [_torrent("a", 100, 90, None)]

    stats = await collect_space(adapter, torrents)

    assert len(stats.by_dir) == 1
    assert stats.by_dir[0].dir == "(未知目录)"
    assert stats.by_dir[0].bytes == 90


async def test_missing_completed_bytes_counts_as_zero() -> None:
    """缺 ``completed_bytes`` 字段（旧适配器）→ 按 0 算，不报错。"""
    adapter = _FakeAdapter(DiskSpace(free_bytes=1, total_bytes=2))
    torrents = [{"id": 0, "hash": "a" * 40, "name": "x", "size": 100}]

    stats = await collect_space(adapter, torrents)

    assert stats.used_bytes == 0
    assert stats.total_size_bytes == 100


# ------------------------------------------------------------------ 磁盘信息


async def test_disk_space_uses_torrent_dir_as_reference() -> None:
    """磁盘查询用辅种数据实际所在目录（定位到正确的分区）。"""
    adapter = _FakeAdapter(DiskSpace(free_bytes=1, total_bytes=2))
    torrents = [_torrent("a", 100, 90, "/share/Public/downloads/辅种")]

    await collect_space(adapter, torrents)

    assert adapter.requested_path == "/share/Public/downloads/辅种"


async def test_disk_space_unsupported_degrades_to_null() -> None:
    """适配器不支持 → 磁盘信息为 NULL，占用数据照样返回。"""
    adapter = _FakeAdapter(unsupported=True)
    torrents = [_torrent("a", 100, 90, "/data/x")]

    stats = await collect_space(adapter, torrents)

    assert stats.disk_free_bytes is None
    assert stats.disk_total_bytes is None
    assert stats.used_bytes == 90


async def test_disk_space_failure_does_not_block() -> None:
    """磁盘查询失败 → 降级成 NULL，**不抛**（不能因辅助数据让整个抓取失败）。"""
    adapter = _FakeAdapter(error=DownloaderError("boom"))
    torrents = [_torrent("a", 100, 90, "/data/x")]

    stats = await collect_space(adapter, torrents)

    assert stats.disk_free_bytes is None
    assert stats.used_bytes == 90


async def test_qb_disk_space_has_no_total() -> None:
    """qB 拿不到总量 → ``total_bytes`` 为 None（不是 0——0 会被画成「磁盘已满」）。"""
    space = DiskSpace(free_bytes=1000, total_bytes=None)
    assert space.total_bytes is None
    assert space.used_bytes is None
    assert space.used_ratio is None


# ------------------------------------------------------------------ 汇总


def test_summarize_spaces_aggregates() -> None:
    """跨下载器汇总。"""
    spaces = [
        ("a", SpaceStats(used_bytes=100, total_size_bytes=200,
                         disk_free_bytes=10, disk_total_bytes=1000)),
        ("b", SpaceStats(used_bytes=300, total_size_bytes=400,
                         disk_free_bytes=20, disk_total_bytes=2000)),
    ]

    summary = summarize_spaces(spaces)

    assert summary.downloaders == 2
    assert summary.used_bytes == 400
    assert summary.total_size_bytes == 600
    assert summary.disk_free_bytes == 30
    assert summary.disk_total_bytes == 3000


def test_summarize_spaces_skips_missing_disk_info() -> None:
    """没有磁盘信息的下载器不计入剩余空间合计（不是「当作 0」——那会低估）。"""
    spaces = [
        ("a", SpaceStats(used_bytes=100, disk_free_bytes=10, disk_total_bytes=1000)),
        ("b", SpaceStats(used_bytes=300, disk_free_bytes=None, disk_total_bytes=None)),
    ]

    summary = summarize_spaces(spaces)

    assert summary.disk_free_bytes == 10
    assert summary.disk_total_bytes == 1000
    assert summary.used_bytes == 400


def test_summarize_spaces_empty() -> None:
    """空列表 → 全 0，磁盘为 None。"""
    summary = summarize_spaces([])
    assert summary.downloaders == 0
    assert summary.disk_free_bytes is None
    assert summary.disk_total_bytes is None


@pytest.mark.parametrize(
    ("total", "free", "expected_used"),
    [(1000, 400, 600), (1000, 1000, 0), (1000, 0, 1000)],
)
def test_disk_space_used_bytes(total: int, free: int, expected_used: int) -> None:
    """已用 = 总量 - 剩余。"""
    space = DiskSpace(free_bytes=free, total_bytes=total)
    assert space.used_bytes == expected_used
