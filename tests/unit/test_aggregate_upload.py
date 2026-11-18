"""落点分配器（:mod:`app.services.aggregate_upload`）的单元测试。

**纯逻辑**：限额墙判定、候选闸门、月份键。不碰 DB。
"""

from __future__ import annotations

from app.services.aggregate_upload import (
    LandingCandidate,
    candidate_ok,
    current_month_key,
    is_upload_quota_wall,
)


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
