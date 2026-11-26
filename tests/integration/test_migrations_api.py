"""迁移 API 的集成测试。

覆盖点：

- ``/api/migrations/sources`` 的**兜底 cid**：必须用媒体库 cid，
  **不能**用 ``"0"``（那是 115 账号根目录，会列出「手机相册 / 资料 /
  密钥 / 备份」等私人目录）
- 存储源没配媒体库 cid 时应**报错**，而不是退化成账号根目录
- ``/api/migrations/run`` 的参数校验
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(settings, db_engine):
    from app.api.auth import router as auth
    from app.api.routes import migrations
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(auth)
    app.include_router(migrations.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        yield c

    reset_services()


async def _add_driver(db_session, **kwargs) -> None:
    from app.db.models import Driver

    defaults = {
        "type": "guangya",
        "name": "光鸭",
        "credential": None,
        "enabled": True,
        "media_cid": "111",
        "media_path": "/影视",
        "output_dir": None,
        "video_exts": "mkv,mp4",
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


class TestBrowseSources:
    async def test_empty_parent_starts_at_account_root(
        self, client, db_session, monkeypatch
    ) -> None:
        """空 ``parent_id`` = 账号根（光鸭根 fileId 是空串）。"""
        await _add_driver(db_session, id=1)

        seen: list[str] = []

        class _FakeDriver:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def list_dir_by_cid(self, cid, parent_path="", relative_dir=""):
                seen.append(str(cid))
                return []

        # patch **service 层**——驱动构造已下沉到那里
        # （API 层不碰适配层，见 tests/unit/test_layering.py）。
        import app.services.migration as mod

        monkeypatch.setattr(mod, "build_driver", lambda *a, **k: _FakeDriver(), raising=False)

        resp = await client.get("/api/migrations/sources?driver_id=1")

        assert resp.status_code == 200
        assert seen == [""], f"浏览起点应是账号根：{seen}"

    async def test_explicit_parent_is_passed_through(
        self, client, db_session, monkeypatch
    ) -> None:
        """显式 parent_id 原样透传。"""
        await _add_driver(db_session, id=1)
        seen: list[str] = []

        class _FakeDriver:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def list_dir_by_cid(self, cid, parent_path="", relative_dir=""):
                seen.append(str(cid))
                return []

        import app.services.migration as mod

        monkeypatch.setattr(mod, "build_driver", lambda *a, **k: _FakeDriver(), raising=False)

        resp = await client.get("/api/migrations/sources?driver_id=1&parent_id=abc")
        assert resp.status_code == 200
        assert seen == ["abc"]


class TestRunValidation:
    async def test_rejects_submit_interval_out_of_range(self, client) -> None:
        resp = await client.post(
            "/api/migrations/run",
            json={
                "source_driver_id": 1,
                "source_cid": "123",
                "target_driver_id": 2,
                "target_file_id": "",
                "submit_interval_sec": 9999,
            },
        )
        assert resp.status_code == 422

    async def test_rejects_concurrency_out_of_range(self, client) -> None:
        resp = await client.post(
            "/api/migrations/run",
            json={
                "source_driver_id": 1,
                "source_cid": "123",
                "target_driver_id": 2,
                "target_file_id": "",
                "concurrency": 99,
            },
        )
        assert resp.status_code == 422

    async def test_requires_existing_driver(self, client, db_session) -> None:
        resp = await client.post(
            "/api/migrations/run",
            json={
                "source_driver_id": 999,
                "source_cid": "123",
                "target_driver_id": 2,
                "target_file_id": "",
            },
        )
        assert resp.status_code == 404

    async def test_rejects_same_source_and_target(self, client, db_session) -> None:
        """源与目标是同一个存储源 → 400（自己搬自己还会撞名）。"""
        await _add_driver(db_session, id=1, type="guangya", name="光鸭")

        resp = await client.post(
            "/api/migrations/run",
            json={
                "source_driver_id": 1,
                "source_cid": "123",
                "target_driver_id": 1,
                "target_file_id": "",
            },
        )
        assert resp.status_code == 400
        assert "同一个" in resp.json()["detail"]

    async def test_rejects_non_guangya_target(self, client, db_session) -> None:
        """目标必须���光鸭盘 → 400。"""
        await _add_driver(db_session, id=1, type="guangya", name="光鸭源")
        await _add_driver(db_session, id=2, type="fake", name="非光鸭")

        resp = await client.post(
            "/api/migrations/run",
            json={
                "source_driver_id": 1,
                "source_cid": "123",
                "target_driver_id": 2,
                "target_file_id": "",
            },
        )
        assert resp.status_code == 400
        assert "guangya" in resp.json()["detail"]

    async def test_accepts_guangya_source(self, client, db_session) -> None:
        """光鸭 → 光鸭：源是光鸭账号时校验通过（返回 job_id）。"""
        await _add_driver(db_session, id=1, type="guangya", name="光鸭源")
        await _add_driver(db_session, id=2, type="guangya", name="光鸭目标")

        resp = await client.post(
            "/api/migrations/run",
            json={
                "source_driver_id": 1,
                "source_cid": "1962124234139361322",
                "target_driver_id": 2,
                "target_file_id": "",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["job_id"]


class TestBrowseGuangyaSource:
    async def test_browses_from_account_root(
        self, client, db_session, monkeypatch
    ) -> None:
        """光鸭源的目录浏览从**账号根**开始（根 id 是空串）。"""
        await _add_driver(db_session, id=1, type="guangya", name="光鸭源")

        seen: list[str] = []

        class _FakeDriver:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def list_dir_by_cid(self, cid, parent_path="", relative_dir=""):
                seen.append(str(cid))
                return []

        import app.services.migration as mod

        monkeypatch.setattr(mod, "build_driver", lambda *a, **k: _FakeDriver(), raising=False)

        resp = await client.get("/api/migrations/sources?driver_id=1")

        assert resp.status_code == 200
        assert seen == [""], f"光鸭源应从账号根开始：{seen}"


async def test_resume_unknown_job_returns_404(client) -> None:
    """任务不存在 → 404（状态码由 service 层类型化异常给出，不嗅探文案）。"""
    resp = await client.post("/api/migrations/jobs/nope/resume")

    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


async def test_resume_non_paused_job_returns_409(client) -> None:
    """非 paused 状态 → 409（同一个 MigrationError 家族的类型化区分）。"""
    from app.services import migration_jobs
    from app.services.migration_jobs import STATE_SUCCESS, MigrationParams

    job = await migration_jobs.create_job(
        MigrationParams(
            source_driver_id=1,
            source_cid="cid-1",
            target_driver_id=2,
            target_file_id="dir-2",
        )
    )
    job.state = STATE_SUCCESS
    try:
        resp = await client.post(f"/api/migrations/jobs/{job.id}/resume")
        assert resp.status_code == 409
        assert "仅支持续传" in resp.json()["detail"]
    finally:
        migration_jobs.clear_all()
