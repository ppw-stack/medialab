"""存储源「扫描时清理已删除」配置（``drivers.auto_prune``）的契约测试。

**为什么要有这个配置**：prune（清理「网盘上已消失」的条目）原本只能靠
手敲 ``?prune=true`` 触发——面板上的「扫描」按钮从不带这个参数，于是
DB 与本地 strm 里会一直攒着网盘已删文件的遗留数据。

固化的契约：

- 列默认 **关**（清理会**永久删除**本地 strm，不进回收站）
- 配置可保存、可读回
- **不传** ``prune`` → 用存储源配置
- **显式传** ``prune=true/false`` → 覆盖配置（区分「没传」与「传 false」）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.db.models import Driver
from app.services import driver_service, scan_runner
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


@pytest.fixture
async def driver_row(db_session) -> Driver:
    """已配置媒体库的存储源（``auto_prune`` 默认关）。"""
    row = Driver(
        id=1,
        type="fake",
        name="测试源",
        credential=None,
        enabled=True,
        media_cid="cid-root",
        media_path="/影视",
        output_dir="/tmp/lib",
        video_exts="mkv,mp4",
        image_exts="jpg",
        data_exts="nfo",
        min_size_mb=0,
        include_subdirs=True,
        created_at="2026-01-01 00:00:00",
        updated_at="2026-01-01 00:00:00",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestAutoPruneColumn:
    async def test_default_off(self, db_session):
        """新建存储源时 ``auto_prune`` **默认关**（清理不可逆）。"""
        row = Driver(
            id=9,
            type="fake",
            name="新源",
            credential=None,
            enabled=True,
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        assert row.auto_prune is False

    async def test_config_roundtrip(self, db_session, driver_row):
        """配置可保存、可读回。

        **不传** ``media_cid``：保存时会实时解析 cid 对应的路径，那步
        需要真实凭据——与本用例要验的 ``auto_prune`` 无关。
        """
        row = await driver_service.update_driver_config(
            driver_row.id, auto_prune=True
        )
        assert row.auto_prune is True

        # 只查列值（不加载整个 ORM 对象——避免惰性加载在同步上下文里 await）
        val = (
            await db_session.execute(
                select(Driver.auto_prune).where(Driver.id == driver_row.id)
            )
        ).scalar_one()
        assert bool(val) is True

        # 再关掉
        row = await driver_service.update_driver_config(
            driver_row.id, auto_prune=False
        )
        assert row.auto_prune is False


class TestPruneResolution:
    """「没传 prune」与「传 prune=false」必须区分得开。"""

    async def _capture(self, monkeypatch, driver_id: int, **kwargs) -> bool:
        """跑一次 start_scan_task，捕获实际传给扫描的 prune 值。"""
        seen: dict[str, bool] = {}

        def fake_start(driver_id, driver_name, *, prune=False):
            seen["prune"] = prune
            # 不建真 job——真 job 会留在「运行中」，下一次调用被 409 拦
            return SimpleNamespace(id="fake-job", state="running")

        monkeypatch.setattr(scan_runner, "start_scan_job", fake_start)
        await driver_service.start_scan_task(driver_id, **kwargs)
        return seen["prune"]

    async def test_uses_config_when_not_given(
        self, db_session, driver_row, monkeypatch
    ):
        """不传 prune → 用存储源配置（关 → 不清理）。"""
        assert await self._capture(monkeypatch, driver_row.id) is False

        await driver_service.update_driver_config(driver_row.id, auto_prune=True)
        assert await self._capture(monkeypatch, driver_row.id) is True

    async def test_explicit_overrides_config(
        self, db_session, driver_row, monkeypatch
    ):
        """显式传 prune → 覆盖配置（两个方向都要能覆盖）。"""
        await driver_service.update_driver_config(driver_row.id, auto_prune=True)
        assert await self._capture(monkeypatch, driver_row.id, prune=False) is False

        await driver_service.update_driver_config(driver_row.id, auto_prune=False)
        assert await self._capture(monkeypatch, driver_row.id, prune=True) is True


@pytest.fixture
async def client(settings, db_engine):
    """带登录态的 API 客户端（只挂 drivers 路由）。"""
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


class TestAutoPruneApi:
    async def test_config_api_roundtrip(self, client, driver_row):
        """配置经 API 保存 → 读回 → 列表里也带得上。

        ``media_cid`` 传 null：保存时会实时解析 cid 对应的路径，那步需要
        真实凭据——与本用例要验的 ``auto_prune`` 无关。
        """
        resp = await client.put(
            f"/api/drivers/{driver_row.id}/config",
            json={
                "media_cid": None,
                "output_dir": "/tmp/lib",
                "video_exts": "mkv,mp4",
                "min_size_mb": 0,
                "include_subdirs": True,
                "auto_prune": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auto_prune"] is True

        items = (await client.get("/api/drivers")).json()
        mine = [d for d in items if d["id"] == driver_row.id]
        assert mine and mine[0]["auto_prune"] is True

    async def test_scan_endpoint_prune_defaults_to_none(
        self, client, driver_row, monkeypatch
    ):
        """**不传 prune → 透传 None**（由 service 层按配置解析）。

        「没传」与「传 false」必须区分得开，否则配置为关时就没法单次强制清理。
        """
        seen: dict[str, object] = {}

        async def fake_start(driver_id, *, prune=None):
            seen["prune"] = prune
            return SimpleNamespace(job_id="fake-job", state="running")

        monkeypatch.setattr(
            "app.api.routes.drivers.start_scan_task", fake_start
        )

        resp = await client.post(f"/api/drivers/{driver_row.id}/scan")
        assert resp.status_code == 200
        assert seen["prune"] is None

        resp = await client.post(f"/api/drivers/{driver_row.id}/scan?prune=true")
        assert resp.status_code == 200
        assert seen["prune"] is True

        resp = await client.post(f"/api/drivers/{driver_row.id}/scan?prune=false")
        assert resp.status_code == 200
        assert seen["prune"] is False
