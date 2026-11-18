"""上传任务 API 的集成测试。

覆盖点：

- 监听目录不存在 → 400
- 目标存储源不支持上传 → 400
- CRUD 基本流程
- 需要登录（401）
- ``/capabilities`` 返回支持上传的驱动类型（前端据此过滤下拉）
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(settings, db_engine, tmp_path):
    """带鉴权的测试客户端。"""
    from app.api.auth import router as auth
    from app.api.routes import upload_tasks
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(auth)
    app.include_router(upload_tasks.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login", json={"username": "tester", "password": "test-pass-123"}
        )
        yield c

    reset_services()


@pytest.fixture
async def anon_client(settings, db_engine):
    """未登录的客户端（测 401）。"""
    from app.api.auth import router as auth
    from app.api.routes import upload_tasks
    from app.core.auth import init_hmac_key
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())

    app = FastAPI()
    app.include_router(auth)
    app.include_router(upload_tasks.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _add_driver(db_session, **kwargs) -> int:
    """加一个存储源，返回 id。"""
    from app.db.models import Driver

    defaults = {
        "type": "guangya",
        "name": "光鸭盘",
        "credential": None,
        "device_type": None,
        "enabled": True,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    defaults.update(kwargs)
    row = Driver(**defaults)
    db_session.add(row)
    await db_session.commit()
    return row.id


@pytest.fixture
async def aggregate_group(db_session):
    """一个配齐的聚合组：1 主 + 1 副（副盘带缓冲根）。"""
    from app.services import aggregate_groups

    master_id = await _add_driver(db_session, name="主号")
    secondary_id = await _add_driver(db_session, name="副号")
    group = await aggregate_groups.create_group("测试组")
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group.id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group.id, role="secondary"
    )
    await aggregate_groups.set_mapping(
        group.id,
        target_dir_id="target-dir",
        target_dir_path="/影视",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary_id, root_id="root-1", root_path="/缓冲"
            )
        ],
    )
    return group.id


class TestAggregateTarget:
    async def test_创建聚合任务返回组id(
        self, client, db_session, aggregate_group, tmp_path
    ) -> None:
        """目标是聚合组 → 落点由后端分配（副盘 + 缓冲根）。"""
        watch = tmp_path / "watch"
        watch.mkdir()

        resp = await client.post(
            "/api/upload-tasks",
            json={
                "name": "聚合任务",
                "aggregate_group_id": aggregate_group,
                "watch_root": str(watch),
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["aggregate_group_id"] == aggregate_group
        assert data["target_file_id"] == "root-1"

    async def test_聚合目标端点返回可用组(
        self, client, aggregate_group
    ) -> None:
        """``/aggregate-targets`` 返回可用组 + 各副盘状态。"""
        resp = await client.get("/api/upload-tasks/aggregate-targets")
        assert resp.status_code == 200
        groups = resp.json()["groups"]
        assert len(groups) == 1
        item = groups[0]
        assert item["group_id"] == aggregate_group
        assert item["target_dir_path"] == "/影视"
        assert len(item["candidates"]) == 1
        cand = item["candidates"][0]
        assert cand["root_id"] == "root-1"
        assert cand["exhausted"] is False

    async def test_可清空聚合组退回存储源模式(
        self, client, db_session, aggregate_group, tmp_path
    ) -> None:
        """显式传 ``aggregate_group_id=null`` → 退回「指定存储源」。"""
        watch = tmp_path / "watch"
        watch.mkdir()
        driver_id = await _add_driver(db_session, name="普通盘")

        created = await client.post(
            "/api/upload-tasks",
            json={
                "name": "聚合任务",
                "aggregate_group_id": aggregate_group,
                "watch_root": str(watch),
            },
        )
        task_id = created.json()["id"]

        patched = await client.patch(
            f"/api/upload-tasks/{task_id}",
            json={"aggregate_group_id": None, "driver_id": driver_id},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["aggregate_group_id"] is None
        assert patched.json()["driver_id"] == driver_id


class TestAuth:
    async def test_列表需要登录(self, anon_client) -> None:
        """未登录 → 401。"""
        resp = await anon_client.get("/api/upload-tasks")
        assert resp.status_code == 401


class TestWhitelist:
    async def test_目标存储源不支持上传(self, client, db_session, tmp_path) -> None:
        """目标存储源不支持上传（如 115）→ 400。"""
        driver_id = await _add_driver(db_session, type="p115")
        resp = await client.post(
            "/api/upload-tasks",
            json={
                "name": "测试",
                "driver_id": driver_id,
                "target_file_id": "123",
                "watch_root": str(tmp_path),
            },
        )
        assert resp.status_code == 400
        assert "不支持上传" in resp.json()["detail"]

    async def test_存储源不存在(self, client, tmp_path) -> None:
        """存储源 id 不存在 → 404。"""
        resp = await client.post(
            "/api/upload-tasks",
            json={
                "name": "测试",
                "driver_id": 9999,
                "target_file_id": "123",
                "watch_root": str(tmp_path),
            },
        )
        assert resp.status_code == 404


class TestCapabilities:
    async def test_返回支持上传的驱动类型(self, client) -> None:
        """``/capabilities`` 返回支持上传的驱动类型列表。

        前端靠它过滤「目标网盘」下拉——加新网盘时前端零改动。
        """
        from app.adapters.drivers.upload_base import UPLOADABLE_DRIVER_TYPES

        resp = await client.get("/api/upload-tasks/capabilities")
        assert resp.status_code == 200
        types = resp.json()["uploadable_types"]
        assert types == sorted(UPLOADABLE_DRIVER_TYPES)
        assert "guangya" in types

    async def test_需要登录(self, anon_client) -> None:
        resp = await anon_client.get("/api/upload-tasks/capabilities")
        assert resp.status_code == 401


class TestCrud:
    async def test_创建与列出(self, client, db_session, tmp_path) -> None:
        """创建后能列出来。"""
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()

        resp = await client.post(
            "/api/upload-tasks",
            json={
                "name": "测试任务",
                "driver_id": driver_id,
                "target_file_id": "1962124234139361322",
                "watch_root": str(watch),
                "target_path": "/影视",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "测试任务"
        assert data["watch_root"] == str(watch)

        listing = await client.get("/api/upload-tasks")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

    async def test_修改与删除(self, client, db_session, tmp_path) -> None:
        """改名 → 删掉。"""
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()

        created = await client.post(
            "/api/upload-tasks",
            json={
                "name": "原名",
                "driver_id": driver_id,
                "target_file_id": "1",
                "watch_root": str(watch),
            },
        )
        task_id = created.json()["id"]

        patched = await client.patch(
            f"/api/upload-tasks/{task_id}", json={"name": "新名"}
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "新名"

        deleted = await client.delete(f"/api/upload-tasks/{task_id}")
        assert deleted.status_code == 200

        listing = await client.get("/api/upload-tasks")
        assert listing.json() == []

    async def test_冲突策略默认为保留大的(self, client, db_session, tmp_path) -> None:
        """新建任务默认 ``conflict_policy=1``（保留大的）。

        2026-11-09：本地重新压制出的版本往往更小，直接覆盖会顶掉云端那份大的。
        """
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()

        resp = await client.post(
            "/api/upload-tasks",
            json={
                "name": "策略任务",
                "driver_id": driver_id,
                "target_file_id": "1",
                "watch_root": str(watch),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["conflict_policy"] == 1

    async def test_可以改成本地优先(self, client, db_session, tmp_path) -> None:
        """显式传 0 → 覆盖云端（旧行为）；传 1 → 保留大的。"""
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()

        created = await client.post(
            "/api/upload-tasks",
            json={
                "name": "策略任务",
                "driver_id": driver_id,
                "target_file_id": "1",
                "watch_root": str(watch),
                "conflict_policy": 0,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["conflict_policy"] == 0

        patched = await client.patch(
            f"/api/upload-tasks/{created.json()['id']}", json={"conflict_policy": 1}
        )
        assert patched.status_code == 200
        assert patched.json()["conflict_policy"] == 1

    async def test_冲突策略取值非法被拒(self, client, db_session, tmp_path) -> None:
        """越界取值 → 422（只允许 0 / 1）。"""
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()

        resp = await client.post(
            "/api/upload-tasks",
            json={
                "name": "非法策略",
                "driver_id": driver_id,
                "target_file_id": "1",
                "watch_root": str(watch),
                "conflict_policy": 5,
            },
        )
        assert resp.status_code == 422

    async def test_修改不存在的任务(self, client) -> None:
        """改不存在的任务 → 404。"""
        resp = await client.patch("/api/upload-tasks/9999", json={"name": "x"})
        assert resp.status_code == 404

    async def test_删除不存在的任务(self, client) -> None:
        """删不存在的任务 → 404。"""
        resp = await client.delete("/api/upload-tasks/9999")
        assert resp.status_code == 404

    async def test_浏览本地目录(self, client, tmp_path) -> None:
        """``/local-dirs`` 列出本地子目录（用于选监听目录）。"""
        base = tmp_path / "browse"
        base.mkdir()
        (base / "movies").mkdir()
        (base / ".hidden").mkdir()

        resp = await client.get(
            "/api/upload-tasks/local-dirs", params={"path": str(base)}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == str(base)
        # 隐藏目录不列
        assert [d["name"] for d in body["dirs"]] == ["movies"]
        # **id 必须是完整路径**（前端拿它当下一层路径直接请求）
        assert body["dirs"][0]["id"] == str(base / "movies")

    async def test_浏览本地目录_不存在(self, client, tmp_path) -> None:
        """目录不存在 → 400。"""
        resp = await client.get(
            "/api/upload-tasks/local-dirs", params={"path": str(tmp_path / "nope")}
        )
        assert resp.status_code == 400

    async def test_静态路由不被参数化路由吃掉(self, client) -> None:
        """**回归测试**：静态路径必须排在 ``/{task_id}`` **之前**。

        FastAPI 按注册顺序匹配。``/local-dirs`` / ``/targets`` 若排在
        ``/{task_id}`` 之后，会先被 ``/{task_id}`` 匹配 → 422
        （"local-dirs" 解析成 int 失败）→ 目录浏览直接挂掉。
        """
        resp = await client.get("/api/upload-tasks/local-dirs?path=/")
        assert resp.status_code == 200
        assert "dirs" in resp.json()

    async def test_取单个任务(self, client, db_session, tmp_path) -> None:
        """``GET /api/upload-tasks/{id}`` 返回任务详情。

        **必须有这个路由**：否则会落到 SPA 的 catch-all → 返回
        index.html（HTTP 200 + HTML），调用方以为拿到了 JSON。
        """
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()
        created = await client.post(
            "/api/upload-tasks",
            json={
                "name": "详情测试",
                "driver_id": driver_id,
                "target_file_id": "1",
                "watch_root": str(watch),
                "enabled": False,
            },
        )
        task_id = created.json()["id"]

        resp = await client.get(f"/api/upload-tasks/{task_id}")

        assert resp.status_code == 200
        # 必须是 JSON（不是 SPA 的 HTML）
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["name"] == "详情测试"

    async def test_取不存在的任务(self, client) -> None:
        """不存在的任务 → 404。"""
        resp = await client.get("/api/upload-tasks/9999")
        assert resp.status_code == 404

    async def test_状态接口(self, client, db_session, tmp_path) -> None:
        """状态接口返回运行态。"""
        driver_id = await _add_driver(db_session)
        watch = tmp_path / "watch"
        watch.mkdir()
        created = await client.post(
            "/api/upload-tasks",
            json={
                "name": "任务",
                "driver_id": driver_id,
                "target_file_id": "1",
                "watch_root": str(watch),
                # 禁用：不启动监听（避免测试里真的去连网盘）
                "enabled": False,
            },
        )
        task_id = created.json()["id"]

        resp = await client.get(f"/api/upload-tasks/{task_id}/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == task_id
        assert body["listening"] is False
        assert "logs" in body
