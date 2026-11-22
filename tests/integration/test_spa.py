"""SPA 静态资源托管的契约测试。

深链接（``/library``、``/scheduler``、``/settings``）在浏览器刷新时是
**服务端路径**。
若不回退到 index.html，用户一刷新就 404 白屏。

这条契约原先由浏览器自动化测试暴露，去掉自动化测试后用单元测试固化。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def spa_app(tmp_path: Path, settings):
    """构造一个托管了临时 dist/ 的应用。"""
    from app.main import _mount_frontend
    from fastapi import FastAPI

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>strmmanger</body></html>", "utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)", "utf-8")

    app = FastAPI()
    _mount_frontend(app, dist)
    return app


@pytest.fixture
async def spa_client(spa_app):
    transport = ASGITransport(app=spa_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestSpaFallback:
    async def test_root_returns_index(self, spa_client):
        resp = await spa_client.get("/")
        assert resp.status_code == 200
        assert "strmmanger" in resp.text

    async def test_deep_link_falls_back_to_index(self, spa_client):
        """深链接返回 index.html（**不是** 404）——刷新不白屏。"""
        for path in ("/library", "/scheduler", "/settings", "/health", "/drivers/1"):
            resp = await spa_client.get(path)
            assert resp.status_code == 200, f"{path} 返回 {resp.status_code}"
            assert "strmmanger" in resp.text

    async def test_static_asset_served(self, spa_client):
        """真实静态资源正常返回（且不是 index.html）。"""
        resp = await spa_client.get("/assets/app.js")
        assert resp.status_code == 200
        assert "console.log" in resp.text

    async def test_index_has_no_cache_header(self, spa_client):
        """index.html 完全不缓存——前端更新后能立刻生效。

        用 ``no-store``（不是 ``no-cache``）：``no-cache`` 允许浏览器**存**
        缓存、只是要求每次**回源验证**——某些浏览器在离线/弱网时会直接
        用旧副本。``no-store`` 才是真的不存。
        """
        resp = await spa_client.get("/")
        assert resp.headers.get("cache-control") == "no-store"

    async def test_asset_has_immutable_cache(self, spa_client):
        """带 hash 的构建产物 → 不可变长缓存。

        内容变了 hash 就会变 → URL 变了就不该再缓存旧的。
        """
        resp = await spa_client.get("/assets/app.js")
        cache = resp.headers.get("cache-control") or ""
        assert "immutable" in cache
        assert "max-age=31536000" in cache

    async def test_missing_asset_returns_404_not_index(self, spa_client):
        """**不存在的资源返回真 404**，不能回 index.html。

        **回归测试**：曾经不存在的资源也回 index.html → 浏览器把 HTML
        当 JS 解析 → 整页白屏。这正是「更新服务后浏览器访问失败」的
        原因（浏览器缓存旧 index.html → 引用旧 hash 的 JS → 已删除）。
        """
        resp = await spa_client.get("/assets/index-OLDHASH.js")
        assert resp.status_code == 404
        assert "strmmanger" not in resp.text

    async def test_path_traversal_rejected(self, spa_client):
        """目录穿越被拒（不能读到 dist 之外的文件）。"""
        resp = await spa_client.get("/../../etc/passwd")
        # httpx 会归一化路径；无论归一化成什么，都不能返回 passwd 内容
        assert "root:" not in resp.text


class TestFrontendMount:
    def test_missing_dist_skips_mount(self, tmp_path: Path):
        """dist 目录不存在时不挂载（开发模式不报错）。"""
        from app.main import _mount_frontend
        from fastapi import FastAPI

        app = FastAPI()
        _mount_frontend(app, tmp_path / "does-not-exist")
        # 不抛异常即可
        assert app is not None

    def test_mount_adds_route(self, tmp_path: Path):
        """挂载后有 catch-all 路由。"""
        from app.main import _mount_frontend
        from fastapi import FastAPI

        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html></html>", "utf-8")

        app = FastAPI()
        _mount_frontend(app, dist)
        paths = [getattr(r, "path", "") for r in app.routes]
        assert "/{full_path:path}" in paths
