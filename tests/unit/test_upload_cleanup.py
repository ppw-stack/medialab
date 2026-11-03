"""本地文件**彻底删除**的测试。

**这是不可逆操作**，测试要覆盖：

- 只删「确认落盘」的文件（云端 fileId + 大小必须一致）
- 只删监听根内的普通文件（软链 / 目录 / 隐藏文件一律拒绝）
- ``unchanged`` / 失败 / 大小不符 → **不删**
- **删完文件顺手清空空目录**（向上递归到监听根，不含根）
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
    is_safe_to_prune_dir,
    prune_empty_parents,
)
from app.services.uploader import (
    RESULT_UNCHANGED,
    RESULT_UPLOADED,
    SyncContext,
    drain_cleanup_tasks,
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
    """``delete_local=True`` → 上传成功后本地文件被删掉。

    删除是**异步**的（2026-10-31）——CIFS 上删大文件要几十秒到几分钟，
    同步做会让 worker 空转。故 ``sync_file`` 返回时文件可能还在，
    要等后台任务完成。
    """
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    assert outcome.status == RESULT_UPLOADED
    assert outcome.local_deleted is True
    # 删除是后台任务——等它完成
    await drain_cleanup_tasks(wait_sec=5)
    assert not path.exists()
    # 没有留下任何回收目录
    assert not (tmp_path / ".strmmanger-trash").exists()


async def test_删除是异步的_不阻塞sync_file返回(tmp_path: Path):
    """删除在后台跑——``sync_file`` **不等**它完成就返回。

    动机：CIFS 上删一个 6.9 GB 文件要几十秒到几分钟（等 SMB 服务器释放
    句柄）。同步做会让 worker 长时间空转，吞吐白白损失。
    """
    driver, uploader = FakeDriver(), FakeUploader()
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True), path, "a.mkv"
    )

    # 返回时「已安排删除」，但文件可能还在（后台任务还没跑完）
    assert outcome.local_deleted is True
    # 最终会被删掉
    await drain_cleanup_tasks(wait_sec=5)
    assert not path.exists()


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
    # 删除是后台任务——等它完成
    await drain_cleanup_tasks(wait_sec=5)
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


# --------------------------------------------------------------------------
# 空目录清理
#
# **这是不可逆操作**——删的是磁盘上的目录。测试要覆盖：
#
# - 只删**空**目录（``rmdir`` 内核保证，非空必失败）
# - 向上递归但**停在监听根**（根是用户骨架，永不删）
# - 隐藏目录 / 软链 / 根外目录一律不动
# - 非空目录 → 停止向上（不越级删更上层）
# --------------------------------------------------------------------------


def test_删完文件后空目录被删(tmp_path: Path):
    """文件删掉后，它所在的空目录一并删掉。"""
    sub = tmp_path / "片名"
    sub.mkdir()
    path = sub / "a.mkv"
    path.write_bytes(b"content")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_OK
    assert not sub.exists()
    assert result.removed_dirs == [str(sub)]


def test_空目录递归向上删(tmp_path: Path):
    """**链式向上**：``a/b/c/x.mkv`` 删掉后 ``c`` → ``b`` → ``a`` 全删。

    只到监听根为止（根不删）。
    """
    a = tmp_path / "a"
    b = a / "b"
    c = b / "c"
    c.mkdir(parents=True)
    path = c / "x.mkv"
    path.write_bytes(b"x")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_OK
    assert not c.exists()
    assert not b.exists()
    assert not a.exists()
    # **监听根必须还在**
    assert tmp_path.exists()
    # 自下而上
    assert result.removed_dirs == [str(c), str(b), str(a)]


def test_监听根本身不删(tmp_path: Path):
    """监听根是用户指定的骨架 → 永不删除。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    path = sub / "a.mkv"
    path.write_bytes(b"x")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_OK
    assert not sub.exists()
    assert tmp_path.is_dir()


