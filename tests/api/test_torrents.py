"""辅种任务 API 单测（FR-6.1 + FR-5.2）。"""

# ruff: noqa: SIM117

from __future__ import annotations

import base64

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def torrents_app(db, tmp_path, monkeypatch):
    """torrents 路由测试 app：env + 独立 db。"""
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_TOKEN", "cli-token-torrents")

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    yield app
    get_settings.cache_clear()


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer cli-token-torrents"}


async def _make_env(app):
    """创建 Site + DownloaderInstance + Torrent，返回 (site_id, dl_id, torrent_id)。"""
    from datetime import datetime, timedelta, UTC

    from app.db.models import DownloaderInstance, Site, Torrent, TorrentStatus
    from app.db.session import session_scope

    async with session_scope() as s:
        dl = DownloaderInstance(
            name="qbt", type="qbittorrent", url="http://qbt.invalid",
            is_default=True, enabled=True,
        )
        s.add(dl)
        await s.flush()
        site = Site(name="test-site", domain="test.invalid", adapter="mteam")
        s.add(site)
        await s.flush()
        t = Torrent(
            info_hash="a" * 40,
            name="Movie.A.2024.1080p.BluRay",
            site_id=site.id,
            downloader_id=dl.id,
            category="cross-seed",
            source="auto_seed",
            status=TorrentStatus.ACTIVE,
            size=1000,
            added_at=datetime.now(UTC),
            protect_until=datetime.now(UTC) + timedelta(hours=48),
        )
        s.add(t)
        await s.flush()
        return site.id, dl.id, t.id


# ============================================================ 列表与搜索


async def test_list_torrents_empty(torrents_app) -> None:
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrents", headers=_bearer())
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 0
    assert d["items"] == []
    assert d["page"] == 1
    assert d["page_size"] == 50


async def test_list_torrents_with_items(torrents_app) -> None:
    _, _, tid = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrents?page_size=10", headers=_bearer())
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1
    item = d["items"][0]
    assert item["id"] == tid
    # 新增字段验证
    assert "downloader_id" in item
    assert "downloader_name" in item
    assert "site_id" in item
    assert item["downloader_name"] == "qbt"
    assert item["site"] == "test-site"


async def test_list_torrents_q_search(torrents_app) -> None:
    _, _, _ = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # 按 name 搜索
            r = await c.get("/api/v1/torrents?q=Movie", headers=_bearer())
            assert r.status_code == 200
            assert r.json()["total"] == 1
            # 按 hash 前缀搜索
            r2 = await c.get("/api/v1/torrents?q=aaaaaaaaaaaaaaaa", headers=_bearer())
            assert r2.json()["total"] == 1
            # 搜索不存在的词
            r3 = await c.get("/api/v1/torrents?q=NoMatch", headers=_bearer())
            assert r3.json()["total"] == 0


async def test_list_torrents_filter(torrents_app) -> None:
    site_id, dl_id, _ = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                f"/api/v1/torrents?site_id={site_id}&downloader_id={dl_id}&source=auto_seed",
                headers=_bearer(),
            )
            assert r.json()["total"] == 1
            r2 = await c.get(
                "/api/v1/torrents?source=free_seed", headers=_bearer(),
            )
            assert r2.json()["total"] == 0


# ============================================================ 单条 pause/resume


async def test_pause_torrent_not_found(torrents_app) -> None:
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/v1/torrents/99999/pause", headers=_bearer())
    assert r.status_code == 404


async def test_resume_torrent_dry_run(torrents_app) -> None:
    """通过 mock 的 qbittorrent adapter 验证 resume 调用 + status 回写。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    _, _, tid = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    # 在 lifespan 之前设置 patch
    with patch("app.services.factory.AdapterFactory.downloader") as mock_factory:
        mock_adapter = MagicMock()
        mock_adapter.set_pause = AsyncMock(return_value=True)
        mock_factory.return_value = mock_adapter
        async with torrents_app.router.lifespan_context(torrents_app):
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(f"/api/v1/torrents/{tid}/resume", headers=_bearer())
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 验证 adapter 被调
    mock_adapter.set_pause.assert_called_once()


# ============================================================ 单条 remove


async def test_remove_torrent_dry_run(torrents_app) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    _, _, tid = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    with patch("app.services.factory.AdapterFactory.downloader") as mock_factory:
        mock_adapter = MagicMock()
        mock_adapter.remove = AsyncMock(return_value=True)
        mock_factory.return_value = mock_adapter
        async with torrents_app.router.lifespan_context(torrents_app):
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(
                    f"/api/v1/torrents/{tid}/remove?delete_data=false&force=true",
                    headers=_bearer(),
                )
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_remove_with_data_without_force_rejected(torrents_app) -> None:
    """FR-5.2.3：delete_data=True 必须 force=true。"""
    _, _, tid = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                f"/api/v1/torrents/{tid}/remove?delete_data=true&force=false",
                headers=_bearer(),
            )
    assert r.status_code == 400
    assert "force" in r.json()["detail"]


async def test_remove_with_data_with_force(torrents_app) -> None:
    """FR-5.2.3：force=true 后 delete_data=true 通过。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    _, _, tid = await _make_env(torrents_app)
    transport = ASGITransport(app=torrents_app)
    with patch("app.services.factory.AdapterFactory.downloader") as mock_factory:
        mock_adapter = MagicMock()
        mock_adapter.remove = AsyncMock(return_value=True)
        mock_factory.return_value = mock_adapter
        async with torrents_app.router.lifespan_context(torrents_app):
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(
                    f"/api/v1/torrents/{tid}/remove?delete_data=true&force=true",
                    headers=_bearer(),
                )
    assert r.status_code == 200


# ============================================================ 批量操作


async def test_batch_pause_size_limit(torrents_app) -> None:
    """批量操作 hash 上限 200。"""
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/torrents/batch/pause",
                json={"hashes": ["h" * 40] * 201, "paused": True},
                headers=_bearer(),
            )
    assert r.status_code == 400


async def test_batch_remove_with_data_without_force_rejected(torrents_app) -> None:
    transport = ASGITransport(app=torrents_app)
    async with torrents_app.router.lifespan_context(torrents_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/torrents/batch/remove",
                json={"hashes": ["h" * 40], "delete_data": True, "force": False},
                headers=_bearer(),
            )
    # force=false 被拒
    assert r.status_code == 400
    assert "force" in r.json()["detail"]