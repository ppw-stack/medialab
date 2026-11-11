"""刮削配置与手动刮削 API 集成测试。

固化的契约：
- ``tmdb_api_key`` **永不回显**——响应只有 ``has_key``
- 保存时 Key 留空 = 不修改（防误清）
- 未配置 Key 时手动刮削返回 400（不给「点了就报错」的入口）
- 未配置 Key 时记录列表返回空集（不报错）
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from app.core.config import AppSettings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

TOKEN = "test-token-32-chars-abcdefgh"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def runtime_params() -> AsyncIterator[None]:
    """注入运行时参数 manager。"""
    from app.services.runtime_params import RuntimeParamManager, set_runtime_params

    set_runtime_params(RuntimeParamManager())
    yield
    set_runtime_params(None)


@pytest.fixture
async def client(db_engine, env_settings, runtime_params) -> AsyncIterator[AsyncClient]:
    """挂了 scrape 路由的测试客户端。"""
    from app.api.routes import scrape

    app = FastAPI()
    app.include_router(scrape.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_get_config_defaults(client: AsyncClient) -> None:
    """默认配置：刮削关 / 仅补缺 / 无 Key / 目录为空 / 源目录为空 / 整理关 / copy。"""
    response = await client.get("/api/scrape/config", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["scrape_enabled"] == 0
    assert data["scrape_policy"] == 0
    assert data["has_key"] is False
    assert data["media_library_dir"] == ""
    assert data["organize_enabled"] == 0
    assert data["organize_action"] == 0
    assert data["organize_source_dirs"] == ""
    assert data["ready"] is False


@pytest.mark.asyncio
async def test_get_config_requires_auth(client: AsyncClient) -> None:
    """未登录 → 401。"""
    response = await client.get("/api/scrape/config")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_put_config_updates_runtime_params(client: AsyncClient) -> None:
    """开关 / 策略写入 runtime params。"""
    response = await client.put(
        "/api/scrape/config",
        headers=HEADERS,
        json={"scrape_enabled": 1, "scrape_policy": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["scrape_enabled"] == 1
    assert data["scrape_policy"] == 1


@pytest.mark.asyncio
async def test_put_config_saves_key_without_echo(
    client: AsyncClient, app_settings: AppSettings
) -> None:
    """保存 Key 后只回 has_key，绝不回显明文。"""
    response = await client.put(
        "/api/scrape/config",
        headers=HEADERS,
        json={"tmdb_api_key": "sk-secret-key", "media_library_dir": "/media"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["has_key"] is True
    assert data["media_library_dir"] == "/media"
    assert data["ready"] is True
    # 整个响应体都不含明文 Key
    assert "sk-secret-key" not in response.text


@pytest.mark.asyncio
async def test_put_config_empty_key_keeps_old(
    client: AsyncClient, app_settings: AppSettings
) -> None:
    """Key 传空（或 None）= 不修改旧值。"""
    await client.put(
        "/api/scrape/config",
        headers=HEADERS,
        json={"tmdb_api_key": "sk-secret-key"},
    )
    response = await client.put(
        "/api/scrape/config", headers=HEADERS, json={"tmdb_api_key": ""}
    )
    assert response.status_code == 200
    assert response.json()["has_key"] is True


@pytest.mark.asyncio
async def test_put_config_rejects_unknown_field(client: AsyncClient) -> None:
    """未知字段 → 422（extra=forbid，防手写参数被静默丢弃）。"""
    response = await client.put(
        "/api/scrape/config",
        headers=HEADERS,
        json={"unknown_field": 1},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_scrape_file_without_key_returns_400(client: AsyncClient) -> None:
    """未配置 Key 时手动刮削 → 400（不给点了就报错的入口）。"""
    response = await client.post(
        "/api/scrape/file",
        headers=HEADERS,
        json={"path": "/media/Movie (2025)/movie.mkv"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_records_without_key_returns_empty(client: AsyncClient) -> None:
    """未配置 Key 时记录列表返回空集（不是报错）。"""
    response = await client.get("/api/scrape/records", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"total": 0, "items": []}
