"""API 集成测试（Phase 0）。

覆盖认证 + 端点契约。FastAPI 用 ``httpx.ASGITransport`` 直接打
应用（不启真服务器），``lifespan`` 手动跑——这依赖 Phase 0 修好的
settings 注入通道（否则会用 ``/app/data`` 的真 DB）。

注意：``scheduler_enabled=False`` → lifespan 不会启动调度中心，
不会真的去拉站点。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from app.core.config import AppSettings
from app.db.models import Site
from app.db.session import session_scope
from app.main import create_app
from sqlalchemy import select


@pytest_asyncio.fixture
async def client(
    app_settings: AppSettings, db: None
) -> AsyncIterator[httpx.AsyncClient]:
    """已登录的 API 客户端（走 lifespan 初始化）。"""
    app = create_app(app_settings)
    # lifespan 里 init_db 会重置 engine（幂等）——先 dispose 让它重建
    from app.db.session import dispose_db

    await dispose_db()

    # 触发 lifespan（ASGITransport 不自动跑）
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
        app.router.lifespan_context(app),
    ):
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "test-pass-123"},
        )
        assert resp.status_code == 200, resp.text
        yield c


async def _add_site(name: str = "s1", **kw: object) -> int:
    async with session_scope() as session:
        s = Site(name=name, domain=f"{name}.test", adapter="hdsky", **kw)  # type: ignore[arg-type]
        session.add(s)
        await session.flush()
        return s.id


# ------------------------------------------------------------------ 认证

async def test_login_success_sets_cookie(client: httpx.AsyncClient) -> None:
    assert "seedhelper_session" in client.cookies


async def test_login_wrong_password_401(app_settings: AppSettings) -> None:
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "wrong"},
        )
    assert resp.status_code == 401


async def test_login_wrong_username_401(app_settings: AppSettings) -> None:
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "test-pass-123"},
        )
    assert resp.status_code == 401


async def test_protected_endpoint_requires_session(app_settings: AppSettings) -> None:
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/api/v1/sites")
    assert resp.status_code == 401


async def test_bearer_token_auth_works(app_settings: AppSettings) -> None:
    """Bearer token 是 CLI/脚本入口（FR-6.1）。"""
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c, app.router.lifespan_context(app):
        resp = await c.get(
            "/api/v1/sites",
            headers={"Authorization": "Bearer test-token-32-chars-abcdefgh"},
        )
    assert resp.status_code == 200


async def test_auth_status(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["username"] == "tester"


# ------------------------------------------------------------------ 运维

async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] is True


async def test_metrics(client: httpx.AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus 文本格式（prometheus_client 返回 openmetrics 或 text/plain）
    assert "text/plain" in resp.headers["content-type"] or "openmetrics" in (
        resp.headers["content-type"]
    )


# ------------------------------------------------------------------ 站点

async def test_list_sites_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_sites(client: httpx.AsyncClient) -> None:
    await _add_site("s1")
    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "s1"
    assert data[0]["adapter"] == "hdsky"


async def test_create_site(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "newsite", "adapter": "hdsky", "domain": "x.test"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "newsite"


async def test_create_site_duplicate_name_409(client: httpx.AsyncClient) -> None:
    await _add_site("dup")
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "dup", "adapter": "hdsky", "domain": "y.test"},
    )
    assert resp.status_code == 409


async def test_create_site_invalid_adapter_400(client: httpx.AsyncClient) -> None:
    """未注册的 adapter → 400。

    之前用 pydantic ``Literal`` 校验返回 422，错误信息是「不符合
    Literal」而非「未注册的站点 adapter」。改为运行时校验后（名单
    单一源在 ``_registry``），错误信息更明确。
    """
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "x", "adapter": "nonexistent"},
    )
    assert resp.status_code == 400
    assert "未注册的站点 adapter" in resp.json()["detail"]


async def test_update_site_invalid_adapter_400(client: httpx.AsyncClient) -> None:
    """更新时传未注册 adapter → 400。"""
    site_id = await _add_site("s1")
    resp = await client.post(
        "/api/v1/sites/update",
        json={"id": site_id, "adapter": "nonexistent"},
    )
    assert resp.status_code == 400
    assert "未注册的站点 adapter" in resp.json()["detail"]


async def test_create_site_valid_adapter_accepted(client: httpx.AsyncClient) -> None:
    """注册表里的 adapter 都能通过校验。"""
    for i, adapter in enumerate(("mteam", "hdsky", "ourbits", "soulvoice")):
        resp = await client.post(
            "/api/v1/sites",
            json={"name": f"site{i}", "adapter": adapter},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["adapter"] == adapter


async def test_update_site_valid_adapter_accepted(client: httpx.AsyncClient) -> None:
    site_id = await _add_site("s1")
    resp = await client.post(
        "/api/v1/sites/update",
        json={"id": site_id, "adapter": "mteam"},
    )
    assert resp.status_code == 200
    assert resp.json()["adapter"] == "mteam"


async def test_create_site_invalid_name_422(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "bad name!", "adapter": "hdsky"},
    )
    assert resp.status_code == 422


async def test_get_site_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/sites/999999")
    assert resp.status_code == 404


async def test_delete_site(client: httpx.AsyncClient) -> None:
    site_id = await _add_site("todelete")
    resp = await client.post("/api/v1/sites/delete", json={"id": site_id})
    assert resp.status_code == 200

    async with session_scope() as session:
        rows = (await session.execute(select(Site))).scalars().all()
        assert rows == []


async def test_list_adapters(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/sites/adapters")
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()}
    assert "hdsky" in names
    assert "mteam" in names
    assert len(names) == 9


# ------------------------------------------------------------------ 用户数据

async def test_list_user_data_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/user-data")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_site_user_data_no_data(client: httpx.AsyncClient) -> None:
    site_id = await _add_site("s1")
    resp = await client.get(f"/api/v1/sites/{site_id}/user-data")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is False


async def test_list_user_data_with_row(client: httpx.AsyncClient) -> None:
    from app.db.models import SiteUserData

    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            SiteUserData(
                site_id=site_id,
                upload=1024,
                download=2048,
                seeding_count=10,
                leeching_count=2,
            )
        )
        await session.flush()

    resp = await client.get("/api/v1/user-data")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["upload"] == 1024
    assert data[0]["seeding_count"] == 10


# ------------------------------------------------------------------ 调度

async def test_scheduler_status(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/scheduler")
    assert resp.status_code == 200
    data = resp.json()
    assert "jobs" in data
    assert "inflight" in data


async def test_run_unknown_job_reports_failure(client: httpx.AsyncClient) -> None:
    """未知作业：返回 200 + ok=False（不是 404）。

    设计如此——手动触发走 scheduler_center.run_now()，它对未知作业
    记录一条失败历史并回 ok=False，语义上与「作业存在但执行失败」一致。
    """
    resp = await client.post("/api/v1/scheduler/nonexistent_job/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "unknown job" in data["error"]


async def test_toggle_unknown_job_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/scheduler/nonexistent_job/toggle", params={"enabled": "true"}
    )
    assert resp.status_code == 404


async def test_update_cron_invalid_400(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/scheduler/nonexistent_job/cron", json={"cron": "not-a-cron"}
    )
    # cron 非法先返 400（校验早于作业存在性检查）
    assert resp.status_code == 400


async def test_runtime_params_view(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.status_code == 200
    data = resp.json()
    assert "params" in data
    assert "specs" in data


async def test_job_runs_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/scheduler/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


# ------------------------------------------------------------------ 手动推送

async def test_push_missing_index_404(client: httpx.AsyncClient) -> None:
    """手推不存在的索引 → 404。"""
    resp = await client.post("/api/v1/torrent-index/999999/push", json={})
    assert resp.status_code == 404


async def test_push_without_default_downloader_400(
    client: httpx.AsyncClient,
) -> None:
    """未配置默认下载器 → 400。"""
    from app.db.models import Site, TorrentIndex

    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="hdsky"))
        session.add(
            TorrentIndex(site_id=1, item_id="i1", name="t", free_type="freeleech_permanent")
        )
        await session.flush()
        index_id = (
            await session.execute(
                select(TorrentIndex.id).where(TorrentIndex.item_id == "i1")
            )
        ).scalar_one()

    resp = await client.post(f"/api/v1/torrent-index/{index_id}/push", json={})
    assert resp.status_code == 400
    assert "未配置默认下载器" in resp.json()["detail"]
