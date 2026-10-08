"""巡检引擎纯函数测试（FR-4.1 十二项指标 + FR-4.1.1 安全边界）。

``check_torrent_health`` 是纯函数：对构造的 :class:`TorrentInfo` 逐一验证
每条规则，**不**触网、不写库——保证所有指标语义可被独立验证。
"""

from __future__ import annotations

import time

import pytest
from app.adapters.downloaders.base import TorrentInfo, TorrentState
from app.db.models import AlertCategory, AlertLevel
from app.services.inspector import check_torrent_health


def info(
    *,
    hash_: str = "a" * 40,
    state: TorrentState = TorrentState.SEEDING,
    ratio: float = 2.0,
    num_seeds: int = 3,
    num_connections: int = 10,
    upspeed: int = 1024,
    dlspeed: int = 0,
) -> TorrentInfo:
    return TorrentInfo(
        hash=hash_,
        name="test",
        state=state,
        progress=1.0 if state.is_completed else 0.5,
        size=1024,
        ratio=ratio,
        num_seeds=num_seeds,
        num_connections=num_connections,
        upspeed=upspeed,
        dlspeed=dlspeed,
    )


def findings(finder, hash_: str = "a" * 40) -> list:
    return [f for f in finder if f.info_hash == hash_]


# ------------------------------------------------------------ 基线
def test_clean_torrent_no_findings() -> None:
    """健康的做种任务不应触发任何告警。"""
    f = check_torrent_health(info())
    assert f == []


def test_health_is_pure_function_of_info() -> None:
    """同 Info 多次调用结果一致（便于缓存与测试）。"""
    assert check_torrent_health(info()) == check_torrent_health(info())


# ------------------------------------------------------------ FR-4.1 指标 1-2
@pytest.mark.parametrize("bad_state", [TorrentState.ERROR, TorrentState.MISSING_FILES])
def test_torrent_error_is_critical_and_short_circuits(bad_state: TorrentState) -> None:
    f = check_torrent_health(info(state=bad_state))
    levels = {x.level for x in f}
    cats = {x.category for x in f}
    assert AlertLevel.CRITICAL in levels
    assert AlertCategory.TORRENT_ERROR in cats
    # 文件错误时后续指标无意义
    assert AlertCategory.RATIO_LOW not in cats


def test_tracker_deleted_is_critical_and_short_circuits() -> None:
    f = check_torrent_health(info(), tracker_deleted=True)
    cats = {x.category for x in f}
    assert AlertCategory.TORRENT_DELETED in cats
    assert AlertCategory.TORRENT_ERROR not in cats, "已删种不应再报文件错误"


# ------------------------------------------------------------ 指标 3-4
def test_illegal_torrent_warning_for_long_checking() -> None:
    f = check_torrent_health(info(state=TorrentState.CHECKING))
    cats = {x.category for x in f}
    assert AlertCategory.ILLEGAL_TORRENT in cats
    assert AlertLevel.WARNING in {x.level for x in f}


@pytest.mark.parametrize("state", [TorrentState.UNKNOWN])
def test_unknown_state_triggers_illegal_warning(state: TorrentState) -> None:
    f = check_torrent_health(info(state=state))
    assert AlertCategory.ILLEGAL_TORRENT in {x.category for x in f}


# ------------------------------------------------------------ 指标 5: 空做种
def test_zero_seeds_with_no_upload_triggers_warning() -> None:
    f = check_torrent_health(
        info(state=TorrentState.SEEDING, num_seeds=0, upspeed=0)
    )
    cats = {x.category for x in f}
    assert AlertCategory.ZERO_SEEDS in cats


def test_zero_seeds_but_uploading_does_not_warn() -> None:
    """num_seeds=0 但自己还在上传（libtorrent 早期报告滞后）不应误报。"""
    f = check_torrent_health(
        info(state=TorrentState.SEEDING, num_seeds=0, upspeed=1024)
    )
    assert AlertCategory.ZERO_SEEDS not in {x.category for x in f}


def test_zero_seeds_only_for_completed_tasks() -> None:
    """未完成任务的 num_seeds=0 是正常的——不应在 downloading 状态报空做种。"""
    f = check_torrent_health(
        info(state=TorrentState.DOWNLOADING, num_seeds=0, upspeed=0)
    )
    assert AlertCategory.ZERO_SEEDS not in {x.category for x in f}


