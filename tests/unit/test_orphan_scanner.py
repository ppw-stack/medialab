"""辅种孤儿文件扫描算法单元测试。

锁住的核心契约：

- **孤儿判定**：磁盘有、任务无 → 孤儿；任务有、磁盘无 → 不是孤儿
  （那是「文件丢失」，由现有异常检测覆盖）
- **顶层名匹配**：下载器任务的 ``top_names`` 与磁盘顶层条目名比对
- **口径是保守下界**：同名但不同种子的目录会**漏判**（数字偏少，不偏多）
- **扫描失败不抛**：目录不存在 / 不可读 → 返回带 ``error`` 的结果
- **大小统计**：目录递归求和（实测与下载器报告的 size_gb 一致）
"""
from __future__ import annotations

from pathlib import Path

from app.services.orphan_scanner import failed_scan, scan_orphans


def _torrent(top_names: list[str]) -> dict[str, object]:
    """构造一条带 top_names 的种子记录。"""
    return {"hash": "a" * 40, "name": "x", "top_names": top_names}


def _make_tree(root: Path, spec: dict[str, int]) -> None:
    """按 ``{相对路径: 字节数}`` 造目录树。"""
    for rel, size in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * size)


# ------------------------------------------------------------------ 基本判定


def test_orphan_is_disk_entry_without_task(tmp_path: Path) -> None:
    """磁盘有、任务无 → 孤儿。"""
    _make_tree(tmp_path, {"orphan_dir/file.mkv": 1000, "task_dir/file.mkv": 2000})
    torrents = [_torrent(["task_dir"])]

    result = scan_orphans(torrents, str(tmp_path))

    assert result.orphan_count == 1
    assert result.orphans[0].name == "orphan_dir"
    assert result.orphans[0].bytes == 1000
    assert result.orphans[0].file_count == 1
    assert result.error is None


def test_task_entry_on_disk_is_not_orphan(tmp_path: Path) -> None:
    """任务有、磁盘也有 → 不是孤儿。"""
    _make_tree(tmp_path, {"task_dir/file.mkv": 2000})
    torrents = [_torrent(["task_dir"])]

    result = scan_orphans(torrents, str(tmp_path))

    assert result.orphan_count == 0
    assert result.total_count == 1
    assert result.total_bytes == 2000


def test_task_without_files_on_disk_is_not_orphan(tmp_path: Path) -> None:
    """任务有、磁盘无 → **不是**孤儿（那是「文件丢失」，由异常检测覆盖）。"""
    torrents = [_torrent(["gone_dir"])]

    result = scan_orphans(torrents, str(tmp_path))

    assert result.orphan_count == 0
    assert result.total_count == 0


# ------------------------------------------------------------------ 大小统计


def test_orphan_size_is_recursive(tmp_path: Path) -> None:
    """目录大小递归求和（实测与下载器报告一致）。"""
    _make_tree(
        tmp_path,
        {
            "big/a.mkv": 3000,
            "big/sub/b.mkv": 4000,
            "big/sub/c.mkv": 5000,
        },
    )

    result = scan_orphans([_torrent([])], str(tmp_path))

    assert result.orphan_count == 1
    assert result.orphans[0].bytes == 12000
    assert result.orphans[0].file_count == 3


def test_single_file_orphan(tmp_path: Path) -> None:
    """单文件种子（无目录）也能识别为孤儿。"""
    _make_tree(tmp_path, {"movie.mkv": 7000})

    result = scan_orphans([_torrent([])], str(tmp_path))

    assert result.orphan_count == 1
    assert result.orphans[0].name == "movie.mkv"
    assert result.orphans[0].bytes == 7000


def test_orphans_sorted_by_size_desc(tmp_path: Path) -> None:
    """孤儿按占用降序（大的在前，便于先清理）。"""
    _make_tree(tmp_path, {"small/a": 100, "medium/a": 2000, "large/a": 30000})

    result = scan_orphans([_torrent([])], str(tmp_path))

    assert [o.name for o in result.orphans] == ["large", "medium", "small"]


def test_total_bytes_includes_task_dirs(tmp_path: Path) -> None:
    """总量含任务目录（用于算孤儿占比）。"""
    _make_tree(tmp_path, {"task_dir/a": 5000, "orphan_dir/a": 1000})
    torrents = [_torrent(["task_dir"])]

    result = scan_orphans(torrents, str(tmp_path))

    assert result.total_bytes == 6000
    assert result.orphan_bytes == 1000


# ------------------------------------------------------------------ 保守下界


def test_same_name_task_prevents_orphan(tmp_path: Path) -> None:
    """同名但不同种子的目录 → **漏判**（不算孤儿）。

    这是口径的已知边界：数字只会偏少，不会偏多。
    """
    _make_tree(tmp_path, {"shared/a": 1000})
    # 任务里有个同名条目（但其实是另一个种子）
    torrents = [_torrent(["shared"])]

    result = scan_orphans(torrents, str(tmp_path))

    assert result.orphan_count == 0, "同名任务存在 → 不算孤儿（保守下界）"


# ------------------------------------------------------------------ 失败降级


def test_missing_dir_returns_error(tmp_path: Path) -> None:
    """目录不存在 → 返回 error（**不抛**）。"""
    result = scan_orphans([], str(tmp_path / "nope"))

    assert result.error is not None
    assert result.orphan_count == 0


def test_none_dir_returns_error() -> None:
    """未配置扫描目录 → 返回 error。"""
    result = scan_orphans([], None)

    assert result.error is not None
    assert "未配置" in result.error


def test_failed_scan_helper() -> None:
    """failed_scan 构造带 error 的空结果。"""
    result = failed_scan("boom")
    assert result.error == "boom"
    assert result.orphan_count == 0
    assert result.orphan_bytes == 0


# ------------------------------------------------------------------ 多任务


def test_multiple_tasks_dedup_top_names(tmp_path: Path) -> None:
    """多个任务共享顶层名（同名种子）→ 只算一次。"""
    _make_tree(tmp_path, {"a/x": 100, "b/y": 200})
    torrents = [_torrent(["a"]), _torrent(["a"]), _torrent(["b"])]

    result = scan_orphans(torrents, str(tmp_path))

    assert result.orphan_count == 0
    assert result.total_count == 2
