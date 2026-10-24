"""API 集成测试（Phase 0）。

覆盖认证 + 端点契约。FastAPI 用 ``httpx.ASGITransport`` 直接打
应用（不启真服务器），``lifespan`` 手动跑——这依赖 Phase 0 修好的
settings 注入通道（否则会用 ``/app/data`` 的真 DB）。

注意：``scheduler_enabled=False`` → lifespan 不会启动调度中心，
不会真的去拉站点。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from app.core.config import AppSettings
from app.db.models import Site, SiteUserData, TorrentIndex
from app.db.session import session_scope
from app.main import create_app
from sqlalchemy import select


@pytest_asyncio.fixture
async def client(
    app_settings: AppSettings, db: None
) -> AsyncIterator[httpx.AsyncClient]:
    """已登录的 API 客户端（走 lifespan 初始化）。"""
    app = create_app(app_settings)
    # lifespan 里 init_db 会重置 engine（幂等）——先 dispose 让它重建
    from app.db.session import dispose_db

    await dispose_db()

    # 触发 lifespan（ASGITransport 不自动跑）
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
        app.router.lifespan_context(app),
    ):
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "test-pass-123"},
        )
        assert resp.status_code == 200, resp.text
        yield c


async def _add_site(name: str = "s1", **kw: object) -> int:
    async with session_scope() as session:
        s = Site(name=name, domain=f"{name}.test", adapter="hdsky", **kw)  # type: ignore[arg-type]
        session.add(s)
        await session.flush()
        return s.id


# ------------------------------------------------------------------ 认证

async def test_login_success_sets_cookie(client: httpx.AsyncClient) -> None:
    assert "seedhelper_session" in client.cookies


async def test_login_wrong_password_401(app_settings: AppSettings) -> None:
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "wrong"},
        )
    assert resp.status_code == 401


async def test_login_wrong_username_401(app_settings: AppSettings) -> None:
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "test-pass-123"},
        )
    assert resp.status_code == 401


async def test_protected_endpoint_requires_session(app_settings: AppSettings) -> None:
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/api/v1/sites")
    assert resp.status_code == 401


async def test_bearer_token_auth_works(app_settings: AppSettings) -> None:
    """Bearer token 是 CLI/脚本入口（FR-6.1）。"""
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c, app.router.lifespan_context(app):
        resp = await c.get(
            "/api/v1/sites",
            headers={"Authorization": "Bearer test-token-32-chars-abcdefgh"},
        )
    assert resp.status_code == 200


async def test_auth_status(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["username"] == "tester"


# ------------------------------------------------------------------ 运维

async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] is True


async def test_metrics(client: httpx.AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus 文本格式（prometheus_client 返回 openmetrics 或 text/plain）
    assert "text/plain" in resp.headers["content-type"] or "openmetrics" in (
        resp.headers["content-type"]
    )


# ------------------------------------------------------------------ 站点

async def test_list_sites_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_sites(client: httpx.AsyncClient) -> None:
    await _add_site("s1")
    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "s1"
    assert data[0]["adapter"] == "hdsky"


async def test_create_site(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "newsite", "adapter": "hdsky", "domain": "x.test"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "newsite"


async def test_create_site_duplicate_name_409(client: httpx.AsyncClient) -> None:
    await _add_site("dup")
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "dup", "adapter": "hdsky", "domain": "y.test"},
    )
    assert resp.status_code == 409


async def test_create_site_invalid_adapter_400(client: httpx.AsyncClient) -> None:
    """未注册的 adapter → 400。

    之前用 pydantic ``Literal`` 校验返回 422，错误信息是「不符合
    Literal」而非「未注册的站点 adapter」。改为运行时校验后（名单
    单一源在 ``_registry``），错误信息更明确。
    """
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "x", "adapter": "nonexistent"},
    )
    assert resp.status_code == 400
    assert "未注册的站点 adapter" in resp.json()["detail"]


async def test_update_site_invalid_adapter_400(client: httpx.AsyncClient) -> None:
    """更新时传未注册 adapter → 400。"""
    site_id = await _add_site("s1")
    resp = await client.post(
        "/api/v1/sites/update",
        json={"id": site_id, "adapter": "nonexistent"},
    )
    assert resp.status_code == 400
    assert "未注册的站点 adapter" in resp.json()["detail"]


async def test_create_site_valid_adapter_accepted(client: httpx.AsyncClient) -> None:
    """注册表里的 adapter 都能通过校验。"""
    for i, adapter in enumerate(("mteam", "hdsky", "ourbits", "soulvoice")):
        resp = await client.post(
            "/api/v1/sites",
            json={"name": f"site{i}", "adapter": adapter},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["adapter"] == adapter


async def test_update_site_valid_adapter_accepted(client: httpx.AsyncClient) -> None:
    site_id = await _add_site("s1")
    resp = await client.post(
        "/api/v1/sites/update",
        json={"id": site_id, "adapter": "mteam"},
    )
    assert resp.status_code == 200
    assert resp.json()["adapter"] == "mteam"


async def test_create_site_invalid_name_422(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "bad name!", "adapter": "hdsky"},
    )
    assert resp.status_code == 422


async def test_get_site_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/sites/999999")
    assert resp.status_code == 404


async def test_delete_site(client: httpx.AsyncClient) -> None:
    site_id = await _add_site("todelete")
    resp = await client.post("/api/v1/sites/delete", json={"id": site_id})
    assert resp.status_code == 200

    async with session_scope() as session:
        rows = (await session.execute(select(Site))).scalars().all()
        assert rows == []


async def test_delete_site_with_index_rows_succeeds(
    client: httpx.AsyncClient,
) -> None:
    """站点下有拉取索引行时删除**成功**（级联删，不再 409）。

    2026-10-23：原实现是「站点下还有 N 条任务，请先清理」→ 409。但那是
    死胡同——``torrent_index`` 是可重建的拉取快照，全仓没有删除它的
    入口，且 ``fetch_all_sites`` 每 10 分钟就重新灌满。
    """
    site_id = await _add_site("hasindex")
    async with session_scope() as session:
        session.add(TorrentIndex(site_id=site_id, item_id="i1", name="t"))
        session.add(SiteUserData(site_id=site_id, upload=1, download=2))
        await session.flush()

    resp = await client.post("/api/v1/sites/delete", json={"id": site_id})
    assert resp.status_code == 200

    async with session_scope() as session:
        assert (await session.execute(select(Site))).scalars().all() == []
        assert (await session.execute(select(TorrentIndex))).scalars().all() == []
        assert (await session.execute(select(SiteUserData))).scalars().all() == []


async def test_list_adapters(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/sites/adapters")
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()}
    assert "hdsky" in names
    assert "mteam" in names
    assert len(names) == 9


# ------------------------------------------------------------------ 用户数据

async def test_list_user_data_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/user-data")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_site_user_data_no_data(client: httpx.AsyncClient) -> None:
    site_id = await _add_site("s1")
    resp = await client.get(f"/api/v1/sites/{site_id}/user-data")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is False


async def test_list_user_data_with_row(client: httpx.AsyncClient) -> None:
    from app.db.models import SiteUserData

    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            SiteUserData(
                site_id=site_id,
                upload=1024,
                download=2048,
                seeding_count=10,
                leeching_count=2,
            )
        )
        await session.flush()

    resp = await client.get("/api/v1/user-data")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["upload"] == 1024
    assert data[0]["seeding_count"] == 10


# ------------------------------------------------------------------ 辅种快照

async def _add_cross_seed_downloader(name: str = "dl1") -> int:
    """加一个辅种下载器（is_cross_seed=True）."""
    from app.db.models import DownloaderInstance

    async with session_scope() as session:
        row = DownloaderInstance(
            name=name,
            type="qbittorrent",
            url="http://localhost:8081",
            is_cross_seed=True,
            enabled=True,
        )
        session.add(row)
        await session.flush()
        return row.id


async def test_cross_seed_downloaders_list(client: httpx.AsyncClient) -> None:
    dl_id = await _add_cross_seed_downloader("dl1")
    resp = await client.get("/api/v1/cross-seed-monitor/downloaders")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == dl_id
    assert data[0]["is_cross_seed"] is True


async def test_cross_seed_snapshots_empty(client: httpx.AsyncClient) -> None:
    """没有快照 → 返回空列表 + pending（等首次抓取）。"""
    await _add_cross_seed_downloader("dl1")
    resp = await client.get("/api/v1/cross-seed-monitor/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshots"] == []
    assert len(data["pending"]) == 1
    assert data["pending"][0]["name"] == "dl1"


async def test_cross_seed_snapshots_with_row(client: httpx.AsyncClient) -> None:
    from app.db.models import CrossSeedSnapshot

    dl_id = await _add_cross_seed_downloader("dl1")
    async with session_scope() as session:
        session.add(
            CrossSeedSnapshot(
                downloader_id=dl_id,
                downloader_name="dl1",
                scanned_at="2026-10-24 12:00:00",
                total_torrents=3000,
                unique_count=120,
                anomaly_count=2,
                inflight_count=2900,
                stats=[{"name": "X", "size_gb": 1.0, "count": 3, "sample_hash": "aaa"}],
                anomalies=[
                    {
                        "id": 1,
                        "hash": "a" * 40,
                        "name": "Bad",
                        "size_gb": 1.0,
                        "status": 6,
                        "anomaly_type": "tracker_not_registered",
                        "error_code": 2,
                        "error_string": "Tracker not registered",
                        "paused": False,
                    }
                ],
            )
        )
        await session.flush()

    resp = await client.get("/api/v1/cross-seed-monitor/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["snapshots"]) == 1
    snap = data["snapshots"][0]
    assert snap["downloader_id"] == dl_id
    assert snap["total_torrents"] == 3000
    assert snap["anomaly_count"] == 2
    assert snap["inflight_count"] == 2900
    # 已抓过 → 不在 pending 里
    assert data["pending"] == []


async def test_cross_seed_snapshot_detail(client: httpx.AsyncClient) -> None:
    from app.db.models import CrossSeedSnapshot

    dl_id = await _add_cross_seed_downloader("dl1")
    async with session_scope() as session:
        session.add(
            CrossSeedSnapshot(
                downloader_id=dl_id,
                downloader_name="dl1",
                total_torrents=2,
                unique_count=1,
                anomaly_count=1,
                stats=[{"name": "X", "size_gb": 1.0, "count": 2, "sample_hash": "aaa"}],
                anomalies=[
                    {
                        "id": 1,
                        "hash": "b" * 40,
                        "name": "Bad",
                        "size_gb": 1.0,
                        "status": 6,
                        "anomaly_type": "no_data_found",
                        "error_code": 3,
                        "error_string": "No data found",
                        "paused": True,
                    }
                ],
            )
        )
        await session.flush()

    resp = await client.get(f"/api/v1/cross-seed-monitor/downloaders/{dl_id}/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_torrents"] == 2
    assert len(data["stats"]) == 1
    assert len(data["anomalies"]) == 1
    assert data["anomalies"][0]["anomaly_type"] == "no_data_found"


async def test_cross_seed_snapshot_detail_404_when_missing(
    client: httpx.AsyncClient,
) -> None:
    """还没抓过 → 404（前端据此显示「等待首次抓取」）。"""
    dl_id = await _add_cross_seed_downloader("dl1")
    resp = await client.get(f"/api/v1/cross-seed-monitor/downloaders/{dl_id}/snapshot")
    assert resp.status_code == 404


async def test_cross_seed_refresh_non_cross_seed_400(
    client: httpx.AsyncClient,
) -> None:
    """非辅种下载器 → 400（不是 500）。"""
    from app.db.models import DownloaderInstance

    async with session_scope() as session:
        row = DownloaderInstance(
            name="plain", type="qbittorrent", url="http://x", is_cross_seed=False
        )
        session.add(row)
        await session.flush()
        plain_id = row.id

    resp = await client.post(
        f"/api/v1/cross-seed-monitor/downloaders/{plain_id}/refresh"
    )
    assert resp.status_code == 400


async def test_cross_seed_refresh_unknown_downloader_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post("/api/v1/cross-seed-monitor/downloaders/9999/refresh")
    assert resp.status_code == 404


async def test_cross_seed_space_empty(client: httpx.AsyncClient) -> None:
    """没有快照 → 空汇总（磁盘为 null，不是 0）。"""
    resp = await client.get("/api/v1/cross-seed-monitor/space")
    assert resp.status_code == 200
    data = resp.json()
    assert data["downloaders"] == []
    assert data["summary"]["downloaders"] == 0
    assert data["summary"]["disk_free_bytes"] is None


async def test_cross_seed_space_with_snapshot(client: httpx.AsyncClient) -> None:
    """空间汇总：含磁盘剩余与辅种占用。"""
    from app.db.models import CrossSeedSnapshot

    dl_id = await _add_cross_seed_downloader("dl1")
    async with session_scope() as session:
        session.add(
            CrossSeedSnapshot(
                downloader_id=dl_id,
                downloader_name="dl1",
                scanned_at="2026-10-24 12:00:00",
                total_torrents=10,
                unique_count=5,
                anomaly_count=0,
                disk_free_bytes=4000,
                disk_total_bytes=10000,
                used_bytes=3200,
                total_size_bytes=3500,
            )
        )
        await session.flush()

    resp = await client.get("/api/v1/cross-seed-monitor/space")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["downloaders"]) == 1
    assert data["downloaders"][0]["used_bytes"] == 3200
    assert data["downloaders"][0]["disk_free_bytes"] == 4000
    assert data["summary"]["used_bytes"] == 3200
    assert data["summary"]["disk_free_bytes"] == 4000
    assert data["summary"]["disk_total_bytes"] == 10000


# ------------------------------------------------------------------ 调度

async def test_scheduler_status(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/scheduler")
    assert resp.status_code == 200
    data = resp.json()
    assert "jobs" in data
    assert "inflight" in data


async def test_run_unknown_job_reports_failure(client: httpx.AsyncClient) -> None:
    """未知作业：返回 200 + ok=False（不是 404）。

    设计如此——手动触发走 scheduler_center.run_now()，它对未知作业
    记录一条失败历史并回 ok=False，语义上与「作业存在但执行失败」一致。
    """
    resp = await client.post("/api/v1/scheduler/nonexistent_job/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "unknown job" in data["error"]


async def test_toggle_unknown_job_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/scheduler/nonexistent_job/toggle", params={"enabled": "true"}
    )
    assert resp.status_code == 404


async def test_update_cron_invalid_400(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/scheduler/nonexistent_job/cron", json={"cron": "not-a-cron"}
    )
    # cron 非法先返 400（校验早于作业存在性检查）
    assert resp.status_code == 400


async def test_update_cron_is_persisted(client: httpx.AsyncClient) -> None:
    """改 cron → **落库**（跨重启保持）。

    测试里调度中心没启动（``scheduler_enabled=False``），但
    ``scheduler_center.register()`` 仍会填 ``_specs``——故
    ``update_cron`` 对**已注册**的作业有效（会重排 APScheduler 槽，
    没启动时该步是 no-op）。
    """
    from app.db.models import SchedulerJobSetting
    from app.services.scheduler import JobSpec, scheduler_center
    from sqlalchemy import select

    async def _noop() -> dict[str, object]:
        return {}

    scheduler_center.register(
        JobSpec(name="test_job_cron", func=_noop, cron="*/10 * * * *")
    )
    try:
        resp = await client.post(
            "/api/v1/scheduler/test_job_cron/cron", json={"cron": "15,45 * * * *"}
        )
        assert resp.status_code == 200
        assert resp.json()["cron"] == "15,45 * * * *"

        # 落库了
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SchedulerJobSetting).where(
                        SchedulerJobSetting.job_name == "test_job_cron"
                    )
                )
            ).scalar_one()
            assert row.cron == "15,45 * * * *"

        # 内存也生效了
        jobs = {j["name"]: j for j in scheduler_center.list_jobs()}
        assert jobs["test_job_cron"]["cron"] == "15,45 * * * *"
    finally:
        scheduler_center._specs.pop("test_job_cron", None)


async def test_job_config_returns_persisted_cron(
    client: httpx.AsyncClient,
) -> None:
    """配置端点返回**当前生效**的 cron（DB 覆盖值优先于代码默认值）。"""
    from app.services.scheduler import JobSpec, scheduler_center

    async def _noop() -> dict[str, object]:
        return {}

    scheduler_center.register(
        JobSpec(name="test_job_cfg", func=_noop, cron="*/10 * * * *")
    )
    try:
        # 先改 cron（落库 + 内存生效）
        resp = await client.post(
            "/api/v1/scheduler/test_job_cfg/cron", json={"cron": "15,45 * * * *"}
        )
        assert resp.status_code == 200

        # 配置端点返回的就是生效值
        resp = await client.get("/api/v1/scheduler/test_job_cfg/config")
        assert resp.status_code == 200
        assert resp.json()["cron"] == "15,45 * * * *"
    finally:
        scheduler_center._specs.pop("test_job_cfg", None)


async def test_runtime_params_view(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.status_code == 200
    data = resp.json()
    assert "params" in data
    assert "specs" in data


async def test_runtime_params_exposes_enum_options(
    client: httpx.AsyncClient,
) -> None:
    """枚举参数的 options 要透出到 API（前端靠它渲染单选按钮组）。

    - 枚举参数 → options 非 null，且是 [[值, 显示名], ...]
    - 连续数值参数 → options 为 null（前端渲染数字输入框）
    """
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.status_code == 200
    specs = {s["key"]: s for s in resp.json()["specs"]}

    # 枚举参数
    enum_spec = specs["monitor_delete_expired"]
    assert enum_spec["options"] == [[0, "仅暂停"], [1, "删除任务+删数据"]]
    # 显示名非空（空名 radio 渲染出来用户看不懂）
    assert all(text.strip() for _, text in enum_spec["options"])

    # 连续数值参数没有 options
    assert specs["push_min_remaining_hours"]["options"] is None
    assert specs["monitor_pre_pause_minutes"]["options"] is None


async def test_runtime_params_monitor_delete_expired_roundtrip(
    client: httpx.AsyncClient,
) -> None:
    """monitor_delete_expired 参数：归属正确 → 可改 → 持久化 → 越界被拒。

    这个参数控制"命中条件时删除种子（含磁盘数据）"——不可逆操作，
    故必须能通过 API 改，且值域被限制在 0/1（1=开删除，0=仅暂停）。
    """
    # 1) 归属 monitor_free_window 作业（前端按 job 渲染配置弹窗）
    from app.services.runtime_params import specs_for_job

    keys = [s.key for s in specs_for_job("monitor_free_window")]
    assert "monitor_delete_expired" in keys

    # 2) 默认值是 0（仅暂停）
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.status_code == 200
    data = resp.json()
    params = data["params"]
    assert params["monitor_delete_expired"] == 0

    # 3) spec 带不可逆风险提示（前端渲染成警告行）
    spec = next(s for s in data["specs"] if s["key"] == "monitor_delete_expired")
    assert spec["note"], "删除模式必须提示不可恢复"

    # 4) 可改为 1 并持久化
    resp = await client.put(
        "/api/v1/scheduler/runtime-params", json={"monitor_delete_expired": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["params"]["monitor_delete_expired"] == 1

    # 5) 越界值被拒（400）
    resp = await client.put(
        "/api/v1/scheduler/runtime-params", json={"monitor_delete_expired": 2}
    )
    assert resp.status_code == 400

    # 6) 改回 0，避免污染后续用例
    resp = await client.put(
        "/api/v1/scheduler/runtime-params", json={"monitor_delete_expired": 0}
    )
    assert resp.status_code == 200


async def test_job_runs_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/scheduler/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


# ------------------------------------------------------------------ 手动推送

async def test_push_missing_index_404(client: httpx.AsyncClient) -> None:
    """手推不存在的索引 → 404。"""
    resp = await client.post("/api/v1/torrent-index/999999/push", json={})
    assert resp.status_code == 404


async def test_push_without_default_downloader_400(
    client: httpx.AsyncClient,
) -> None:
    """未配置默认下载器 → 400。"""
    from app.db.models import Site, TorrentIndex

    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="hdsky"))
        session.add(
            TorrentIndex(site_id=1, item_id="i1", name="t", free_type="freeleech_permanent")
        )
        await session.flush()
        index_id = (
            await session.execute(
                select(TorrentIndex.id).where(TorrentIndex.item_id == "i1")
            )
        ).scalar_one()

    resp = await client.post(f"/api/v1/torrent-index/{index_id}/push", json={})
    assert resp.status_code == 400
    assert "未配置默认下载器" in resp.json()["detail"]
