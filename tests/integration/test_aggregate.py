"""网盘聚合（主从）的集成测试。

覆盖**有 DB / 有 API** 的部分：

1. 聚合组 CRUD
2. 角色指派的三道校验（组内唯一主 / 盘不干净 / 类型不支持）
3. 副数据源不参与媒体库扫描
4. 额度账本（先记后做，跨天归零）
5. 在途台账登记（幂等）
6. HTTP 端点
"""

from __future__ import annotations

import pytest
from app.db.models import Driver, MediaFile, SecondaryStaging
from app.db.session import session_scope
from app.services import aggregate_groups, aggregate_staging, driver_service
from app.services.errors import ServiceError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def master_id(db) -> int:
    row = await driver_service.add_driver(driver_type="guangya", name="主号")
    return row.id


@pytest.fixture
async def secondary_id(db) -> int:
    row = await driver_service.add_driver(driver_type="guangya", name="副号")
    return row.id


@pytest.fixture
async def group_id(db) -> int:
    row = await aggregate_groups.create_group("测试组")
    return row.id


@pytest.fixture
async def client(settings, db_engine):
    from app.api.auth import router as auth
    from app.api.routes import aggregate, drivers
    from app.core.auth import init_hmac_key
    from app.services.svc_factory import init_services, reset_services
    from fastapi import FastAPI

    init_hmac_key(settings.web_token.get_secret_value())
    reset_services()
    init_services(settings)

    app = FastAPI()
    app.include_router(auth)
    app.include_router(drivers.router)
    app.include_router(aggregate.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "test-pass-123"},
        )
        yield c

    reset_services()


# ---------------------------------------------------------------- 聚合组


async def test_group_crud(db) -> None:
    """组的创建 / 改名 / 停用 / 解散。"""
    group = await aggregate_groups.create_group("影视库")
    assert group.name == "影视库"
    assert group.enabled is True

    renamed = await aggregate_groups.update_group(group.id, name="影视库 2")
    assert renamed.name == "影视库 2"

    disabled = await aggregate_groups.update_group(group.id, enabled=False)
    assert disabled.enabled is False

    await aggregate_groups.delete_group(group.id)
    assert await aggregate_groups.list_groups() == []


async def test_group_with_drivers_cannot_be_deleted(
    db, group_id: int, master_id: int
) -> None:
    """组内还有盘 → 拒绝解散（409）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.delete_group(group_id)
    assert exc.value.status == 409


async def test_missing_group_raises_404(db) -> None:
    """组不存在 → 404。"""
    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.get_group(999999)
    assert exc.value.status == 404


# ---------------------------------------------------------------- 角色指派


async def test_assign_master_and_secondary(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """组内一主多副。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )

    async with session_scope() as db_session:
        master = (
            await db_session.execute(select(Driver).where(Driver.id == master_id))
        ).scalar_one()
        second = (
            await db_session.execute(select(Driver).where(Driver.id == secondary_id))
        ).scalar_one()
    assert master.role == "master"
    assert master.aggregate_group_id == group_id
    assert second.role == "secondary"


