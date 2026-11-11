"""媒体后缀默认值的契约测试（domain 层单一真源）。

**核心契约**：三个默认清单与 ``parse_ext_list`` 的输入格式一致——
逗号分隔、无点前缀、小写。格式破坏会让 ``parse_ext_list`` 之外的新
消费方（如前端预填）拿到脏值。
"""

from __future__ import annotations

from app.domain.media_exts import (
    DEFAULT_DATA_EXTS,
    DEFAULT_IMAGE_EXTS,
    DEFAULT_VIDEO_EXTS,
)
from app.services.naming import parse_ext_list


def _assert_normalized(exts: str) -> None:
    """清单必须是规范形态：逗号分隔、无点前缀、小写、无空项。"""
    parts = exts.split(",")
    assert all(p.strip() == p for p in parts), exts  # 不带首尾空格
    assert all(p and p == p.lower() for p in parts), exts  # 非空、小写
    assert not any(p.startswith(".") for p in parts), exts


def test_video_defaults_are_normalized() -> None:
    _assert_normalized(DEFAULT_VIDEO_EXTS)
    assert "mkv" in DEFAULT_VIDEO_EXTS.split(",")
    assert "iso" in DEFAULT_VIDEO_EXTS.split(",")


def test_image_defaults_are_normalized() -> None:
    _assert_normalized(DEFAULT_IMAGE_EXTS)
    assert {"jpg", "jpeg", "png", "webp"} <= set(DEFAULT_IMAGE_EXTS.split(","))


def test_data_defaults_are_normalized() -> None:
    _assert_normalized(DEFAULT_DATA_EXTS)
    assert {"nfo", "ass", "ssa", "srt"} <= set(DEFAULT_DATA_EXTS.split(","))


def test_defaults_roundtrip_through_parse_ext_list() -> None:
    """三个清单经 ``parse_ext_list`` 归一后仍是原集合（格式真正兼容）。"""
    assert parse_ext_list(DEFAULT_VIDEO_EXTS) == frozenset(
        f".{p}" for p in DEFAULT_VIDEO_EXTS.split(",")
    )
    assert parse_ext_list(DEFAULT_IMAGE_EXTS) == frozenset(
        f".{p}" for p in DEFAULT_IMAGE_EXTS.split(",")
    )
    assert parse_ext_list(DEFAULT_DATA_EXTS) == frozenset(
        f".{p}" for p in DEFAULT_DATA_EXTS.split(",")
    )


def test_single_source_for_video_exts() -> None:
    """``DEFAULT_VIDEO_EXTS`` 只有 domain 一处定义（models/naming 不再各写一份）。"""
    import inspect

    import app.db.models as models
    import app.services.naming as naming

    assert models.DEFAULT_VIDEO_EXTS == DEFAULT_VIDEO_EXTS
    # naming 的死常量已删除——不再有第二份清单
    assert not hasattr(naming, "DEFAULT_VIDEO_EXTS")
    assert not hasattr(naming, "DEFAULT_IMAGE_EXTENSIONS")
    assert not hasattr(naming, "DEFAULT_DATA_EXTENSIONS")
    # 确认 models 是从 domain import 的（不是本地重定义）
    src = inspect.getsource(models)
    assert "from app.domain.media_exts import" in src
    assert 'DEFAULT_VIDEO_EXTS = "mkv' not in src
