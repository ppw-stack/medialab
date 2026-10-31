"""上传文件过滤与稳态判定的测试。"""

from __future__ import annotations

import os
from pathlib import Path

from app.services.upload_filter import (
    TRASH_DIR_NAME,
    FileStamp,
    aiter_local_files,
    is_empty,
    is_stable,
    is_temporary,
    iter_local_files,
    iter_local_files_lazy,
    relative_to_root,
    should_skip,
)


def test_临时文件识别():
    """临时文件后缀 / 前缀都要认出来。"""
    assert is_temporary("movie.mkv.part")
    assert is_temporary("movie.crdownload")
    assert is_temporary("~$movie.docx")
    assert is_temporary(".DS_Store")
    assert not is_temporary("movie.mkv")
    assert not is_temporary("S01E01.mp4")


def test_gitkeep不是临时文件():
    """``.gitkeep`` 是占位文件，不当临时文件。"""
    assert not is_temporary(".gitkeep")


def test_空文件不在事件阶段被跳过(tmp_path: Path):
    """空文件**不在** ``should_skip`` 里跳——真实写入是先建空文件再写。

    空文件规则只在稳态判定之后生效（见 ``is_empty``）。
    """
    empty = tmp_path / "empty.mkv"
    empty.write_bytes(b"")
    assert should_skip(empty, root=tmp_path) is None
    assert is_empty(empty) is True


def test_非空文件不是空的(tmp_path: Path):
    """非空文件 → ``is_empty`` 为 False。"""
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x")
    assert is_empty(path) is False


def test_文件不存在时视为空(tmp_path: Path):
    """文件读不到 → 视为空。"""
    assert is_empty(tmp_path / "nope") is True


def test_跳过隐藏文件(tmp_path: Path):
    """隐藏文件跳过（``.gitkeep`` 除外——但它通常 0 字节，仍会被空文件规则挡下）。"""
    hidden = tmp_path / ".secret"
    hidden.write_bytes(b"x")
    assert should_skip(hidden, root=tmp_path) == "隐藏文件"

    # .gitkeep 不算「隐藏文件」（但它通常是 0 字节 → 被空文件规则挡下）
    keep = tmp_path / ".gitkeep"
    keep.write_bytes(b"")
    assert should_skip(keep, root=tmp_path) is None
    assert is_empty(keep) is True
    keep.write_bytes(b"x")
    assert should_skip(keep, root=tmp_path) is None


def test_正常文件不跳过(tmp_path: Path):
    """普通文件不跳过。"""
    normal = tmp_path / "movie.mkv"
    normal.write_bytes(b"x" * 10)
    assert should_skip(normal, root=tmp_path) is None


def test_软链逃逸被拒(tmp_path: Path):
    """软链指向监听根外 → 拒绝。"""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        return  # 平台不支持软链，跳过
    assert should_skip(link, root=root) == "软链逃出监听根"


def test_稳态判定_size不变(tmp_path: Path):
    """size 与 mtime 都不变 → 稳态。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 10)
    first = FileStamp.of(path)
    assert first is not None
    assert is_stable(path, first) is True


def test_稳态判定_size变了(tmp_path: Path):
    """size 变了 → 不稳态（还在写）。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 10)
    first = FileStamp.of(path)
    assert first is not None
    path.write_bytes(b"x" * 20)
    assert is_stable(path, first) is False


def test_首次采样不算稳态(tmp_path: Path):
    """没有历史采样 → 不稳态（得等下一次）。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    assert is_stable(path, None) is False


def test_文件消失时不算稳态(tmp_path: Path):
    """文件没了 → 不稳态（会被清出待确认表）。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    stamp = FileStamp.of(path)
    assert stamp is not None
    path.unlink()
    assert is_stable(path, stamp) is False


def test_遍历本地文件跳过临时文件(tmp_path: Path):
    """全量遍历要跳过临时文件与隐藏文件。"""
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv.part").write_bytes(b"x")
    (tmp_path / ".hidden").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.mkv").write_bytes(b"x")

    files = iter_local_files(tmp_path)
    names = sorted(p.name for p in files)
    assert names == ["a.mkv", "c.mkv"]


def test_遍历不递归(tmp_path: Path):
    """``include_subdirs=False`` → 只看顶层。"""
    (tmp_path / "a.mkv").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.mkv").write_bytes(b"x")

    files = iter_local_files(tmp_path, include_subdirs=False)
    assert [p.name for p in files] == ["a.mkv"]


def test_回收目录内容不跳过会被重传(tmp_path: Path):
    """**回归测试**：回收目录里的文件必须跳过。

    曾经 ``should_skip`` 只看 ``path.name``——回收目录里的 ``a.mkv``
    文件名正常，会被当待传文件重新上传，形成
    「移进回收站 → 又传一遍 → 再移进回收站」的死循环。
    """
    trash = tmp_path / TRASH_DIR_NAME / "20261030-120000"
    trash.mkdir(parents=True)
    path = trash / "a.mkv"
    path.write_bytes(b"content")

    assert should_skip(path, root=tmp_path) == "回收目录内的文件"


def test_任意隐藏目录都会被跳过(tmp_path: Path):
    """路径中任一目录以 ``.`` 开头 → 跳过（不只文件名）。"""
    hidden = tmp_path / ".cache" / "a.mkv"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"x")
    assert should_skip(hidden, root=tmp_path) == "隐藏目录内"


def test_普通子目录不误跳(tmp_path: Path):
    """正常子目录照常处理。"""
    normal = tmp_path / "sub" / "a.mkv"
    normal.parent.mkdir(parents=True)
    normal.write_bytes(b"x")
    assert should_skip(normal, root=tmp_path) is None


def test_相对路径用posix分隔符(tmp_path: Path):
    """相对路径必须是 posix 风格（云端路径用 ``/``）。"""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    path = sub / "c.mkv"
    path.write_bytes(b"x")
    assert relative_to_root(path, tmp_path) == "a/b/c.mkv"


def test_遍历不存在的目录返回空(tmp_path: Path):
    """目录不存在 → 返回空列表，不抛。"""
    assert iter_local_files(tmp_path / "nope") == []


def test_惰性遍历是生成器(tmp_path: Path):
    """``iter_local_files_lazy`` 是生成器——**不物化整个列表**。

    CIFS 上 os.walk 极慢，物化会让启动阻塞几小时。惰性是「边扫边同步」
    的前提。
    """
    (tmp_path / "a.mkv").write_bytes(b"x")
    it = iter_local_files_lazy(tmp_path)
    # 拿到的是生成器本身，不是 list
    assert not isinstance(it, list)
    assert hasattr(it, "__next__")
    assert [p.name for p in it] == ["a.mkv"]


def test_惰性遍历与物化版结果一致(tmp_path: Path):
    """惰性版与 ``iter_local_files`` 结果一致（同一真源）。"""
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv.part").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.mkv").write_bytes(b"x")

    lazy = list(iter_local_files_lazy(tmp_path))
    materialized = iter_local_files(tmp_path)
    assert lazy == materialized


async def test_aiter不阻塞事件循环(tmp_path: Path):
    """``aiter_local_files`` 逐个产出文件，且不阻塞 loop。

    每次 next() 放线程池——CIFS 上 os.walk 同步跑会卡住整个事件循环
    （uvicorn 无法完成 startup）。
    """
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")

    names = [p.name async for p in aiter_local_files(tmp_path)]
    assert sorted(names) == ["a.mkv", "b.mkv"]
