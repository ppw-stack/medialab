"""驱动类型的契约测试（domain 层单一真源）。

**核心契约**：

1. ``ROOT_DIR_IDS`` —— 各网盘的**账号根目录 id**（浏览起点）
2. ``MEDIA_CID_LABELS`` —— 媒体库目录 id 的**展示名**（前端零分支）

为什么这两份映射放 domain：它们的共同点是「**按驱动类型而异**」——
写进业务代码就会散落成 ``if type == "guangya"`` 的分支网（加新网盘时
必然漏改一处）。
"""

from __future__ import annotations

from app.domain.driver_types import (
    MEDIA_CID_LABELS,
    PROBEABLE_DRIVER_TYPES,
    ROOT_DIR_IDS,
    is_probeable,
    media_cid_label,
    root_dir_id,
)


class TestRootDirId:
    def test_guangya_root_is_empty_string(self) -> None:
        """光鸭的账号根 fileId 就是空串（``list_dir_by_cid("")`` 列的就是根）。"""
        assert root_dir_id("guangya") == ""

    def test_unknown_type_returns_none(self) -> None:
        """未知类型返回 ``None``——调用方决定兜底，不静默给错 id。"""
        assert root_dir_id("nope") is None

    def test_every_registered_type_has_a_root(self) -> None:
        """所有可测连的驱动类型都有根目录映射（漏登记 = 浏览器起点为空）。"""
        for driver_type in PROBEABLE_DRIVER_TYPES:
            assert root_dir_id(driver_type) is not None, driver_type


class TestMediaCidLabel:
    def test_guangya_calls_it_file_id(self) -> None:
        """光鸭叫 fileId——文案写死别的名字会让用户以为存错了。"""
        assert media_cid_label("guangya") == "光鸭媒体库 fileId"

    def test_unknown_type_falls_back_to_generic(self) -> None:
        """未知类型给通用名（不抛异常——文案不值得让请求失败）。"""
        assert media_cid_label("nope") == "媒体库目录 id"

    def test_labels_cover_all_root_mapped_types(self) -> None:
        """有根映射的驱动类型都该有展示名。"""
        for driver_type in ROOT_DIR_IDS:
            assert MEDIA_CID_LABELS.get(driver_type), driver_type


class TestProbeable:
    def test_guangya(self) -> None:
        assert is_probeable("guangya")

    def test_unknown(self) -> None:
        assert not is_probeable("nope")
