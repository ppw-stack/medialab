"""上传器工厂（按驱动类型分派）。

**为什么需要它**：「数据同步」功能要支持多个网盘，但各网盘的**上传流程**
差异极大（光鸭走 token → OSS 分片 → 轮询；115 走秒传 + 分片 + 续传）。
业务代码不该知道这些差异——它只认
:class:`app.adapters.drivers.upload_base.UploadDriver` 协议。

本模块是**唯一**知道「哪个网盘怎么构造上传器」的地方：

- 新增支持上传的网盘 → 在 :func:`build_upload_driver` 加一个分支，
  并在 :data:`app.adapters.drivers.upload_base.UPLOADABLE_DRIVER_TYPES`
  加一行。前端**零改动**（存储源下拉靠 capabilities 接口过滤）。
"""

from __future__ import annotations

from typing import Any

from app.adapters.drivers.base import DriverError
from app.adapters.drivers.upload_base import (
    UPLOADABLE_DRIVER_TYPES,
    UploadDriver,
    is_uploadable,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "UPLOADABLE_DRIVER_TYPES",
    "build_upload_clients",
    "is_uploadable",
]


async def build_upload_clients(
    driver_type: str, **kwargs: Any
) -> tuple[Any, UploadDriver]:
    """按驱动类型构造**驱动**与**上传器**。

    Returns:
        ``(driver, uploader)``——``driver`` 提供读能力（列目录 / 换直链），
        ``uploader`` 提供写能力。二者都满足对应协议，调用方不必知道
        具体网盘。

    Raises:
        DriverError: 该驱动不支持上传，或构造失败。
    """
    if not is_uploadable(driver_type):
        raise DriverError(
            f"该存储源不支持上传（当前：{driver_type}）", kind="config"
        )

    if driver_type == "guangya":
        return await _build_guangya_clients(**kwargs)

    # 新增支持上传的网盘时在这里加分支。
    # 记得同时在 UPLOADABLE_DRIVER_TYPES 里登记——否则前端下拉看不到它。
    raise DriverError(
        f"该存储源不支持上传（当前：{driver_type}）", kind="config"
    )


async def _build_guangya_clients(
    *,
    credential: str | None,
    device_type: str | None = None,
    on_token_refresh: Any = None,
) -> tuple[Any, UploadDriver]:
    """构造光鸭驱动 + 上传器。

    **Token 刷新回调**：光鸭 access_token 只有 1~2 小时有效，上传是长跑
    ——必须把刷新后的 token **写回 DB**，否则重启后又用旧 token → 401。
    """
    from app.adapters.drivers.guangya import build_guangya_driver
    from app.adapters.drivers.guangya_upload import build_guangya_uploader

    driver = build_guangya_driver(
        credential, device_type, on_token_refresh=on_token_refresh
    )
    uploader = build_guangya_uploader(driver.client, driver)
    return driver, uploader
