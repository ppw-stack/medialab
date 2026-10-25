"""辅种 API 的 DTO 契约测试。

**防回归的关键用例**：``SnapshotHeaderOut`` / ``RefreshResultOut`` 这类
pydantic DTO 必须在**每个字段**上都与 service 层的 dataclass 对齐。

为什么需要这条测试（真实踩过的坑）：
    2026-10-25 加「辅种空间统计」时，只给 service 层的
    :class:`~app.services.cross_seed_monitor.SnapshotHeader` 加了空间列，
    **忘了**同步 API 层的 ``SnapshotHeaderOut``。pydantic 对未声明的
    字段**静默丢弃**（不报错）→ 端点正常返回 200，但响应里没有
    ``used_bytes`` / ``disk_free_bytes``。

    那个 bug 在本地 dev（DB 里没数据）完全测不出来——只有 prod 上有
    真实数据时才暴露。本文件把「DTO 字段集 ⊇ 扁平化后的 dataclass 字段集」
    钉死。
"""
from __future__ import annotations

import pytest


def _flat_fields(obj: object) -> set[str]:
    """dataclass 的**扁平**字段集（把嵌套 dataclass 展开）。

    ``ScanResult`` 里 ``space`` 是嵌套的 :class:`SpaceStats`（用
    ``field(default_factory=SpaceStats)``），它的字段在响应里是**平铺**的
    （``to_dict`` 用 ``**self.space.to_dict()`` 展开）。故比较时要按同样
    的规则展开，否则会把 ``space`` 当成「缺失字段」。
    """
    from dataclasses import MISSING, fields, is_dataclass

    out: set[str] = set()
    for f in fields(obj):  # type: ignore[arg-type]
        out.add(f.name)
        # 嵌套 dataclass：default 是实例（default_factory）或 MISSING
        nested = None
        if f.default is not MISSING and is_dataclass(f.default):
            nested = f.default
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            nested = f.default_factory()  # type: ignore[misc]
        if nested is not None and is_dataclass(nested):
            out |= {f"{f.name}.{n}" for n in _flat_fields(nested)}
    return out


def _dto_flat_fields(dto: object) -> set[str]:
    """pydantic DTO 的字段集（**平铺**语义，与 service 层一致）。

    service 层用 ``**self.space.to_dict()`` 把嵌套的 space 摊平成顶层键
    （``used_bytes`` 而不是 ``space.used_bytes``），故 DTO 侧也是平铺的
    ——比较时要把 dataclass 的 ``space.xxx`` 折成 ``xxx``。
    """
    out: set[str] = set()
    for name in dto.model_fields:  # type: ignore[attr-defined]
        out.add(name)
    return out


def _fold_space(fields_set: set[str]) -> set[str]:
    """把 ``space.xxx`` 折成 ``xxx``（与 ``to_dict`` 的摊平语义对齐）。

    ``space`` 本身是嵌套 dataclass，在响应里被摊平——它**不是**一个响应
    字段，故要排除。
    """
    out: set[str] = set()
    for f in fields_set:
        if f == "space":
            continue
        out.add(f.split(".", 1)[1] if f.startswith("space.") else f)
    return out


def test_snapshot_header_out_covers_dataclass_fields() -> None:
    """``SnapshotHeaderOut`` 必须覆盖 ``SnapshotHeader`` 的**全部**字段。

    少一个 → pydantic 静默丢弃 → 端点返回 200 但缺字段。
    """
    from app.api.routes.cross_seed import SnapshotHeaderOut
    from app.services.cross_seed_monitor import SnapshotHeader

    missing = _fold_space(_flat_fields(SnapshotHeader)) - _dto_flat_fields(
        SnapshotHeaderOut
    )
    assert not missing, (
        f"SnapshotHeaderOut 缺少字段 {sorted(missing)}——"
        f"pydantic 会静默丢弃它们，端点返回 200 但响应里没有这些字段"
    )


def test_refresh_result_out_covers_dataclass_fields() -> None:
    """``RefreshResultOut`` 必须覆盖 ``ScanResult`` 的全部字段。"""
    from app.api.routes.cross_seed import RefreshResultOut
    from app.services.cross_seed_monitor import ScanResult

    missing = _fold_space(_flat_fields(ScanResult)) - _dto_flat_fields(
        RefreshResultOut
    )
    assert not missing, (
        f"RefreshResultOut 缺少字段 {sorted(missing)}——pydantic 会静默丢弃它们"
    )


@pytest.mark.parametrize(
    "field",
    [
        "used_bytes",
        "total_size_bytes",
        "disk_free_bytes",
        "disk_total_bytes",
    ],
)
def test_space_fields_are_exposed(field: str) -> None:
    """空间相关的 4 个字段必须在两个 DTO 里都在。

    单独钉住，避免上面那条漏掉某几个时看不出问题在哪。
    """
    from app.api.routes.cross_seed import RefreshResultOut, SnapshotHeaderOut

    assert field in SnapshotHeaderOut.model_fields, f"SnapshotHeaderOut 缺 {field}"
    assert field in RefreshResultOut.model_fields, f"RefreshResultOut 缺 {field}"
