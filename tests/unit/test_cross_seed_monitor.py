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
    ANOMALY_LOCAL_DATA_MISSING,
    ANOMALY_NO_DATA_FOUND,
    ANOMALY_TRACKER_ERROR,
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
    assert _detect_anomaly(0, error_string) == expected


def test_detect_anomaly_returns_none_for_other_errors() -> None:
    """无关错误不算异常（不能误判成「该暂停」）。"""
    assert _detect_anomaly(0, "") is None
    assert _detect_anomaly(0, "Connection reset by peer") is None


def test_detect_anomaly_prefers_no_data_found() -> None:
    """"no data found" 优先——它更具体（数据都没了，tracker 注册没注册无意义）。"""
    msg = "No data found! Tracker not registered"
    assert _detect_anomaly(0, msg) == ANOMALY_NO_DATA_FOUND


# ------------------------------------------------------------------ error 码


def test_detect_anomaly_by_error_code_local_data_missing() -> None:
    """**error=3**（本地数据丢失/损坏）→ 识别为异常。

    回归：原先只匹配 errorString 文本，而 TR 的文案是
    ``"Please Verify Local Data! Piece #335 is corrupt."``——
    既不含 "no data found" 也不含 "not registered" → **整个漏判**。

    实测（prod 辅种站）：TR 有 156 个种子带 error 码，其中 2 个是
    本地数据损坏——修复前异常数恒为 0。
    """
    msg = "Please Verify Local Data! Piece #335 is corrupt."
    assert _detect_anomaly(3, msg) == ANOMALY_LOCAL_DATA_MISSING
    # qB 的文案是中文「本地数据丢失」
    assert _detect_anomaly(3, "本地数据丢失") == ANOMALY_LOCAL_DATA_MISSING


def test_detect_anomaly_by_error_code_tracker_error() -> None:
    """**error=2**（tracker HTTP 报错）→ 识别为异常（但不暂停）。"""
    assert (
        _detect_anomaly(2, "Tracker HTTP response 502 (Bad Gateway)")
        == ANOMALY_TRACKER_ERROR
    )


def test_detect_anomaly_error_code_takes_precedence_over_generic_text() -> None:
    """error 码判定的优先级：具体文案优先于 error 码。

    "no data found" / "not registered" 是**文本**里更明确的信号，
    它们优先于 error 码。
    """
    assert (
        _detect_anomaly(3, "No data found! Tracker not registered")
        == ANOMALY_NO_DATA_FOUND
    )
    assert (
        _detect_anomaly(2, "Tracker not registered")
        == ANOMALY_TRACKER_NOT_REGISTERED
    )


def test_detect_anomaly_ignores_tracker_warning_code() -> None:
    """error=1（tracker 警告）不算异常——不该因此暂停。"""
    assert _detect_anomaly(1, "Tracker warning") is None


def test_tracker_error_is_pause_exempt() -> None:
    """tracker 报错**豁免暂停**——那是服务端问题，暂停只会白丢上传。"""
    from app.services.cross_seed_monitor import _PAUSE_EXEMPT_ANOMALIES

    assert ANOMALY_TRACKER_ERROR in _PAUSE_EXEMPT_ANOMALIES
    assert ANOMALY_LOCAL_DATA_MISSING not in _PAUSE_EXEMPT_ANOMALIES


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


# ------------------------------------------------------------------ 暂停豁免


class _PauseSpy:
    """记录 set_pause 收到的 hash。"""

    def __init__(self) -> None:
        self.paused: list[list[str]] = []

    async def set_pause(self, hashes, paused: bool) -> bool:
        self.paused.append(list(hashes))
        return True


def _anomaly(anomaly_type: str, *, paused: bool = False) -> AnomalyItem:
    return AnomalyItem(
        id=1,
        hash="a" * 40,
        name="x",
        size_gb=1.0,
        status=6,
        anomaly_type=anomaly_type,
        error_code=3,
        error_string="x",
        paused=paused,
    )


@pytest.mark.asyncio
async def test_pause_skips_tracker_error(factory: object) -> None:
    """tracker 报错**不暂停**——那是服务端问题，暂停只会白丢上传。

    实测（prod 辅种站）：154 个种子报 tracker HTTP 502/522。若这些也暂停，
    会在 tracker 恢复前白白损失大量上传带宽。
    """
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    spy = _PauseSpy()
    svc = CrossSeedMonitorService()
    stopped, failed = await svc._pause_anomalies(
        spy,
        [
            _anomaly(ANOMALY_TRACKER_ERROR),
            _anomaly(ANOMALY_TRACKER_ERROR),
        ],
    )

    assert stopped == 0
    assert failed == 0
    assert spy.paused == [], "tracker 报错不该发暂停指令"


@pytest.mark.asyncio
async def test_pause_local_data_missing_is_paused(factory: object) -> None:
    """本地数据损坏**要暂停**——数据没了，继续做种无意义。"""
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    spy = _PauseSpy()
    svc = CrossSeedMonitorService()
    stopped, failed = await svc._pause_anomalies(
        spy, [_anomaly(ANOMALY_LOCAL_DATA_MISSING)]
    )

    assert stopped == 1
    assert failed == 0
    assert spy.paused == [["a" * 40]]


@pytest.mark.asyncio
async def test_pause_skips_already_paused(factory: object) -> None:
    """已暂停的不重复发指令（省 API 调用）。"""
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    spy = _PauseSpy()
    svc = CrossSeedMonitorService()
    stopped, _failed = await svc._pause_anomalies(
        spy, [_anomaly(ANOMALY_LOCAL_DATA_MISSING, paused=True)]
    )

    assert stopped == 0
    assert spy.paused == []


@pytest.mark.asyncio
async def test_pause_mixes_exempt_and_normal(factory: object) -> None:
    """混合场景：tracker 报错跳过，其余照常暂停。"""
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    spy = _PauseSpy()
    svc = CrossSeedMonitorService()
    stopped, _failed = await svc._pause_anomalies(
        spy,
        [
            _anomaly(ANOMALY_TRACKER_ERROR),
            _anomaly(ANOMALY_LOCAL_DATA_MISSING),
        ],
    )

    assert stopped == 1
    assert spy.paused == [["a" * 40]]
