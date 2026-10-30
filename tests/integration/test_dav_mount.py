"""WebDAV 挂载的集成测试。

**重点**：DAV 必须挂在 SPA 兜底路由**之前** —— 路由按注册顺序匹配，
SPA 的 ``/{full_path:path}`` 会吞掉一切路径。挂晚了客户端就只拿到
index.html，报「网络异常」。

（这正是旧自研版被回退的原因之一。）
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from app.core.config import AppSettings
from app.db.models import DavUser
from app.db.session import session_scope
from app.services.dav_auth import hash_password
from httpx import ASGITransport, AsyncClient

from tests.integration.test_dav_api import _hrefs


def _basic(user: str, pwd: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
async def mounted_app(settings: AppSettings, db_engine, tmp_path: Path, monkeypatch):
    """构造一个挂了 DAV 的完整应用（含 SPA 兜底）。"""
    from app.webdav import paths
    from app.main import create_app

    library = tmp_path / "library"
    (library / "电影").mkdir(parents=True)
    (library / "电影" / "阿凡达.strm").write_text("http://example.com/302/1/sig")

    async with session_scope() as db:
        db.add(
            DavUser(
                username="alice",
                password_hash=hash_password("secret"),
                is_active=True,
                last_login_at=0,
                created_at=0,
                updated_at=0,
            )
        )

    # DAV 根指向测试库
    paths._root_cache.set(str(library))

    settings.dav_enabled = True
    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app


async def test_dav_mounted_before_spa(mounted_app):
    """**关键回归**：DAV 不被 SPA 兜底吞掉。

    若 DAV 挂载在 SPA 之后，PROPFIND 会拿到 index.html（200 + HTML）
    而不是 207 —— 客户端直接报「网络异常」。
    """
    client, _ = mounted_app
    resp = await client.request(
        "PROPFIND", "/dav/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    assert resp.status_code == 207
    assert resp.headers.get("content-type", "").startswith("application/xml")
    # 不是 HTML（SPA 的 index.html）
    assert "<html" not in resp.text.lower()


async def test_dav_propfind_lists_library(mounted_app):
    """PROPFIND 能列出媒体库目录。"""
    client, _ = mounted_app
    resp = await client.request(
        "PROPFIND", "/dav/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    hrefs = _hrefs(resp)
    from urllib.parse import unquote

    assert any("电影" in unquote(h) for h in hrefs)


async def test_dav_get_strm(mounted_app):
    """GET ``.strm`` 返回中转 URL 原文。"""
    client, _ = mounted_app
    resp = await client.get(
        "/dav/%E7%94%B5%E5%BD%B1/%E9%98%BF%E5%87%A1%E8%BE%BE.strm",
        headers=_basic("alice", "secret"),
    )
    assert resp.status_code == 200
    assert resp.text == "http://example.com/302/1/sig"


async def test_dav_requires_auth(mounted_app):
    """未认证 401。"""
    client, _ = mounted_app
    resp = await client.request("PROPFIND", "/dav/", headers={"Depth": "1"})
    assert resp.status_code == 401


async def test_spa_still_works(mounted_app):
    """SPA 兜底路由不受影响。"""
    client, _ = mounted_app
    resp = await client.get("/library")
    # 没有前端资源时返回 404 / 前端缺失提示，但不能是 500
    assert resp.status_code in (200, 404)


async def test_dav_disabled_not_mounted(settings: AppSettings, db_engine):
    """DAV 未启用时不挂载。"""
    from app.main import create_app

    settings.dav_enabled = False
    app = create_app()
    routes = [getattr(r, "path", "") for r in app.routes]
    assert not any(r.startswith("/dav") for r in routes)


async def test_dav_without_trailing_slash_redirects(mounted_app):
    """**关键回归**：`/dav`（无尾斜杠）重定向到 `/dav/`。

    Starlette 的 ``Mount`` 把路径编译成 ``/dav/{path:path}`` —— 要求
    ``/dav`` 后面**必须有**斜杠。裸的 ``/dav`` 匹配不上挂载点，会落到
    别处 → **405 Method Not Allowed**。

    生产日志里实测到客户端发过 ``PROPFIND /dav``（405）—— 有些客户端
    会先试不带尾斜杠的路径。

    用 **308**（保持方法）—— 301/302 会被浏览器把 PROPFIND 改成 GET。
    """
    client, _ = mounted_app
    resp = await client.request(
        "PROPFIND", "/dav", headers={**_basic("alice", "secret"), "Depth": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 308
    assert resp.headers.get("location") == "/dav/"


async def test_dav_get_without_trailing_slash_redirects(mounted_app):
    """GET `/dav` 同样重定向。"""
    client, _ = mounted_app
    resp = await client.get("/dav", headers=_basic("alice", "secret"), follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers.get("location") == "/dav/"
