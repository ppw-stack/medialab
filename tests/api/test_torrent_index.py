"""拉取索引 API 单测（FR-6.3）。"""

# ruff: noqa: SIM117

from __future__ import annotations

import base64

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def tidx_app(db, tmp_path, monkeypatch):
    """拉取索引路由测试 app：env + 独立 db。"""
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_TOKEN", "cli-token-tidx")

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    yield app
    get_settings.cache_clear()


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer cli-token-tidx"}


async def _make_env(app, *, n_per_site=3):
    """建站 + TorrentIndex 条目。返回 [(site_id, [(idx_id, name, free_type, hash)])]。"""
    from datetime import UTC, datetime, timedelta

    from app.db.models import FreeType, Site, TorrentIndex
    from app.db.session import session_scope

    out = []
    fixtures = [
        ("site-a", FreeType.PERMANENT, "AAAA" * 10),
        ("site-b", FreeType.TIMED, "BBBB" * 10),
        ("site-c", FreeType.NEUTRAL, "CCCC" * 10),
    ]
    async with session_scope() as s:
        for site_name, ft, h in fixtures:
            site = Site(name=site_name, domain=f"{site_name}.invalid", adapter="mteam")
            s.add(site)
            await s.flush()
            site_entries = []
            for i in range(n_per_site):
                end = None
                if ft == FreeType.TIMED:
                    end = datetime.now(UTC) + timedelta(hours=12)
                ti = TorrentIndex(
                    site_id=site.id,
                    item_id=f"{site_name}-{i}",
                    info_hash=h[:-2] + f"{i:02d}",
                    name=f"Movie.{site_name}.{i}.1080p.BluRay",
                    size=1000 * (i + 1),
                    category="movies",
                    free_type=ft,
                    free_end_at=end,
                    free_source="api",
                    free_confidence=0.9,
                    torrent_url=f"https://{site_name}.invalid/dl/{i}",
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC) - timedelta(minutes=10 * i),
                )
                s.add(ti)
                await s.flush()
                site_entries.append((ti.id, ti.name, ft, ti.info_hash))
            out.append((site.id, site_entries))
    return out


# ============================================================ 列表

async def test_list_torrent_index_empty(tidx_app) -> None:
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrent-index", headers=_bearer())
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 0
    assert d["items"] == []


async def test_list_torrent_index_with_data(tidx_app) -> None:
    data = await _make_env(tidx_app)
    total_expected = sum(len(entries) for _, entries in data)
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrent-index", headers=_bearer())
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == total_expected
    assert len(d["items"]) == total_expected
    first = d["items"][0]
    for key in (
        "id", "site_id", "site_name", "item_id", "info_hash",
        "name", "size", "category", "free_type",
        "free_end_at", "free_source", "free_confidence",
        "has_torrent_url", "first_seen_at", "last_seen_at",
    ):
        assert key in first, key
    assert first["has_torrent_url"] is True
    assert first["site_name"] in {"site-a", "site-b", "site-c"}


async def test_filter_by_site(tidx_app) -> None:
    data = await _make_env(tidx_app)
    target_site_id = data[1][0]  # site-b
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                f"/api/v1/torrent-index?site_id={target_site_id}",
                headers=_bearer(),
            )
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3
    for item in d["items"]:
        assert item["site_id"] == target_site_id
        assert item["site_name"] == "site-b"


async def test_filter_by_free_type(tidx_app) -> None:
    await _make_env(tidx_app)
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                "/api/v1/torrent-index?free_type=freeleech_timed",
                headers=_bearer(),
            )
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3
    for item in d["items"]:
        assert item["free_type"] == "freeleech_timed"
        assert item["free_end_at"] is not None


async def test_search_by_keyword(tidx_app) -> None:
    await _make_env(tidx_app)
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # 按 name 子串
            r = await c.get(
                "/api/v1/torrent-index?q=site-b.1",
                headers=_bearer(),
            )
            assert r.status_code == 200
            assert r.json()["total"] == 1
            # 按 info_hash 子串
            r2 = await c.get(
                "/api/v1/torrent-index?q=cccccccc",
                headers=_bearer(),
            )
            assert r2.json()["total"] == 3
            # 不存在的关键字
            r3 = await c.get(
                "/api/v1/torrent-index?q=NoMatch",
                headers=_bearer(),
            )
            assert r3.json()["total"] == 0


async def test_pagination(tidx_app) -> None:
    await _make_env(tidx_app)
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                "/api/v1/torrent-index?limit=2&offset=0",
                headers=_bearer(),
            )
            page1 = r.json()
            assert page1["total"] == 9
            assert len(page1["items"]) == 2

            r2 = await c.get(
                "/api/v1/torrent-index?limit=2&offset=2",
                headers=_bearer(),
            )
            page2 = r2.json()
            assert len(page2["items"]) == 2
            # 分页结果不重叠
            page1_ids = {i["id"] for i in page1["items"]}
            page2_ids = {i["id"] for i in page2["items"]}
            assert page1_ids.isdisjoint(page2_ids)


# ============================================================ 详情 / 元数据

async def test_get_torrent_index_detail(tidx_app) -> None:
    data = await _make_env(tidx_app)
    target_idx_id = data[0][1][0][0]  # site-a 第 0 条
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/v1/torrent-index/{target_idx_id}", headers=_bearer())
    assert r.status_code == 200
    item = r.json()
    assert item["id"] == target_idx_id
    assert item["site_name"] == "site-a"
    assert item["free_type"] == "freeleech_permanent"


async def test_get_torrent_index_404(tidx_app) -> None:
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrent-index/99999", headers=_bearer())
    assert r.status_code == 404


# ============================================================ 元数据端点

async def test_list_sites(tidx_app) -> None:
    await _make_env(tidx_app, n_per_site=1)
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrent-index/sites", headers=_bearer())
    assert r.status_code == 200
    sites = r.json()
    names = {s["name"] for s in sites}
    assert names == {"site-a", "site-b", "site-c"}


async def test_list_free_types(tidx_app) -> None:
    await _make_env(tidx_app, n_per_site=1)
    transport = ASGITransport(app=tidx_app)
    async with tidx_app.router.lifespan_context(tidx_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/torrent-index/free-types", headers=_bearer())
    assert r.status_code == 200
    types = r.json()
    assert set(types) == {"freeleech_permanent", "freeleech_timed", "neutral_leech"}