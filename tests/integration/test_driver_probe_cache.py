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
    """建一个光鸭存储源并写入假凭据（走正规 store_credential 路径）。"""
    row = await driver_service.add_driver(driver_type="guangya", name="测试光鸭")
    async with driver_service.session_scope() as db2:
        await driver_service.store_credential(db2, row.id, "fake-cookie-string")
    return row.id


@pytest.fixture(autouse=True)
def _encryption_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据加密密钥（store_credential 需要；生产来自 CREDENTIAL_ENCRYPTION_SECRET）。"""
    from app.adapters.drivers.credential_crypto import SECRET_ENV

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
    assert cached.status.account_id == "uid-123"
    assert cached.checked_at  # 北京时间字符串

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
        driver_type="guangya", name="无凭据"
    )
    status = await driver_service.probe_driver(row.id)
    assert status.status == STATUS_NO_CREDENTIAL

    cached = driver_service.cached_probe(row.id)
    assert cached is not None
    assert cached.status.logged_in is False

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
    assert result[driver_id].status.status == STATUS_OK


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

    async def test_probe_route_returns_checked_at(self, client, driver_id: int) -> None:
        """手动测连返回 ``checked_at``（前端据此判断状态是否陈旧）。"""
        r = await client.get(f"/api/drivers/{driver_id}/probe")
        assert r.status_code == 200
        assert r.json()["checked_at"]


# ---------------------------------------------------------------- 回归
#
# 以下用例针对已修复的真实缺陷——**每个都对应一个会出错的具体场景**，
# 不是「为了覆盖率」。


class TestTokenRefreshPersisted:
    """光鸭 Token 刷新必须写回 DB（否则账号会掉线）。

    **回归的缺陷**（High）：``probe_driver`` 构造驱动时没传
    ``on_token_refresh`` → 探测触发刷新后，新 token 只存在于驱动实例
    内存 → 探测结束实例丢弃 → **DB 里仍是旧 token** → 刷新接口一旦
    轮换 ``refresh_token``，账号彻底掉线。
    """

    async def test_probe_passes_on_token_refresh(self, monkeypatch, db) -> None:
        """``probe_driver`` 构造驱动时必须传 ``on_token_refresh``。"""
        captured: dict[str, object] = {}

        def fake_build_driver(driver_type, credential, **kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(driver_service, "build_driver", fake_build_driver)
        monkeypatch.setattr(
            driver_service,
            "probe_account",
            _fake_probe_account,
        )

        row = await driver_service.add_driver(
            driver_type="guangya", name="光鸭"
        )
        async with driver_service.session_scope() as db2:
            await driver_service.store_credential(db2, row.id, "x", scheme="gypcipher:v2:")

        await driver_service.probe_driver(row.id)
        assert "on_token_refresh" in captured, "探测时没传 on_token_refresh"

    async def test_refreshed_token_written_back(self, monkeypatch, db) -> None:
        """刷新出的新 token 必须落库（DB 里不能还是旧 token）。"""
        from app.adapters.drivers.guangya_credential import GuangyaCredential
        from app.db.models import Driver
        from sqlalchemy import select

        refreshed = GuangyaCredential(
            access_token="NEW-TOKEN",
            refresh_token="NEW-REFRESH",
            device_id="D",
            expires_at=9999999999,
        )

        class FakeGuangyaDriver:
            name = "guangya"

            def __init__(
                self,
                driver_type,
                credential,
                *,
                on_token_refresh=None,
            ):
                self._on_token_refresh = on_token_refresh

            async def check_login(self) -> bool:
                # 模拟驱动内部刷新 token → 回调写回
                if self._on_token_refresh is not None:
                    await self._on_token_refresh(refreshed)
                return True

            async def get_space_info(self) -> dict[str, object]:
                return {
                    "account_id": "u1",
                    "total_bytes": 100,
                    "used_bytes": 10,
                    "remain_bytes": 90,
                }

            async def list_root(self) -> list[object]:
                return []

        monkeypatch.setattr(driver_service, "build_driver", FakeGuangyaDriver)
        # 假的 probe_account **必须真的调 check_login**——刷新逻辑在驱动
        # 内部（`_ensure_token`），不调就永远不会触发 token 刷新。
        async def fake_probe_account(driver):
            await driver.check_login()
            return AccountStatus(
                logged_in=True,
                account_id="u1",
                status=STATUS_OK,
                message="一切正常",
            )

        monkeypatch.setattr(driver_service, "probe_account", fake_probe_account)

        row = await driver_service.add_driver(driver_type="guangya", name="光鸭")
        async with driver_service.session_scope() as db2:
            await driver_service.store_credential(
                db2, row.id, "OLD", scheme="gypcipher:v2:"
            )

        await driver_service.probe_driver(row.id)

        async with driver_service.session_scope() as db2:
            stored = (
                await db2.execute(select(Driver).where(Driver.id == row.id))
            ).scalar_one()
        # 密文，不能直接比对明文——解密后必须含新 token
        from app.adapters.drivers.guangya_credential import decode_credential

        plain_cred = decode_credential(stored.credential or "")
        assert plain_cred is not None
        assert plain_cred.access_token == "NEW-TOKEN", (
            "刷新出的 token 没写回 DB（库里还是旧 token）"
        )


class TestTokenRefreshKeepsCache:
    """光鸭 Token 刷新（常规操作）**不能**清掉测连缓存。

    **回归的缺陷**（Medium）：``store_credential`` 无差别清缓存，而
    Token 刷新写回也走它 → 1~2 小时一次的常规操作把面板从「正常」
    打成「未测连」，正好抵消本特性的意义。
    """

    async def test_token_refresh_writeback_keeps_cache(self, driver_id: int) -> None:
        await driver_service.probe_driver(driver_id)
        assert driver_service.cached_probe(driver_id) is not None

        from app.adapters.drivers.guangya_credential import (
            GuangyaCredential,
        )
        from app.services.migration import persist_guangya_credential

        cred = GuangyaCredential(
            access_token="new", refresh_token="r", device_id="d", expires_at=999
        )
        await persist_guangya_credential(driver_id, cred)

        assert (
            driver_service.cached_probe(driver_id) is not None
        ), "常规 Token 刷新把测连缓存清了"

    async def test_relogin_still_invalidates_cache(self, driver_id: int) -> None:
        """重新扫码登录（凭据真的换了）→ 缓存**仍然要清**。"""
        await driver_service.probe_driver(driver_id)
        assert driver_service.cached_probe(driver_id) is not None

        async with driver_service.session_scope() as db2:
            await driver_service.store_credential(db2, driver_id, "brand-new-cookie")

        assert driver_service.cached_probe(driver_id) is None


class TestDeleteClearsCache:
    """删除存储源必须清掉测连缓存。

    **回归的缺陷**（Medium）：SQLite 的 ``id`` 无 AUTOINCREMENT →
    **删掉最大的 id 后会被复用**。不清缓存的话，新建存储源会继承上一个
    的测连状态（账号 id / 容量 / 「直链正常」）——面板显示一个根本没
    测过的账号。
    """

    async def test_delete_clears_cache(self, driver_id: int) -> None:
        await driver_service.probe_driver(driver_id)
        assert driver_service.cached_probe(driver_id) is not None

        await driver_service.remove_driver(driver_id)
        assert driver_service.cached_probe(driver_id) is None

    async def test_new_driver_does_not_inherit_state(self, db) -> None:
        """删掉再建 → 新存储源**不能**继承旧状态（id 会被 SQLite 复用）。"""
        row = await driver_service.add_driver(
            driver_type="guangya", name="A"
        )
        driver_service._cache_probe(
            row.id,
            AccountStatus(
                logged_in=True,
                account_id="uid-OLD",
                status=STATUS_OK,
                message="ok",
            ),
        )
        await driver_service.remove_driver(row.id)

        row2 = await driver_service.add_driver(
            driver_type="guangya", name="B"
        )
        assert row2.id == row.id, "SQLite 应该复用了 id（这是本用例的前提）"
        assert (
            driver_service.cached_probe(row2.id) is None
        ), "新存储源继承了上一个的测连状态"


class TestProbeResultsRetention:
    """``probe_results`` 表要有保留期清理（否则只增不删）。"""

    async def test_old_probe_results_purged(self, db_session) -> None:
        """超期的探测历史被清理，保留期内的留下。"""
        from datetime import timedelta

        from app.core.timeutils import bj_now, bj_now_dt, format_bj
        from app.db.models import ProbeResult
        from app.services.task_drain import _purge_old_probe_results
        from sqlalchemy import select

        row = await driver_service.add_driver(
            driver_type="guangya", name="探测源"
        )
        old = ProbeResult(
            driver_id=row.id,
            account_id="old",
            used_gb=1.0,
            total_gb=2.0,
            available=True,
            probed_at=format_bj(bj_now_dt() - timedelta(days=400)),
        )
        fresh = ProbeResult(
            driver_id=row.id,
            account_id="fresh",
            used_gb=1.0,
            total_gb=2.0,
            available=True,
            probed_at=bj_now(),
        )
        db_session.add_all([old, fresh])
        await db_session.commit()

        async with driver_service.session_scope() as db2:
            purged = await _purge_old_probe_results(db2)

        assert purged == 1
        rows = (
            (
                await db_session.execute(
                    select(ProbeResult).where(ProbeResult.driver_id == row.id)
                )
            )
            .scalars()
            .all()
        )
        assert [r.account_id for r in rows] == ["fresh"]


async def _fake_probe_account(driver) -> AccountStatus:
    """假的 ``probe_account``（返回「一切正常」，不打云端 API）。"""
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
