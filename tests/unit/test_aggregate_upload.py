"""落点分配器（:mod:`app.services.aggregate_upload`）的单元测试。

**纯逻辑**：限额墙判定、候选闸门、月份键。不碰 DB。
"""

from __future__ import annotations

import pytest
from app.services.aggregate_upload import (
    LandingCandidate,
    candidate_ok,
    current_month_key,
    is_upload_quota_wall,
    normalize_sub_path,
)
from app.services.errors import ServiceError


def _cand(**kwargs) -> LandingCandidate:
    defaults = {
        "driver_id": 1,
        "driver_name": "副号",
        "driver_type": "guangya",
        "root_id": "root",
        "root_path": "/缓冲",
        "exhausted": False,
        "healthy": None,
        "free_bytes": None,
    }
    defaults.update(kwargs)
    return LandingCandidate(**defaults)


class Test限额墙判定:
    def test_限额类文案命中(self) -> None:
        """额度墙的各种说法都要认出来。"""
        for text in (
            "上传失败：光鸭上传失败：本月上传流量超出限额",
            "上传失败：光鸭上传失败：上传额度已用尽",
            "上传失败：光鸭上传失败：配额不足",
            "上传失败：光鸭上传失败：quota exceeded",
        ):
            assert is_upload_quota_wall(text) is True, text

    def test_限流不算额度墙(self) -> None:
        """限流是抖动——换盘只会把问题放大。"""
        assert is_upload_quota_wall("上传失败：请求过于频繁，请稍后重试") is False
        assert is_upload_quota_wall("上传失败：rate limit") is False
        assert is_upload_quota_wall("上传失败：限流") is False

    def test_网络与凭据错误不算额度墙(self) -> None:
        assert is_upload_quota_wall("上传失败：连接超时") is False
        assert is_upload_quota_wall("上传失败：凭据已失效") is False
        assert is_upload_quota_wall("") is False


class Test候选闸门:
    def test_额度耗尽被拦(self) -> None:
        assert candidate_ok(_cand(exhausted=True), 0) is False

    def test_测连不健康被拦_未知放行(self) -> None:
        assert candidate_ok(_cand(healthy=False), 0) is False
        assert candidate_ok(_cand(healthy=None), 0) is True

    def test_空间不足被拦_未知放行(self) -> None:
        assert candidate_ok(_cand(free_bytes=100), 200) is False
        assert candidate_ok(_cand(free_bytes=100), 50) is True
        assert candidate_ok(_cand(free_bytes=None), 10**12) is True


class Test月份键:
    def test_格式是年月(self) -> None:
        key = current_month_key()
        assert len(key) == 7
        assert key[4] == "-"
        assert key[:4].isdigit()
        assert key[5:].isdigit()


class Test目标子目录归一化:
    """任务级「目标子目录」（组映射的子路径覆盖）的归一化与校验。"""

    def test_空值表示不覆盖(self) -> None:
        assert normalize_sub_path(None) is None
        assert normalize_sub_path("") is None
        assert normalize_sub_path("   ") is None
        assert normalize_sub_path("/") is None

    def test_去掉首尾斜杠与空段(self) -> None:
        assert normalize_sub_path("/电影/外语/") == "电影/外语"
        assert normalize_sub_path("//电影//外语//") == "电影/外语"

    def test_反斜杠当分隔符(self) -> None:
        """Windows 习惯输入的路径分隔符也要认。"""
        assert normalize_sub_path(r"电影\外语") == "电影/外语"

    def test_拒绝路径穿越(self) -> None:
        """``..`` / ``.`` 一律拒绝——「相对路径」必须可推理。"""
        for bad in ("..", "../etc", "电影/../..", "电影/./外语", "."):
            with pytest.raises(ServiceError) as exc:
                normalize_sub_path(bad)
            assert exc.value.status == 400, bad

    def test_拒绝过长的单段(self) -> None:
        with pytest.raises(ServiceError) as exc:
            normalize_sub_path("x" * 300)
        assert exc.value.status == 400

    def test_拒绝过深的层级(self) -> None:
        with pytest.raises(ServiceError) as exc:
            normalize_sub_path("/".join(["a"] * 20))
        assert exc.value.status == 400

    def test_多段合法路径原样保留(self) -> None:
        assert normalize_sub_path("电视剧/国产/Season 01") == (
            "电视剧/国产/Season 01"
        )
