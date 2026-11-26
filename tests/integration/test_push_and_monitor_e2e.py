"""免费种「监测清理 → 腾槽 → 动态水位补位」端到端闭环集成测试。

全部使用隔离 SQLite 测试库与内存状态机 FakeDownloader（严禁接触生产数据），
覆盖：
1. 堵塞 → 监测精准清理自推死种（不动手动/校验中/排队中任务）→ 推送段按剩余水位精准补位；
2. 满水位空转不刷新节流 → 槽位释放后下轮立即补推；
3. 单轮异常/停滞清理上限（``monitor_max_actions_per_run``）拦截停滞雪崩，且不阻塞临期窗口止损；
4. 删除模式结构化审计日志与 Prometheus ``/metrics`` 指标接线验证。
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

import pytest
from app.adapters.downloaders.base import (
    AddTorrentPayload,
    DownloaderAdapter,
    TorrentInfo,
    TorrentState,
)
from app.core.metrics import render_metrics
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.factory import AdapterFactory
from app.services.push_and_monitor import reset_throttle_state, run_push_and_monitor
from app.services.runtime_params import RuntimeParamManager, set_runtime_params


def _make_torrent_bytes(name: str) -> bytes:
    """构造最小合法 bencoded .torrent，确保 infohash_from_torrent 可算出确定 40 位 hex。"""
    name_b = name.encode("utf-8")
    info = (
        b"d6:lengthi1024e4:name"
        + f"{len(name_b)}:".encode()
        + name_b
        + b"12:piece lengthi16384e6:pieces20:"
        + b"\x00" * 20
        + b"e"
    )
    return b"d4:info" + info + b"e"


class _StatefulFakeDownloader(DownloaderAdapter):
    """会真实维护任务状态的假下载器（pause / remove / add 均实时反映到 list_torrents）。"""

    def __init__(self, initial: list[TorrentInfo]) -> None:
        self.torrents: dict[str, TorrentInfo] = {
            t.hash.lower(): t for t in initial
        }
        self.paused_hashes: list[str] = []
        self.removed: list[tuple[str, bool]] = []
        self.added_names: list[str] = []

    async def ping(self) -> bool:
        return True

    async def list_torrents(self, *, category: str | None = None) -> list[TorrentInfo]:
        return list(self.torrents.values())

    async def add_torrent(self, payload: AddTorrentPayload) -> str:
        from app.adapters.sites._bencode import infohash_from_torrent

        raw = payload.torrent_bytes or b""
        h = infohash_from_torrent(raw).lower()
        name = f"added-{len(self.added_names) + 1}"
        self.added_names.append(name)
        self.torrents[h] = TorrentInfo(
            hash=h,
            name=name,
            state=TorrentState.DOWNLOADING,
            progress=0.0,
            size=1024,
            completed_bytes=0,
            dlspeed=200 * 1024,
            added_at=int(time.time()),
        )
        return h

    async def set_pause(self, hashes: list[str], *, paused: bool) -> None:
        for h in hashes:
            key = h.lower()
            self.paused_hashes.append(key)
            cur = self.torrents.get(key)
            if cur is not None:
                self.torrents[key] = TorrentInfo(
                    hash=cur.hash,
                    name=cur.name,
                    state=TorrentState.PAUSED_DL if paused else TorrentState.DOWNLOADING,
                    progress=cur.progress,
                    size=cur.size,
                    completed_bytes=cur.completed_bytes,
                    dlspeed=0 if paused else cur.dlspeed,
                    added_at=cur.added_at,
                )

    async def remove(self, hashes: list[str], *, delete_data: bool = False) -> None:
        for h in hashes:
            key = h.lower()
            self.removed.append((key, delete_data))
            self.torrents.pop(key, None)


class _FakeSiteAdapter:
    async def download_torrent(self, item_id: str) -> bytes:
        return _make_torrent_bytes(f"cand-{item_id}")


class _FakeFactory(AdapterFactory):
    def __init__(self, dl: _StatefulFakeDownloader) -> None:
        super().__init__()
        self._dl = dl
        self._site = _FakeSiteAdapter()

    def downloader(self, row: DownloaderInstance) -> DownloaderAdapter:
        return self._dl

    def site(self, row: Site) -> Any:
        return self._site


@pytest.fixture(autouse=True)
def _reset_state() -> Any:
    reset_throttle_state()
    mgr = RuntimeParamManager()
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_max_active_downloads", 10)
    mgr.set_memory("push_download_interval_ms", 0)
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("monitor_delete_expired", 0)
    mgr.set_memory("monitor_stalled_timeout_minutes", 60)
    mgr.set_memory("monitor_min_dlspeed_kbps", 50)
    mgr.set_memory("monitor_max_actions_per_run", 5)
    set_runtime_params(mgr)
    yield mgr
    reset_throttle_state()
    set_runtime_params(None)  # type: ignore[arg-type]


async def _seed_base() -> tuple[int, int]:
    async with session_scope() as session:
        site = Site(name="e2e-site", domain="e2e.test", adapter="generic", enabled=True)
        dl = DownloaderInstance(
            name="e2e-qb",
            type="qbittorrent",
            url="http://localhost:8081",
            is_default=True,
            enabled=True,
        )
        session.add_all([site, dl])
        await session.flush()
        return site.id, dl.id


async def _add_index(
    site_id: int,
    *,
    item_id: str,
    info_hash: str | None = None,
    free_type: str = FreeType.PERMANENT,
    free_end_at: str | None = None,
    push_status: str | None = None,
) -> None:
    async with session_scope() as session:
        session.add(
            TorrentIndex(
                site_id=site_id,
                item_id=item_id,
                info_hash=info_hash,
                name=f"item-{item_id}",
                free_type=free_type,
                free_end_at=free_end_at,
                push_status=push_status,
            )
        )


def _h(ch: str) -> str:
    return ch * 40


async def test_e2e_clog_cleanup_and_refill_protects_manual_tasks(db: None) -> None:
    """堵塞 → 监测清理自推死种（保护手动/校验中/排队中）→ 推送段按空闲槽位精准补推。"""
    site_id, _ = await _seed_base()
    now_ts = int(time.time())
    old_ts = now_ts - 120 * 60
    far_end = format_bj(bj_now_dt() + timedelta(hours=24))

    # 已推任务：2 停滞（限时+永久）+ 1 致命错误 + 1 校验中已完成 + 1 排队超时 + 6 正常下载
    for item_id, h, ft, end in [
        ("stalled-timed", _h("1"), FreeType.TIMED, far_end),
        ("stalled-perm", _h("2"), FreeType.PERMANENT, None),
        ("fatal-err", _h("3"), FreeType.PERMANENT, None),
        ("checking-done", _h("4"), FreeType.PERMANENT, None),
        ("queued-old", _h("5"), FreeType.PERMANENT, None),
        ("normal-1", _h("6"), FreeType.PERMANENT, None),
        ("normal-2", _h("7"), FreeType.PERMANENT, None),
        ("normal-3", _h("8"), FreeType.PERMANENT, None),
        ("normal-4", _h("9"), FreeType.PERMANENT, None),
        ("normal-5", _h("a"), FreeType.PERMANENT, None),
        ("normal-6", _h("b"), FreeType.PERMANENT, None),
    ]:
        await _add_index(
            site_id,
            item_id=item_id,
            info_hash=h,
            free_type=ft,
            free_end_at=end,
            push_status=PushStatus.PUSHED,
        )

    # 5 个未推送候选（本轮空闲槽位应只有 2 个，故只补推 2 个）
    for i in range(5):
        await _add_index(
            site_id, item_id=f"cand-{i}", free_type=FreeType.PERMANENT, push_status=None
        )

    # 初始在途任务数（is_active_download）：
    # stalled-timed(1) + stalled-perm(1) + queued-old(1) + normal*6(6) + manual-stalled(1) = 10（满水位）
    # fatal-err / manual-fatal / checking-done 不计入在途
    initial = [
        TorrentInfo(hash=_h("1"), name="stalled-timed", state=TorrentState.STALLED_DL, progress=0.2, size=1000, completed_bytes=200, dlspeed=0, added_at=old_ts),
        TorrentInfo(hash=_h("2"), name="stalled-perm", state=TorrentState.DOWNLOADING, progress=0.1, size=1000, completed_bytes=100, dlspeed=10 * 1024, added_at=old_ts),
        TorrentInfo(hash=_h("3"), name="fatal-err", state=TorrentState.ERROR, progress=0.1, size=1000, completed_bytes=100, dlspeed=0, added_at=old_ts),
        TorrentInfo(hash=_h("4"), name="checking-done", state=TorrentState.CHECKING, progress=0.2, size=1000, completed_bytes=1000, dlspeed=0, added_at=old_ts),
        TorrentInfo(hash=_h("5"), name="queued-old", state=TorrentState.DOWNLOADING, progress=0.0, size=1000, completed_bytes=0, dlspeed=0, added_at=old_ts, is_queued=True),
        *[
            TorrentInfo(hash=_h(c), name=f"normal-{c}", state=TorrentState.DOWNLOADING, progress=0.5, size=1000, completed_bytes=500, dlspeed=500 * 1024, added_at=now_ts)
            for c in ("6", "7", "8", "9", "a", "b")
        ],
        # 用户直接在下载器添加的停滞 & 致命错误任务（不在 torrent_index）——绝不能被清理
        TorrentInfo(hash=_h("e"), name="manual-stalled", state=TorrentState.STALLED_DL, progress=0.1, size=1000, completed_bytes=100, dlspeed=0, added_at=old_ts),
        TorrentInfo(hash=_h("f"), name="manual-fatal", state=TorrentState.ERROR, progress=0.1, size=1000, completed_bytes=100, dlspeed=0, added_at=old_ts),
    ]
    fake_dl = _StatefulFakeDownloader(initial)
    factory = _FakeFactory(fake_dl)

    out = await run_push_and_monitor(factory)

    # 监测段：仅清理 3 个自推死种，绝不动手动任务/校验中/排队中/正常种
    assert set(fake_dl.paused_hashes) == {_h("1"), _h("2"), _h("3")}
    assert out["monitor"]["aborted_stalled"] == 2
    assert out["monitor"]["aborted_fatal"] == 1
    assert out["monitor"]["remaining_active_downloads"] == 8

    # 推送段：水位 10，剩余在途 8 → 精准补推 2 个
    assert out["push"]["active_downloads"] == 8
    assert out["push"]["selected"] == 2
    assert out["push"]["pushed"] == 2
    assert len(fake_dl.added_names) == 2


async def test_e2e_capacity_idle_does_not_block_next_refill(
    db: None, _reset_state: RuntimeParamManager
) -> None:
    """满水位空转不刷新节流 → 槽位释放后紧接着下一轮立即补推。"""
    _reset_state.set_memory("push_max_active_downloads", 2)
    site_id, _ = await _seed_base()
    now_ts = int(time.time())

    for i in range(4):
        await _add_index(site_id, item_id=f"c-{i}", free_type=FreeType.PERMANENT)

    initial = [
        TorrentInfo(hash=_h("1"), name="active-1", state=TorrentState.DOWNLOADING, progress=0.5, size=1000, completed_bytes=500, dlspeed=500 * 1024, added_at=now_ts),
        TorrentInfo(hash=_h("2"), name="active-2", state=TorrentState.DOWNLOADING, progress=0.5, size=1000, completed_bytes=500, dlspeed=500 * 1024, added_at=now_ts),
    ]
    fake_dl = _StatefulFakeDownloader(initial)
    factory = _FakeFactory(fake_dl)

    # 第一轮：满水位 → 暂缓推送（不刷新节流）
    r1 = await run_push_and_monitor(factory)
    assert r1["push"]["skipped_by_capacity"] is True
    assert r1["push"]["pushed"] == 0

    # 模拟 1 个任务完成做种 → 腾出 1 个槽位
    fake_dl.torrents[_h("1")] = TorrentInfo(
        hash=_h("1"), name="active-1", state=TorrentState.SEEDING, progress=1.0, size=1000, completed_bytes=1000, dlspeed=0, added_at=now_ts
    )

    # 第二轮紧接着跑（未满 30 分钟）：因上轮空转不刷新节流，本轮立即补推 1 个
    r2 = await run_push_and_monitor(factory)
    assert r2["push"].get("skipped_by_interval") is not True
    assert r2["push"]["pushed"] == 1


async def test_e2e_action_cap_limits_stalled_without_blocking_window_stop(
    db: None, _reset_state: RuntimeParamManager
) -> None:
    """单轮清理上限拦住多余停滞清理，但不阻塞临期窗口止损。"""
    _reset_state.set_memory("monitor_max_actions_per_run", 2)
    site_id, _ = await _seed_base()
    old_ts = int(time.time()) - 120 * 60
    near_end = format_bj(bj_now_dt() + timedelta(minutes=3))

    initial: list[TorrentInfo] = []
    # 5 个停滞永久免费种
    for idx, ch in enumerate(("1", "2", "3", "4", "5")):
        await _add_index(
            site_id,
            item_id=f"stalled-{idx}",
            info_hash=_h(ch),
            free_type=FreeType.PERMANENT,
            push_status=PushStatus.PUSHED,
        )
        initial.append(
            TorrentInfo(hash=_h(ch), name=f"stalled-{idx}", state=TorrentState.STALLED_DL, progress=0.1, size=1000, completed_bytes=100, dlspeed=0, added_at=old_ts)
        )

    # 1 个临期限时种（排在最后，验证不会被已打满的停滞上限拦住）
    await _add_index(
        site_id,
        item_id="expiring",
        info_hash=_h("9"),
        free_type=FreeType.TIMED,
        free_end_at=near_end,
        push_status=PushStatus.PUSHED,
    )
    initial.append(
        TorrentInfo(hash=_h("9"), name="expiring-soon", state=TorrentState.DOWNLOADING, progress=0.5, size=1000, completed_bytes=500, dlspeed=500 * 1024, added_at=old_ts)
    )

    fake_dl = _StatefulFakeDownloader(initial)
    out = await run_push_and_monitor(_FakeFactory(fake_dl))

    mon = out["monitor"]
    assert mon["aborted_stalled"] == 2
    assert mon["skipped_action_cap"] == 3
    assert mon["pre_paused"] == 3  # 2 停滞 + 1 临期
    assert _h("9") in fake_dl.paused_hashes


async def test_e2e_delete_audit_log_and_prometheus_metrics(
    db: None,
    _reset_state: RuntimeParamManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """删除模式输出结构化审计日志，且 /metrics 正确暴露水位与结果计数。"""
    _reset_state.set_memory("monitor_delete_expired", 1)
    _reset_state.set_memory("push_max_active_downloads", 6)
    site_id, _ = await _seed_base()
    old_ts = int(time.time()) - 120 * 60

    await _add_index(
        site_id,
        item_id="dead-1",
        info_hash=_h("d"),
        free_type=FreeType.PERMANENT,
        push_status=PushStatus.PUSHED,
    )
    await _add_index(site_id, item_id="fresh-1", free_type=FreeType.PERMANENT)

    fake_dl = _StatefulFakeDownloader(
        [
            TorrentInfo(
                hash=_h("d"),
                name="dead-torrent-name",
                state=TorrentState.STALLED_DL,
                progress=0.25,
                size=4000,
                completed_bytes=1000,
                dlspeed=0,
                added_at=old_ts,
            )
        ]
    )

    with caplog.at_level(logging.WARNING):
        out = await run_push_and_monitor(_FakeFactory(fake_dl))

    assert out["monitor"]["deleted"] == 1
    assert out["push"]["pushed"] == 1
    assert fake_dl.removed == [(_h("d"), True)]

    # 校验结构化审计日志包含完整取证上下文
    audit_logs = [r.getMessage() for r in caplog.records if "monitor 审计[删除任务+磁盘数据]" in r.getMessage()]
    assert len(audit_logs) == 1
    msg = audit_logs[0]
    assert "downloader=e2e-qb" in msg
    assert f"hash={_h('d')}" in msg
    assert "name='dead-torrent-name'" in msg
    assert "completed=1000/4000" in msg
    assert "reason=" in msg

    # 校验 Prometheus /metrics 输出
    body, _ = render_metrics()
    text = body.decode("utf-8")
    assert "seedhelper_free_seed_active 1.0" in text
    assert "seedhelper_free_seed_max_active 6.0" in text
    assert 'seedhelper_cleanup_actions_total{action="delete",trigger="stalled"}' in text
    assert 'seedhelper_free_seed_outcomes_total{outcome="pushed",reason="ok"}' in text