def test_非空目录保留且停止向上(tmp_path: Path):
    """父目录里还有别的文件 → 保留它，**且不再往上删**。

    上层必然也非空（含这个非空目录）→ 越级删是灾难。
    """
    a = tmp_path / "a"
    b = a / "b"
    b.mkdir(parents=True)
    path = b / "a.mkv"
    path.write_bytes(b"x")
    # b 里还有另一个文件 → b 非空
    keep = b / "keep.mkv"
    keep.write_bytes(b"y")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_OK
    assert not path.exists()
    # b 非空 → 保留
    assert b.is_dir()
    assert keep.exists()
    # a 里面还有 b → 也保留（不该越级）
    assert a.is_dir()
    assert result.removed_dirs == []


def test_隐藏目录不删(tmp_path: Path):
    """隐藏目录里的文件删掉后，隐藏目录本身保留。"""
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    path = hidden / "a.mkv"
    path.write_bytes(b"x")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_OK
    assert not path.exists()
    # 隐藏目录保留
    assert hidden.is_dir()


def test_回收目录内的文件不删(tmp_path: Path):
    """``.strmmanger-trash`` 里的文件**根本不许删**（既有行为）。

    故它里面的目录更不会被清空。
    """
    trash = tmp_path / ".strmmanger-trash" / "20260101-000000"
    trash.mkdir(parents=True)
    path = trash / "old.mkv"
    path.write_bytes(b"x")

    result = delete_local_file(path, tmp_path)

    assert result.status == DELETE_REJECTED
    assert path.exists()
    assert trash.is_dir()


def test_软链目录不删(tmp_path: Path):
    """软链目录里的文件**根本不许删**（软链逃逸，既有行为）。

    故软链目录更不会被清空——真实目录里的文件也不受影响。
    """
    outside = tmp_path.parent / "outside_prune"
    outside.mkdir(exist_ok=True)
    (outside / "other.mkv").write_bytes(b"x")

    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("平台不支持软链")

    path = link / "a.mkv"
    path.write_bytes(b"x")

    result = delete_local_file(path, tmp_path)

    # 软链内的文件被拒（``is_safe_to_delete`` 拦下）
    assert result.status == DELETE_REJECTED
    assert path.exists()
    # 软链本身还在
    assert link.is_symlink()
    # 真实目录里的文件没被动
    assert (outside / "other.mkv").exists()
    assert result.removed_dirs == []


def test_根外目录不删(tmp_path: Path):
    """监听根外的目录不删。"""
    root = tmp_path / "watch"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "a.mkv"
    path.write_bytes(b"x")

    result = delete_local_file(path, root)

    # 根外的文件本来就拒绝删
    assert result.status == DELETE_REJECTED
    assert outside.is_dir()


def test_目录安全闸校验(tmp_path: Path):
    """``is_safe_to_prune_dir`` 的四道闸。"""
    root = tmp_path / "watch"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    hidden = root / ".hidden"
    hidden.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("平台不支持软链")

    # 监听根 → 不删
    assert is_safe_to_prune_dir(root, root) == "是监听根，不删"
    # 普通子目录 → 可以
    assert is_safe_to_prune_dir(sub, root) is None
    # 隐藏目录 → 不删
    assert is_safe_to_prune_dir(hidden, root) == "隐藏目录内，不删"
    # 根外 → 不删
    assert is_safe_to_prune_dir(outside, root) == "软链逃出监听根"
    # 软链 → 不删
    assert is_safe_to_prune_dir(link, root) == "是软链，不删"


def test_prune_empty_parents_空目录全删(tmp_path: Path):
    """直接调 ``prune_empty_parents`` —— 空目录全部删掉。"""
    a = tmp_path / "a"
    b = a / "b"
    b.mkdir(parents=True)

    removed = prune_empty_parents(b, tmp_path)

    assert removed == [str(b), str(a)]
    assert not a.exists()
    assert tmp_path.exists()


def test_prune_empty_parents_非空即停(tmp_path: Path):
    """非空目录 → 删不掉，且**停止向上**。"""
    a = tmp_path / "a"
    b = a / "b"
    b.mkdir(parents=True)
    (b / "keep.mkv").write_bytes(b"x")

    removed = prune_empty_parents(b, tmp_path)

    assert removed == []
    assert b.is_dir()
    assert a.is_dir()


