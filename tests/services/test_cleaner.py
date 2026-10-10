"""Cleaner 统一管线测试（FR-5.1 / FR-5.2 六重保护）。"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest_asyncio
from app.core.timeutils import utcnow
from app.db.models import (
    CleanupPolicy,
    CleanupRule,
    DownloaderInstance,
    Site,
    Source,
    Torrent,
    TorrentStatus,
)
from app.db.session import session_scope
from app.services.cleanup import (
    Cleaner,
    DryRunGuard,
    PathSafetyGuard,
    ProtectPeriodGuard,
    TrashGuard,
    WhitelistGuard,
)
from sqlalchemy import select


# ------------------------------------------------------------ 白名单
class TestWhitelist:
    def test_manual_source_blocked(self) -> None:
        t = _t(source=Source.MANUAL)
        block, why = WhitelistGuard.blocks(t)
        assert block and why == "manual_collection"

    def test_protected_category_blocked(self) -> None:
        t = _t(category="keep")
        block, why = WhitelistGuard.blocks(t)
        assert block and why.startswith("protected_category")

    def test_manual_keep_tag_blocked(self) -> None:
        t = _t(tags="auto,manual_keep,site-a")
        block, why = WhitelistGuard.blocks(t)
        assert block and why == "manual_keep_tag"

    def test_normal_task_not_blocked(self) -> None:
        block, _ = WhitelistGuard.blocks(_t())
        assert not block


# ------------------------------------------------------------ 保护期
class TestProtectPeriod:
    def test_within_protect_hours_blocks(self) -> None:
        t = _t(protect_until=utcnow() + timedelta(hours=5))
        assert ProtectPeriodGuard.blocks(t, hours=12) is True

    def test_past_protect_allows(self) -> None:
        t = _t(protect_until=utcnow() - timedelta(hours=1))
        assert ProtectPeriodGuard.blocks(t, hours=12) is False

    def test_zero_hours_skips_check(self) -> None:
        """小时数 0 → 跳过检查（清理可立即执行）。"""
        t = _t(protect_until=utcnow() + timedelta(hours=5))
        assert ProtectPeriodGuard.blocks(t, hours=0) is False

    def test_null_protect_allows(self) -> None:
        assert ProtectPeriodGuard.blocks(_t(protect_until=None), hours=12) is False


# ------------------------------------------------------------ 干跑门
class TestDryRunGuard:
    def test_first_use_requires_dry_run(self) -> None:
        rule = CleanupRule(name="r", dry_run_done=False)
        assert DryRunGuard.must_dry_run(rule) is True

    def test_dry_run_done_lets_run(self) -> None:
        rule = CleanupRule(name="r", dry_run_done=True)
        assert DryRunGuard.must_dry_run(rule) is False


# ------------------------------------------------------------ 路径三重校验
class TestPathSafety:
    async def test_empty_paths_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok, why = await PathSafetyGuard.verify(tmp, [], None, "h" * 40)
            assert ok is False
            assert "empty" in why

    async def test_path_outside_root_rejected(self) -> None:
        """FR-5.2.5c：路径必须在 download_root 之下。"""
        with tempfile.TemporaryDirectory() as tmp:
            outside = "/tmp/outside"
            Path(outside).mkdir(exist_ok=True)
            ok, why = await PathSafetyGuard.verify(tmp, [outside], None, "h" * 40)
            assert ok is False
            assert "outside_root" in why

    async def test_path_inside_root_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "movie.mkv"
            f.write_text("x")
            ok, _ = await PathSafetyGuard.verify(tmp, [str(f)], None, "h" * 40)
            assert ok is True

    async def test_path_traversal_rejected(self) -> None:
        """路径穿越尝试：template 给出的 ../ 会被 ../ 直接拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            attack = f"{tmp}/../escape"
            ok, _ = await PathSafetyGuard.verify(tmp, [attack], None, "h" * 40)
            assert ok is False


async def await_path_safety(download_root: str, paths: list[str], _adapter) -> tuple[bool, str]:
    return await PathSafetyGuard.verify(download_root, paths, _adapter, "h" * 40)


# ------------------------------------------------------------ 回收站移动
class TestTrashGuard:
    async def test_quarantine_moves_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "movie.mkv"
            f.write_bytes(b"data")
            ok, trash, err = await TrashGuard.quarantine(
                tmp, "h" * 40, [str(f)], retention_days=7
            )
            assert ok, err
            assert Path(trash).exists()
            assert not f.exists()
            assert (Path(trash) / "trash_meta.json").exists()


# ------------------------------------------------------------ 完整流水线：dry-run 模式
@pytest_asyncio.fixture
async def env_with_dl(db):
    """为 Cleaner 测试准备下载器 + 站点 + 清理策略。

    关键：每个测试要新连接，不能跨测试共享——避免 SQLite "database is locked"。
    """
    async with session_scope() as s:
        dl = DownloaderInstance(
            name="qbt", type="qbittorrent", url="http://qbt", download_root="/tmp"
        )
        s.add(dl)
        await s.flush()
        site = Site(
            name="pt", domain="p.invalid", save_path_template="/d/{site}/{name}"
        )
        s.add(site)
        await s.flush()
        policy = CleanupPolicy(id=1)
        s.add(policy)
        dl_id = dl.id
        site_id = site.id
    yield {"dl_id": dl_id, "site_id": site_id}


