"""**上传（写）能力**的驱动协议。

与 :mod:`app.adapters.drivers.base` 的 :class:`CloudDriver` 配套：

- :class:`CloudDriver` —— **读**能力（列目录 / 换直链）
- :class:`UploadDriver` —— **写**能力（上传 / 删除 / 建目录）

**为什么要分成两个协议**：不是所有网盘都能写。有的网盘只能列目录 / 换直链，
但「把本地文件传上去」是另一套流程（OSS 分片 + 秒传 + 断点续传）。
把写能力单列成协议，才能让「同步」功能**按驱动声明**支持哪些网盘，
而不是在业务代码里写死 ``if driver.type == "guangya"``。

**新增一个支持上传的网盘**只需三步：

1. 实现本协议的 :class:`UploadDriver`
2. 在 :data:`UPLOADABLE_DRIVER_TYPES` 里加一行
3. 在上传器工厂（:mod:`app.services.upload_driver_factory`）里加构造分支

前端**零改动**——存储源下拉靠 ``/api/upload-tasks/capabilities``
返回的类型列表自动过滤。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class UploadResult:
    """一次上传的结果。

    Attributes:
        file_id: 云端文件标识（上传完成后由驱动返回）。**删除接口只认它**。
        name: 云端文件名（**可能与请求名不同**——部分网盘同名时会自动加
            ``(1)`` 后缀，故这个值才是真实落地的名字）。
        size: 云端字节数（清理本地文件前用它做二次校验）。
        size_uploaded: 实际传输的字节数。
    """

    file_id: str
    name: str
    size: int
    size_uploaded: int = 0


@runtime_checkable
class UploadDriver(Protocol):
    """支持「本地 → 云端」上传的存储源驱动。

    契约（所有实现必须遵守）：

    - :meth:`upload_file` 失败**必须抛** :class:`~app.adapters.drivers.base.DriverError`
      ——返回「失败」标志会让上层把故障伪装成正常状态。
    - :meth:`create_dir` **只能建一层**（名字不含 ``/``）——逐层调用由上层负责。
    - :meth:`list_entries` 返回 ``{文件名: (字节数, file_id)}``——**必须带
      file_id**，否则「删旧文件」这条幂等路径走不通。
    """

    #: 驱动类型标识（``guangya`` 等）
    name: str

    async def upload_file(
        self,
        path: Path,
        parent_id: str,
        *,
        name: str | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> UploadResult:
        """把本地文件上传到 ``parent_id`` 目录。

        Args:
            path: 本地文件路径。
            parent_id: 目标目录 id。
            name: 云端文件名。默认用本地文件名。
            progress: 进度回调（已传字节数）——大文件上传要几分钟，
                没有进度用户会以为卡死了。

        Returns:
            上传结果（含**真实落地**的文件名与 file_id）。

        Raises:
            DriverError: 上传任一步失败。
        """
        ...

    async def delete_file(self, file_id: str) -> None:
        """删除云端文件。

        Args:
            file_id: :attr:`UploadResult.file_id`。

        Raises:
            DriverError: 删除失败。
        """
        ...

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        """在 ``parent_id`` 下建目录（**已存在则复用**）。

        Args:
            name: 目录名（**不含 ``/``**）。
            parent_id: 父目录 id（``""`` = 账号根目录）。

        Returns:
            ``(目录 id, 是否新建)``。

        Raises:
            DriverError: 建目录失败。
        """
        ...

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        """列出目录里的**文件**（不含子目录）。

        Args:
            dir_id: 目录 id。

        Returns:
            ``{文件名: (字节数, file_id)}``。

        Raises:
            DriverError: 列目录失败。
        """
        ...


#: **支持上传的驱动类型**（单一真源）。
#:
#: 后端用它校验「这个存储源能不能作为同步目标」，前端靠
#: ``GET /api/upload-tasks/capabilities`` 拿它来过滤存储源下拉——
#: **两侧读同一份**，加驱动时前端零改动。
UPLOADABLE_DRIVER_TYPES: frozenset[str] = frozenset({"guangya"})


def is_uploadable(driver_type: str) -> bool:
    """该驱动类型是否支持上传（作为同步目标）。"""
    return driver_type in UPLOADABLE_DRIVER_TYPES


__all__ = [
    "UPLOADABLE_DRIVER_TYPES",
    "UploadDriver",
    "UploadResult",
    "is_uploadable",
]
