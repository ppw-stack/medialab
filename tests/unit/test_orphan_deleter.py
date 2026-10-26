"""辅种孤儿文件**删除**的单元测试。

锁住的核心契约（安全机制，逐条对应实现）：

1. **只在配置的扫描目录内删**——路径必须是扫描目录的**直接子项**
   （防路径穿越：不能删到别的目录、不能删子目录内部的某个文件）
2. **二次扫描复核**——删除前重新判定「有主」；已有任务的条目必须
   **跳过**（不删）
3. **逐条删除，单条失败不中断**其余条目
4. **审计日志**——删了什么必须能查到
"""
from __future__ import annotations

from pathlib import Path

from app.adapters.downloaders.base import TorrentFileNames
from app.services.orphan_deleter import delete_orphans, verify_deletable


def _owned(
    top_names: list[str], save_path: str = "/data/aux"
) -> list[TorrentFileNames]:
    return [
        TorrentFileNames(
            hash="a" * 40,
            save_path=save_path,
            top_names=tuple(top_names),
        )
    ]


def _make(root: Path, spec: dict[str, int]) -> None:
    """按 ``{相对路径: 字节数}`` 造目录树。"""
    for rel, size in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * size)


# ------------------------------------------------------------------ 闸 1：路径范围


def test_deletes_direct_child_of_scan_dir(tmp_path: Path) -> None:
    """扫描目录的直接子项 → 正常删除。"""
    _make(tmp_path, {"orphan/file.mkv": 1000})
    target = tmp_path / "orphan"

    result = delete_orphans([str(target)], _owned([]), [str(tmp_path)])

    assert result.succeeded == 1
    assert result.failed == 0
    assert result.freed_bytes == 1000
    assert not target.exists()


def test_rejects_path_outside_scan_dir(tmp_path: Path) -> None:
    """**不在**扫描目录内的路径 → 跳过（防路径穿越）。"""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _make(outside, {"secret/file.mkv": 1000})
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()

    result = delete_orphans([str(outside)], _owned([]), [str(scan_dir)])

    assert result.succeeded == 0
    assert result.skipped == 1
    assert result.items[0]["status"] == "skipped"
    assert "扫描目录" in result.items[0]["reason"]
    assert outside.exists(), "不该被删"


def test_rejects_scan_dir_itself(tmp_path: Path) -> None:
    """扫描目录**自身**不能被删（否则整个目录都没了）。"""
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    _make(scan_dir, {"a/file.mkv": 100})

    result = delete_orphans([str(scan_dir)], _owned([]), [str(scan_dir)])

    assert result.succeeded == 0
    assert result.skipped == 1
    assert scan_dir.exists()


def test_rejects_nested_file_inside_orphan_dir(tmp_path: Path) -> None:
    """孤儿目录**内部**的某个文件不能被单独删（只删顶层条目）。"""
    _make(tmp_path, {"orphan/sub/file.mkv": 1000})
    nested = tmp_path / "orphan" / "sub" / "file.mkv"

    result = delete_orphans([str(nested)], _owned([]), [str(tmp_path)])

    assert result.succeeded == 0
    assert result.skipped == 1
    assert nested.exists(), "不该被删"


def test_rejects_path_traversal(tmp_path: Path) -> None:
    """``../`` 穿越出的路径 → 跳过。"""
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    traversal = str(scan_dir / ".." / "secret.txt")

    result = delete_orphans([traversal], _owned([]), [str(scan_dir)])

    assert result.succeeded == 0
    assert result.skipped == 1
    assert secret.exists()


# ------------------------------------------------------------------ 闸 2：二次复核


def test_skips_entry_that_gained_a_task(tmp_path: Path) -> None:
    """复核时发现**已有对应任务** → 跳过（快照过期，任务被加回来了）。"""
    _make(tmp_path, {"movie/file.mkv": 1000})
    target = tmp_path / "movie"

    # 快照时它是无主的，但复核时已有任务认领了它
    result = delete_orphans(
        [str(target)], _owned(["movie"], save_path=str(tmp_path)), [str(tmp_path)]
    )

    assert result.succeeded == 0
    assert result.skipped == 1
    assert "已有对应任务" in result.items[0]["reason"]
    assert target.exists(), "有主的条目绝不能被删"


