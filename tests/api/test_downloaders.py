"""下载器管理 API 单测（FR-1.1）。"""
# ruff: noqa: SIM117

from __future__ import annotations

import base64

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def dl_app(db, tmp_path, monkeypatch):
    """下载器路由测试 app：env 驱动的 settings + 独立 db。"""
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_TOKEN", "cli-token-dl")

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    yield app
    get_settings.cache_clear()


def _bearer(client: AsyncClient) -> dict[str, str]:
    return {"Authorization": "Bearer cli-token-dl"}


async def test_list_empty(dl_app) -> None:
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/v1/downloaders", headers=_bearer(c))
    assert r.status_code == 200
    assert r.json() == []


async def test_create_and_get(dl_app) -> None:
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # 新增
            r = await c.post(
                "/api/v1/downloaders",
                headers=_bearer(c),
                json={
                    "name": "qbt-main",
                    "type": "qbittorrent",
                    "url": "http://qbittorrent:8080",
                    "username": "admin",
                    "password": "secret-password",
                    "download_root": "/downloads",
                    "enabled": True,
                    "verify_tls": False,
                },
            )
            assert r.status_code == 201
            body = r.json()
            assert body["name"] == "qbt-main"
            assert body["has_password"] is True
            assert body["verify_tls"] is False
            assert body["type"] == "qbittorrent"
            assert "password" not in body
            dl_id = body["id"]

            # 详情
            r = await c.get(f"/api/v1/downloaders/{dl_id}", headers=_bearer(c))
            assert r.status_code == 200
            assert r.json()["name"] == "qbt-main"

            # 列表
            r = await c.get("/api/v1/downloaders", headers=_bearer(c))
            assert r.status_code == 200
            assert len(r.json()) == 1


async def test_create_duplicate_name(dl_app) -> None:
    """name 唯一约束。"""
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            payload = {"name": "qbt", "type": "qbittorrent", "url": "http://q"}
            r1 = await c.post("/api/v1/downloaders", headers=_bearer(c), json=payload)
            assert r1.status_code == 201
            r2 = await c.post("/api/v1/downloaders", headers=_bearer(c), json=payload)
            assert r2.status_code == 409


async def test_create_unknown_type(dl_app) -> None:
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders",
                headers=_bearer(c),
                json={"name": "x", "type": "made-up", "url": "http://x"},
            )
            assert r.status_code == 400


async def test_create_unimplemented_type(dl_app) -> None:
    """transmission/deluge/aria2：声明但未实现 → 501。"""
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders",
                headers=_bearer(c),
                json={"name": "tr", "type": "transmission", "url": "http://tr"},
            )
            assert r.status_code == 501


async def test_update_keeps_password_when_blank(dl_app) -> None:
    """PUT 留空 password 应当保留旧凭据。"""
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders",
                headers=_bearer(c),
                json={"name": "qbt", "type": "qbittorrent", "url": "http://q", "password": "old-pw"},
            )
            dl_id = r.json()["id"]
            assert r.json()["has_password"] is True

            # URL 改 + 密码留空
            r = await c.post(
                "/api/v1/downloaders/update",
                headers=_bearer(c),
                json={"id": dl_id, "url": "http://q2", "password": ""},
            )
            assert r.status_code == 200
            assert r.json()["url"] == "http://q2"
            assert r.json()["has_password"] is True  # 保留


async def test_update_renames_with_uniqueness(dl_app) -> None:
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r1 = await c.post(
                "/api/v1/downloaders", headers=_bearer(c),
                json={"name": "a", "type": "qbittorrent", "url": "http://a"},
            )
            r2 = await c.post(
                "/api/v1/downloaders", headers=_bearer(c),
                json={"name": "b", "type": "qbittorrent", "url": "http://b"},
            )
            assert r1.status_code == 201 and r2.status_code == 201
            # 试图把 a 改名为 b
            r = await c.post(
                "/api/v1/downloaders/update",
                headers=_bearer(c),
                json={"id": r1.json()["id"], "name": "b"},
            )
            assert r.status_code == 409


async def test_delete_blocks_when_bound_to_site(dl_app) -> None:
    """删除下载器：已不再有"站点绑定"检查（全局默认下载器模式）。"""
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders", headers=_bearer(c),
                json={"name": "qbt", "type": "qbittorrent", "url": "http://q"},
            )
            dl_id = r.json()["id"]

            # 删除时应直接成功（200）
            r = await c.post(
                "/api/v1/downloaders/delete",
                headers=_bearer(c),
                json={"id": dl_id},
            )
            assert r.status_code == 200
            assert r.json()["id"] == dl_id


async def test_delete_404(dl_app) -> None:
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders/delete",
                headers=_bearer(c),
                json={"id": 99999},
            )
    assert r.status_code == 404


async def test_password_encrypted_at_rest(dl_app) -> None:
    """DB 中 password_enc 不可逆：必须是密文，不是明文。"""
    from app.db.models import DownloaderInstance
    from app.db.session import session_scope
    from sqlalchemy import select

    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders", headers=_bearer(c),
                json={"name": "qbt", "type": "qbittorrent", "url": "http://q", "password": "supersecret"},
            )
            dl_id = r.json()["id"]

        async with session_scope() as s:
            row = (await s.execute(select(DownloaderInstance).where(DownloaderInstance.id == dl_id))).scalar_one()
            assert row.password_enc is not None
            assert "supersecret" not in row.password_enc  # 密文中不出现明文
            # AES-GCM 密文 = base64(VERSION_BYTE | nonce | ct)，至少 45 字符
            assert len(row.password_enc) >= 45


async def test_test_endpoint_unreachable(dl_app, monkeypatch) -> None:
    """测连指向不存在的服务 → reachable=false。"""
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders", headers=_bearer(c),
                json={"name": "qbt", "type": "qbittorrent", "url": "http://127.0.0.1:1"},
                # 关闭 verify_tls 没意义，目标是 HTTP；只测连通性
            )
            dl_id = r.json()["id"]
            r = await c.post(f"/api/v1/downloaders/{dl_id}/test", headers=_bearer(c), json={})
    # 测连失败但接口应正常返回 200 + reachable=false
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is False
    assert "detail" in body


async def test_test_endpoint_disabled(dl_app) -> None:
    """已禁用的下载器测连 → 400。"""
    transport = ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/downloaders", headers=_bearer(c),
                json={"name": "qbt", "type": "qbittorrent", "url": "http://q", "enabled": False},
            )
            dl_id = r.json()["id"]
            r = await c.post(f"/api/v1/downloaders/{dl_id}/test", headers=_bearer(c), json={})
    assert r.status_code == 400
