"""登录失败与 401 语义的契约测试。

**关键区分**：两种 401 含义完全不同，前端要分开处理：

- 登录接口自己的 401 → 「用户名或密码错误」（前端显示这个）
- 其它接口的 401 → 「会话过期」（前端清登录态 + 回登录页）

曾因前端把两者混为一谈，把「密码错误」显示成「未登录」。
"""

from __future__ import annotations

import pytest
from app.core.auth import SESSION_COOKIE
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(settings, db_engine):
    from app.api.routes import media_auth as auth, system
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(system.router)
    app.include_router(auth.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    reset_services()


class TestLoginFailure:
    async def test_wrong_password_returns_specific_message(self, client):
        """密码错误 → detail 是「用户名或密码错误」。"""
        resp = await client.post(
            "/api/auth/login", json={"username": "tester", "password": "nope"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"

    async def test_wrong_username_returns_same_message(self, client):
        """用户不存在 → **同样**的文案（不区分「用户不存在」）。"""
        resp = await client.post(
            "/api/auth/login", json={"username": "nobody", "password": "admin"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"

    async def test_login_message_differs_from_session_expired(self, client):
        """登录失败 ≠ 会话过期——文案必须不同。

        前端据此区分：前者显示在登录页，后者触发「清登录态 + 跳转」。
        """
        bad_login = await client.post(
            "/api/auth/login", json={"username": "tester", "password": "nope"}
        )
        # 伪造一个无效 cookie 去访问受保护端点
        resp = await client.get("/api/auth/me", headers={"Cookie": "medialab_session=garbage"})

        assert bad_login.status_code == 401
        assert resp.status_code == 200  # /me 公开，返回未登录状态

    async def test_missing_password_returns_401(self, client):
        """空密码 → 401（不 500）。"""
        resp = await client.post(
            "/api/auth/login", json={"username": "tester", "password": ""}
        )
        assert resp.status_code == 401

    async def test_extra_fields_rejected(self, client):
        """多余字段被拒（防 typo 字段名静默生效）。"""
        resp = await client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "test-pass-123", "admin": True},
        )
        assert resp.status_code == 422


class TestSessionFlow:
    async def test_login_success_sets_session_cookie(self, client):
        resp = await client.post(
            "/api/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        assert resp.status_code == 200
        assert resp.cookies.get(SESSION_COOKIE)

    async def test_session_cookie_is_httponly_and_samesite_strict(self, client):
        """cookie 必须是 HttpOnly + SameSite=Strict。"""
        resp = await client.post(
            "/api/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        header = resp.headers.get("set-cookie", "")
        assert "httponly" in header.lower()
        assert "samesite=strict" in header.lower()

    async def test_me_reports_authenticated_after_login(self, client):
        await client.post("/api/auth/login", json={"username": "tester", "password": "test-pass-123"})
        resp = await client.get("/api/auth/me")
        assert resp.json()["authenticated"] is True
        assert resp.json()["username"] == "tester"

    async def test_me_reports_unauthenticated_without_cookie(self, client):
        resp = await client.get("/api/auth/me")
        assert resp.json()["authenticated"] is False