def test_skips_entry_owned_by_other_download_dir(tmp_path: Path) -> None:
    """**别的 downloadDir** 的任务不认领本目录 → 仍可删。"""
    _make(tmp_path, {"movie/file.mkv": 1000})
    target = tmp_path / "movie"

    result = delete_orphans(
        [str(target)], _owned(["movie"], save_path="/somewhere/else"), [str(tmp_path)]
    )

    assert result.succeeded == 1
    assert target.exists() is False


def test_skips_missing_path(tmp_path: Path) -> None:
    """路径已不存在 → 跳过（不算失败）。"""
    missing = tmp_path / "gone"

    result = delete_orphans([str(missing)], _owned([]), [str(tmp_path)])

    assert result.succeeded == 0
    assert result.failed == 0
    assert result.skipped == 1
    assert "不存在" in result.items[0]["reason"]


def test_verify_deletable_splits_ok_and_skipped(tmp_path: Path) -> None:
    """复核函数把可删与跳过分开返回。"""
    _make(tmp_path, {"ok/file.mkv": 1, "owned/file.mkv": 1})
    ok, skipped = verify_deletable(
        [str(tmp_path / "ok"), str(tmp_path / "owned")],
        _owned(["owned"], save_path=str(tmp_path)),
        [str(tmp_path)],
    )

    assert ok == [str(tmp_path / "ok")]
    assert len(skipped) == 1
    assert skipped[0][0] == str(tmp_path / "owned")


# ------------------------------------------------------------------ 逐条删除


def test_deletes_multiple_entries(tmp_path: Path) -> None:
    """多条一起删。"""
    _make(tmp_path, {"a/file.mkv": 100, "b/file.mkv": 200, "c/file.mkv": 300})

    result = delete_orphans(
        [str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "c")],
        _owned([]),
        [str(tmp_path)],
    )

    assert result.succeeded == 3
    assert result.freed_bytes == 600
    assert result.requested == 3


def test_empty_dir_deleted(tmp_path: Path) -> None:
    """空目录也能删（0 字节）。"""
    (tmp_path / "empty").mkdir()

    result = delete_orphans([str(tmp_path / "empty")], _owned([]), [str(tmp_path)])

    assert result.succeeded == 1
    assert result.freed_bytes == 0
    assert not (tmp_path / "empty").exists()


def test_single_file_orphan_deleted(tmp_path: Path) -> None:
    """单文件种子（无目录）也能删。"""
    _make(tmp_path, {"movie.mkv": 7000})

    result = delete_orphans([str(tmp_path / "movie.mkv")], _owned([]), [str(tmp_path)])

    assert result.succeeded == 1
    assert result.freed_bytes == 7000


def test_no_scan_dirs_returns_error(tmp_path: Path) -> None:
    """未配置扫描目录 → 整体失败，不删任何东西。"""
    _make(tmp_path, {"a/file.mkv": 100})

    result = delete_orphans([str(tmp_path / "a")], _owned([]), [])

    assert result.succeeded == 0
    assert result.error is not None
    assert (tmp_path / "a").exists()


def test_empty_paths_noop(tmp_path: Path) -> None:
    """空列表 → 无事发生。"""
    result = delete_orphans([], _owned([]), [str(tmp_path)])
    assert result.requested == 0
    assert result.succeeded == 0
    assert result.error is None


def test_result_items_record_each_path(tmp_path: Path) -> None:
    """逐条结果里记了路径与状态。"""
    _make(tmp_path, {"a/file.mkv": 100})

    result = delete_orphans([str(tmp_path / "a")], _owned([]), [str(tmp_path)])

    assert len(result.items) == 1
    assert result.items[0]["path"] == str(tmp_path / "a")
    assert result.items[0]["status"] == "deleted"
    assert result.items[0]["bytes"] == 100
