"""115 设备类型（app）的**单一真源**。

前端下拉、后端校验、扫码可用性判断——**全部**从这里取，避免出现
「前端手写一份、后端另一份」的不一致（本项目踩过：前端只给了 4 个，
后端根本没校验，扫码时 ``device_type`` 压根没传过去）。

## 实测结论（真实调 115 API 逐个验证，2026-10-27）

``p115client`` 的 ``AVAILABLE_APPS`` 有 **18** 个 app，但**只有 10 个能真正
用于扫码登录**——其余 8 个会返回 ``state=1``（表面成功）却**没有
``qrcode`` 字段**，属静默失败。

两种集合**本就不同**，因此分开维护：

- :data:`ALL_DEVICE_TYPES` —— 全部 18 个，存进 ``drivers.device_type``
  （标识「这个账号以哪个设备登录」，任何值都合法）
- :data:`QR_CAPABLE_DEVICE_TYPES` —— 其中能出二维码的 10 个，
  **扫码登录时**才要求

## 库的自动归一化（读 :mod:`app.adapters.drivers.p115_qr` 前必看）

``p115client`` 的 ``login_qrcode_scan_result`` 内部会改写 app：

============================  ==============================
传入                          实际生效
============================  ==============================
``desktop``                   ``web``
``windows`` / ``mac`` / ``linux``  ``os_windows`` / ``os_mac`` / ``os_linux``
``ios`` / ``qios``            ``ios``（UA 设为 ``UPhone/1.0.0`` / ``OfficePhone/1.0.0``）
``ipad`` / ``qipad``          ``ios``（UA 设为 ``UPad/1.0.0`` / ``OfficePad/1.0.0``）
============================  ==============================

注意 ``ipad`` / ``qipad`` 最终**等价于** ``ios``——只是请求 UA 不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DeviceType:
    """一个设备类型。

    Attributes:
        value: 传给 115 API 的 app 值。
        label: 界面显示名。
        qr_capable: 是否**能用于扫码登录**（实测：能否取到 ``qrcode``）。
        note: 补充说明（如「实际等价于 ios」）。
    """

    value: str
    label: str
    qr_capable: bool
    note: str = ""


#: 全部设备类型（18 个）—— 与 ``p115client.const.AVAILABLE_APPS`` 一致
ALL_DEVICE_TYPES: tuple[DeviceType, ...] = (
    DeviceType("115android", "115 Android", True, "推荐；避开 web 的 IP 敏感"),
    DeviceType("115ios", "115 iOS", True),
    DeviceType("115ipad", "115 iPad", False, "扫码不支持（实际等价 ios）"),
    DeviceType("android", "Android", True),
    DeviceType("harmony", "HarmonyOS", True),
    DeviceType("ios", "iOS", True),
    DeviceType("ipad", "iPad", False, "扫码不支持（实际等价 ios）"),
    DeviceType("qandroid", "115 管理 Android", True),
    DeviceType("qios", "115 管理 iOS", True),
    DeviceType("qipad", "115 管理 iPad", False, "扫码不支持（实际等价 ios）"),
    DeviceType("web", "网页版", True, "最易触发「IP登录异常」，需次日零点解禁"),
    DeviceType("tv", "115 TV", False, "扫码不支持"),
    DeviceType("apple_tv", "Apple TV", False, "扫码不支持"),
    DeviceType("os_windows", "Windows 客户端", False, "扫码不支持"),
    DeviceType("os_mac", "macOS 客户端", False, "扫码不支持"),
    DeviceType("os_linux", "Linux 客户端", False, "扫码不支持"),
    DeviceType("wechatmini", "微信小程序", True),
    DeviceType("alipaymini", "支付宝小程序", True),
)

#: 全部设备类型的值集合
ALL_DEVICE_TYPE_VALUES: frozenset[str] = frozenset(d.value for d in ALL_DEVICE_TYPES)

#: **能用于扫码登录**的设备类型（实测 10 个）
QR_CAPABLE_DEVICE_TYPES: tuple[DeviceType, ...] = tuple(
    d for d in ALL_DEVICE_TYPES if d.qr_capable
)

#: 能扫码的设备类型的值集合
QR_CAPABLE_DEVICE_TYPE_VALUES: frozenset[str] = frozenset(
    d.value for d in QR_CAPABLE_DEVICE_TYPES
)

#: 默认设备类型——``115android``
#:
#: 不用 ``web``：115 对 ``app="web"`` 的扫码登录最敏感，频繁触发
#: 「IP登录异常」，且**要等次日零点才解禁**（官方文档明确说明）。
DEFAULT_DEVICE_TYPE = "115android"


def is_qr_capable(device_type: str | None) -> bool:
    """该设备类型能否用于扫码登录。"""
    if not device_type:
        return False
    return device_type in QR_CAPABLE_DEVICE_TYPE_VALUES


def get_device_type(value: str | None) -> DeviceType | None:
    """按值查设备类型。"""
    if not value:
        return None
    for device in ALL_DEVICE_TYPES:
        if device.value == value:
            return device
    return None


def describe_qr_incapable(device_type: str) -> str:
    """给「不能扫码的设备类型」生成可读的提示文案。"""
    device = get_device_type(device_type)
    if device is None:
        return f"设备类型 {device_type} 不支持扫码登录"
    note = f"（{device.note}）" if device.note else ""
    return f"设备类型「{device.label}」不支持扫码登录{note}"


#: 供 FastAPI 生成 OpenAPI 用的 Literal（可选值 = 全部 18 个）
DeviceTypeLiteral = Literal[
    "115android",
    "115ios",
    "115ipad",
    "android",
    "harmony",
    "ios",
    "ipad",
    "qandroid",
    "qios",
    "qipad",
    "web",
    "tv",
    "apple_tv",
    "os_windows",
    "os_mac",
    "os_linux",
    "wechatmini",
    "alipaymini",
]
