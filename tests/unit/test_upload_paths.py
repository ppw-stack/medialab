"""上传监听路径校验的测试。

**没有白名单**——监听目录完全由用户在面板上配置，故只校验：

1. 必须是绝对路径
2. 必须是已存在的目录
3. 词法规范化（消掉 ``..`` / ``.`` / 重复斜杠）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.upload_paths import (
    PathGuardError,
    list_subdirs,
    resolve_watch_dir,
)


def test_存在的目录通过(tmp_path: Path):
    """已存在的目录 → 通过。"""
    assert resolve_watch_dir(str(tmp_path)) == tmp_path


def test_相对路径拒绝(tmp_path: Path):
    """相对路径没有意义（进程工作目录会变）→ 拒绝。"""
    with pytest.raises(PathGuardError, match="绝对路径"):
        resolve_watch_dir("app/watch")


def test_不存在的目录拒绝(tmp_path: Path):
    """目录不存在 → 拒绝。"""
    with pytest.raises(PathGuardError, match="不存在"):
        resolve_watch_dir(str(tmp_path / "nope"))


def test_文件不是目录拒绝(tmp_path: Path):
    """指向文件而非目录 → 拒绝。"""
    path = tmp_path / "a.txt"
    path.write_bytes(b"x")
    with pytest.raises(PathGuardError, match="不存在"):
        resolve_watch_dir(str(path))


def test_规范化消掉重复斜杠(tmp_path: Path):
    """重复斜杠被消掉。"""
    assert resolve_watch_dir(f"{tmp_path}//") == tmp_path


def test_规范化消掉dot(tmp_path: Path):
    """``.`` 被消掉。"""
    assert resolve_watch_dir(f"{tmp_path}/.") == tmp_path


def test_规范化处理dotdot(tmp_path: Path):
    """``..`` 被词法消解（不碰磁盘）。"""
    sub = tmp_path / "a"
    sub.mkdir()
    assert resolve_watch_dir(f"{sub}/..") == tmp_path


def test_任意路径都允许(tmp_path: Path):
    """**没有白名单**——不在任何白名单里的目录也允许。

    用户明确要求去掉白名单：监听目录完全在面板上配。
    """
    elsewhere = tmp_path.parent / "somewhere_else"
    elsewhere.mkdir(exist_ok=True)
    try:
        assert resolve_watch_dir(str(elsewhere)) == elsewhere
    finally:
        elsewhere.rmdir()


def test_列出子目录(tmp_path: Path):
    """列出子目录（隐藏目录不列）。"""
    (tmp_path / "movies").mkdir()
    (tmp_path / "shows").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_bytes(b"x")

    result = list_subdirs(tmp_path)

    assert result == [str(tmp_path / "movies"), str(tmp_path / "shows")]


def test_列出子目录返回完整路径(tmp_path: Path):
    """**回归测试**：必须返回**完整路径**，不是目录名。

    只返回目录名的话，前端会请求 ``path=mnt``（缺前导 ``/``）
    → 400 → 目录浏览卡在第一层，点不进去。
    """
    (tmp_path / "mnt").mkdir()

    result = list_subdirs(tmp_path)

    assert result == [str(tmp_path / "mnt")]
    assert result[0].startswith("/")


def test_列出不存在的目录返回空(tmp_path: Path):
    """目录读不到 → 空列表，不抛。"""
    assert list_subdirs(tmp_path / "nope") == []