async def test_dry_run_does_not_touch_torrent(env_with_dl) -> None:
    """FR-5.2.2：干跑模式仅收集，不写日志不改状态。"""
    async with session_scope() as s:
        s.add(
            Torrent(
                info_hash="a" * 40,
                name="x",
                site_id=env_with_dl["site_id"],
                downloader_id=env_with_dl["dl_id"],
                size=1024**3,
                ratio=5.0,
                source=Source.AUTO_SEED,
                status=TorrentStatus.ACTIVE,
            )
        )
    async with session_scope() as s:
        rule = CleanupRule(name="high-ratio", when={"ratio_gte": 3.0})
        from app.services.factory import AdapterFactory

        cleaner = Cleaner(s, AdapterFactory.__new__(AdapterFactory))  # type: ignore[arg-type]
        out = await cleaner.run_rule(rule, dry_run=True)
    assert out.candidates_total == 1
    assert out.candidates_executed == 0


async def test_dry_run_required_first_use(env_with_dl) -> None:
    """首次启用强制干跑：dry_run_done=False 时即使不传 dry_run 也走干跑。"""
    async with session_scope() as s:
        s.add(
            Torrent(
                info_hash="b" * 40,
                name="y",
                site_id=env_with_dl["site_id"],
                downloader_id=env_with_dl["dl_id"],
                size=1024**3,
                ratio=5.0,
                source=Source.AUTO_SEED,
            )
        )
    async with session_scope() as s:
        rule = CleanupRule(name="r", when={"ratio_gte": 3.0})
        from app.services.factory import AdapterFactory

        cleaner = Cleaner(s, AdapterFactory.__new__(AdapterFactory))  # type: ignore[arg-type]
        out = await cleaner.run_rule(rule)
    assert out.candidates_total == 1
    assert out.candidates_executed == 0  # 干跑门生效


async def test_whitelist_blocks_manual_task(env_with_dl) -> None:
    """FR-5.2.1：手动任务命中白名单 → 跳过 + 计数。

    需要把 default_sources 改为包含 manual，否则该任务在源过滤阶段就被排除。
    """
    async with session_scope() as s:
        # 把 default_sources 改成全清
        policy = (
            await s.execute(select(CleanupPolicy).where(CleanupPolicy.id == 1))
        ).scalar_one_or_none()
        if policy:
            policy.default_sources = "manual,auto_seed,free_seed"
        s.add(
            Torrent(
                info_hash="c" * 40,
                name="manual",
                site_id=env_with_dl["site_id"],
                downloader_id=env_with_dl["dl_id"],
                ratio=10.0,
                source=Source.MANUAL,
                status=TorrentStatus.ACTIVE,
            )
        )
    async with session_scope() as s:
        rule = CleanupRule(name="r", when={"ratio_gte": 3.0}, dry_run_done=True)
        from app.services.factory import AdapterFactory

        cleaner = Cleaner(s, AdapterFactory.__new__(AdapterFactory))  # type: ignore[arg-type]
        out = await cleaner.run_rule(rule)
    assert out.candidates_total == 1
    assert out.candidates_blocked == 1
    assert out.candidates_executed == 0
    assert "whitelist:manual_collection" in out.blocked_reasons


async def test_protect_period_blocks_recently_added(env_with_dl) -> None:
    """FR-5.2.6：保护期内不删（默认 12h）。"""
    async with session_scope() as s:
        s.add(
            Torrent(
                info_hash="d" * 40,
                name="new",
                site_id=env_with_dl["site_id"],
                downloader_id=env_with_dl["dl_id"],
                ratio=10.0,
                source=Source.AUTO_SEED,
                status=TorrentStatus.ACTIVE,
                protect_until=utcnow() + timedelta(hours=11, minutes=30),
            )
        )
    async with session_scope() as s:
        rule = CleanupRule(name="r", when={"ratio_gte": 3.0}, dry_run_done=True)
        from app.services.factory import AdapterFactory

        cleaner = Cleaner(s, AdapterFactory.__new__(AdapterFactory))  # type: ignore[arg-type]
        out = await cleaner.run_rule(rule)
    assert out.candidates_total == 1
    assert out.candidates_executed == 0
    assert "protect_period" in out.blocked_reasons


def _t(**kw):
    defaults = {
        "info_hash": "a" * 40,
        "source": Source.AUTO_SEED,
        "status": TorrentStatus.ACTIVE,
        "ratio": 1.0,
        "size": 1024,
        "added_at": utcnow(),
        "category": "movie",
        "tags": "",
        "site_id": 1,
        "protect_until": None,
    }
    defaults.update(kw)
    ns = type("T", (), defaults)()
    return ns
