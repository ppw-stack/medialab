"""API 冒烟测试（FR-6 / FR-8.2）。"""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.asyncio
async def test_healthz_ok(settings) -> None:
    """FR-8.2：健康检查端点。"""
    import os

    from app.core.config import get_settings
    from app.db.session import init_db, writer
    from app.main import create_app

    get_settings.cache_clear()
    os.environ["ENCRYPTION_KEY"] = settings.encryption_key.get_secret_value()
    init_db(settings)
    await writer.create_all()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] is True


@pytest.mark.asyncio
async def test_metrics_endpoint(settings) -> None:
    """FR-8.3：Prometheus 指标。"""
    import os

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    os.environ["ENCRYPTION_KEY"] = settings.encryption_key.get_secret_value()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "seedhelper_" in resp.text


@pytest.mark.asyncio
async def test_openapi_schema_builds(settings) -> None:
    import os

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    os.environ["ENCRYPTION_KEY"] = settings.encryption_key.get_secret_value()
    app = create_app(settings)
    schema = app.openapi()
    assert schema["info"]["title"] == "SeedHelper"
    paths = schema["paths"]
    for expected in ("/healthz", "/metrics", "/api/v1/dashboard", "/api/v1/sites"):
        assert expected in paths, expected


def test_base64_key_helper() -> None:
    """确保测试密钥格式合法，避免夹具本身成为噪声源。"""
    key = base64.urlsafe_b64encode(b"a" * 32).decode()
    assert len(base64.urlsafe_b64decode(key)) == 32