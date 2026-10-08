"""窗口看护引擎测试（FR-3.4 / FR-3.8 抢救策略）。"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from app.core.timeutils import utcnow
from app.db.models import (
    AbortedReason,
    DownloaderInstance,
    PausedReason,
    Site,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.window_watcher import (
    WindowWatcher,
)


# Helper: build the dependency row set for a watch scenario
async def _seed(
    *,
    free_end_at,
    deadline_at,
    abort_at,
    status=TorrentStatus.ACTIVE,
    paused_reason=None,
    free_type: str = "freeleech_timed",
    source=Source.FREE_SEED,
    protect_until=None,
):
    async with session_scope() as s:
        dl = DownloaderInstance(name="qbt", type="qbittorrent", url="http://x")
        s.add(dl)
        await s.flush()
        site = Site(
            name="pt",
            domain="pt.invalid",
            save_path_template="/d/{site}/{name}",
            downloader_id=dl.id,
        )
        s.add(site)
        await s.flush()
        t = Torrent(
            info_hash="a" * 40,
            name="t",
            site_id=site.id,
            downloader_id=dl.id,
            size=1000,
            source=source,
            status=status,
            free_type=free_type,
            free_end_at=free_end_at,
            deadline_at=deadline_at,
            abort_at=abort_at,
            paused_reason=paused_reason,
            protect_until=protect_until,
        )
        s.add(t)
        await s.flush()
        return t.id


# ------------------------------------------------------------ 永久免费不在看护范围
async def test_permanent_free_skipped(db) -> None:
    await _seed(free_end_at=None, deadline_at=None, abort_at=None, free_type="freeleech_permanent")
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts["inspected"] == 1
    # permanent 不计入任何 outcomes


# ------------------------------------------------------------ 让路不视为中止
async def test_yield_paused_skipped(db) -> None:
    now = utcnow()
    await _seed(
        free_end_at=now + timedelta(hours=1),
        deadline_at=now + timedelta(minutes=30),
        abort_at=now + timedelta(hours=1),
        paused_reason=PausedReason.YIELD,
    )
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts["inspected"] == 1
    assert counts.get("aborted", 0) == 0


# ------------------------------------------------------------ 缺锚点不强行处理
async def test_no_anchor_returns_noop(db) -> None:
    """数据回填场景：旧任务没有 deadline/abort 字段。"""
    await _seed(free_end_at=None, deadline_at=None, abort_at=None, free_type="freeleech_timed")
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts["inspected"] == 1
    assert counts.get("aborted", 0) == 0


# ------------------------------------------------------------ 到红线立即中止
async def test_abort_at_past_triggers_abort(db) -> None:
    past = utcnow() - timedelta(minutes=1)
    await _seed(
        free_end_at=past,
        deadline_at=past - timedelta(minutes=30),
        abort_at=past,
    )
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts.get("aborted") == 1

    # 验证 DB：状态被标记 aborted
    async with session_scope() as s:
        from sqlalchemy import select

        t = (
            await s.execute(select(Torrent).where(Torrent.name == "t"))
        ).scalar_one()
        assert t.status == TorrentStatus.ABORTED
        assert t.aborted_reason == AbortedReason.WINDOW_EXPIRED
        assert t.aborted_at is not None

        # 冷却阶梯在 CooldownService.record() 内已生效（由 _trigger_abort 调用）


# ------------------------------------------------------------ 完成归类
async def test_completed_inside_window_marked_completed(db) -> None:
    now = utcnow()
    end = now + timedelta(hours=2)
    async with session_scope() as s:
        dl = DownloaderInstance(name="qbt", type="qbittorrent", url="http://x")
        s.add(dl)
        await s.flush()
        site = Site(
            name="pt",
            domain="p.invalid",
            save_path_template="/d/{site}/{name}",
            downloader_id=dl.id,
        )
        s.add(site)
        await s.flush()
        t = Torrent(
            info_hash="b" * 40,
            name="done",
            site_id=site.id,
            downloader_id=dl.id,
            size=1000,
            source=Source.FREE_SEED,
            status=TorrentStatus.ACTIVE,  # 还在 active 但已完成
            free_type="freeleech_timed",
            free_end_at=end,
            deadline_at=end - timedelta(minutes=30),
            abort_at=end,
            completed_at=now,  # 已完成
        )
        s.add(t)
        await s.flush()

    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts.get("completed") == 1


# ------------------------------------------------------------ 抢救策略（FR-3.8）
async def test_rescue_notify_only_does_nothing(db) -> None:
    """默认 notify_only：只告警，不调下载器。"""
    now = utcnow()
    end = now + timedelta(hours=1)
    await _seed(
        free_end_at=end,
        deadline_at=now + timedelta(seconds=10),  # 已经进入预警区间
        abort_at=end,
    )
    # 把 site 的 rescue_strategy 改为 notify_only
    async with session_scope() as s:
        from sqlalchemy import select

        site = (await s.execute(select(Site).where(Site.name == "pt"))).scalar_one()
        site.rescue_strategy = "notify_only"

    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    # 没调用下载器 API → 抢救未发生
    assert counts.get("rescued", 0) == 0


async def test_rescue_speed_up_calls_downloader(db) -> None:
    """speed_up：临时解除下载限速（FR-3.8）。"""
    now = utcnow()
    end = now + timedelta(hours=1)
    await _seed(
        free_end_at=end,
        deadline_at=now - timedelta(seconds=5),  # 已过 deadline 但未到 abort
        abort_at=end,
    )
    async with session_scope() as s:
        from sqlalchemy import select

        site = (await s.execute(select(Site).where(Site.name == "pt"))).scalar_one()
        site.rescue_strategy = "speed_up"

    fake = AsyncMock(return_value=None)
    fake.set_rate_limit = AsyncMock(return_value=True)
    factory = type("F", (), {"downloader": staticmethod(lambda _dl: fake)})()

    watcher = WindowWatcher(factory=factory)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    # 抢救执行
    assert counts.get("rescued") == 1
    fake.set_rate_limit.assert_awaited()


async def test_rescue_respects_cooldown(db) -> None:
    """RESCUE_COOLDOWN 内不重复提速，避免反复打下载器。"""
    now = utcnow()
    end = now + timedelta(hours=1)
    torrent_id = await _seed(
        free_end_at=end,
        deadline_at=now - timedelta(seconds=5),
        abort_at=end,
    )
    async with session_scope() as s:
        from sqlalchemy import select

        site = (await s.execute(select(Site).where(Site.name == "pt"))).scalar_one()
        site.rescue_strategy = "speed_up"
        t = (await s.execute(select(Torrent).where(Torrent.id == torrent_id))).scalar_one()
        t.last_rescued_at = now - timedelta(seconds=10)  # 在冷却内

    fake = AsyncMock()
    fake.set_rate_limit = AsyncMock(return_value=True)
    factory = type("F", (), {"downloader": staticmethod(lambda _dl: fake)})()
    watcher = WindowWatcher(factory=factory)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    # 不应执行抢救
    assert counts.get("rescued", 0) == 0
    fake.set_rate_limit.assert_not_called()


# ------------------------------------------------------------ 中止被保护期阻挡
async def test_protect_until_blocks_abort(db) -> None:
    """FR-5.2.6：禁删保护期内不得中止——任务刚加进来瞬间也要保护。"""
    past = utcnow() - timedelta(minutes=1)
    protect_until = utcnow() + timedelta(hours=1)  # 仍在保护期
    await _seed(
        free_end_at=past,
        deadline_at=past - timedelta(minutes=30),
        abort_at=past,
        protect_until=protect_until,
    )

    # 直接调用 _trigger_abort 应当被保护期阻挡
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    async with session_scope() as s:
        from sqlalchemy import select

        t = (await s.execute(select(Torrent).where(Torrent.name == "t"))).scalar_one()
        # watch_one 内部在 _trigger_abort 之前检查 protect_until
        outcome = await watcher._watch_one(s, t)
        # 应为 in_window（保护期阻挡了中止）
    assert outcome == "protected"
