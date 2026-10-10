"""窗口看护引擎测试（FR-3.4）。"""

from __future__ import annotations

from datetime import timedelta

from app.core.timeutils import utcnow
from app.db.models import (
    DownloaderInstance,
    PausedReason,
    Site,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.window_watcher import WindowWatcher


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
    await _seed(free_end_at=None, deadline_at=None, abort_at=None)
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts.get("no_anchor", 0) == 1
    assert counts.get("aborted", 0) == 0


# ------------------------------------------------------------ 到红线触发中止
async def test_abort_at_past_triggers_abort(db) -> None:
    past = utcnow() - timedelta(hours=1)
    await _seed(free_end_at=past, deadline_at=past, abort_at=past)
    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts.get("aborted") == 1


# ------------------------------------------------------------ 窗口内完成判定
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
            status=TorrentStatus.ACTIVE,
            free_type="freeleech_timed",
            free_end_at=end,
            deadline_at=end - timedelta(minutes=30),
            abort_at=end,
            completed_at=now,
        )
        s.add(t)
        await s.flush()

    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    counts = await watcher.watch_active()
    assert counts.get("completed") == 1


# ------------------------------------------------------------ 中止被保护期阻挡
async def test_protect_until_blocks_abort(db) -> None:
    past = utcnow() - timedelta(minutes=1)
    protect_until = utcnow() + timedelta(hours=1)
    await _seed(
        free_end_at=past,
        deadline_at=past - timedelta(minutes=30),
        abort_at=past,
        protect_until=protect_until,
    )

    watcher = WindowWatcher(factory=None)  # type: ignore[arg-type]
    async with session_scope() as s:
        from sqlalchemy import select

        t = (await s.execute(select(Torrent).where(Torrent.name == "t"))).scalar_one()
        outcome = await watcher._watch_one(s, t)
    assert outcome == "protected"
