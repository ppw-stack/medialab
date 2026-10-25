"""辅种孤儿文件扫描算法单元测试。

锁住的核心契约：

- **按 downloadDir 分组**：扫描某个目录时，**只把 downloadDir 等于该
  目录的任务**算作「有主」——否则父目录的任务会把子目录里的同名条目
  误认有主
- **空目录也算孤儿**（0 字节）——它们是「任务没了的残留」
- **多路径**：每个配置目录独立扫描 + 独立统计
- **扫描失败不抛**：目录不存在 / 不可读 → 记 error
- **大小统计**：目录递归求和
"""
from __future__ import annotations

from pathlib import Path

from app.adapters.downloaders.base import TorrentFileNames
from app.services.orphan_scanner import (
    failed_scan,
    parse_scan_dirs,
    scan_orphans,
)


def _torrent(
    top_names: list[str],
    save_path: str = "/data/aux",
) -> TorrentFileNames:
    """构造一条带顶层名与 downloadDir 的种子记录。"""
    return TorrentFileNames(
        hash="a" * 40,
        save_path=save_path,
        top_names=tuple(top_names),
    )


def _make_tree(root: Path, spec: dict[str, int]) -> None:
    """按 ``{相对路径: 字节数}`` 造目录树。"""
    for rel, size in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * size)


# ------------------------------------------------------------------ 配置解析


def test_parse_scan_dirs_splits_and_strips() -> None:
    """逗号分隔，去空白、去空项、去重复。"""
    assert parse_scan_dirs("/a,/b") == ["/a", "/b"]
    assert parse_scan_dirs("/a, /b ") == ["/a", "/b"]
    assert parse_scan_dirs("/a,,/b,") == ["/a", "/b"]
    assert parse_scan_dirs("") == []
    assert parse_scan_dirs("  ") == []
    # 重复只保留一个
    assert parse_scan_dirs("/a,/a") == ["/a"]


# ------------------------------------------------------------------ 基本判定


def test_orphan_is_disk_entry_without_task(tmp_path: Path) -> None:
    """磁盘有、任务无 → 孤儿。"""
    _make_tree(tmp_path, {"orphan_dir/file.mkv": 1000, "task_dir/file.mkv": 2000})
    torrents = [_torrent(["task_dir"], save_path=str(tmp_path))]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.orphan_count == 1
    assert result.orphans[0].name == "orphan_dir"
    assert result.orphans[0].bytes == 1000
    assert result.orphans[0].file_count == 1
    assert result.error is None


def test_task_entry_on_disk_is_not_orphan(tmp_path: Path) -> None:
    """任务有、磁盘也有 → 不是孤儿。"""
    _make_tree(tmp_path, {"task_dir/file.mkv": 2000})
    torrents = [_torrent(["task_dir"], save_path=str(tmp_path))]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.orphan_count == 0
    assert result.total_count == 1
    assert result.total_bytes == 2000


# ------------------------------------------------------------------ 按 downloadDir 分组


def test_task_from_other_download_dir_does_not_claim(tmp_path: Path) -> None:
    """**别的 downloadDir** 的任务不能认领本目录的条目。

    辅种站有多个 downloadDir（辅种 / 父目录 / game）。若不按目录分组，
    父目录的任务会把辅种目录里的同名条目误认有主 → 漏判。
    """
    _make_tree(tmp_path, {"shared/file.mkv": 1000})
    # 任务的 downloadDir 是**另一个**目录
    torrents = [_torrent(["shared"], save_path="/somewhere/else")]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.orphan_count == 1, "别的 downloadDir 的任务不算有主"
    assert result.orphans[0].name == "shared"


def test_same_download_dir_claims(tmp_path: Path) -> None:
    """同一个 downloadDir 的任务正常认领。"""
    _make_tree(tmp_path, {"shared/file.mkv": 1000})
    torrents = [_torrent(["shared"], save_path=str(tmp_path))]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.orphan_count == 0


def test_download_dir_trailing_slash_matches(tmp_path: Path) -> None:
    """downloadDir 尾部斜杠差异不算不同目录。"""
    _make_tree(tmp_path, {"shared/file.mkv": 1000})
    torrents = [_torrent(["shared"], save_path=str(tmp_path) + "/")]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.orphan_count == 0


def test_download_dir_prefix_is_not_same(tmp_path: Path) -> None:
    """``/downloads`` 与 ``/downloads/辅种`` **不是**同一个目录。

    前缀匹配会把两者混为一谈 → 辅种目录的条目被父目录任务误认有主。
    """
    _make_tree(tmp_path, {"shared/file.mkv": 1000})
    torrents = [_torrent(["shared"], save_path="/downloads")]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.orphan_count == 1


# ------------------------------------------------------------------ 空目录


