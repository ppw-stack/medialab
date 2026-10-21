"""领域层：业务概念与常量（不依赖具体技术层）。"""

from __future__ import annotations

from app.domain.enums import (
    NON_ABORTABLE_FREE_TYPES,
    TYPES_WITHOUT_WINDOW,
    FreeSource,
    FreeType,
    PushStatus,
)

__all__ = [
    "NON_ABORTABLE_FREE_TYPES",
    "TYPES_WITHOUT_WINDOW",
    "FreeSource",
    "FreeType",
    "PushStatus",
]
