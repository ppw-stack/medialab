"""推送服务测试（Phase 1 —— 手推/自动推合流）。

此前手动推送与自动推送是**两条分叉的实现**，且已经漂移：
手动推送有 TOCTOU 防御、``task_executor`` 没有；手动推送不走队列
（无崩溃恢复 / 幂等键 / 退避）。

本文件验证合流后的行为：
- 手动推送走**同一条**队列 + 同一个执行器
- TOCTOU 防御在执行器里（两条链路都受保护）
- 幂等：已推过 / 已在队列中 / 下载器报 duplicate
"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.adapters.sites.base import SiteAdapter, SiteHealth, SiteProfile
from app.db.models import DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import PushStatus
from app.services.factory import AdapterFactory
from app.services.push_service import PushError, push_index_entry
from app.services.task_queue import TaskKind
from sqlalchemy import select

# ------------------------------------------------------------- 假适配器

def _make_torrent_bytes() -> bytes:
    """构造一个最小合法 torrent（info dict 含 name/length）。"""
    from app.adapters.sites._bencode import bencode

    info = {b"name": b"file.bin", b"length": 1024}
    return b"d" + bencode(b"info") + bencode(info) + b"e"


class _FakeSiteAdapter(SiteAdapter):
    """返回固定 torrent bytes 的假站点适配器。"""

    adapter_type = "fake"

    def __init__(self, profile: SiteProfile, *, fail: bool = False, **_: object) -> None:
        super().__init__(profile)
        self.download_calls = 0
        self.fail = fail

    async def check_reachability(self) -> SiteHealth:
        return SiteHealth(reachable=True)

    async def search_torrents(self, query: dict[str, object], **kw: object) -> list[object]:
        return []

    async def download_torrent(self, item_id: str) -> bytes:
        self.download_calls += 1
        if self.fail:
            raise RuntimeError("站点下载失败")
        return _make_torrent_bytes()

    async def get_torrent_url(self, item_id: str) -> str | None:
        return None

    async def check_status(self, item_id: str) -> object:
        from app.adapters.sites.base import TorrentBrief

        return TorrentBrief.UNKNOWN

    async def get_torrent_detail(self, item_id: str) -> object:
        from app.adapters.sites.base import TorrentDetail

        return TorrentDetail(item_id=item_id)


class _FakeDownloader:
    """记录 add_torrent 调用的假下载器。"""

    def __init__(self, *, fail_with: str | None = None) -> None:
        self.added: list[AddTorrentPayload] = []
        self.fail_with = fail_with

    async def add_torrent(self, payload: AddTorrentPayload) -> object:
        from app.adapters.downloaders.base import TorrentHandle

        if self.fail_with is not None:
            raise DownloaderError(self.fail_with)
        self.added.append(payload)
        return TorrentHandle(info_hash="b" * 40, downloader="fake")

    async def close(self) -> None:
        return None


async def _make_push_env(
    *,
    push_status: str | None = None,
    downloader_fail: str | None = None,
    site_fail: bool = False,
) -> tuple[AdapterFactory, _FakeSiteAdapter, _FakeDownloader, int]:
    """建站点 + 下载器 + 索引行，注入假适配器。"""
    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="generic"))
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
                name="torrent",
                free_type="freeleech_permanent",
                push_status=push_status,
            )
        )
        await session.flush()
        index_id = (
            await session.execute(
                select(TorrentIndex.id).where(TorrentIndex.item_id == "i1")
            )
        ).scalar_one()

    site_adapter = _FakeSiteAdapter(
        SiteProfile(name="s1", domain="s1.test"), fail=site_fail
    )
    dl_adapter = _FakeDownloader(fail_with=downloader_fail)
    factory = AdapterFactory()
    factory.site = lambda row, cookies=None: site_adapter  # type: ignore[method-assign]
    factory.downloader = lambda row, **kw: dl_adapter  # type: ignore[method-assign]
    return factory, site_adapter, dl_adapter, index_id


# ------------------------------------------------------------------ 正常推送

async def test_push_succeeds(db: None) -> None:
    factory, site, dl, index_id = await _make_push_env()
    result = await push_index_entry(index_id, None, factory)

    assert result.ok is True
    assert result.detail == "推送成功"
    assert result.info_hash is not None
    assert len(result.info_hash) == 40
    assert len(dl.added) == 1
    assert dl.added[0].category == "manual-push"
    assert site.download_calls == 1


async def test_push_marks_index_as_pushed(db: None) -> None:
    factory, _, _, index_id = await _make_push_env()
    await push_index_entry(index_id, None, factory)

    async with session_scope() as session:
        row = (
            await session.execute(
                select(TorrentIndex).where(TorrentIndex.id == index_id)
            )
        ).scalar_one()
        assert row.push_status == PushStatus.PUSHED
        assert row.pushed_at is not None
        assert row.info_hash is not None


async def test_push_creates_queue_task(db: None) -> None:
    """手动推送必须**入队**——这是与自动推合流的关键。"""
    from app.db.models import TaskQueue

    factory, _, _, index_id = await _make_push_env()
    await push_index_entry(index_id, None, factory)

    async with session_scope() as session:
        tq_rows = (await session.execute(select(TaskQueue))).scalars().all()
    assert len(tq_rows) == 1
    assert tq_rows[0].kind == TaskKind.ADD_TORRENT
    assert tq_rows[0].status == "done"


# ------------------------------------------------------------------ 幂等

async def test_push_already_pushed_is_idempotent(db: None) -> None:
    """已推过 → 不重复推送。"""
    factory, site, dl, index_id = await _make_push_env(push_status=PushStatus.PUSHED)
    result = await push_index_entry(index_id, None, factory)

    assert result.ok is True
    assert result.idempotent is True
    assert dl.added == [], "已推过不应再推送"
    assert site.download_calls == 0


async def test_push_duplicate_error_is_idempotent(db: None) -> None:
    """下载器报 duplicate → 视为幂等成功。"""
    factory, _, _, index_id = await _make_push_env(downloader_fail="duplicate torrent")
    result = await push_index_entry(index_id, None, factory)

    assert result.ok is True
    assert result.idempotent is True


async def test_push_twice_second_is_idempotent(db: None) -> None:
    """同一条目推两次 → 第二次幂等。"""
    factory, site, dl, index_id = await _make_push_env()
    await push_index_entry(index_id, None, factory)
    second = await push_index_entry(index_id, None, factory)

    assert second.ok is True
    assert second.idempotent is True
    assert len(dl.added) == 1, "不应重复 add"
    assert site.download_calls == 1


# ------------------------------------------------------------------ 失败

async def test_push_missing_index_404(db: None) -> None:
    factory, _, _, _ = await _make_push_env()
    with pytest.raises(PushError) as exc:
        await push_index_entry(999999, None, factory)
    assert exc.value.status == 404


async def test_push_without_downloader_400(db: None) -> None:
    """未配置默认下载器 → 400。"""
    factory, _, _, index_id = await _make_push_env()
    async with session_scope() as session:
        dl = (await session.execute(select(DownloaderInstance))).scalar_one()
        dl.is_default = False
        await session.flush()

    with pytest.raises(PushError) as exc:
        await push_index_entry(index_id, None, factory)
    assert exc.value.status == 400


async def test_push_disabled_downloader_400(db: None) -> None:
    factory, _, _, index_id = await _make_push_env()
    async with session_scope() as session:
        dl = (await session.execute(select(DownloaderInstance))).scalar_one()
        dl.enabled = False
        await session.flush()

    with pytest.raises(PushError) as exc:
        await push_index_entry(index_id, None, factory)
    assert exc.value.status == 400


async def test_push_site_download_failure_502(db: None) -> None:
    """站点下载 .torrent 失败 → 502（可重试类错误）。"""
    factory, _, _, index_id = await _make_push_env(site_fail=True)
    with pytest.raises(PushError) as exc:
        await push_index_entry(index_id, None, factory)
    assert exc.value.status == 502


async def test_push_failure_marks_task_failed(db: None) -> None:
    """失败的任务要落终态（不能停在 running）。"""
    from app.db.models import TaskQueue

    factory, _, _, index_id = await _make_push_env(site_fail=True)
    with pytest.raises(PushError):
        await push_index_entry(index_id, None, factory)

    async with session_scope() as session:
        rows = (await session.execute(select(TaskQueue))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status in ("pending", "failed")
    assert rows[0].last_error


# ------------------------------------------------------------------ TOCTOU

async def test_concurrent_push_only_adds_once(db: None) -> None:
    """两个并发推送 → 只 add 一次（TOCTOU 防御在执行器里）。"""
    import asyncio

    factory, _, dl, index_id = await _make_push_env()

    async def _push() -> object:
        return await push_index_entry(index_id, None, factory)

    results = await asyncio.gather(_push(), _push(), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 2, "两个请求都应返回成功（一个真推，一个幂等）"
    assert len(dl.added) == 1, "只应真正 add 一次"
