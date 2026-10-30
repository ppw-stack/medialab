"""设备类型 API 的集成测试。

覆盖：清单端点、创建时的枚举校验、修改 device_type、扫码拒绝不可用类型。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(settings, db_engine):
    from app.api.routes import drivers
    from app.api.routes import media_auth as auth
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(drivers.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # 登录
        await c.post(
            "/api/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        yield c

    reset_services()


class TestDeviceTypesEndpoint:
    async def test_lists_all_18(self, client) -> None:
        resp = await client.get("/api/drivers/device-types")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 18

    async def test_marks_qr_capability(self, client) -> None:
        """每个类型都带 qr_capable 标记（前端据此禁用不可扫码的项）。"""
        items = (await client.get("/api/drivers/device-types")).json()
        for item in items:
            assert "qr_capable" in item
            assert isinstance(item["qr_capable"], bool)

    async def test_exactly_10_qr_capable(self, client) -> None:
        items = (await client.get("/api/drivers/device-types")).json()
        capable = [i for i in items if i["qr_capable"]]
        assert len(capable) == 10

    async def test_has_label_for_each(self, client) -> None:
        items = (await client.get("/api/drivers/device-types")).json()
        assert all(i["label"] for i in items)

    async def test_os_windows_is_incapable(self, client) -> None:
        items = (await client.get("/api/drivers/device-types")).json()
        win = next(i for i in items if i["value"] == "os_windows")
        assert win["qr_capable"] is False

    async def test_115android_is_capable(self, client) -> None:
        items = (await client.get("/api/drivers/device-types")).json()
        android = next(i for i in items if i["value"] == "115android")
        assert android["qr_capable"] is True


class TestDriverCreation:
    async def test_create_with_valid_device_type(self, client) -> None:
        resp = await client.post(
            "/api/drivers",
            json={"type": "p115", "name": "测试", "device_type": "qios"},
        )
        assert resp.status_code == 201
        assert resp.json()["device_type"] == "qios"

    async def test_create_defaults_to_android(self, client) -> None:
        """不传 device_type → 默认 115android（不是 web）。"""
        resp = await client.post(
            "/api/drivers", json={"type": "p115", "name": "默认值测试"}
        )
        assert resp.status_code == 201
        assert resp.json()["device_type"] == "115android"

    async def test_invalid_device_type_rejected(self, client) -> None:
        """拼错的设备类型 → 422（不是一路溜到 115 API 才炸）。"""
        resp = await client.post(
            "/api/drivers",
            json={"type": "p115", "name": "x", "device_type": "115_android"},
        )
        assert resp.status_code == 422

    async def test_incapable_device_type_allowed_at_creation(self, client) -> None:
        """不可扫码的类型**可以**创建（它仍可用于其它接口）。"""
        resp = await client.post(
            "/api/drivers",
            json={"type": "p115", "name": "os", "device_type": "os_windows"},
        )
        assert resp.status_code == 201


class TestDriverUpdate:
    async def test_change_device_type(self, client) -> None:
        created = (
            await client.post("/api/drivers", json={"type": "p115", "name": "改设备"})
        ).json()

        resp = await client.patch(
            f"/api/drivers/{created['id']}", json={"device_type": "qandroid"}
        )

        assert resp.status_code == 200
        assert resp.json()["device_type"] == "qandroid"

    async def test_change_name_only(self, client) -> None:
        created = (
            await client.post("/api/drivers", json={"type": "p115", "name": "旧名"})
        ).json()

        resp = await client.patch(
            f"/api/drivers/{created['id']}", json={"name": "新名"}
        )

        assert resp.status_code == 200
        assert resp.json()["name"] == "新名"
        # 设备类型保持不变
        assert resp.json()["device_type"] == created["device_type"]

    async def test_invalid_device_type_rejected(self, client) -> None:
        created = (
            await client.post("/api/drivers", json={"type": "p115", "name": "x"})
        ).json()

        resp = await client.patch(
            f"/api/drivers/{created['id']}", json={"device_type": "bogus"}
        )
        assert resp.status_code == 422

    async def test_patch_unknown_driver_404(self, client) -> None:
        resp = await client.patch("/api/drivers/99999", json={"name": "x"})
        assert resp.status_code == 404


class TestQRStartRejectsIncapable:
    async def test_qr_start_400_for_os_windows(self, client) -> None:
        """选了不可扫码的类型 → 400（明确告知，不是 502 上游故障）。"""
        created = (
            await client.post(
                "/api/drivers",
                json={"type": "p115", "name": "win", "device_type": "os_windows"},
            )
        ).json()

        resp = await client.post(f"/api/drivers/{created['id']}/qr/start")

        assert resp.status_code == 400
        assert "不支持扫码" in resp.json()["detail"]