async def test_second_master_in_group_is_rejected(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """组内只能有一个主数据源——第二个被拒（409），不静默改旧主的角色。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.assign_driver_role(
            secondary_id, group_id=group_id, role="master"
        )
    assert exc.value.status == 409
    assert "主数据源" in str(exc.value)


async def test_non_guangya_driver_cannot_join_aggregate(db, group_id: int) -> None:
    """当前只支持光鸭参与聚合（跨网盘类型要另建通道）。"""
    row = await driver_service.add_driver(driver_type="fake", name="非光鸭")
    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.assign_driver_role(
            row.id, group_id=group_id, role="master"
        )
    assert exc.value.status == 400
    assert "光鸭" in str(exc.value)


async def test_demoting_master_with_media_rows_is_rejected(
    db, group_id: int, master_id: int
) -> None:
    """主 → 副：盘上还挂着媒体库条目时必须先清理（否则留下指向副盘的死引用）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    async with session_scope() as db_session:
        db_session.add(
            MediaFile(
                driver_id=master_id,
                remote_id="f1",
                path="/a.mkv",
                name="a.mkv",
                size=1,
            )
        )
        await db_session.commit()

    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.assign_driver_role(
            master_id, group_id=group_id, role="secondary"
        )
    assert exc.value.status == 409
    assert "媒体库记录" in str(exc.value)


async def test_role_change_blocked_by_pending_staging(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """盘上还有未消化的在途任务 → 角色变更被拒（台账归属会与新角色矛盾）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    async with session_scope() as db_session:
        db_session.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=secondary_id,
                target_driver_id=master_id,
                remote_id="f1",
                file_name="a.mkv",
                size_bytes=10,
                state="pending",
            )
        )
        await db_session.commit()

    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.clear_driver_role(secondary_id)
    assert exc.value.status == 409
    assert "待归拢" in str(exc.value)


async def test_clear_role_detaches_driver(
    db, group_id: int, master_id: int
) -> None:
    """移出组 → 恢复为独立存储源。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.clear_driver_role(master_id)
    async with session_scope() as db_session:
        row = (
            await db_session.execute(select(Driver).where(Driver.id == master_id))
        ).scalar_one()
    assert row.role is None
    assert row.aggregate_group_id is None


async def test_secondary_driver_cannot_be_scanned(
    db, group_id: int, secondary_id: int
) -> None:
    """副数据源是缓冲——不参与媒体库扫描（不生成 strm、不进库）。"""
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    from app.services.driver_service import DriverServiceError

    with pytest.raises(DriverServiceError) as exc:
        await driver_service.start_scan_task(secondary_id)
    assert exc.value.status_code == 400
    assert "副数据源" in str(exc.value)


# ---------------------------------------------------------------- 目录映射


async def test_mapping_is_group_level(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """目录映射是**组级配置**——两端都不借存储源的媒体库配置。

    - 主盘落点：``aggregate_groups.target_dir_id``
    - 副盘缓冲根：``aggregate_group_roots`` 表（每个副盘一行）
    """
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    await aggregate_groups.set_mapping(
        group_id,
        target_dir_id="target-dir",
        target_dir_path="影视",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary_id, root_id="buf-1", root_path="影视"
            )
        ],
    )

    mapping = await aggregate_groups.get_mapping(group_id)
    assert mapping.target_dir_id == "target-dir"
    assert mapping.target_dir_path == "影视"
    assert len(mapping.roots) == 1
    assert mapping.roots[0].driver_id == secondary_id
    assert mapping.roots[0].root_id == "buf-1"

    # 组信息里也带着主盘落点（面板展示用）
    group = await aggregate_groups.get_group(group_id)
    assert group.target_dir_id == "target-dir"


async def test_mapping_rejects_driver_outside_group(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """组外的盘给了缓冲根 → 400（不静默接受）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    outsider = await driver_service.add_driver(driver_type="guangya", name="组外的盘")

    with pytest.raises(ServiceError) as exc:
        await aggregate_groups.set_mapping(
            group_id,
            target_dir_id="target",
            roots=[
                aggregate_groups.GroupRootSpec(
                    driver_id=outsider.id, root_id="buf"
                )
            ],
        )
    assert exc.value.status == 400


async def test_mapping_replaces_roots(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """重新提交映射 → **全量替换**副盘侧（未列出的副盘被清掉）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    await aggregate_groups.set_mapping(
        group_id,
        target_dir_id="target",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary_id, root_id="buf-1"
            )
        ],
    )
    await aggregate_groups.set_mapping(
        group_id,
        target_dir_id="target",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary_id, root_id="buf-2"
            )
        ],
    )
    mapping = await aggregate_groups.get_mapping(group_id)
    assert len(mapping.roots) == 1
    assert mapping.roots[0].root_id == "buf-2"


