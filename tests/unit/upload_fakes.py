"""上传相关的测试固件。

**不连真网盘**：全部用假的驱动 / 上传器，只验证「决策逻辑」与
「路径校验」——真正的网盘行为在真实账号上实测过（见
:mod:`app.adapters.drivers.guangya_upload` 的模块文档）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.adapters.drivers.base import CloudFile, DriverError
from app.services.cloud_index import CloudDirIndex


class FakeUploader:
    """假上传器（记录调用，不真的传）。"""

    def __init__(self, *, fail: bool = False) -> None:
        #: 传过的文件：``[(name, size)]``
        self.uploaded: list[tuple[str, int]] = []
        #: 删过的 fileId
        self.deleted: list[str] = []
        #: 模拟失败
        self.fail = fail
        #: 传完后自动改名的规则（``{原名: 新名}``）
        self.rename_map: dict[str, str] = {}
        self._counter = 0

    async def upload_file(
        self,
        path: Path,
        parent_id: str,
        *,
        name: str | None = None,
        progress: Any = None,
    ) -> Any:
        """假上传。"""
        if self.fail:
            raise DriverError("假上传失败", kind="unknown")
        file_name = name or path.name
        size = path.stat().st_size
        self._counter += 1
        self.uploaded.append((file_name, size))
        if progress is not None:
            progress(size)
        from app.adapters.drivers.guangya_upload import UploadResult

        return UploadResult(
            file_id=f"fid-{self._counter}",
            name=self.rename_map.get(file_name, file_name),
            size=size,
            size_uploaded=size,
        )

    async def delete_file(self, file_id: str) -> None:
        """假删除。"""
        if self.fail:
            raise DriverError("假删除失败", kind="unknown")
        self.deleted.append(file_id)


class FakeDriver:
    """假光鸭驱动（内存里的目录树）。"""

    def __init__(self) -> None:
        #: ``{dir_id: {文件名: (size, fileId)}}``
        self.files: dict[str, dict[str, tuple[int, str]]] = {}
        #: ``{dir_id: {目录名: fileId}}``
        self.dirs: dict[str, dict[str, str]] = {}
        #: 目录 id 分配器
        self._next_id = 1000
        #: 创建目录时是否报告「已存在」
        self.existing_dirs: set[str] = set()

    # -- 测试辅助 --

    def add_file(self, dir_id: str, name: str, size: int) -> str:
        """往目录里塞一个文件，返回 fileId。"""
        self._next_id += 1
        file_id = f"fid-{self._next_id}"
        self.files.setdefault(dir_id, {})[name] = (size, file_id)
        return file_id

    def add_dir(self, parent_id: str, name: str) -> str:
        """建一个子目录，返回 fileId。"""
        self._next_id += 1
        dir_id = f"dir-{self._next_id}"
        self.dirs.setdefault(parent_id, {})[name] = dir_id
        return dir_id

    def remove_file(self, dir_id: str, name: str) -> None:
        """删一个文件（模拟云端删除生效）。"""
        self.files.get(dir_id, {}).pop(name, None)

    # -- 驱动协议 --

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        """列出文件（带 fileId）。"""
        return dict(self.files.get(dir_id, {}))

    async def list_files(self, dir_id: str) -> dict[str, int]:
        """列出文件（只要大小）。"""
        return {name: size for name, (size, _) in self.files.get(dir_id, {}).items()}

    async def list_subdirs(self, dir_id: str) -> list[CloudFile]:
        """列出子目录。"""
        return [
            CloudFile(remote_id=cid, path=f"/{name}", name=name, is_dir=True)
            for name, cid in self.dirs.get(dir_id, {}).items()
        ]

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        """建一层目录。"""
        existing = self.dirs.get(parent_id, {}).get(name)
        if existing is not None:
            return existing, False
        self._next_id += 1
        dir_id = f"dir-{self._next_id}"
        self.dirs.setdefault(parent_id, {})[name] = dir_id
        self.dirs.setdefault(dir_id, {})
        self.files.setdefault(dir_id, {})
        return dir_id, True


@pytest.fixture
def fake_driver() -> FakeDriver:
    """假驱动实例。"""
    return FakeDriver()


@pytest.fixture
def fake_uploader() -> FakeUploader:
    """假上传器实例。"""
    return FakeUploader()


@pytest.fixture
def make_index(fake_driver: FakeDriver):
    """构造索引的工厂。"""

    def _make(root_id: str = "root") -> CloudDirIndex:
        return CloudDirIndex(fake_driver, root_id)

    return _make
