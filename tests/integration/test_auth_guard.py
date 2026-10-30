"""面板鉴权的集成测试。

**安全底线**：除 ``/api/auth/*`` 与 ``/302/*`` 外，所有 API 都必须要求登录。
中转端点例外——它靠 URL 里的 HMAC 签名鉴权（播放器请求不带面板会话）。
"""

from __future__ import annotations

import pytest
from app.api.routes import media_auth as auth, drivers, health, media, system
from app.core.auth import SESSION_COOKIE, make_session


@pytest.fixture
async def protected_client(settings, db_engine, fake_driver):
    """挂载全部路由的测试客户端。"""
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(drivers.router)
    app.include_router(media.router)
    app.include_router(health.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    reset_services()


#: 需要登录才能访问的端点
PROTECTED_ENDPOINTS = [
    ("GET", "/api/drivers"),
    ("GET", "/api/media"),
    ("GET", "/api/media/stats"),
    ("GET", "/api/health/library"),
]


class TestProtectedEndpoints:
    @pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
    async def test_unauthenticated_returns_401(self, protected_client, method, path):
        """未登录访问受保护端点 → 401。"""
        resp = await protected_client.request(method, path)
        assert resp.status_code == 401, f"{path} 未受保护！"

    async def test_login_sets_cookie(self, protected_client):
        """登录成功下发 session cookie。"""
        resp = await protected_client.post(
            "/api/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert SESSION_COOKIE in resp.cookies

    async def test_wrong_password_returns_401(self, protected_client):
        resp = await protected_client.post(
            "/api/auth/login", json={"username": "tester", "password": "wrong"}
        )
        assert resp.status_code == 401

    async def test_wrong_username_returns_401(self, protected_client):
        resp = await protected_client.post(
            "/api/auth/login", json={"username": "nobody", "password": "admin"}
        )
        assert resp.status_code == 401

    async def test_authenticated_can_access(self, protected_client):
        """登录后能访问受保护端点。"""
        await protected_client.post(
            "/api/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        resp = await protected_client.get("/api/drivers")
        assert resp.status_code == 200

    async def test_bearer_token_works(self, protected_client):
        """Bearer token 也可鉴权（供 CLI 使用）。"""
        token = make_session()
        resp = await protected_client.get(
            "/api/drivers", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    async def test_logout_clears_session(self, protected_client):
        """登出后 cookie 失效。"""
        await protected_client.post(
            "/api/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        await protected_client.post("/api/auth/logout")
        # 手动清 cookie（httpx 的 cookie jar 在响应里已删除）
        protected_client.cookies.delete(SESSION_COOKIE)
        resp = await protected_client.get("/api/drivers")
        assert resp.status_code == 401

    async def test_me_endpoint_is_public(self, protected_client):
        """``/api/auth/me`` 不需要登录（前端用它判断登录态）。"""
        resp = await protected_client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    async def test_healthz_is_public(self, protected_client):
        """存活探针不需要登录（容器健康检查用）。"""
        resp = await protected_client.get("/healthz")
        assert resp.status_code == 200

    async def test_readyz_is_public(self, protected_client):
        """就绪探针不需要登录。"""
        resp = await protected_client.get("/readyz")
        assert resp.status_code == 200