async def test_api_mapping_roundtrip(client, db) -> None:
    """HTTP：写映射 → 读回来。"""
    await driver_service.add_driver(driver_type="guangya", name="主号")
    await driver_service.add_driver(driver_type="guangya", name="副号")
    drivers = (await client.get("/api/drivers")).json()
    guangya = [d for d in drivers if d["type"] == "guangya"]
    group = (await client.post("/api/aggregate/groups", json={"name": "G"})).json()

    resp = await client.put(
        f"/api/aggregate/drivers/{guangya[0]['id']}/role",
        json={"group_id": group["id"], "role": "master"},
    )
    assert resp.status_code == 200
    resp = await client.put(
        f"/api/aggregate/drivers/{guangya[1]['id']}/role",
        json={"group_id": group["id"], "role": "secondary"},
    )
    assert resp.status_code == 200

    resp = await client.put(
        f"/api/aggregate/groups/{group['id']}/mapping",
        json={
            "target_dir_id": "t-dir",
            "target_dir_path": "影视",
            "roots": [{"driver_id": guangya[1]["id"], "root_id": "buf"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_dir_id"] == "t-dir"
    assert body["roots"][0]["root_id"] == "buf"

    resp = await client.get(f"/api/aggregate/groups/{group['id']}/mapping")
    assert resp.status_code == 200
    assert resp.json() == body


# ---------------------------------------------------------------- 在途台账


async def test_enqueue_is_idempotent(
    db, group_id: int, master_id: int, secondary_id: int, monkeypatch
) -> None:
    """同一个文件重复扫描不会产生第二行（重复登记 = 白扣两次额度）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    await aggregate_groups.set_mapping(
        group_id,
        target_dir_id="target",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary_id, root_id="buf", root_path="影视"
            )
        ],
    )

    class _Fake:
        async def list_dir_by_cid(self, cid, parent_path=""):
            from app.adapters.drivers.base import CloudFile

            return [
                CloudFile(remote_id="f1", path="/a.mkv", name="a.mkv", size=100),
                CloudFile(remote_id="d1", path="/dir", name="dir", is_dir=True),
            ]

    monkeypatch.setattr(aggregate_staging, "build_driver", lambda *a, **k: _Fake())

    first = await aggregate_staging.enqueue_from_staging_root(group_id)
    assert first == {"scanned": 1, "added": 1, "skipped": 0}

    second = await aggregate_staging.enqueue_from_staging_root(group_id)
    assert second == {"scanned": 1, "added": 0, "skipped": 1}

    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert len(rows) == 1
    assert rows[0].file_name == "a.mkv"
    assert rows[0].state == "pending"
    assert rows[0].size_bytes == 100


async def test_enqueue_keeps_relative_dir_structure(
    db, group_id: int, master_id: int, secondary_id: int, monkeypatch
) -> None:
    """归拢要**保留目录结构**（媒体库按「剧名 / Season N / 集」组织）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    await aggregate_groups.set_mapping(
        group_id,
        target_dir_id="target",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary_id, root_id="buf", root_path="影视"
            )
        ],
    )

    class _Fake:
        async def iter_tree_by_cid(self, cid, parent_path="", include_subdirs=True):
            from app.adapters.drivers.base import CloudFile

            yield CloudFile(
                remote_id="f1",
                path="/三体 (2023)/Season 1/ep1.mkv",
                name="ep1.mkv",
                size=100,
                relative_dir="三体 (2023)/Season 1",
            )

    monkeypatch.setattr(aggregate_staging, "build_driver", lambda *a, **k: _Fake())

    result = await aggregate_staging.enqueue_from_staging_root(group_id)
    assert result["added"] == 1

    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].target_dir == "三体 (2023)/Season 1"
    assert rows[0].source_dir == "三体 (2023)/Season 1"
    assert rows[0].file_name == "ep1.mkv"


async def test_enqueue_requires_group_target_dir(
    db, group_id: int, master_id: int, secondary_id: int, monkeypatch
) -> None:
    """组没配**归拢目标目录** → 归拢无处可放 → 明确报错（不静默跳过）。

    归拢落点是**组自己的配置**（不是主盘的媒体库目录）。
    """
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary_id, group_id=group_id, role="secondary"
    )
    with pytest.raises(ServiceError) as exc:
        await aggregate_staging.enqueue_from_staging_root(group_id)
    assert exc.value.status == 400
    assert "归拢目标目录" in str(exc.value)


async def test_retry_resets_failed_row(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """人工重试：失败 → 待归拢。"""
    async with session_scope() as db_session:
        db_session.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=secondary_id,
                target_driver_id=master_id,
                remote_id="f1",
                file_name="a.mkv",
                size_bytes=1,
                state="failed",
                last_error="网络超时",
            )
        )
        await db_session.commit()
        row_id = (
            await db_session.execute(
                select(SecondaryStaging.id).where(
                    SecondaryStaging.remote_id == "f1"
                )
            )
        ).scalar_one()

    await aggregate_staging.retry_staging(row_id)
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "pending"
    assert rows[0].last_error is None


