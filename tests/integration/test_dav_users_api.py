"""WebDAV 账号管理 API 的集成测试。

覆盖：账号 CRUD、审计查询、DAV 根配置、鉴权守卫。
"""

from __future__ import annotations

import time

import pytest
from app.core.config import AppSettings
from app.db.models import DavAuditLog, DavUser
from app.db.session import session_scope
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


@pytest.fixture
async def client(settings: AppSettings, db_engine):
    """已登录的面板客户端。"""
    from app.api.auth import router as auth
    from app.api.routes import dav_users
    from app.core.auth import init_hmac_key
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())

    app = FastAPI()
    app.include_router(auth)
    app.include_router(dav_users.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # 登录
        resp = await c.post(
            "/api/v1/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        assert resp.status_code == 200, resp.text
        yield c


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------


async def test_requires_login(settings: AppSettings, db_engine):
    """未登录访问被拒。"""
    from app.api.routes import dav_users
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(dav_users.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/dav-users")
        assert resp.status_code == 401


# --------------------------------------------------------------------------
# 账号 CRUD
# --------------------------------------------------------------------------


async def test_create_user(client):
    """新建账号。"""
    resp = await client.post(
        "/api/dav-users", json={"username": "alice", "password": "secret"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "alice"
    assert body["is_active"] is True
    # 响应里不能有密码哈希
    assert "password" not in body
    assert "password_hash" not in body


async def test_create_duplicate_user(client):
    """重名返回 409。"""
    await client.post("/api/dav-users", json={"username": "alice", "password": "x"})
    resp = await client.post(
        "/api/dav-users", json={"username": "alice", "password": "y"}
    )
    assert resp.status_code == 409


async def test_list_users(client):
    """列出账号。"""
    await client.post("/api/dav-users", json={"username": "alice", "password": "x"})
    await client.post("/api/dav-users", json={"username": "bob", "password": "y"})
    resp = await client.get("/api/dav-users")
    assert resp.status_code == 200
    usernames = {u["username"] for u in resp.json()}
    assert usernames == {"alice", "bob"}


async def test_update_password(client):
    """改密码。"""
    resp = await client.post(
        "/api/dav-users", json={"username": "alice", "password": "old"}
    )
    user_id = resp.json()["id"]

    resp = await client.patch(f"/api/dav-users/{user_id}", json={"password": "new"})
    assert resp.status_code == 200

    async with session_scope() as db:
        row = (await db.execute(select(DavUser).where(DavUser.id == user_id))).scalar_one()
        from app.services.dav_auth import verify_password

        assert verify_password("new", row.password_hash)
        assert not verify_password("old", row.password_hash)


async def test_update_password_clears_auth_cache(client):
    """**关键安全用例**：改密码后清空认证缓存。

    不清的话旧凭据还能继续用（缓存默认 60 秒）—— 改密码的语义就该是
    「立即失效」。
    """
    from app.services.dav_auth import dav_authenticator

    resp = await client.post(
        "/api/dav-users", json={"username": "alice", "password": "old"}
    )
    user_id = resp.json()["id"]

    # 造一条缓存
    dav_authenticator._cache[("1.2.3.4", "hash")] = type(
        "_E", (), {"user_id": user_id, "username": "alice", "expires_at": time.time() + 60}
    )()
    assert dav_authenticator.stats()["cached_credentials"] == 1

    await client.patch(f"/api/dav-users/{user_id}", json={"password": "new"})
    assert dav_authenticator.stats()["cached_credentials"] == 0


async def test_disable_account(client):
    """停用账号。"""
    resp = await client.post(
        "/api/dav-users", json={"username": "alice", "password": "x"}
    )
    user_id = resp.json()["id"]

    resp = await client.patch(f"/api/dav-users/{user_id}", json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_delete_user(client):
    """删除账号。"""
    resp = await client.post(
        "/api/dav-users", json={"username": "alice", "password": "x"}
    )
    user_id = resp.json()["id"]

    resp = await client.delete(f"/api/dav-users/{user_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/dav-users")
    assert resp.json() == []


async def test_update_missing_user(client):
    """改不存在的账号 → 404。"""
    resp = await client.patch("/api/dav-users/9999", json={"is_active": False})
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


async def test_list_audit(client):
    """查询审计。"""
    async with session_scope() as db:
        db.add(
            DavAuditLog(
                user_id=1,
                username="alice",
                method="GET",
                path="/电影/x.strm",
                status=200,
                client="Infuse/1.0",
                remote_addr="1.2.3.4",
                duration_ms=12,
                item_count=0,
                created_at=int(time.time()),
            )
        )

    resp = await client.get("/api/dav-users/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["username"] == "alice"
    assert item["method"] == "GET"
    assert item["path"] == "/电影/x.strm"


async def test_audit_filter_by_username(client):
    """按用户名过滤。"""
    async with session_scope() as db:
        for name in ("alice", "bob"):
            db.add(
                DavAuditLog(
                    user_id=1,
                    username=name,
                    method="GET",
                    path="/x",
                    status=200,
                    created_at=int(time.time()),
                )
            )

    resp = await client.get("/api/dav-users/audit", params={"username": "alice"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["username"] == "alice"


async def test_audit_filter_by_method(client):
    """按方法过滤。"""
    async with session_scope() as db:
        for method in ("GET", "PROPFIND"):
            db.add(
                DavAuditLog(
                    user_id=1,
                    username="alice",
                    method=method,
                    path="/x",
                    status=207,
                    created_at=int(time.time()),
                )
            )

    resp = await client.get("/api/dav-users/audit", params={"method": "propfind"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["method"] == "PROPFIND"


async def test_clear_audit(client):
    """清空审计。"""
    async with session_scope() as db:
        db.add(
            DavAuditLog(
                user_id=1,
                username="alice",
                method="GET",
                path="/x",
                status=200,
                created_at=int(time.time()),
            )
        )

    resp = await client.delete("/api/dav-users/audit")
    assert resp.status_code == 204

    resp = await client.get("/api/dav-users/audit")
    assert resp.json()["total"] == 0


async def test_audit_route_not_shadowed_by_user_id(client):
    """**关键回归**：`DELETE /audit` 不被 `/{user_id}` 接走。

    若 `/{user_id}` 先注册，`user_id="audit"` 解析失败 → 422。
    """
    resp = await client.delete("/api/dav-users/audit")
    assert resp.status_code == 204


# --------------------------------------------------------------------------
# DAV 根配置
# --------------------------------------------------------------------------


async def test_get_root_lists_drivers(client):
    """查询 DAV 根配置会列出存储源。"""
    from app.db.models import Driver

    async with session_scope() as db:
        db.add(
            Driver(
                type="fake",
                name="测试源",
                credential=None,
                enabled=True,
                output_dir="/tmp",
                created_at=0,
                updated_at=0,
            )
        )

    resp = await client.get("/api/dav-users/root")
    assert resp.status_code == 200
    body = resp.json()
    assert body["driver_id"] is None
    assert any(d["name"] == "测试源" for d in body["drivers"])


async def test_set_root_driver(client):
    """切换 DAV 根。"""
    from app.db.models import Driver

    async with session_scope() as db:
        db.add(
            Driver(
                type="fake",
                name="测试源",
                credential=None,
                enabled=True,
                output_dir="/tmp",
                created_at=0,
                updated_at=0,
            )
        )
        await db.flush()
        driver_id = (await db.execute(select(Driver.id))).scalar_one()

    resp = await client.put("/api/dav-users/root", json={"driver_id": driver_id})
    assert resp.status_code == 200
    assert resp.json()["driver_id"] == driver_id


async def test_set_root_to_missing_driver(client):
    """切到不存在的存储源 → 404。"""
    resp = await client.put("/api/dav-users/root", json={"driver_id": 9999})
    assert resp.status_code == 404


async def test_set_root_without_output_dir(client):
    """切到没配 output_dir 的存储源 → 400。"""
    from app.db.models import Driver

    async with session_scope() as db:
        db.add(
            Driver(
                type="fake",
                name="无输出目录",
                credential=None,
                enabled=True,
                output_dir=None,
                created_at=0,
                updated_at=0,
            )
        )
        await db.flush()
        driver_id = (await db.execute(select(Driver.id))).scalar_one()

    resp = await client.put("/api/dav-users/root", json={"driver_id": driver_id})
    assert resp.status_code == 400
