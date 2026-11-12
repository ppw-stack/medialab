"""转存 API 的集成测试。

覆盖点：

- ``/api/relays/dirs``：存储源不存在 → 404；**非光鸭盘** → 400
- ``/api/relays/run``：源 = 目标 → 400（自己转存自己没意义）；参数越界 → 422
- 已有任务在跑 → 409（避免互相抢光鸭的分享配额）
- ``/api/relays/jobs/{id}`` / ``cancel``：不存在 → 404
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(settings, db_engine):
    from app.api.auth import router as auth
    from app.api.routes import relays
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(auth)
    app.include_router(relays.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        yield c

    reset_services()


@pytest.fixture(autouse=True)
def clean_jobs():
    """清空内存任务表（转存任务与迁移任务一样是内存态）。"""
    from app.services import relay_jobs

    relay_jobs.clear_all()
    yield
    relay_jobs.clear_all()


async def _add_driver(db_session, **kwargs) -> None:
    from app.db.models import Driver

    defaults = {
        "type": "guangya",
        "name": "光鸭",
        "credential": None,
        "device_type": None,
        "enabled": True,
        "media_cid": None,
        "media_path": None,
        "output_dir": None,
        "video_exts": "mkv",
        "image_exts": "",
        "data_exts": "",
        "min_size_mb": 0,
        "include_subdirs": True,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    defaults.update(kwargs)
    db_session.add(Driver(**defaults))
    await db_session.commit()


class TestBrowseDirs:
    async def test_missing_driver_is_404(self, client) -> None:
        resp = await client.get("/api/relays/dirs?driver_id=999")
        assert resp.status_code == 404

    async def test_non_guangya_is_400(self, client, db_session) -> None:
        await _add_driver(db_session, id=1, type="p115")
        resp = await client.get("/api/relays/dirs?driver_id=1")
        assert resp.status_code == 400
        assert "光鸭" in resp.json()["detail"]

    async def test_lists_subdirs(self, client, db_session, monkeypatch) -> None:
        await _add_driver(db_session, id=1)

        class _FakeDriver:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def list_dir_by_cid(self, cid, parent_path="", relative_dir=""):
                from app.adapters.drivers.base import CloudFile

                return [
                    CloudFile(remote_id="d1", path="/影视", name="电影", is_dir=True),
                    CloudFile(remote_id="f1", path="/a.mkv", name="a.mkv", size=10),
                ]

        import app.services.cloud_relay as mod

        monkeypatch.setattr(mod, "build_driver", lambda *a, **k: _FakeDriver(), raising=False)

        resp = await client.get("/api/relays/dirs?driver_id=1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["dirs"] == [{"id": "d1", "name": "电影"}]


class TestRunValidation:
    async def test_rejects_same_driver(self, client, db_session) -> None:
        await _add_driver(db_session, id=1)
        resp = await client.post(
            "/api/relays/run",
            json={
                "source_driver_id": 1,
                "source_file_id": "100",
                "target_driver_id": 1,
                "target_file_id": "200",
            },
        )
        assert resp.status_code == 400
        assert "同一个" in resp.json()["detail"]

    async def test_requires_source_file_id(self, client) -> None:
        resp = await client.post(
            "/api/relays/run",
            json={
                "source_driver_id": 1,
                "source_file_id": "",
                "target_driver_id": 2,
                "target_file_id": "200",
            },
        )
        assert resp.status_code == 422

    async def test_rejects_submit_interval_out_of_range(self, client) -> None:
        resp = await client.post(
            "/api/relays/run",
            json={
                "source_driver_id": 1,
                "source_file_id": "100",
                "target_driver_id": 2,
                "target_file_id": "200",
                "submit_interval_sec": 9999,
            },
        )
        assert resp.status_code == 422

    async def test_rejects_unknown_field(self, client) -> None:
        """多余字段直接拒（``extra="forbid"``）——别默默吞掉前端传错的东西。"""
        resp = await client.post(
            "/api/relays/run",
            json={
                "source_driver_id": 1,
                "source_file_id": "100",
                "target_driver_id": 2,
                "target_file_id": "200",
                "delete_source": True,
                "bogus": 1,
            },
        )
        assert resp.status_code == 422


class TestJobLifecycle:
    async def test_run_starts_job(self, client, db_session, monkeypatch) -> None:
        await _add_driver(db_session, id=1)
        await _add_driver(db_session, id=2, name="目标")

        async def _fake_relay(job) -> None:
            job.success = 3

        import app.services.cloud_relay as mod

        monkeypatch.setattr(mod, "run_relay", _fake_relay)

        resp = await client.post(
            "/api/relays/run",
            json={
                "source_driver_id": 1,
                "source_file_id": "100",
                "target_driver_id": 2,
                "target_file_id": "200",
                "delete_source": True,
            },
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]

        # 后台任务跑完后状态可见
        for _ in range(50):
            detail = await client.get(f"/api/relays/jobs/{job_id}")
            if detail.json()["state"] != "running":
                break
            import asyncio

            await asyncio.sleep(0.02)

        detail = await client.get(f"/api/relays/jobs/{job_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["state"] == "success"
        assert body["success"] == 3
        assert body["params"]["delete_source"] is True

    async def test_rejects_second_concurrent_job(self, client, db_session, monkeypatch) -> None:
        await _add_driver(db_session, id=1)
        await _add_driver(db_session, id=2, name="目标")

        import app.services.cloud_relay as mod
        from app.services.relay_jobs import STATE_RUNNING, RelayJob, RelayParams

        # 造一个「在跑」的任务占位（引擎不启动，避免真跑）
        job = RelayJob(
            id="running-1",
            params=RelayParams(
                source_driver_id=1,
                source_file_id="100",
                target_driver_id=2,
                target_file_id="200",
            ),
        )
        job.state = STATE_RUNNING
        monkeypatch.setattr(mod, "run_relay", lambda j: None)
        from app.services import relay_jobs

        await relay_jobs.create_job(job.params)
        relay_jobs._JOBS[job.id] = job

        resp = await client.post(
            "/api/relays/run",
            json={
                "source_driver_id": 1,
                "source_file_id": "100",
                "target_driver_id": 2,
                "target_file_id": "200",
            },
        )
        assert resp.status_code == 409

    async def test_unknown_job_is_404(self, client) -> None:
        resp = await client.get("/api/relays/jobs/nope")
        assert resp.status_code == 404

    async def test_cancel_unknown_job_is_404(self, client) -> None:
        resp = await client.post("/api/relays/jobs/nope/cancel")
        assert resp.status_code == 404
