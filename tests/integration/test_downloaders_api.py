"""下载器 API 的集成测试。

**覆盖点**：

- ``GET /downloaders/state-meta`` —— 任务状态元信息（中文名 + 颜色）
  **由后端下发**（前端不再硬编码一份映射表）
- 路由顺序——``/state-meta`` 必须**先于** ``/{downloader_id}`` 声明，
  否则会被当成 id 解析成 422
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture
async def client(settings, db_engine):
    """带鉴权的测试客户端（只挂下载器路由 + 认证）。"""
    from app.api.auth import router as auth
    from app.api.routes import downloaders
    from app.core.auth import init_hmac_key
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())

    app = FastAPI()
    app.include_router(auth)
    # downloaders 路由的 prefix 是 /downloaders，主应用挂载时再加 /api/v1
    app.include_router(downloaders.router, prefix="/api/v1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "test-pass-123"},
        )
        yield c


@pytest.fixture
async def anon_client(settings, db_engine):
    """未登录的客户端（测 401）。"""
    from app.api.auth import router as auth
    from app.api.routes import downloaders
    from app.core.auth import init_hmac_key
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())

    app = FastAPI()
    app.include_router(auth)
    app.include_router(downloaders.router, prefix="/api/v1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


async def test_state_meta_返回全部状态(client: httpx.AsyncClient) -> None:
    """端点返回每个状态的值 + 中文名 + 颜色。"""
    from app.adapters.downloaders.base import TorrentState

    resp = await client.get("/api/v1/downloaders/state-meta")
    assert resp.status_code == 200
    meta = resp.json()
    assert len(meta) == len(list(TorrentState))
    for item in meta:
        assert item["value"].strip()
        assert item["label"].strip()
        assert item["color"].strip()


async def test_state_meta_值与后端枚举一致(client: httpx.AsyncClient) -> None:
    """下发的 value 与后端枚举**逐字一致**（大小写敏感）。

    前端把它当过滤参数传回后端——值不一致会导致过滤 400。
    """
    from app.adapters.downloaders.base import TorrentState

    resp = await client.get("/api/v1/downloaders/state-meta")
    values = {m["value"] for m in resp.json()}
    assert values == {s.value for s in TorrentState}
    # camelCase 的状态不能被转小写
    assert "pausedDL" in values
    assert "missingFiles" in values


async def test_state_meta_不被当成下载器id(client: httpx.AsyncClient) -> None:
    """路由顺序：``/state-meta`` 不能落到 ``/{downloader_id}`` 上。

    若声明顺序反了，FastAPI 会先匹配 ``/{downloader_id}`` →
    "state-meta" 解析成 int 失败 → 422。
    """
    resp = await client.get("/api/v1/downloaders/state-meta")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_state_meta_需要登录(anon_client: httpx.AsyncClient) -> None:
    """未登录 → 401（跟其它下载器端点同级别）。"""
    resp = await anon_client.get("/api/v1/downloaders/state-meta")
    assert resp.status_code == 401


async def test_列表不返回任务数(client: httpx.AsyncClient) -> None:
    """列表接口**不含**任务数——它要实时打下载器接口（实测 7.6s）。

    放在列表里 → 前端列表超时 → 页面误显示「暂未配置下载器」。
    """
    resp = await client.get("/api/v1/downloaders")
    assert resp.status_code == 200
    for row in resp.json():
        assert "torrent_count" not in row


async def test_torrent_count_端点存在(client: httpx.AsyncClient) -> None:
    """任务数走**单独端点**（前端异步补，不阻塞列表）。"""
    resp = await client.get("/api/v1/downloaders/1/torrent-count")
    assert resp.status_code == 200
    body = resp.json()
    assert body["downloader_id"] == 1
    # 下载器不存在 → count=null（**不写 0 兜底**：0 会把故障伪装成「没有任务」）
    assert body["count"] is None


async def test_torrent_count_需要登录(anon_client: httpx.AsyncClient) -> None:
    resp = await anon_client.get("/api/v1/downloaders/1/torrent-count")
    assert resp.status_code == 401
