"""存储源测连缓存的集成测试。

覆盖：

1. ``probe_driver`` 写进程内缓存 + ``probe_results`` 落库（激活历史）
2. ``invalidate_probe`` / ``store_credential``（凭据变更联动）
3. ``GET /drivers`` 携带缓存状态（``conn`` 字段）
4. ``probe_all_enabled`` 空集 / 正常路径
"""

from __future__ import annotations

import pytest
from app.services import driver_service
from app.services.account_status import STATUS_NO_CREDENTIAL, STATUS_OK, AccountStatus
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def driver_id(db) -> int:
    """建一个 p115 存储源并写入假凭据（走正规 store_credential 路径）。"""
    row = await driver_service.add_driver(
        driver_type="p115", name="测试115", device_type="web"
    )
    async with driver_service.session_scope() as db2:
        await driver_service.store_credential(db2, row.id, "fake-cookie-string")
    return row.id


@pytest.fixture(autouse=True)
def _encryption_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据加密密钥（store_credential 需要；生产来自 CREDENTIAL_ENCRYPTION_SECRET）。"""
    from app.adapters.drivers.p115_credential import SECRET_ENV

    monkeypatch.setenv(SECRET_ENV, "test-encryption-secret-32-chars")


@pytest.fixture(autouse=True)
def _fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """把真实探测替换为固定结果（不打云端 API、不构造真驱动）。"""

    async def fake_probe_account(driver) -> AccountStatus:
        return AccountStatus(
            logged_in=True,
            account_id="uid-123",
            total_bytes=100 * 1024**3,
            used_bytes=10 * 1024**3,
            remain_bytes=90 * 1024**3,
            can_get_link=True,
            status=STATUS_OK,
            message="一切正常",
        )

    monkeypatch.setattr(driver_service, "probe_account", fake_probe_account)
    monkeypatch.setattr(
        driver_service, "build_driver", lambda *a, **k: object()
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    yield
    driver_service._probe_cache.clear()


@pytest.fixture
async def client(settings, db_engine):
    from app.api.auth import router as auth
    from app.api.routes import drivers
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(auth)
    app.include_router(drivers.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "test-pass-123"},
        )
        yield c

    reset_services()


# ---------------------------------------------------------------- 缓存


async def test_probe_driver_caches_result(driver_id: int, db_session) -> None:
    """probe_driver → 进程内缓存 + probe_results 落库。"""
    from app.db.models import ProbeResult
    from sqlalchemy import select

    status = await driver_service.probe_driver(driver_id)
    assert status.status == STATUS_OK

    cached = driver_service.cached_probe(driver_id)
    assert cached is not None
    assert cached["status"].account_id == "uid-123"
    assert cached["checked_at"]  # 北京时间字符串

    rows = (
        (
            await db_session.execute(
                select(ProbeResult).where(ProbeResult.driver_id == driver_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].available is True


async def test_invalidate_probe(driver_id: int) -> None:
    """显式失效 → 缓存清空。"""
    await driver_service.probe_driver(driver_id)
    assert driver_service.cached_probe(driver_id) is not None

    driver_service.invalidate_probe(driver_id)
    assert driver_service.cached_probe(driver_id) is None


async def test_store_credential_invalidates_cache(driver_id: int) -> None:
    """QR/短信登录都会走 store_credential → 旧缓存必须作废。"""
    await driver_service.probe_driver(driver_id)
    assert driver_service.cached_probe(driver_id) is not None

    async with driver_service.session_scope() as db2:
        await driver_service.store_credential(db2, driver_id, "new-cookie")
    assert driver_service.cached_probe(driver_id) is None


async def test_no_credential_still_caches(db_session) -> None:
    """未配置凭据 → no_credential 也进缓存（面板显示「未登录」而非「未测连」）。"""
    row = await driver_service.add_driver(
        driver_type="p115", name="无凭据", device_type="web"
    )
    status = await driver_service.probe_driver(row.id)
    assert status.status == STATUS_NO_CREDENTIAL

    cached = driver_service.cached_probe(row.id)
    assert cached is not None
    assert cached["status"].logged_in is False

    # 未登录没有账号信息，不落 probe_results
    from app.db.models import ProbeResult
    from sqlalchemy import select

    rows = (
        (
            await db_session.execute(
                select(ProbeResult).where(ProbeResult.driver_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_probe_all_enabled_empty(db) -> None:
    """没有可探测的存储源 → 空缓存、不抛。"""
    result = await driver_service.probe_all_enabled()
    assert result == {}


async def test_probe_all_enabled_probes(driver_id: int) -> None:
    """启动自动测连：启用 + 有凭据 → 进缓存。"""
    result = await driver_service.probe_all_enabled()
    assert driver_id in result
    assert result[driver_id]["status"].status == STATUS_OK


# ---------------------------------------------------------------- API


class TestConnInDriverList:
    async def test_list_includes_conn(self, client, driver_id: int) -> None:
        """GET /drivers 携带缓存状态（conn 字段）。"""
        await driver_service.probe_driver(driver_id)

        r = await client.get("/api/drivers")
        assert r.status_code == 200
        items = r.json()
        mine = next(i for i in items if i["id"] == driver_id)
        assert mine["conn"] is not None
        assert mine["conn"]["status"] == STATUS_OK
        assert mine["conn"]["account_id"] == "uid-123"
        assert mine["conn"]["checked_at"]

    async def test_list_conn_none_when_unprobed(self, client, driver_id: int) -> None:
        """没测连过 → conn 为 None（前端显示「未测连」）。"""
        r = await client.get("/api/drivers")
        assert r.status_code == 200
        mine = next(i for i in r.json() if i["id"] == driver_id)
        assert mine["conn"] is None

    async def test_probe_route_updates_cache(self, client, driver_id: int) -> None:
        """手动测连（GET /drivers/{id}/probe）也写缓存。"""
        r = await client.get(f"/api/drivers/{driver_id}/probe")
        assert r.status_code == 200
        assert r.json()["status"] == STATUS_OK
        assert driver_service.cached_probe(driver_id) is not None
