"""识别别名（scrape_alias）集成测试。

固化的契约：
- **别名优先于搜索**：命中别名时**不打 TMDB 搜索接口**，直接按 ID 取详情
- 别名键按归一化标题去重（「Fight Club」与「fight club」是同一条）
- 别名 CRUD（API 契约）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from app.db.models import ScrapeAlias
from app.db.session import session_scope
from app.domain.scrape_types import ScrapePolicy
from app.services.scrape_service import ScrapeService
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

API = "https://api.themoviedb.org/3"

#: 测试用 Bearer 凭据（与 conftest 的 settings 一致）
TOKEN = "test-token-32-chars-abcdefgh"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def runtime_params() -> AsyncIterator[None]:
    """注入运行时参数 manager（服务层读策略用）。"""
    from app.services.runtime_params import RuntimeParamManager, set_runtime_params

    manager = RuntimeParamManager()
    manager.set_memory("scrape_policy", 0)
    manager.set_memory("scrape_enabled", 1)
    set_runtime_params(manager)
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


# ------------------------------------------------------------------ 服务层


@respx.mock
async def test_alias_skips_tmdb_search(
    db, runtime_params, tmp_path: Path
) -> None:
    """命中别名 → 不打搜索接口，直接按 ID 取详情。"""
    service = ScrapeService(tmdb=_client())
    await service.save_alias("被放逐的作弊魔术师", 288328, media_type="tv")

    search_route = respx.get(f"{API}/search/tv").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    detail_route = respx.get(f"{API}/tv/288328").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 288328,
                "name": "把黑夜点燃",
                "first_air_date": "2026-10-15",
                "overview": "简介",
            },
        )
    )

    # 走完整识别链（scrape_video 会 parse 文件名 → 识别 → 刮削）
    video = tmp_path / "被放逐的作弊魔术师.2026.mkv"
    video.write_bytes(b"fake")

    status, detail, _ = await service.scrape_video(
        video, policy=ScrapePolicy.OVERWRITE
    )

    assert status == "ok", detail
    assert detail_route.called, "应当按别名直取详情"
    assert search_route.call_count == 0, "命中别名时不该再打搜索接口"


@respx.mock
async def test_alias_normalizes_key(db, runtime_params) -> None:
    """别名键按归一化标题去重——大小写与标点差异视为同一条。"""
    service = ScrapeService(tmdb=_client())
    await service.save_alias("Fight Club", 550)
    await service.save_alias("fight   club", 550)

    async with session_scope() as session:
        rows = (await session.execute(select(ScrapeAlias))).scalars().all()
    assert len(rows) == 1, "同一标题只应有一条别名"
    assert rows[0].tmdb_id == 550


async def test_alias_list_and_delete(db, runtime_params) -> None:
    """别名列表与删除。"""
    service = ScrapeService(tmdb=_client())
    await service.save_alias("A Title", 1, note="备注")
    await service.save_alias("B Title", 2)

    items = await service.list_aliases()
    assert len(items) == 2
    assert {i["alias_text"] for i in items} == {"A Title", "B Title"}

    target_id = next(i["id"] for i in items if i["alias_text"] == "A Title")
    assert await service.delete_alias(target_id) is True
    assert await service.delete_alias(target_id) is False
    assert len(await service.list_aliases()) == 1


async def test_alias_save_rejects_blank_title(db, runtime_params) -> None:
    """空标题 → ServiceError（不静默存空别名）。"""
    from app.services.errors import ServiceError

    service = ScrapeService(tmdb=_client())
    with pytest.raises(ServiceError):
        await service.save_alias("   ", 1)


@respx.mock
async def test_remember_alias_records_media_type(
    db, runtime_params, tmp_path: Path
) -> None:
    """「记住此修正」写入的别名要带**媒体类型**。

    回归：类型缺失时，别名命中会按 movie 取详情——TV 条目取错，
    表现为识别不到或元数据为空。
    """
    respx.get(f"{API}/tv/123").mock(
        return_value=httpx.Response(
            200,
            json={"id": 123, "name": "测试剧", "first_air_date": "2026-01-01"},
        )
    )
    respx.get(f"{API}/tv/123/season/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "season_number": 1,
                "name": "第一季",
                "episodes": [{"episode_number": 1, "name": "第一集"}],
            },
        )
    )
    service = ScrapeService(tmdb=_client())
    video = tmp_path / "[Test].Show.S01E01.2026.mkv"
    video.write_bytes(b"fake")

    status, _detail, remembered = await service.scrape_video(
        video, tmdb_id=123, remember_alias=True
    )

    assert status == "ok"
    assert remembered is True

    async with session_scope() as session:
        row = (
            await session.execute(select(ScrapeAlias).where(ScrapeAlias.tmdb_id == 123))
        ).scalar_one()
    assert row.alias_text == "Show", "别名 key 必须是文件名解析出的标题"
    assert row.media_type == "tv", "媒体类型必须记录（否则命中时按 movie 取详情）"


# ------------------------------------------------------------------ API


@respx.mock
async def test_alias_api_crud(client: AsyncClient) -> None:
    """别名 CRUD 的 API 契约。"""
    resp = await client.post(
        "/api/scrape/aliases",
        headers=HEADERS,
        json={"alias_text": "夜神", "tmdb_id": 288328, "media_type": "tv", "note": "测试"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["alias_text"] == "夜神"
    assert created["tmdb_id"] == 288328

    resp = await client.get("/api/scrape/aliases", headers=HEADERS)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["alias_key"] == "夜神"

    resp = await client.delete(f"/api/scrape/aliases/{created['id']}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    resp = await client.get("/api/scrape/aliases", headers=HEADERS)
    assert resp.json() == []


async def test_alias_api_rejects_bad_media_type(client: AsyncClient) -> None:
    """非法媒体类型 → 400。"""
    resp = await client.post(
        "/api/scrape/aliases",
        headers=HEADERS,
        json={"alias_text": "x", "tmdb_id": 1, "media_type": "bogus"},
    )
    assert resp.status_code == 400


# ------------------------------------------------------------------ 辅助


def _client():
    """TMDB 客户端（带测试 Key）。"""
    from app.adapters.metadata.tmdb import TmdbClient

    return TmdbClient("test-key")


def _meta(tmdb_id: int):
    """媒体元数据样本。"""
    from app.domain.scrape_types import MediaMeta

    return MediaMeta(tmdb_id=tmdb_id, media_type="tv", title="把黑夜点燃", year=2026)