def test_empty_dir_counts_as_orphan(tmp_path: Path) -> None:
    """**空目录也算孤儿**（0 字节），但单独计数。"""
    (tmp_path / "empty_dir").mkdir()
    _make_tree(tmp_path, {"full_dir/a": 100})

    result = scan_orphans([], [str(tmp_path)])

    assert result.orphan_count == 2
    assert result.empty_dir_count == 1
    empty = next(o for o in result.orphans if o.name == "empty_dir")
    assert empty.file_count == 0
    assert empty.bytes == 0


# ------------------------------------------------------------------ 多路径


def test_multiple_dirs_scanned_independently(tmp_path: Path) -> None:
    """多个配置目录 → 各自独立扫描 + 独立统计。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _make_tree(a, {"orphan_a/x": 100})
    _make_tree(b, {"orphan_b/x": 200, "owned_b/y": 50})

    torrents = [_torrent(["owned_b"], save_path=str(b))]
    result = scan_orphans(torrents, [str(a), str(b)])

    assert result.orphan_count == 2
    names = {o.name for o in result.orphans}
    assert names == {"orphan_a", "orphan_b"}
    # 逐目录统计
    assert len(result.scanned_dirs) == 2
    by_dir = {d["scan_dir"]: d for d in result.scanned_dirs}
    assert by_dir[str(a)]["orphan_count"] == 1
    assert by_dir[str(b)]["orphan_count"] == 1
    assert by_dir[str(b)]["owned_count"] == 1


def test_orphan_records_scan_dir(tmp_path: Path) -> None:
    """孤儿条目记录它属于哪个扫描目录。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _make_tree(a, {"x": 10})
    _make_tree(b, {"y": 20})

    result = scan_orphans([], [str(a), str(b)])

    by_name = {o.name: o for o in result.orphans}
    assert by_name["x"].scan_dir == str(a)
    assert by_name["y"].scan_dir == str(b)


# ------------------------------------------------------------------ 大小统计


def test_orphan_size_is_recursive(tmp_path: Path) -> None:
    """目录大小递归求和。"""
    _make_tree(
        tmp_path,
        {
            "big/a.mkv": 3000,
            "big/sub/b.mkv": 4000,
            "big/sub/c.mkv": 5000,
        },
    )

    result = scan_orphans([], [str(tmp_path)])

    assert result.orphan_count == 1
    assert result.orphans[0].bytes == 12000
    assert result.orphans[0].file_count == 3


def test_single_file_orphan(tmp_path: Path) -> None:
    """单文件种子（无目录）也能识别为孤儿。"""
    _make_tree(tmp_path, {"movie.mkv": 7000})

    result = scan_orphans([], [str(tmp_path)])

    assert result.orphan_count == 1
    assert result.orphans[0].name == "movie.mkv"
    assert result.orphans[0].bytes == 7000


def test_orphans_sorted_by_size_desc(tmp_path: Path) -> None:
    """孤儿按占用降序（大的在前，便于先清理）。"""
    _make_tree(tmp_path, {"small/a": 100, "medium/a": 2000, "large/a": 30000})

    result = scan_orphans([], [str(tmp_path)])

    assert [o.name for o in result.orphans] == ["large", "medium", "small"]


def test_total_bytes_includes_task_dirs(tmp_path: Path) -> None:
    """总量含任务目录（用于算孤儿占比）。"""
    _make_tree(tmp_path, {"task_dir/a": 5000, "orphan_dir/a": 1000})
    torrents = [_torrent(["task_dir"], save_path=str(tmp_path))]

    result = scan_orphans(torrents, [str(tmp_path)])

    assert result.total_bytes == 6000
    assert result.orphan_bytes == 1000


# ------------------------------------------------------------------ 失败降级


def test_missing_dir_returns_error(tmp_path: Path) -> None:
    """目录不存在 → 记 error（**不抛**）。"""
    result = scan_orphans([], [str(tmp_path / "nope")])

    assert result.error is not None
    assert result.orphan_count == 0


def test_no_dirs_returns_error() -> None:
    """未配置扫描目录 → 记 error。"""
    result = scan_orphans([], [])

    assert result.error is not None
    assert "未配置" in result.error


def test_partial_failure_records_error(tmp_path: Path) -> None:
    """部分目录失败 → 记 error，但成功目录的结果照常返回。"""
    ok = tmp_path / "ok"
    ok.mkdir()
    _make_tree(ok, {"orphan/x": 100})

    result = scan_orphans([], [str(ok), str(tmp_path / "nope")])

    assert result.orphan_count == 1
    assert result.error is not None
    assert "nope" in result.error


def test_failed_scan_helper() -> None:
    """failed_scan 构造带 error 的空结果。"""
    result = failed_scan("boom")
    assert result.error == "boom"
    assert result.orphan_count == 0
    assert result.orphan_bytes == 0