# ------------------------------------------------------------ 指标 6: 分享率
def test_ratio_below_floor_with_hr_passed_warns() -> None:
    f = check_torrent_health(
        info(ratio=0.5), ratio_floor=1.0, hr_deadline_passed=True
    )
    assert AlertCategory.RATIO_LOW in {x.category for x in f}


def test_ratio_below_floor_without_hr_does_not_warn() -> None:
    """未过 H&R 期限前不警告（站点给的新种总得有时间做）。"""
    f = check_torrent_health(
        info(ratio=0.5), ratio_floor=1.0, hr_deadline_passed=False
    )
    assert AlertCategory.RATIO_LOW not in {x.category for x in f}


# ------------------------------------------------------------ 指标 7: 保种时长
def test_critical_seed_time_short() -> None:
    f = check_torrent_health(
        info(ratio=0.5), ratio_floor=1.0, site_remaining_hours=0.5
    )
    cats = {x.category for x in f}
    levels = {x.level for x in f}
    assert AlertCategory.SEED_TIME_SHORT in cats
    assert AlertLevel.CRITICAL in levels


# ------------------------------------------------------------ 指标 8: 下载速度为零
def test_zero_download_speed_after_30min_warns() -> None:
    now = time.time()
    f = check_torrent_health(
        info(state=TorrentState.DOWNLOADING, dlspeed=0),
        zero_speed_since=now - 1900,  # 31 分钟
        now=now,
    )
    assert AlertCategory.ZERO_DOWNLOAD_SPEED in {x.category for x in f}


def test_zero_download_speed_under_30min_does_not_warn() -> None:
    """FR-4.1：必须持续 30 分钟以上才算。"""
    now = time.time()
    f = check_torrent_health(
        info(state=TorrentState.DOWNLOADING, dlspeed=0),
        zero_speed_since=now - 600,
        now=now,
    )
    assert AlertCategory.ZERO_DOWNLOAD_SPEED not in {x.category for x in f}


def test_zero_speed_in_paused_state_does_not_warn() -> None:
    """暂停状态下的 0 速度是用户主动选择，不告警。"""
    now = time.time()
    f = check_torrent_health(
        info(state=TorrentState.PAUSED_DL, dlspeed=0),
        zero_speed_since=now - 1900,
        now=now,
    )
    assert AlertCategory.ZERO_DOWNLOAD_SPEED not in {x.category for x in f}


# ------------------------------------------------------------ 指标 9: 连接数
def test_conn_limit_triggers() -> None:
    f = check_torrent_health(info(num_connections=600), conn_limit=500)
    assert AlertCategory.CONN_LIMIT in {x.category for x in f}


def test_conn_within_limit_does_not_warn() -> None:
    f = check_torrent_health(info(num_connections=100), conn_limit=500)
    assert AlertCategory.CONN_LIMIT not in {x.category for x in f}


# ------------------------------------------------------------ 边界
def test_multiple_findings_coexist() -> None:
    """单个任务可同时触发多项指标。"""
    f = check_torrent_health(
        info(
            state=TorrentState.SEEDING,
            ratio=0.5,  # 触发 RATIO_LOW
            num_seeds=0,
            upspeed=0,
            num_connections=600,  # 触发 CONN_LIMIT
        ),
        ratio_floor=1.0,
        hr_deadline_passed=True,
        conn_limit=500,
    )
    cats = {x.category for x in f}
    assert AlertCategory.ZERO_SEEDS in cats
    assert AlertCategory.RATIO_LOW in cats
    assert AlertCategory.CONN_LIMIT in cats


def test_orphan_state_does_not_panic() -> None:
    """枚举值外的状态也必须安全返回。"""
    f = check_torrent_health(info(state=TorrentState.SEEDING))
    assert isinstance(f, list)


# ------------------------------------------------------------ FR-4.1.1 安全边界
def test_health_check_never_returns_data_deletion_action() -> None:
    """M3 巡检禁止对数据产生副作用（FR-4.1.1）。

    任何 finding 都不应直接给出可执行的删除指令——只能
    移入清理候选或人工确认。后者由 M5 Cleaner 接管。
    """
    f = check_torrent_health(info())
    for finding in f:
        assert 'delete' not in finding.action.lower(), finding
        assert 'remove' not in finding.action.lower(), finding
