"""上传（写）能力的驱动协议测试。

**契约的价值**：「数据同步」功能按协议调用，不认具体网盘。新增支持
上传的网盘只须实现 :class:`UploadDriver` 协议 + 在
``UPLOADABLE_DRIVER_TYPES`` 登记——本文件守住这条边界。
"""

from __future__ import annotations

import pytest


def test_光鸭上传器满足协议() -> None:
    """``GuangyaUploader`` 必须满足 ``UploadDriver`` 协议。

    协议方法集变了而光鸭没跟上 → 同步功能在运行时才炸。
    这里提前拦。
    """
    from app.adapters.drivers.guangya_upload import GuangyaUploader
    from app.adapters.drivers.upload_base import UploadDriver

    assert isinstance(GuangyaUploader.__new__(GuangyaUploader), UploadDriver)


def test_协议要求四个方法() -> None:
    """协议必须含这四个方法（上传 / 删除 / 建目录 / 列目录）。

    少一个 → 同步流程走到那一步才 AttributeError。
    """
    from app.adapters.drivers.upload_base import UploadDriver

    for name in ("upload_file", "delete_file", "create_dir", "list_entries"):
        assert hasattr(UploadDriver, name), f"协议缺少 {name}"


def test_可上传类型登记表非空() -> None:
    """至少要登记一个支持上传的驱动——否则同步功能完全不可用。"""
    from app.adapters.drivers.upload_base import UPLOADABLE_DRIVER_TYPES, is_uploadable

    assert UPLOADABLE_DRIVER_TYPES
    assert is_uploadable("guangya")
    assert not is_uploadable("fake")


def test_登记的类型与工厂支持的一致() -> None:
    """登记表与上传器工厂必须一致。

    **不一致的后果**：类型在登记表里但工厂没有分支 → 构造时抛
    「不支持上传」，而前端却把它显示在下拉里。
    """
    from app.adapters.drivers.upload_base import UPLOADABLE_DRIVER_TYPES
    from app.services import upload_driver_factory

    src = upload_driver_factory.__file__
    with open(src, encoding="utf-8") as f:
        content = f.read()
    for driver_type in UPLOADABLE_DRIVER_TYPES:
        assert f'"{driver_type}"' in content, (
            f"{driver_type} 在 UPLOADABLE_DRIVER_TYPES 里登记，"
            "但上传器工厂没有对应分支"
        )


@pytest.mark.parametrize(
    "driver_type",
    ["fake", "unknown", "", "guangyaa"],
)
def test_不支持的类型被拒(driver_type: str) -> None:
    """不支持上传的类型 → 工厂直接抛错（而不是构造到一半才炸）。"""
    import asyncio

    from app.adapters.drivers.base import DriverError
    from app.services.upload_driver_factory import build_upload_clients

    with pytest.raises(DriverError, match="不支持上传"):
        asyncio.run(build_upload_clients(driver_type))