async def test_group_stats_reports_counts(
    db, group_id: int, master_id: int, secondary_id: int
) -> None:
    """水位：各状态条数 + 待归拢字节 + **各状态字节**（与条数同口径）。"""
    await aggregate_groups.assign_driver_role(
        master_id, group_id=group_id, role="master"
    )
    async with session_scope() as db_session:
        db_session.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=secondary_id,
                target_driver_id=master_id,
                remote_id="f1",
                file_name="a.mkv",
                size_bytes=2048,
                state="pending",
            )
        )
        db_session.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=secondary_id,
                target_driver_id=master_id,
                remote_id="f2",
                file_name="b.mkv",
                size_bytes=4096,
                state="failed",
            )
        )
        await db_session.commit()

    stats = await aggregate_staging.group_stats()
    assert len(stats) == 1
    s = stats[0]
    assert s.group_id == group_id
    assert s.master_driver_id == master_id
    assert s.counts.get("pending") == 1
    assert s.counts.get("failed") == 1
    assert s.pending_bytes == 2048 + 4096  # 非终态都算「未消化」
    # bytes_by_state 与 counts **同一次 group by 产出**——口径天然一致
    assert s.bytes_by_state.get("pending") == 2048
    assert s.bytes_by_state.get("failed") == 4096
    assert "done" not in s.bytes_by_state


# ---------------------------------------------------------------- HTTP 端点


async def test_api_group_lifecycle(client) -> None:
    """HTTP：建组 → 改名 → 解散。"""
    resp = await client.post("/api/aggregate/groups", json={"name": "HTTP 组"})
    assert resp.status_code == 201
    group = resp.json()
    assert group["name"] == "HTTP 组"

    resp = await client.patch(
        f"/api/aggregate/groups/{group['id']}", json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp = await client.get("/api/aggregate/groups")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    # 组列表也要带「归拢目标目录」（面板在卡片上显示）
    assert "target_dir_id" in resp.json()[0]

    resp = await client.delete(f"/api/aggregate/groups/{group['id']}")
    assert resp.status_code == 200


async def test_api_assign_role_and_reject_conflict(client, db) -> None:
    """HTTP：指派角色；组内第二个主盘被拒（409）。"""
    await driver_service.add_driver(driver_type="guangya", name="主号")
    await driver_service.add_driver(driver_type="guangya", name="副号")
    drivers = (await client.get("/api/drivers")).json()
    guangya = [d for d in drivers if d["type"] == "guangya"]
    assert len(guangya) == 2

    group = (
        await client.post("/api/aggregate/groups", json={"name": "G"})
    ).json()

    resp = await client.put(
        f"/api/aggregate/drivers/{guangya[0]['id']}/role",
        json={"group_id": group["id"], "role": "master"},
    )
    assert resp.status_code == 200

    resp = await client.put(
        f"/api/aggregate/drivers/{guangya[1]['id']}/role",
        json={"group_id": group["id"], "role": "master"},
    )
    assert resp.status_code == 409

    # 副数据源带角色回来
    resp = await client.get("/api/drivers")
    roles = {d["id"]: d["role"] for d in resp.json()}
    assert roles[guangya[0]["id"]] == "master"
    assert roles[guangya[1]["id"]] is None


async def test_api_roles_list_is_backend_single_source(client) -> None:
    """角色清单由后端下发——前端不手写。"""
    resp = await client.get("/api/aggregate/roles")
    assert resp.status_code == 200
    values = [r["value"] for r in resp.json()]
    assert values == ["master", "secondary"]


async def test_api_limits_and_stats(client, db) -> None:
    """HTTP：服务端常量与水位端点。"""
    resp = await client.get("/api/aggregate/limits")
    assert resp.status_code == 200
    assert resp.json()["daily_cloud_add_quota"] == 3

    resp = await client.get("/api/aggregate/stats")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_api_staging_empty_list(client) -> None:
    """HTTP：台账列表（空）。"""
    resp = await client.get("/api/aggregate/staging")
    assert resp.status_code == 200
    assert resp.json() == []
