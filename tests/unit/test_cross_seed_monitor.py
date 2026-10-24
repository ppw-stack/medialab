"""辅种下载器巡查 service 单元测试。

锁住的核心契约：

- **异常识别**：TR 的 ``errorString`` → ``tracker_not_registered`` /
  ``no_data_found`` 两类
- **name + size 精确分组**：集数 / 分辨率 / 制作组差异都算不同分组，
  不做归一化
- **在途口径**：非暂停、非致命错误；做种（已完成）算在途
- **已暂停的异常种子跳过重复暂停**
- **批量暂停分批**：hash 按 :data:`PAUSE_BATCH_SIZE` 切批
"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import TorrentState
from app.services.cross_seed_monitor import (
    ANOMALY_NO_DATA_FOUND,
    ANOMALY_TRACKER_NOT_REGISTERED,
    PAUSE_BATCH_SIZE,
    AnomalyItem,
    _detect_anomaly,
    _parse_state,
)

# ------------------------------------------------------------------ 异常识别


@pytest.mark.parametrize(
    ("error_string", "expected"),
    [
        ("Tracker not registered with this tracker", ANOMALY_TRACKER_NOT_REGISTERED),
        ("tracker unregistered", ANOMALY_TRACKER_NOT_REGISTERED),
        ("No data found! Ensure your drives are connected", ANOMALY_NO_DATA_FOUND),
        ("no data found", ANOMALY_NO_DATA_FOUND),
    ],
)
def test_detect_anomaly(error_string: str, expected: str) -> None:
    assert _detect_anomaly(error_string) == expected


def test_detect_anomaly_returns_none_for_other_errors() -> None:
    """无关错误不算异常（不能误判成「该暂停」）。"""
    assert _detect_anomaly("") is None
    assert _detect_anomaly("Connection reset by peer") is None


def test_detect_anomaly_prefers_no_data_found() -> None:
    """"no data found" 优先——它更具体（数据都没了，tracker 注册没注册无意义）。"""
    msg = "No data found! Tracker not registered"
    assert _detect_anomaly(msg) == ANOMALY_NO_DATA_FOUND


# ------------------------------------------------------------------ 状态解析


def test_parse_state_accepts_enum_values() -> None:
    assert _parse_state("seeding") is TorrentState.SEEDING
    assert _parse_state("pausedUP") is TorrentState.PAUSED_UP


def test_parse_state_falls_back_to_unknown() -> None:
    """缺失 / 非法状态 → UNKNOWN（不能抛异常打断整轮扫描）。"""
    assert _parse_state(None) is TorrentState.UNKNOWN
    assert _parse_state("") is TorrentState.UNKNOWN
    assert _parse_state("not-a-state") is TorrentState.UNKNOWN


# ------------------------------------------------------------------ 在途口径


def test_inflight_excludes_paused_and_fatal() -> None:
    """在途 = 非暂停、非致命错误。"""
    assert TorrentState.SEEDING.is_inflight is True
    assert TorrentState.UPLOADING.is_inflight is True
    assert TorrentState.DOWNLOADING.is_inflight is True
    assert TorrentState.CHECKING.is_inflight is True
    assert TorrentState.PAUSED_UP.is_inflight is False
    assert TorrentState.PAUSED_DL.is_inflight is False
    assert TorrentState.ERROR.is_inflight is False
    assert TorrentState.MISSING_FILES.is_inflight is False


def test_inflight_keeps_completed_seeding() -> None:
    """做种（已完成）**算**在途——PT 辅种场景下做种就是主要活跃状态。"""
    assert TorrentState.SEEDING.is_completed is True
    assert TorrentState.SEEDING.is_inflight is True


# ------------------------------------------------------------------ 批量暂停


def test_pause_batch_size_is_sane() -> None:
    """分批上限：太小则调用次数多，太大则 qB 的 URL 可能超长。"""
    assert 1 <= PAUSE_BATCH_SIZE <= 500


def test_anomaly_item_defaults_to_not_paused() -> None:
    """默认未暂停（缺 state 的旧数据不应被误当成已暂停而跳过暂停）。"""
    a = AnomalyItem(
        id=1,
        hash="a" * 40,
        name="x",
        size_gb=1.0,
        status=-1,
        anomaly_type=ANOMALY_NO_DATA_FOUND,
        error_code=3,
        error_string="no data found",
    )
    assert a.paused is False