def test_prune_empty_parents_已不存在则继续向上(tmp_path: Path):
    """起点目录已被并发任务删了 → 继续向上（父目录可能也空了）。"""
    a = tmp_path / "a"
    b = a / "b"
    b.mkdir(parents=True)
    b.rmdir()

    removed = prune_empty_parents(b, tmp_path)

    # b 不存在（跳过），a 空了 → 被删
    assert removed == [str(a)]
    assert not a.exists()


def test_prune_empty_parents_rmdir遇ENOENT则继续向上(tmp_path: Path, monkeypatch):
    """**回归测试**：`rmdir``ENOENT``（并发竞态）→ 继续向上，不许停。

    ``_try_rmdir`` 对 ``ENOENT`` 返回 ``(False, False)``——第二个 ``False``
    的语义是「**继续向上**」（父目录可能也空了）。若调用方误把它当
    「停止」，并发删除时就会漏清上层空目录。

    **只让第一层失败**：上层 ``a`` 必须能正常删掉——这样才能区分
    「继续向上」（``a`` 被删）与「错误地停止」（``a`` 留着）。
    """
    a = tmp_path / "a"
    b = a / "b"
    b.mkdir(parents=True)

    import errno as _errno

    real_rmdir = Path.rmdir

    def _rmdir(self: Path) -> None:
        # 只让 ``b`` 失败——模拟真实的竞态：**目录刚被并发任务删掉**
        # （先真删掉，再抛 ENOENT）。
        # 必须真删，否则 ``a`` 确实非空 → ``a`` 保留 → 测不出「是否继续向上」。
        if self.name == "b":
            real_rmdir(self)
            raise FileNotFoundError(
                _errno.ENOENT, "No such file or directory", str(self)
            )
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", _rmdir)

    removed = prune_empty_parents(b, tmp_path)

    # b 删不掉（ENOENT）→ **继续向上** → a 空了 → 被删
    assert removed == [str(a)]
    assert not a.exists()


async def test_上传删除后目录一并清理(tmp_path: Path):
    """**集成**：`sync_file``delete_local=True`` → 目录一并清掉。"""
    driver, uploader = FakeDriver(), FakeUploader()
    sub = tmp_path / "影视" / "片名"
    sub.mkdir(parents=True)
    path = sub / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True),
        path,
        "影视/片名/a.mkv",
    )

    assert outcome.status == RESULT_UPLOADED
    assert outcome.local_deleted is True
    await drain_cleanup_tasks(wait_sec=5)
    # 文件与目录都没了
    assert not path.exists()
    assert not sub.exists()
    assert not (tmp_path / "影视").exists()
    # 监听根还在
    assert tmp_path.exists()


async def test_不删本地时目录保留(tmp_path: Path):
    """``delete_local=False`` → 文件都不删，目录**更不能**删。"""
    driver, uploader = FakeDriver(), FakeUploader()
    sub = tmp_path / "片名"
    sub.mkdir()
    path = sub / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=False),
        path,
        "片名/a.mkv",
    )

    assert outcome.status == RESULT_UPLOADED
    assert outcome.local_deleted is False
    await drain_cleanup_tasks(wait_sec=5)
    assert path.exists()
    assert sub.is_dir()


async def test_上传失败时目录保留(tmp_path: Path):
    """上传失败 → 文件都不删，目录**更不能**删。"""
    driver, uploader = FakeDriver(), FakeUploader()
    uploader.fail = True
    sub = tmp_path / "片名"
    sub.mkdir()
    path = sub / "a.mkv"
    path.write_bytes(b"x" * 100)

    outcome = await sync_file(
        _ctx(driver, uploader, tmp_path, delete_local=True),
        path,
        "片名/a.mkv",
    )

    assert outcome.status != RESULT_UPLOADED
    assert path.exists()
    assert sub.is_dir()
