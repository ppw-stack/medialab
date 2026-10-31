"""本地文件**彻底删除**的测试。

**这是不可逆操作**，测试要覆盖：

- 只删「确认落盘」的文件（云端 fileId + 大小必须一致）
- 只删监听根内的普通文件（软链 / 目录 / 隐藏文件一律拒绝）
- ``unchanged`` / 失败 / 大小不符 → **不删**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.cloud_index import CloudDirIndex
from app.services.upload_cleanup import (
    DELETE_FAILED,
    DELETE_MISSING,
    DELETE_OK,
    DELETE_REJECTED,
    delete_local_file,
    is_safe_to_delete,
)
from app.services.uploader import (
    RESULT_UNCHANGED,
    RESULT_UPLOADED,
    SyncContext,
    sync_file,
)

from tests.unit.upload_fakes import FakeDriver, FakeUploader

ROOT_ID = "root"


# --------------------------------------------------------------------------
# 路径校验
# --------------------------------------------------------------------------


def test_普通文件可以删(tmp_path: Path):
    """监听根内的普通文件 → 可以删。"""
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x")
    assert is_safe_to_delete(path, tmp_path) is None


def test_软链不删(tmp_path: Path):
    """软链一律不删（可能指向根外）。"""
    outside = tmp_path.parent / "outside_del.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("平台不支持软链")
    assert is_safe_to_delete(link, tmp_path) == "是软链，不删"


def test_目录不删(tmp_path: Path):
    """目录不删。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    assert is_safe_to_delete(sub, tmp_path) == "不是普通文件"


def test_隐藏文件不删(tmp_path: Path):
    """隐藏文件不删。"""
    hidden = tmp_path / ".secret"
    hidden.write_bytes(b"x")
    assert is_safe_to_delete(hidden, tmp_path) == "隐藏文件，不删"


def test_根外文件不删(tmp_path: Path):
    """监听根外的路径 → 拒绝。"""
    root = tmp_path / "watch"
    root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    assert is_safe_to_delete(outside, root) is not None


def test_旧回收目录内的文件不删(tmp_path: Path):
    """历史残留的 ``.strmmanger-trash`` 里的文件不动。"""
    trash = tmp_path / ".strmmanger-trash" / "20260101-000000"
    trash.mkdir(parents=True)
    inner = trash / "old.mkv"
    inner.write_bytes(b"x")
    assert is_safe_to_delete(inner, tmp_path) == "回收目录内的文件，不删"


# --------------------------------------------------------------------------
# 彻底删除
# --------------------------------------------------------------------------


def test_彻底删除文件(tmp_path: Path):
    """文件被 unlink，磁盘上再无此文件。"""
    path = tmp_path / "a.mkv"
    path.write_bytes(b"content")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_OK
    assert not path.exists()
    assert result.removed_path == str(path)


def test_文件不存在返回missing(tmp_path: Path):
    """要删的文件已经不在了 → ``missing``（不算错）。"""
    result = delete_local_file(tmp_path / "nope.mkv", tmp_path)
    assert result.status == DELETE_MISSING


def test_软链拒绝删除(tmp_path: Path):
    """软链 → ``rejected``，软链本身不动。"""
    outside = tmp_path.parent / "outside_del2.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("平台不支持软链")

    result = delete_local_file(link, tmp_path)

    assert result.status == DELETE_REJECTED
    assert link.is_symlink()
    assert outside.exists()


def test_隐藏文件拒绝删除(tmp_path: Path):
    """隐藏文件 → ``rejected``。"""
    hidden = tmp_path / ".secret"
    hidden.write_bytes(b"x")
    result = delete_local_file(hidden, tmp_path)
    assert result.status == DELETE_REJECTED
    assert hidden.exists()


