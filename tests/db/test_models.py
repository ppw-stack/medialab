"""ORM 元数据与关系测试（§8）。

这些是"结构性"测试：关系配错、建表缺失时 SQLAlchemy 只在**实际查询**时
才报错，早期不易发现（曾导致 ``/api/v1/dashboard`` 在容器内 500）。
"""

from __future__ import annotations

import pytest
from app.db.models import Base, DownloaderInstance, Site, Torrent


def test_all_expected_tables_registered() -> None:
    """§8 要求的核心表必须齐备。"""
    expected = {
        "cooldown_hash",
        "downloader_instances",
        "feeds",
        "free_candidates",
        "site_window_stats",
        "sites",
        "task_queue",
        "torrents",
    }
    assert expected <= set(Base.metadata.tables)


def test_site_config_columns_exist() -> None:
    """站点配置字段存在性。"""
    cols = Base.metadata.tables["sites"].columns
    for name in (
        "adapter",
        "rss_url_enc",
        "apikey_enc",
        "cookies_enc",
        "min_interval_minutes",
        "enabled",
    ):
        assert name in cols, name


def test_required_indexes_present() -> None:
    """§8 索引要求：info_hash 唯一 + 看护/清理查询路径。"""
    torrents = Base.metadata.tables["torrents"]
    index_names = {ix.name for ix in torrents.indexes}
    assert "ix_torrents_info_hash" in index_names
    assert "ix_torrents_status_abort_at" in index_names
    assert "ix_torrents_source_status_free_end" in index_names
    assert "ix_torrents_site_source" in index_names


def test_cooldown_unique_per_site_hash() -> None:
    """FR-2.3.1：同站独立冷却，(site_id, info_hash) 必须唯一。"""
    constraints = Base.metadata.tables["cooldown_hash"].constraints
    assert any(
        {c.name for c in con.columns} == {"site_id", "info_hash"} for con in constraints
    )


@pytest.mark.parametrize(
    "table",
    [
        "torrents",
        "cooldown_hash",
        "feeds",
        "free_candidates",
        "site_window_stats",
    ],
)
def test_foreign_keys_declared(table: str) -> None:
    """强关联表必须显式声明外键，否则站点隔离无法保证。"""
    assert Base.metadata.tables[table].foreign_keys, f"{table} 缺少外键声明"


@pytest.mark.parametrize("table", ["downloader_instances"])
def test_weak_reference_tables_use_plain_ints(table: str) -> None:
    """下载器表故意**不**建 torrent 外键。

    理由：下载器被删除后，历史 torrent 仍要**保留**。若用外键 +
    级联删除，历史会随下载器一起消失。因此存的是裸 id，
    删除由业务层显式处理。
    """
    assert not Base.metadata.tables[table].foreign_keys


def test_torrent_window_columns_exist() -> None:
    """看护锚点字段（FR-3.4）必须持久化。"""
    cols = Base.metadata.tables["torrents"].columns
    for name in (
        "free_type",
        "free_end_at",
        "deadline_at",
        "abort_at",
        "window_slack",
        "free_confidence",
        "paused_reason",
        "trash_id",
    ):
        assert name in cols, name


def test_metadata_creates_all_without_error() -> None:
    """建表语句可在 SQLite 方言下生成。"""
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    dialect = sqlite.dialect()
    for table in Base.metadata.tables.values():
        str(CreateTable(table).compile(dialect=dialect))


async def test_torrent_window_persists_and_indexes(db) -> None:
    """窗口锚点字段可写入并按 abort_at 索引查询（FR-3.4）。"""
    from datetime import timedelta

    from app.core.timeutils import utcnow
    from app.db.models import FreeType, TorrentStatus
    from app.db.session import session_scope
    from sqlalchemy import select

    abort_at = utcnow() + timedelta(hours=6)
    async with session_scope() as session:
        dl = DownloaderInstance(name="dl1", type="qbittorrent", url="http://x", is_default=True)
        session.add(dl)
        await session.flush()
        site = Site(name="s1", domain="d1.test")
        session.add(site)
        await session.flush()
        session.add(
            Torrent(
                info_hash="a" * 40,
                name="test",
                site_id=site.id,
                downloader_id=dl.id,
                source="free_seed",
                status=TorrentStatus.ACTIVE,
                free_type=FreeType.TIMED,
                free_end_at=abort_at,
                abort_at=abort_at,
                deadline_at=abort_at - timedelta(minutes=30),
                free_confidence=0.95,
            )
        )

    async with session_scope() as session:
        t = (
            await session.execute(
                select(Torrent).where(Torrent.source == "free_seed")
            )
        ).scalars().one()
        assert t.abort_at == abort_at
        assert t.free_confidence == 0.95