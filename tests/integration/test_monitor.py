"""免费窗口巡检测试（Phase 1）。

monitor 是「到点必停」的执行者（FR-3.4）——用户规则：
**当前不是免费种就直接暂停**。这套行为在本次 Phase 1 里**不能变**，
故补测试钉住。

关键契约：

- 永久免费（PERMANENT）→ 永不暂停
- 限时种（TIMED / TWOU_TIMED）→ 距截止 < pre_pause 分钟才暂停
- 折扣/未知/退还（NEUTRAL / UNKNOWN / REFUND）→ **立即暂停**
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.adapters.downloaders.base import TorrentInfo, TorrentState
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import (
    DownloaderInstance,
    FreeType,
    PushStatus,
    Site,
    TorrentIndex,
)
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.monitor import MonitorFreeWindowService
from app.services.runtime_params import (
    RuntimeParamManager,
    set_runtime_params,
)


@pytest.fixture(autouse=True)
def _runtime_params() -> None:
    mgr = RuntimeParamManager()
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_download_interval_ms", 0)
    set_runtime_params(mgr)
    yield
    set_runtime_params(None)  # type: ignore[arg-type]


class _FakeDownloader:
    """记录 set_pause 调用的假下载器。"""

    def __init__(self, torrents: list[TorrentInfo]) -> None:
        self._torrents = torrents
        self.paused: list[list[str]] = []

    async def list_torrents(self, filt: object = None) -> list[TorrentInfo]:
        return self._torrents

    async def set_pause(self, hashes: list[str], paused: bool) -> bool:
        self.paused.append(list(hashes))
        return True

    async def close(self) -> None:
        return None


async def _setup(
    *,
    free_type: str,
    free_end_at: str | None = None,
    progress: float = 0.5,
    state: TorrentState = TorrentState.DOWNLOADING,
) -> _FakeDownloader:
    """建默认下载器 + 索引行 + 假下载器，返回假下载器。"""
    hash_ = "a" * 40
    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="hdsky"))
        session.add(
            DownloaderInstance(
                name="dl1",
                type="qbittorrent",
                url="http://localhost:8081",
                is_default=True,
                enabled=True,
            )
        )
        session.add(
            TorrentIndex(
                site_id=1,
                item_id="i1",
                name="t",
                free_type=free_type,
                free_end_at=free_end_at,
                push_status=PushStatus.PUSHED,
                info_hash=hash_,
            )
        )
        await session.flush()

    return _FakeDownloader(
        [
            TorrentInfo(
                hash=hash_,
                name="t",
                state=state,
                progress=progress,
                size=1000,
                downloaded=500,
            )
        ]
    )


async def _run(fake: _FakeDownloader) -> object:
    """跑一次巡检（注入假下载器）。"""
    factory = AdapterFactory()
    factory.downloader = lambda row, **kw: fake  # type: ignore[method-assign]
    svc = MonitorFreeWindowService(factory)
    return await svc.run()


# ------------------------------------------------------------------ 永久免费

async def test_permanent_free_is_never_paused(db: None) -> None:
    """永久免费：用户全程做种赚积分，**永不暂停**。"""
    fake = await _setup(free_type=FreeType.PERMANENT)
    result = await _run(fake)
    assert fake.paused == [], "永久免费不应被暂停"
    assert result.skipped_permanent == 1


# ------------------------------------------------------------------ 限时种

async def test_timed_far_from_deadline_is_not_paused(db: None) -> None:
    """距截止还很远 → 不暂停。"""
    end = format_bj(bj_now_dt() + timedelta(hours=24))
    fake = await _setup(free_type=FreeType.TIMED, free_end_at=end)
    result = await _run(fake)
    assert fake.paused == []
    assert result.skipped_far_from_window == 1


async def test_timed_near_deadline_is_paused(db: None) -> None:
    """距截止 < pre_pause 分钟 → 暂停（FR-3.4 到点必停）。"""
    end = format_bj(bj_now_dt() + timedelta(minutes=3))
    fake = await _setup(free_type=FreeType.TIMED, free_end_at=end)
    result = await _run(fake)
    assert fake.paused == [["a" * 40]]
    assert result.pre_paused == 1


async def test_expired_timed_is_paused(db: None) -> None:
    """已过期的限时种 → 暂停。"""
    end = format_bj(bj_now_dt() - timedelta(hours=1))
    fake = await _setup(free_type=FreeType.TIMED, free_end_at=end)
    result = await _run(fake)
    assert fake.paused == [["a" * 40]]
    assert result.pre_paused == 1


async def test_twoup_timed_follows_same_window_rule(db: None) -> None:
    """2X 限时与普通限时同规则。"""
    end = format_bj(bj_now_dt() + timedelta(minutes=3))
    fake = await _setup(free_type=FreeType.TWOU_TIMED, free_end_at=end)
    result = await _run(fake)
    assert fake.paused == [["a" * 40]]
    assert result.pre_paused == 1


# ------------------------------------------------- 非免费种（本次修复的关键）

async def test_neutral_is_paused_immediately(db: None) -> None:
    """NEUTRAL（半价/三折）→ **立即暂停**。

    用户规则「当前不是免费种，直接暂停」。这是本次 Phase 1 修复
    NEUTRAL 语义矛盾时**必须保持不变**的运行时行为。
    """
    fake = await _setup(free_type=FreeType.NEUTRAL)
    result = await _run(fake)
    assert fake.paused == [["a" * 40]], "NEUTRAL 应被立即暂停"
    assert result.pre_paused == 1


async def test_unknown_is_paused_immediately(db: None) -> None:
    """UNKNOWN → 立即暂停。"""
    fake = await _setup(free_type=FreeType.UNKNOWN)
    result = await _run(fake)
    assert fake.paused == [["a" * 40]]
    assert result.pre_paused == 1


async def test_refund_is_paused_immediately(db: None) -> None:
    """REFUND（退还种）→ 立即暂停（与 NEUTRAL/UNKNOWN 同路径）。"""
    fake = await _setup(free_type=FreeType.REFUND)
    result = await _run(fake)
    assert fake.paused == [["a" * 40]]
    assert result.skipped_refund == 1
    assert result.pre_paused == 1


# ------------------------------------------------------------------ 其他

async def test_completed_torrent_is_skipped(db: None) -> None:
    """已完成（progress=1）的任务不处理。"""
    fake = await _setup(free_type=FreeType.TIMED, progress=1.0)
    result = await _run(fake)
    assert fake.paused == []
    assert result.skipped_already_completed == 1


async def test_already_paused_is_skipped(db: None) -> None:
    """已暂停的任务不重复处理。"""
    fake = await _setup(
        free_type=FreeType.NEUTRAL, state=TorrentState.PAUSED_DL
    )
    result = await _run(fake)
    assert fake.paused == []
    assert result.skipped_already_paused == 1


async def test_no_default_downloader_is_graceful(db: None) -> None:
    """未配置默认下载器 → 跳过，不抛异常。"""
    factory = AdapterFactory()
    svc = MonitorFreeWindowService(factory)
    result = await svc.run()
    assert result.pre_paused == 0
    assert "未配置默认下载器" in result.summary


async def test_unmatched_torrent_is_skipped(db: None) -> None:
    """下载器里有、但 torrent_index 没 tracked 的任务 → 不动它。"""
    async with session_scope() as session:
        session.add(
            DownloaderInstance(
                name="dl1",
                type="qbittorrent",
                url="http://localhost:8081",
                is_default=True,
                enabled=True,
            )
        )
        await session.flush()

    fake = _FakeDownloader(
        [TorrentInfo(hash="b" * 40, name="unknown", state=TorrentState.DOWNLOADING)]
    )
    result = await _run(fake)
    assert fake.paused == []
    assert result.skipped_unmatched == 1
