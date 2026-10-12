"""调度作业 API 单测（含手动触发端点）。"""

# ruff: noqa: SIM117

from __future__ import annotations

import base64

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def sched_app(db, tmp_path, monkeypatch):
    """调度 API 测试 app：env + 独立 db。"""
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_TOKEN", "cli-token-sched")

    from app.core.config import get_settings
    from app.main import create_app
    from app.services.scheduler import scheduler_center

    get_settings.cache_clear()
    # 清空 specs（lifespan 退出时会把 accepting_new_work = False，污染后续测试）
    scheduler_center._specs.clear()
    scheduler_center.accepting_new_work = True
    scheduler_center._running.clear()
    scheduler_center._inflight.clear()
    app = create_app()
    yield app
    scheduler_center.accepting_new_work = True
    scheduler_center._specs.clear()
    get_settings.cache_clear()


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer cli-token-sched"}


def _register_dummy_job(app, name: str = "manual_test"):
    """往 scheduler_center 注册一个 dummy 作业（不在 lifespan 里）。"""
    from app.services.scheduler import JobSpec, scheduler_center

    async def _noop() -> None:
        return None

    scheduler_center._specs[name] = JobSpec(
        name=name, func=_noop, cron="*/1 * * * *", enabled=True
    )


async def test_toggle_job_success(sched_app) -> None:
    _register_dummy_job(sched_app)
    transport = ASGITransport(app=sched_app)
    async with sched_app.router.lifespan_context(sched_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/scheduler/manual_test/toggle?enabled=false",
                headers=_bearer(),
            )
    assert r.status_code == 200
    assert r.json() == {"job": "manual_test", "enabled": False}


async def test_toggle_job_not_found(sched_app) -> None:
    transport = ASGITransport(app=sched_app)
    async with sched_app.router.lifespan_context(sched_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/scheduler/nope/toggle?enabled=false",
                headers=_bearer(),
            )
    assert r.status_code == 404


async def test_run_job_success(sched_app) -> None:
    _register_dummy_job(sched_app, name="run_ok")
    transport = ASGITransport(app=sched_app)
    async with sched_app.router.lifespan_context(sched_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/scheduler/run_ok/run",
                headers=_bearer(),
            )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["error"] == ""
    assert d["duration_ms"] >= 0
    assert "started_at" in d and "finished_at" in d


async def test_run_job_unknown(sched_app) -> None:
    transport = ASGITransport(app=sched_app)
    async with sched_app.router.lifespan_context(sched_app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/scheduler/nope/run",
                headers=_bearer(),
            )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert "unknown job" in d["error"]


async def test_run_job_refused_when_not_accepting(sched_app) -> None:
    from app.services.scheduler import scheduler_center

    _register_dummy_job(sched_app, name="run_blocked")
    scheduler_center.accepting_new_work = False
    try:
        transport = ASGITransport(app=sched_app)
        async with sched_app.router.lifespan_context(sched_app):
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(
                    "/api/v1/scheduler/run_blocked/run",
                    headers=_bearer(),
                )
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "not accepting" in d["error"]
    finally:
        scheduler_center.accepting_new_work = True