def test_目录拒绝删除(tmp_path: Path):
    """目录 → ``rejected``，目录还在。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    result = delete_local_file(sub, tmp_path)
    assert result.status == DELETE_REJECTED
    assert sub.is_dir()


# --------------------------------------------------------------------------
# 与上传引擎的集成（**核心安全逻辑**）
# --------------------------------------------------------------------------


def _ctx(driver, uploader, root: Path, *, delete_local: bool = False) -> SyncContext:
    return SyncContext(
        driver=driver,
        uploader=uploader,
        index=CloudDirIndex(driver, ROOT_ID),
        root=root,
        delete_retries=2,
        delete_delay=0.01,
        delete_local=delete_local,
    )


async def test_开关关闭时不删本地(tmp_path: Path):
    """``delete_local=False``（默认）→ 上传后本地文件保留。"""
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(_ctx(driver, uploader, tmp_path), path, "a.mkv")

    assert outcome.status == RESULT_UPLOADED
    assert path.exists()


async def test_开关开启时彻底删除(tmp_path: Path):
    """``delete_local=True`` → 上传成功后本地文件被删掉。"""
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status == RESULT_UPLOADED
    assert not path.exists()
    assert outcome.local_deleted is True
    # 没有留下任何回收目录
    assert not (tmp_path / ".strmmanger-trash").exists()


async def test_开关关闭时local_deleted为假(tmp_path: Path):
    """``delete_local=False`` → ``local_deleted`` 必须是 False（面板据此显示）。"""
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(_ctx(driver, uploader, tmp_path), path, "a.mkv")

    assert outcome.local_deleted is False


async def test_云端大小不符则不删(tmp_path: Path):
    """**关键安全校验**：云端大小与本地不一致 → 不删。

    API 返回成功不等于落盘正确——大小是唯一硬证据。
    """
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    original = uploader.upload_file

    async def _wrong_size(p: Path, parent_id: str, *, name: str | None = None, progress=None):
        result = await original(p, parent_id, name=name)
        return type(result)(
            file_id=result.file_id,
            name=result.name,
            size=999,  # 与本地 100 不符
            size_uploaded=result.size_uploaded,
        )

    uploader.upload_file = _wrong_size  # type: ignore[method-assign]

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status == RESULT_UPLOADED
    assert path.exists()  # **没删**
    assert "不一致" in outcome.reason


async def test_云端无fileId则不删(tmp_path: Path):
    """云端没返回 fileId → 不删（无法确认落盘）。"""
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    original = uploader.upload_file

    async def _no_fid(p: Path, parent_id: str, *, name: str | None = None, progress=None):
        result = await original(p, parent_id, name=name)
        return type(result)(
            file_id="",
            name=result.name,
            size=result.size,
            size_uploaded=result.size_uploaded,
        )

    uploader.upload_file = _no_fid  # type: ignore[method-assign]

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert path.exists()
    assert "fileId" in outcome.reason


async def test_上传期间本地被移走(tmp_path: Path):
    """上传完成后本地文件不见了 → 不报错，也不崩。"""
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    original = uploader.upload_file

    async def _vanish(p: Path, parent_id: str, *, name: str | None = None, progress=None):
        result = await original(p, parent_id, name=name)
        p.unlink()  # 模拟上传期间被别人删了
        return result

    uploader.upload_file = _vanish  # type: ignore[method-assign]

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status == RESULT_UPLOADED
    assert outcome.reason == ""  # missing 不算错


async def test_已存在unchanged开删本地则删(tmp_path: Path):
    """``unchanged``（云端已有同名同大小）+ ``delete_local=True`` → **删本地**。

    云端已有同名同大小文件 = 之前传过 = 本地是冗余。开关开着代表用户
    已确认「云端是完整副本，本地可删」——故 UNCHANGED 也走清理。
    （2026-10-31 修正：原语义「不删」已废止。）
    """
    driver, uploader = FakeDriver(), FakeUploader()
    driver.add_file(ROOT_ID, "a.mkv", 100)
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status == RESULT_UNCHANGED
    assert outcome.local_deleted is True
    assert not path.exists()


async def test_已存在unchanged关删本地则不删(tmp_path: Path):
    """``unchanged`` + ``delete_local=False`` → **不删**本地（保留原行为）。

    没开「同步成功后删除本地」时，UNCHANGED 走纯跳过——本地文件保留。
    """
    driver, uploader = FakeDriver(), FakeUploader()
    driver.add_file(ROOT_ID, "a.mkv", 100)
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=False), path, "a.mkv"
    )

    assert outcome.status == RESULT_UNCHANGED
    assert outcome.local_deleted is False
    assert path.exists()


async def test_上传失败不删本地(tmp_path: Path):
    """上传失败 → 本地文件必须保留。"""
    driver, uploader = FakeDriver(), FakeUploader()
    uploader.fail = True
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status != RESULT_UPLOADED
    assert path.exists()


async def test_冲突不删本地(tmp_path: Path):
    """云端同名是目录 → 冲突，本地保留。"""
    driver, uploader = FakeDriver(), FakeUploader()
    driver.add_dir(ROOT_ID, "a.mkv")
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status == "conflict"
    assert path.exists()


def test_清理模块常量齐全():
    """``DELETE_*`` 常量都有定义。"""
    assert DELETE_OK == "ok"
    assert DELETE_MISSING == "missing"
    assert DELETE_REJECTED == "rejected"
    assert DELETE_FAILED == "failed"
