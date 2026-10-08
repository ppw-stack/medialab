"""FreeSeedHunter 编排测试（FR-3 流水线）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.adapters.sites.base import FeedItem
from app.core.timeutils import utcnow
from app.core.windows import FreeWindow
from app.db.models import CooldownReason, FreeType, Source, Torrent, TorrentStatus
from app.db.session import session_scope
from app.services.free_seed_hunter import FreeSeedHunter, free_idem_key


def make_item(hash_: str, title: str, size: int = 1024) -> FeedItem:
    return FeedItem(
        title=title,
        link=f"https://pt.invalid/details.php?id={hash_[:8]}",
        info_hash=hash_,
        size=size,
        category="movie",
    )


class _FakeSite:
    def __init__(self) -> None:
        self.returns: list[FeedItem] = []

    async def fetch_feed(self, url: str) -> list[FeedItem]:
        return self.returns

    async def parse_window(self, raw):
        return getattr(self, "_parser", lambda r: FreeWindow())(raw)


class _FakeDownloader:
    def __init__(self) -> None:
        self.added: list[dict] = []

    async def add_torrent(self, payload) -> object:
        self.added.append({"name": payload.name, "urls": payload.urls})
        return object()


class _Factory:
    def __init__(self) -> None:
        self._dl = _FakeDownloader()
        self._site = _FakeSite()
        self.rss_url = "https://pt.invalid/rss"  # type: ignore[assignment]

    def site(self, site):
        return self._site

    def site_rss_url(self, site):
        return self.rss_url

    def downloader(self, dl):
        return self._dl


# ------------------------------------------------------------ 拉取失败
async def test_run_site_breaker_returns_breaked_status(
    db, free_seed_env
) -> None:
    """FR-2.2：RSS 熔断时直接返回 breaker_tripped。"""
    site_id = free_seed_env["site_id"]
    factory = _Factory()

    async def trip(url):
        from app.services.feed_fetcher import BreakerTripped
        raise BreakerTripped("HTTP 429")

    factory._site.fetch_feed = trip  # type: ignore[assignment]

    hunter = FreeSeedHunter(factory)  # type: ignore[arg-type]
    stats = await hunter.run_site(site_id, force=True)
    assert stats.status == "breaker_tripped"
    assert stats.added == 0


# ------------------------------------------------------------ 跳过 90 分钟阈值
async def test_skip_short_window_under_90min(
    db, free_seed_env
) -> None:
    """剩余 < 90min 应当跳过（FR-3.2）。"""
    site_id = free_seed_env["site_id"]
    factory = _Factory()
    factory._site.returns = [make_item("a" * 40, "ShortWindow", size=1024)]

    def parser(raw):
        return FreeWindow(
            free_type=FreeType.TIMED,
            free_end_at=datetime(2026, 1, 1, 12, 0),
            free_source="api",
            confidence=0.95,
        )

    factory._site._parser = parser  # type: ignore[attr-defined]

    hunter = FreeSeedHunter(factory)  # type: ignore[arg-type]
    stats = await hunter.run_site(site_id, force=True)
    assert stats.skipped == 1
    assert "window_expired" in stats.skip_reasons
    assert stats.added == 0


# ------------------------------------------------------------ 永久免费登记
async def test_permanent_free_registered_only(
    db, free_seed_env
) -> None:
    """R10：永久免费转辅种池，**不下载**、不入任务池。"""
    site_id = free_seed_env["site_id"]
    factory = _Factory()
    factory._site.returns = [make_item("a" * 40, "ForeverFree")]

    def parser(raw):
        return FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=None,
            free_source="api",
            confidence=1.0,
        )

    factory._site._parser = parser  # type: ignore[attr-defined]

    hunter = FreeSeedHunter(factory)  # type: ignore[arg-type]
    stats = await hunter.run_site(site_id, force=True)
    assert stats.permanent == 1
    assert stats.added == 0

    async with session_scope() as s:
        from app.db.models import AddHistory
        from sqlalchemy import select

        h = (
            await s.execute(
                select(AddHistory).where(AddHistory.outcome == "permanent_free")
            )
        ).scalar_one()
        assert h is not None


# ------------------------------------------------------------ 正常拉取 → 锚点落库
async def test_full_pipeline_writes_window_anchors(
    db, free_seed_env
) -> None:
    """完整流程：拉取 → 解析窗口 → 锚点字段写入 → add_torrent 调用。"""
    site_id = free_seed_env["site_id"]
    factory = _Factory()
    factory._site.returns = [make_item("a" * 40, "RealFreeSeed", size=1024**2)]

    end = utcnow() + timedelta(hours=2)

    def parser(raw):
        return FreeWindow(
            free_type=FreeType.TIMED,
            free_end_at=end,
            free_source="api",
            confidence=0.95,
        )

    factory._site._parser = parser  # type: ignore[attr-defined]

    hunter = FreeSeedHunter(factory)  # type: ignore[arg-type]
    stats = await hunter.run_site(site_id, force=True)
    assert stats.added == 1
    assert factory._dl.added and factory._dl.added[0]["name"] == "RealFreeSeed"

    async with session_scope() as s:
        from sqlalchemy import select

        t = (
            await s.execute(select(Torrent).where(Torrent.name == "RealFreeSeed"))
        ).scalar_one()
        assert t.source == Source.FREE_SEED
        assert t.is_free is True
        assert t.free_type == FreeType.TIMED
        assert t.free_end_at is not None
        assert t.deadline_at is not None
        # FR-3.4：abort_at == free_end_at（无任何调整）
        assert t.abort_at == t.free_end_at
        assert (t.free_end_at - t.deadline_at) == timedelta(minutes=30)


# ------------------------------------------------------------ 幂等键
def test_free_idem_key_isolated_from_cross_seed() -> None:
    """M4 幂等键与 M2 辅种 key 互不冲突——同 hash 可在两池中并存。"""
    from app.services.task_queue import TaskKind, make_idem_key

    cross = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash="a" * 40)
    free = free_idem_key(1, "a" * 40)
    assert cross != free


# ------------------------------------------------------------ 池内去重
async def test_duplicate_in_free_pool_skipped(
    db, free_seed_env
) -> None:
    """同 hash 在 free_seed 池已有任务时不再添加。"""
    site_id = free_seed_env["site_id"]
    dl_id = free_seed_env["dl_id"]
    hash_ = "a" * 40

    async with session_scope() as s:
        s.add(
            Torrent(
                info_hash=hash_,
                name="existing",
                site_id=site_id,
                downloader_id=dl_id,
                size=1024,
                source=Source.FREE_SEED,
                status=TorrentStatus.ACTIVE,
            )
        )

    factory = _Factory()
    factory._site.returns = [make_item(hash_, "Dup")]

    def parser(raw):
        return FreeWindow(
            free_type=FreeType.TIMED,
            free_end_at=datetime(2030, 1, 1),
            free_source="api",
            confidence=0.95,
        )

    factory._site._parser = parser  # type: ignore[attr-defined]

    hunter = FreeSeedHunter(factory)  # type: ignore[arg-type]
    stats = await hunter.run_site(site_id, force=True)
    assert stats.added == 0
    assert stats.skipped == 1
    assert stats.skip_reasons.get("duplicate_in_pool") == 1


# ------------------------------------------------------------ 冷却压制
async def test_cooldown_suppresses_second_capture(
    db, free_seed_env
) -> None:
    """window_expired 冷却会阻止下轮重复抓取。"""
    site_id = free_seed_env["site_id"]
    hash_ = "a" * 40

    async with session_scope() as s:
        from app.services.cooldown import CooldownService

        await CooldownService(s).record(
            site_id, hash_, CooldownReason.WINDOW_EXPIRED
        )

    factory = _Factory()
    factory._site.returns = [make_item(hash_, "Cooled")]

    def parser(raw):
        return FreeWindow(
            free_type=FreeType.TIMED,
            free_end_at=datetime(2030, 1, 1),
            free_source="api",
            confidence=0.95,
        )

    factory._site._parser = parser  # type: ignore[attr-defined]

    hunter = FreeSeedHunter(factory)  # type: ignore[arg-type]
    stats = await hunter.run_site(site_id, force=True)
    assert stats.added == 0
    assert stats.skip_reasons.get("cooldown") == 1
