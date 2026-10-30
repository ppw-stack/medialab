"""领域层：业务概念与常量（不依赖具体技术层）。"""

from __future__ import annotations

from app.domain.device_types import (
    ALL_DEVICE_TYPES,
    DEFAULT_DEVICE_TYPE,
    QR_CAPABLE_DEVICE_TYPES,
    DeviceType,
    DeviceTypeLiteral,
)
from app.domain.enums import (
    NON_ABORTABLE_FREE_TYPES,
    TYPES_WITHOUT_WINDOW,
    FreeSource,
    FreeType,
    PushStatus,
)

__all__ = [
    "ALL_DEVICE_TYPES",
    "DEFAULT_DEVICE_TYPE",
    "NON_ABORTABLE_FREE_TYPES",
    "QR_CAPABLE_DEVICE_TYPES",
    "TYPES_WITHOUT_WINDOW",
    "DeviceType",
    "DeviceTypeLiteral",
    "FreeSource",
    "FreeType",
    "PushStatus",
]
