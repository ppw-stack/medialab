"""认证层测试（FR-6.1 面板单用户登录）。"""
# ruff: noqa: SIM117

from __future__ import annotations

import base64

import pytest_asyncio
from app.core.auth import (
    hash_password,
    make_session,
    verify_password,
    verify_session,
)
from httpx import ASGITransport, AsyncClient


def test_password_hash_and_verify() -> None:
    """PBKDF2 哈希正常加盐 + 校验。"""
    stored = hash_password("correct-horse")
    assert verify_password("correct-horse", stored) is True
    assert verify_password("wrong", stored) is False
    # 格式错误也必须返回 False 而不是抛错
    assert verify_password("anything", "garbage") is False
    assert verify_password("anything", "") is False
    # None 输入
    assert verify_password("x", "") is False  # type: ignore[arg-type]


def test_make_and_verify_session() -> None:
    """Session token 签名 + 过期校验。"""
    token = make_session()
    assert verify_session(token) is True
    # 篡改 → False
    assert verify_session(token[:-1] + "x") is False
    # 错误段数
    assert verify_session("a.b") is False
    assert verify_session("") is False
    # None 输入
    assert verify_session(None) is False  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def auth_app(db, tmp_path, monkeypatch):
    """环境变量驱动的 app。

    **为什么用 env 而非构造参数**：``get_settings()`` 是 lru_cache 单例，
    通过参数传设置不进缓存。所有测试都要靠 env 变量 + cache_clear
    才能保证 create_app 与 login() 看到同一份配置。
    """
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    from app.core.auth import hash_password

    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_PASSWORD", hash_password("admin123"))

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    return app



async def test_login_endpoint_success(auth_app) -> None:
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin123"},
            )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "username": "admin"}
    assert "seedhelper_session" in resp.cookies


async def test_login_endpoint_wrong_password(auth_app) -> None:
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
            )
    assert resp.status_code == 401


async def test_login_endpoint_wrong_username(auth_app) -> None:
    """错误用户名也必须显式 401（防账户枚举）。"""
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/auth/login",
                json={"username": "root", "password": "admin123"},
            )
    assert resp.status_code == 401


async def test_protected_endpoint_requires_login(auth_app) -> None:
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/dashboard")
    assert resp.status_code == 401


async def test_protected_endpoint_accepts_bearer_token(
    db, tmp_path, monkeypatch
) -> None:
    """API Token（CLI 入口）：可绕过 Cookie 登录。"""
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_TOKEN", "cli-token-xyz")

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/dashboard",
                headers={"Authorization": "Bearer cli-token-xyz"},
            )
    assert resp.status_code == 200


async def test_bearer_token_wrong_value_rejected(auth_app) -> None:
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/dashboard",
                headers={"Authorization": "Bearer wrong-token"},
            )
    assert resp.status_code == 401


async def test_logout_clears_cookie(auth_app) -> None:
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin123"},
            )
            assert "seedhelper_session" in login.cookies
            resp = await client.post("/api/v1/auth/logout")
            # logout Set-Cookie 把 cookie 过期
            set_cookie = resp.headers.get("set-cookie", "")
            assert "seedhelper_session" in set_cookie
            assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


async def test_status_unauth_then_auth(auth_app) -> None:
    transport = ASGITransport(app=auth_app)
    async with auth_app.router.lifespan_context(auth_app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.get("/api/v1/auth/status")
            assert r1.json() == {"authenticated": False, "username": None}
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin123"},
            )
            r2 = await client.get("/api/v1/auth/status", cookies=login.cookies)
            assert r2.json() == {"authenticated": True, "username": "admin"}


async def test_admin_password_auto_hashed_on_first_start(
    db, tmp_path, monkeypatch
) -> None:
    """首次启动：明文密码自动升级为 PBKDF2 哈希（migrations）。"""
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_PASSWORD", "plain-text-password")

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    settings = get_settings()
    async with app.router.lifespan_context(app):
        # 升级后应当以 pbkdf2$ 开头
        assert settings.web_password.get_secret_value().startswith("pbkdf2$")
        # 仍能验证原明文
        assert verify_password(
            "plain-text-password", settings.web_password.get_secret_value()
        )
