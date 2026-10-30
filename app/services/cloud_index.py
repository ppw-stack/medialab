"""云端目录索引（fileId 缓存 + 文件清单）。

**为什么抽出来**：迁移（:mod:`app.services.migration`）与上传
（:mod:`app.services.uploader`）都要做同一件事：

1. 把「相对目录路径」解析成「云端 fileId」
2. 列出目录里的文件做幂等比对

两份实现各写一遍的话，光鸭的坑（同名自动改名、删后需重列确认）会被
踩两次。故抽成公共模块，**单一真源**。

**幂等靠云端，不靠本地状态**：目标目录里「同名 + 同大小」= 已同步。
这样容器重启不丢进度，重跑天然跳过。

**缓存内容**：``{文件名: CloudEntry(size, file_id)}``——**必须带 fileId**：
上传要删旧文件，而删除接口只认 fileId。
"""

from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CloudEntry:
    """云端目录里的一个文件。

    Attributes:
        name: 文件名。
        size: 字节数。
        file_id: 光鸭 fileId（删除接口只认它）。
    """

    name: str
    size: int
    file_id: str


class CloudDirIndex:
    """一个 run / 一个任务内共享的目标目录索引。

    缓存两层：

    - ``_dir_ids``：相对目录 → fileId
    - ``_files``：相对目录 → ``{文件名: CloudEntry}``

    **并发保护**：目录解析是低频操作（每个目录一次），用一把锁串行化，
    避免「两个 worker 同时给同一目录建副本」的竞态。

    目录创建走 ``driver.create_dir(name, parent_id)``——**只能一层**
    （光鸭 ``fs_create_dir`` 不接受带 ``/`` 的名字，实测报 ``code 141``）。
    """

    def __init__(self, driver: Any, root_id: str = "") -> None:
        """构造索引。

        Args:
            driver: 存储源驱动（需有 ``list_files`` / ``create_dir``）。
            root_id: 根目录 fileId（"" = 账号根目录）。
        """
        self._driver = driver
        self._root = str(root_id or "")
        self._dir_ids: dict[str, str] = {"": self._root}
        self._dir_ids_rev: dict[str, str] = {self._root: ""}
        self._files: dict[str, dict[str, CloudEntry]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, rel_dir: str) -> tuple[str, dict[str, CloudEntry]]:
        """解析相对目录，返回 ``(fileId, {文件名: CloudEntry})``。

        目录不存在就建。返回的是缓存的**副本**——调用方随便改都不会污染缓存。
        """
        rel = normalize_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            entries = self._files.get(rel)
            if entries is None:
                entries = await self._list(dir_id)
                self._files[rel] = entries
            return dir_id, dict(entries)

    async def refresh(self, rel_dir: str) -> dict[str, CloudEntry]:
        """**重新**列目录（删文件后校验用，绕过缓存）。"""
        rel = normalize_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            entries = await self._list(dir_id)
            self._files[rel] = entries
            return dict(entries)

    async def note_uploaded(
        self, rel_dir: str, name: str, size: int, file_id: str
    ) -> None:
        """登记「已上传」的文件。

        **必须更新缓存**——否则后续对同一目录的预检看不到刚上传的文件，
        缓存与云端实际状态不一致。
        """
        rel = normalize_dir(rel_dir)
        async with self._lock:
            entries = self._files.setdefault(rel, {})
            entries[name] = CloudEntry(name=name, size=size, file_id=file_id)

    async def note_removed(self, rel_dir: str, name: str) -> None:
        """登记「已删除」的文件（从缓存里摘掉）。"""
        rel = normalize_dir(rel_dir)
        async with self._lock:
            entries = self._files.get(rel)
            if entries is not None:
                entries.pop(name, None)

    def relative_dir_of(self, dir_id: str) -> str:
        """由 fileId 反查相对目录（供刷新指定目录用）。

        找不到时返回空串（根目录）——**这是保守选择**：宁可多刷一次根目录，
        也不要刷错目录。
        """
        return self._dir_ids_rev.get(str(dir_id), "")

    async def _list(self, dir_id: str) -> dict[str, CloudEntry]:
        """列目录里的文件（带 fileId）。"""
        lister = getattr(self._driver, "list_entries", None)
        if lister is not None:
            raw = await lister(dir_id)
            return {
                str(name): CloudEntry(name=str(name), size=int(size), file_id=str(fid))
                for name, (size, fid) in raw.items()
            }
        # 退化路径：只有 ``list_files``（无 fileId）——删除时用不了
        raw = await self._driver.list_files(dir_id)
        return {
            str(name): CloudEntry(name=str(name), size=int(size), file_id="")
            for name, size in raw.items()
        }

    async def _resolve_dir(self, rel: str) -> str:
        """逐层解析相对目录到 fileId（沿途建缺失的目录）。"""
        cached = self._dir_ids.get(rel)
        if cached is not None:
            return cached

        parent_rel = posixpath.dirname(rel)
        name = posixpath.basename(rel)
        parent_id = await self._resolve_dir(parent_rel)
        dir_id, created = await self._driver.create_dir(name, parent_id)
        dir_id = str(dir_id)
        self._dir_ids[rel] = dir_id
        self._dir_ids_rev[dir_id] = rel
        if created:
            # 新建目录必定为空，省掉一次列表请求
            self._files[rel] = {}
        return dir_id


def normalize_dir(rel: str) -> str:
    """规范化相对目录（去掉首尾斜杠与空段）。"""
    return "/".join(p for p in rel.strip("/").split("/") if p)


__all__ = ["CloudDirIndex", "CloudEntry", "normalize_dir"]
