"""上传引擎的决策逻辑测试。

覆盖 :mod:`app.services.uploader` 的决策表：

- 无同名 → 上传
- 同名 + 同大小 → 跳过
- 同名 + 不同大小 → 删旧再传（覆盖）
- 同名但云端是目录 → 冲突
- 上传失败 → 记失败，不抛异常
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.drivers.base import DriverError
from app.services.cloud_index import CloudDirIndex
from app.services.uploader import (
    RESULT_CONFLICT,
    RESULT_FAILED,
    RESULT_OVERWRITTEN,
    RESULT_UNCHANGED,
    RESULT_UPLOADED,
    SyncContext,
    sync_file,
)

from tests.unit.upload_fakes import FakeDriver, FakeUploader

ROOT_ID = "root"


def _ctx(
    driver: FakeDriver,
    uploader: FakeUploader,
    root: Path,
) -> SyncContext:
    return SyncContext(
        driver=driver,
        uploader=uploader,
        index=CloudDirIndex(driver, ROOT_ID),
        root=root,
        delete_retries=3,
        delete_delay=0.01,
    )


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    """造一个本地文件（内容 100 字节）。"""
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * 100)
    return path


async def test_无同名则上传(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """云端没有同名文件 → 直接上传。"""
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_UPLOADED
    assert upload_uploader.uploaded == [("movie.mkv", 100)]
    assert upload_uploader.deleted == []


async def test_同名同大小则跳过(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """云端已有同名同大小 → 跳过（幂等）。"""
    upload_driver.add_file(ROOT_ID, "movie.mkv", 100)
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_UNCHANGED
    assert upload_uploader.uploaded == []
    assert upload_uploader.deleted == []


async def test_同名不同大小则删旧再传(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """云端同名但大小不同 → 先删旧、确认消失、再传新。"""
    old_id = upload_driver.add_file(ROOT_ID, "movie.mkv", 50)
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)

    # 让删除「立即生效」：模拟云端删掉
    original_delete = upload_uploader.delete_file

    async def _delete(file_id: str) -> None:
        await original_delete(file_id)
        upload_driver.remove_file(ROOT_ID, "movie.mkv")

    upload_uploader.delete_file = _delete  # type: ignore[method-assign]

    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_OVERWRITTEN
    assert upload_uploader.deleted == [old_id]
    assert upload_uploader.uploaded == [("movie.mkv", 100)]


async def test_同名但是目录则冲突(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """云端同名项是目录 → 记冲突，不自动处理。"""
    upload_driver.add_dir(ROOT_ID, "movie.mkv")
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_CONFLICT
    assert upload_uploader.uploaded == []
    assert upload_uploader.deleted == []


async def test_上传失败不抛异常(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """上传失败 → 折叠成 failed，不抛。"""
    upload_uploader.fail = True
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_FAILED
    assert "上传失败" in outcome.reason


async def test_子目录会逐层创建(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """相对路径含子目录 → 云端逐层建目录。"""
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, local_file, "a/b/movie.mkv")

    assert outcome.status == RESULT_UPLOADED
    # 根下有 a，a 下有 b
    assert "a" in upload_driver.dirs.get(ROOT_ID, {})
    a_id = upload_driver.dirs[ROOT_ID]["a"]
    assert "b" in upload_driver.dirs.get(a_id, {})


async def test_删除未生效则放弃覆盖(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """删完旧文件仍在 → 放弃覆盖（宁可不覆盖也不产重名副本）。"""
    upload_driver.add_file(ROOT_ID, "movie.mkv", 50)
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    # 不让删除生效 —— upload_driver 里文件还在
    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_FAILED
    assert "仍然存在" in outcome.reason
    assert upload_uploader.uploaded == []
    assert upload_uploader.deleted  # 确实尝试删了


async def test_本地文件不存在则失败(tmp_path: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader):
    """本地文件读不到 → 失败。"""
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, tmp_path / "nope.mkv", "nope.mkv")

    assert outcome.status == RESULT_FAILED
    assert "读取本地文件失败" in outcome.reason


async def test_同名但缺少fileId则拒绝覆盖(local_file: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader, tmp_path: Path):
    """云端旧文件没有 fileId → 拒绝删除（宁可不覆盖）。"""
    upload_driver.files.setdefault(ROOT_ID, {})["movie.mkv"] = (50, "")
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    outcome = await sync_file(ctx, local_file, "movie.mkv")

    assert outcome.status == RESULT_FAILED
    assert "fileId" in outcome.reason
    assert upload_uploader.deleted == []


async def test_驱动失败时记失败(tmp_path: Path, upload_driver: FakeDriver, upload_uploader: FakeUploader):
    """驱动异常 → 折叠成 failed。"""

    async def _boom(_dir_id: str) -> dict[str, tuple[int, str]]:
        raise DriverError("列目录失败", kind="upstream")

    upload_driver.list_entries = _boom  # type: ignore[method-assign]
    ctx = _ctx(upload_driver, upload_uploader, tmp_path)
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x")

    outcome = await sync_file(ctx, path, "a.mkv")
    assert outcome.status == RESULT_FAILED
    assert "解析云端目录失败" in outcome.reason
