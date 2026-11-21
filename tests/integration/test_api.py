"""API 集成测试（Phase 0）。

覆盖认证 + 端点契约。FastAPI 用 ``httpx.ASGITransport`` 直接打
应用（不启真服务器），``lifespan`` 手动跑——这依赖 Phase 0 修好的
settings 注入通道（否则会用 ``/app/data`` 的真 DB）。

注意：``scheduler_enabled=False`` → lifespan 不会启动调度中心，
不会真的去拉站点。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
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
        s = Site(name=name, domain=f"{name}.test", adapter="generic", **kw)  # type: ignore[arg-type]
        session.add(s)
        await session.flush()
        return s.id


# ------------------------------------------------------------------ 认证

async def test_login_success_sets_cookie(client: httpx.AsyncClient) -> None:
    assert "medialab_session" in client.cookies


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
    # 运行时守卫字段（2026-11-06 prod 事故：挂载卡死曾把面板拖假死）
    assert "loop" in data
    assert "loop_stalled_ms" in data["loop"]
    assert "loop_ok" in data["loop"]
    assert isinstance(data["mounts"], list)


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
    assert data[0]["adapter"] == "generic"


async def test_create_site(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "newsite", "adapter": "generic", "domain": "x.test"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "newsite"


async def test_create_site_duplicate_name_409(client: httpx.AsyncClient) -> None:
    await _add_site("dup")
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "dup", "adapter": "generic", "domain": "y.test"},
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
    for i, adapter in enumerate(("generic",)):
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
        json={"id": site_id, "adapter": "generic"},
    )
    assert resp.status_code == 200
    assert resp.json()["adapter"] == "generic"


async def test_create_site_invalid_name_422(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/sites",
        json={"name": "bad name!", "adapter": "generic"},
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
    assert names == {"generic"}


async def test_list_site_rules(client: httpx.AsyncClient) -> None:
    """规则包目录：返回全部站点，字段够前端加站用。"""
    resp = await client.get("/api/v1/sites/rules")
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) >= 180
    by_id = {r["id"]: r for r in rules}
    assert by_id["0ff"]["name"] == "自由农场"
    assert by_id["0ff"]["domain"].startswith("https://")
    assert by_id["0ff"]["userdata_schema"] == "NexusPhp"
    assert all(
        {"id", "name", "domain", "userdata_schema", "media_type", "has_subtitles"} <= set(r)
        for r in rules
    )


# ------------------------------------------------------------------ 用户数据


async def test_list_sites_without_user_data(client: httpx.AsyncClient) -> None:
    """站点列表带用户数据字段；没数据行时是默认值（has_data=false）。"""
    site_id = await _add_site("s1")
    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    row = next(s for s in resp.json() if s["id"] == site_id)
    assert row["has_data"] is False
    assert row["upload"] == 0
    assert row["seeding_count"] == 0
    assert row["user_data_seen_at"] is None


async def test_list_sites_with_user_data(client: httpx.AsyncClient) -> None:
    """站点列表带出 ``site_user_data`` 快照。"""
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            SiteUserData(
                site_id=site_id,
                upload=1024**3,
                download=2 * 1024**3,
                seeding_count=120,
                last_seen_at="2026-10-30 12:00:00",
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    row = next(s for s in resp.json() if s["id"] == site_id)
    assert row["has_data"] is True
    assert row["upload"] == 1024**3
    assert row["download"] == 2 * 1024**3
    assert row["seeding_count"] == 120
    assert row["user_data_seen_at"] == "2026-10-30 12:00:00"


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


async def test_cross_seed_remove_torrents_rejects_bad_hash(
    client: httpx.AsyncClient,
) -> None:
    """非法 hash → 400（拒绝 all / 通配，不误删全部任务）。"""
    dl_id = await _add_cross_seed_downloader("dl1")
    resp = await client.post(
        f"/api/v1/cross-seed-monitor/downloaders/{dl_id}/remove-torrents",
        json={"hashes": ["all"], "delete_data": False},
    )
    assert resp.status_code == 400


async def test_cross_seed_remove_torrents_requires_hashes(
    client: httpx.AsyncClient,
) -> None:
    """空 hash 列表 → 422（pydantic 校验）。"""
    dl_id = await _add_cross_seed_downloader("dl1")
    resp = await client.post(
        f"/api/v1/cross-seed-monitor/downloaders/{dl_id}/remove-torrents",
        json={"hashes": [], "delete_data": False},
    )
    assert resp.status_code == 422


async def test_cross_seed_remove_torrents_unknown_downloader_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/cross-seed-monitor/downloaders/9999/remove-torrents",
        json={"hashes": ["a" * 40], "delete_data": False},
    )
    assert resp.status_code == 404


async def test_cross_seed_snapshot_stats_carry_full_hashes(
    client: httpx.AsyncClient,
) -> None:
    """快照的分组统计带**完整 40 位** hash 列表（统计卡片删除要用）。

    ``sample_hash`` 是 12 位截断的——拿它删除会被适配器拒绝。
    """
    from app.db.models import CrossSeedSnapshot

    dl_id = await _add_cross_seed_downloader("dl1")
    async with session_scope() as session:
        session.add(
            CrossSeedSnapshot(
                downloader_id=dl_id,
                downloader_name="dl1",
                total_torrents=3,
                unique_count=1,
                stats=[
                    {
                        "name": "Group",
                        "size_gb": 1.0,
                        "count": 2,
                        "sample_hash": "a" * 12,
                        "hashes": ["a" * 40, "b" * 40],
                    }
                ],
            )
        )
        await session.flush()

    resp = await client.get(f"/api/v1/cross-seed-monitor/downloaders/{dl_id}/snapshot")
    assert resp.status_code == 200
    row = resp.json()["stats"][0]
    assert row["hashes"] == ["a" * 40, "b" * 40]
    assert all(len(h) == 40 for h in row["hashes"])
    assert len(row["sample_hash"]) == 12


async def test_cross_seed_space_empty(client: httpx.AsyncClient) -> None:
    """没有快照 → 空汇总（磁盘为 null，不是 0）。"""
    resp = await client.get("/api/v1/cross-seed-monitor/space")
    assert resp.status_code == 200
    data = resp.json()
    assert data["downloaders"] == []
    assert data["summary"]["downloaders"] == 0
    assert data["summary"]["disk_free_bytes"] is None


async def test_cross_seed_orphans_empty(client: httpx.AsyncClient) -> None:
    """没扫过 → 空汇总。"""
    resp = await client.get("/api/v1/cross-seed-monitor/orphans")
    assert resp.status_code == 200
    data = resp.json()
    assert data["downloaders"] == []
    assert data["summary"]["orphan_count"] == 0


async def test_cross_seed_snapshot_returns_space_fields(
    client: httpx.AsyncClient,
) -> None:
    """端点**实际**返回空间字段（不只是 DTO 声明了）。

    回归测试：2026-10-25 加空间统计时漏同步了 ``SnapshotHeaderOut``，
    pydantic 静默丢弃 → 端点 200 但响应里没有这些字段（本地 dev 没数据，
    只有 prod 暴露）。见 test_cross_seed_dto.py。
    """
    from app.db.models import CrossSeedSnapshot

    dl_id = await _add_cross_seed_downloader("dl1")
    async with session_scope() as session:
        session.add(
            CrossSeedSnapshot(
                downloader_id=dl_id,
                downloader_name="dl1",
                scanned_at="2026-10-25 12:00:00",
                total_torrents=10,
                unique_count=5,
                used_bytes=12345,
                total_size_bytes=23456,
                disk_free_bytes=34567,
                disk_total_bytes=45678,
            )
        )
        await session.flush()

    # 详情端点
    resp = await client.get(f"/api/v1/cross-seed-monitor/downloaders/{dl_id}/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["used_bytes"] == 12345
    assert data["total_size_bytes"] == 23456
    assert data["disk_free_bytes"] == 34567
    assert data["disk_total_bytes"] == 45678

    # 列表端点
    resp = await client.get("/api/v1/cross-seed-monitor/snapshots")
    assert resp.status_code == 200
    row = resp.json()["snapshots"][0]
    assert row["used_bytes"] == 12345
    assert row["disk_free_bytes"] == 34567
    assert row["disk_total_bytes"] == 45678


async def test_cross_seed_orphans_with_row(client: httpx.AsyncClient) -> None:
    """孤儿统计：含数量、字节与明细。"""
    from app.db.models import CrossSeedOrphan

    dl_id = await _add_cross_seed_downloader("dl1")
    async with session_scope() as session:
        session.add(
            CrossSeedOrphan(
                downloader_id=dl_id,
                downloader_name="dl1",
                scanned_at="2026-10-25 04:07:00",
                orphan_count=2,
                orphan_bytes=527 * 1024**3,
                total_scan_bytes=21 * 1024**4,
                total_scan_count=1669,
                orphans=[
                    {
                        "name": "orphan-a",
                        "path": "/mnt/cross-seed-data/辅种/orphan-a",
                        "bytes": 100,
                        "file_count": 3,
                    }
                ],
            )
        )
        await session.flush()

    resp = await client.get("/api/v1/cross-seed-monitor/orphans")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["downloaders"]) == 1
    row = data["downloaders"][0]
    assert row["orphan_count"] == 2
    assert row["orphan_bytes"] == 527 * 1024**3
    assert row["total_scan_count"] == 1669
    assert row["error"] is None
    assert len(row["orphans"]) == 1
    assert data["summary"]["orphan_count"] == 2


async def test_cross_seed_orphans_delete(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除孤儿文件端点：删掉扫描目录下的孤儿，越界的路径被跳过。"""
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    scan_root = tmp_path / "aux"
    scan_root.mkdir()
    (scan_root / "orphan").mkdir()
    (scan_root / "orphan" / "a.mkv").write_bytes(b"\0" * 100)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.mkv").write_bytes(b"\0" * 200)

    set_env_settings(
        EnvSettingsManager({"cross_seed_scan_dirs": str(scan_root)})
    )

    resp = await client.post(
        "/api/v1/cross-seed-monitor/orphans/delete",
        json={
            "paths": [
                str(scan_root / "orphan"),
                str(outside / "keep.mkv"),
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["succeeded"] == 1
    assert data["skipped"] == 1
    assert not (scan_root / "orphan").exists()
    assert (outside / "keep.mkv").exists(), "越界路径不该被删"


async def test_cross_seed_orphans_delete_requires_paths(
    client: httpx.AsyncClient,
) -> None:
    """删除端点：paths 为空 → 422。"""
    resp = await client.post(
        "/api/v1/cross-seed-monitor/orphans/delete",
        json={"paths": []},
    )
    assert resp.status_code == 422


async def test_cross_seed_orphans_delete_without_scan_dirs(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置扫描目录 → 400（不删任何东西）。"""
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    set_env_settings(EnvSettingsManager({"cross_seed_scan_dirs": ""}))
    resp = await client.post(
        "/api/v1/cross-seed-monitor/orphans/delete",
        json={"paths": ["/tmp/whatever"]},
    )
    assert resp.status_code == 400



async def test_cross_seed_orphans_scan(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """手动扫描孤儿：返回新鲜统计 + 落库。

    孤儿扫描默认**每天一次**（cron 7 4 * * *）——当天发现孤儿想现在清理，
    等不到明天，故提供「只扫不删」的手动入口。
    """
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    scan_root = tmp_path / "aux"
    scan_root.mkdir()
    (scan_root / "orphan").mkdir()
    (scan_root / "orphan" / "a.mkv").write_bytes(b"\0" * 100)

    set_env_settings(
        EnvSettingsManager({"cross_seed_scan_dirs": str(scan_root)})
    )

    # 假下载器：没有任务 → 磁盘上的条目就是孤儿
    class _FakeAdapter:
        async def list_torrent_file_names(self) -> list:
            return []

        async def close(self) -> None:
            return None

    import app.services.factory as factory_mod
    from app.db.models import DownloaderInstance
    from app.db.session import session_scope
    from app.services.factory import AdapterFactory

    async with session_scope() as session:
        row = DownloaderInstance(
            name="dl-scan",
            type="transmission",
            url="http://192.168.0.<nas>:9091",
            is_cross_seed=True,
            enabled=True,
        )
        session.add(row)
        await session.flush()
        dl_id = row.id

    factory = AdapterFactory()
    factory.downloader = lambda row, **kw: _FakeAdapter()  # type: ignore[method-assign]
    prev = factory_mod._factory
    factory_mod._factory = factory
    try:
        resp = await client.post("/api/v1/cross-seed-monitor/orphans/scan")
    finally:
        factory_mod._factory = prev

    assert resp.status_code == 200
    data = resp.json()
    assert data["orphan_count"] == 1
    assert data["orphan_bytes"] == 100
    # 响应里带上有孤儿表的新鲜统计
    assert "orphans" in data
    assert data["orphans"]["summary"]["orphan_count"] == 1
    # 落库了
    from app.db.models import CrossSeedOrphan

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(CrossSeedOrphan).where(
                        CrossSeedOrphan.downloader_id == dl_id
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].orphan_count == 1



async def test_cross_seed_orphans_delete_rescans(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除后**自动重扫**——响应里带最新的孤儿统计。

    孤儿统计表是每天一次的作业快照，不重扫页面会一直显示旧数字
    （已删的还在列表里、且看不到新产生的孤儿）。
    """
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    scan_root = tmp_path / "aux"
    scan_root.mkdir()
    (scan_root / "orphan").mkdir()
    (scan_root / "orphan" / "a.mkv").write_bytes(b"\0" * 100)

    set_env_settings(
        EnvSettingsManager({"cross_seed_scan_dirs": str(scan_root)})
    )

    rescanned = {"n": 0}

    async def _fake_scan() -> dict[str, object]:
        rescanned["n"] += 1
        return {"orphan_count": 0, "orphan_bytes": 0, "summary": "ok"}

    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans", _fake_scan
    )

    resp = await client.post(
        "/api/v1/cross-seed-monitor/orphans/delete",
        json={"paths": [str(scan_root / "orphan")]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["succeeded"] == 1
    assert rescanned["n"] == 1, "删除后必须重扫一次"
    # 响应里带上有孤儿表的新鲜统计
    assert "orphans" in data
    assert "summary" in data["orphans"]
    assert data["rescan_error"] is None


async def test_cross_seed_orphans_delete_rescan_failure_is_reported(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重扫失败**不影响删除结果**——删除成功，但记 ``rescan_error``。"""
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    scan_root = tmp_path / "aux"
    scan_root.mkdir()
    (scan_root / "orphan").mkdir()

    set_env_settings(
        EnvSettingsManager({"cross_seed_scan_dirs": str(scan_root)})
    )

    async def _boom() -> dict[str, object]:
        raise RuntimeError("扫描炸了")

    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans", _boom
    )

    resp = await client.post(
        "/api/v1/cross-seed-monitor/orphans/delete",
        json={"paths": [str(scan_root / "orphan")]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["succeeded"] == 1, "删除本身成功了"
    assert data["rescan_error"] is not None
    assert "扫描炸了" in data["rescan_error"]



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


async def test_scheduler_jobs_carry_title_and_description(
    client: httpx.AsyncClient,
) -> None:
    """每个作业都要带 title + description（前端直接展示）。

    前端曾硬编码 job → 中文名映射表，每加一个作业就要手改前端，漏一处
    就在调度页显示原始 job 名（如 ``scan_orphans``）和 "—" 描述。
    改为后端下发后，这条契约保证新增作业**自动**有中文名。

    注：测试环境的 ``scheduler_enabled=False`` → lifespan 不注册作业，
    故这里显式注册一遍（走真实的 :func:`register_jobs` 路径）。
    """
    from app.services.factory import AdapterFactory
    from app.services.job_registry import register_jobs
    from app.services.scheduler import scheduler_center

    register_jobs(AdapterFactory())
    try:
        resp = await client.get("/api/v1/scheduler")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert jobs, "应至少注册了一个作业"
        for job in jobs:
            assert job["title"].strip(), f"{job['name']} 缺中文名"
            assert job["description"].strip(), f"{job['name']} 缺描述"
            # 业务模块（「定时任务」页按它分组）——空串会落进「其它」块
            assert job["module"].strip(), f"{job['name']} 缺 module"
            assert job["module_title"].strip(), f"{job['name']} 缺 module_title"
            # 数据流依赖：没依赖时必须是 None（不是缺字段）——前端据此画链路
            assert "depends_on" in job, f"{job['name']} 缺 depends_on 字段"
            assert "min_trigger_interval_sec" in job, (
                f"{job['name']} 缺 min_trigger_interval_sec 字段"
            )

        # modules 与 jobs[].module 必须自洽（前端按 modules 的顺序分组）
        body = resp.json()
        modules = body["modules"]
        assert modules, "modules 不能为空"
        known = {m["key"] for m in modules}
        for job in jobs:
            assert job["module"] in known, (
                f"{job['name']} 的 module {job['module']!r} 不在 modules 里"
            )
        for m in modules:
            assert m["title"].strip()
            assert m["job_count"] == sum(
                1 for j in jobs if j["module"] == m["key"]
            ), f"{m['key']} 的 job_count 与 jobs 不一致"

        # 两个业务域的作业都在（分开展示的前提）
        assert {"seed", "media"} <= known

        # 免费种推送与监测已合并为一个作业（2026-11-22），且**不挂依赖边**：
        # cron 心跳（10 分钟）比推送门槛（30 分钟）还细，挂边是永不生效的死边
        by_name = {j["name"]: j for j in jobs}
        assert "push_and_monitor_free" in by_name
        assert "push_free_torrents" not in by_name
        assert "monitor_free_window" not in by_name
        assert [n for n, j in by_name.items() if j["depends_on"]] == []

        # 新增的作业也不能漏（回归点）
        titles = {j["name"]: j["title"] for j in jobs}
        for name in ("cross_seed_maintenance",):
            assert name in titles, f"{name} 未注册"
            assert titles[name].strip(), f"{name} 缺中文名"
    finally:
        # 清掉注册（避免污染其它用例的 scheduler_center 状态）
        scheduler_center._specs.clear()


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


async def test_runtime_params_aggregate_max_submissions_roundtrip(
    client: httpx.AsyncClient,
) -> None:
    """aggregate_max_submissions：归属正确 → 可改 → 持久化 → 越界被拒。

    0 = 不限（与仓库「0 = 不限」的惯例一致）。
    """
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.status_code == 200
    specs = {s["key"]: s for s in resp.json()["specs"]}
    spec = specs["aggregate_max_submissions"]
    assert spec["default"] == 20
    assert spec["min"] == 0 and spec["max"] == 200
    assert spec["options"] is None
    assert spec["note"] and "0 = 不限" in spec["note"]
    # 归属 aggregate_sync 由 unit 测试断言（spec.job）——这里不查
    # /scheduler/{job}/config：测试环境没跑 register_jobs，作业没注册 → 404

    # 改成 0（不限）→ 往返一致
    resp = await client.put(
        "/api/v1/scheduler/runtime-params",
        json={"aggregate_max_submissions": 0},
    )
    assert resp.status_code == 200
    assert resp.json()["params"]["aggregate_max_submissions"] == 0
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.json()["params"]["aggregate_max_submissions"] == 0

    # 越界被拒（> max）
    resp = await client.put(
        "/api/v1/scheduler/runtime-params",
        json={"aggregate_max_submissions": 9999},
    )
    assert resp.status_code == 400

    # 还原默认，避免影响后续用例
    resp = await client.put(
        "/api/v1/scheduler/runtime-params",
        json={"aggregate_max_submissions": 20},
    )
    assert resp.status_code == 200


async def test_runtime_params_monitor_delete_expired_roundtrip(
    client: httpx.AsyncClient,
) -> None:
    """monitor_delete_expired 参数：归属正确 → 可改 → 持久化 → 越界被拒。

    这个参数控制"命中条件时删除种子（含磁盘数据）"——不可逆操作，
    故必须能通过 API 改，且值域被限制在 0/1（1=开删除，0=仅暂停）。
    """
    # 1) 归属免费种合并作业（前端按 job 渲染配置弹窗）
    from app.services.runtime_params import specs_for_job

    keys = [s.key for s in specs_for_job("push_and_monitor_free")]
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


async def test_disk_rotation_params_roundtrip(client: httpx.AsyncClient) -> None:
    """辅种维护作业的运行参数：归属 + 默认值 + 可改 + 越界被拒.

    钉住契约（AGENTS.md「枚举型运行参数用单选按钮组」）：
    - ``disk_rotation_threshold_pct`` 连续数值参数（50-99，默认 90）
    - ``disk_rotation_action`` 枚举参数（0=仅删任务 / 1=删任务+删数据，默认 1）

    注：参数 key 仍叫 ``disk_rotation_*``（历史命名），但归属作业是
    合并后的 ``cross_seed_maintenance``（2026-11-01）。
    """
    from app.services.runtime_params import specs_for_job

    # 1) 归属 cross_seed_maintenance 作业
    keys = [s.key for s in specs_for_job("cross_seed_maintenance")]
    assert "disk_rotation_threshold_pct" in keys
    assert "disk_rotation_action" in keys

    # 2) 默认值（threshold=90，action=1=删数据）
    resp = await client.get("/api/v1/scheduler/runtime-params")
    assert resp.status_code == 200
    params = resp.json()["params"]
    assert params["disk_rotation_threshold_pct"] == 90
    assert params["disk_rotation_action"] == 1

    # 3) 枚举参数透出 options（前端渲染单选按钮组）
    specs = {s["key"]: s for s in resp.json()["specs"]}
    action_spec = specs["disk_rotation_action"]
    assert action_spec["options"] == [[0, "仅删任务"], [1, "删任务+删数据"]]
    assert action_spec["note"], "删数据模式必须提示不可恢复"

    # 连续数值参数不带 options（前端渲染数字输入框）
    assert specs["disk_rotation_threshold_pct"]["options"] is None

    # 4) 改 threshold 并持久化
    resp = await client.put(
        "/api/v1/scheduler/runtime-params",
        json={"disk_rotation_threshold_pct": 80},
    )
    assert resp.status_code == 200
    assert resp.json()["params"]["disk_rotation_threshold_pct"] == 80

    # 5) 越界值被拒（400）
    resp = await client.put(
        "/api/v1/scheduler/runtime-params",
        json={"disk_rotation_threshold_pct": 40},  # < 50
    )
    assert resp.status_code == 400

    # 6) 改回 90，避免污染后续用例
    resp = await client.put(
        "/api/v1/scheduler/runtime-params",
        json={"disk_rotation_threshold_pct": 90},
    )
    assert resp.status_code == 200


# ------------------------------------------------------------------ 手动推送

async def test_push_missing_index_404(client: httpx.AsyncClient) -> None:
    """手推不存在的索引 → 404。"""
    resp = await client.post("/api/v1/torrent-index/999999/push", json={})
    assert resp.status_code == 404


async def test_test_all_summary_counts_unauthorized(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「可达但未登录」的站单独计数——凭据失效不能混进「可达」。

    2026-11-08 prod：ourbits 凭据过期 10 天，面板测连一直显示绿点，
    「全部测连」也只报「可达」——故障彻底不可见。
    """
    from app.api.routes import sites as sites_route
    from app.services.site_service import SiteTestResult

    async def _fake() -> list[SiteTestResult]:
        return [
            SiteTestResult(site_id=1, name="a", reachable=True, logged_in=True, detail="ok"),
            SiteTestResult(
                site_id=2,
                name="b",
                reachable=True,
                logged_in=False,
                detail="cookie 已过期，需重新获取",
            ),
            SiteTestResult(site_id=3, name="c", reachable=False, logged_in=False, detail="boom"),
        ]

    monkeypatch.setattr(sites_route.site_service, "test_all_sites", _fake)

    resp = await client.post("/api/v1/sites/test-all")
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["total"] == 3
    assert summary["reachable"] == 2
    assert summary["unauthorized"] == 1
    assert summary["failed"] == 1


async def test_push_without_default_downloader_400(
    client: httpx.AsyncClient,
) -> None:
    """未配置默认下载器 → 400。"""
    from app.db.models import Site, TorrentIndex

    async with session_scope() as session:
        session.add(Site(name="s1", domain="s1.test", adapter="generic"))
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


# ============================================================ 环境变量配置


async def test_env_settings_get_returns_specs_and_values(
    client: httpx.AsyncClient,
) -> None:
    """GET /env-settings 返回 specs + 当前值（首次启动用 env 默认值回填）。"""
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    set_env_settings(
        EnvSettingsManager({"cross_seed_scan_dirs": "/foo,/bar"})
    )
    try:
        resp = await client.get("/api/v1/env-settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        items = data["items"]
        assert len(items) >= 1
        item = next(
            (i for i in items if i["key"] == "cross_seed_scan_dirs"),
            None,
        )
        assert item is not None
        assert item["label"]
        assert item["note"]
        assert item["placeholder"]
        assert item["max_length"] > 0
        assert item["value"] == "/foo,/bar"
    finally:
        set_env_settings(None)


async def test_env_settings_put_persists_and_updates_memory(
    client: httpx.AsyncClient,
) -> None:
    """PUT /env-settings：DB 先 / 内存后，下次 GET 立即看到新值。"""
    from app.db.models import AppEnvSetting
    from app.db.session import session_scope
    from app.services.env_settings import EnvSettingsManager, set_env_settings
    from sqlalchemy import select

    set_env_settings(EnvSettingsManager({"cross_seed_scan_dirs": "/old"}))
    try:
        resp = await client.put(
            "/api/v1/env-settings",
            json={"cross_seed_scan_dirs": "/new,/dirs"},
        )
        assert resp.status_code == 200
        data = resp.json()
        item = next(
            i for i in data["items"] if i["key"] == "cross_seed_scan_dirs"
        )
        assert item["value"] == "/new,/dirs"

        # DB 已写入
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(AppEnvSetting).where(
                        AppEnvSetting.key == "cross_seed_scan_dirs"
                    )
                )
            ).scalar_one()
            assert row.value == "/new,/dirs"

        # 内存已更新（业务读 get_env_setting 拿到新值）
        from app.services.env_settings import get_env_settings

        assert (
            get_env_settings().get("cross_seed_scan_dirs") == "/new,/dirs"
        )
    finally:
        set_env_settings(None)


async def test_env_settings_put_empty_body_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """PUT /env-settings 不带任何字段 → 400。"""
    resp = await client.put("/api/v1/env-settings", json={})
    assert resp.status_code == 400


async def test_env_settings_put_too_long_value_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """超长值 → 400（拒塞爆 DB）。"""
    from app.services.env_settings import lookup_spec

    spec = lookup_spec("cross_seed_scan_dirs")
    assert spec is not None
    resp = await client.put(
        "/api/v1/env-settings",
        json={"cross_seed_scan_dirs": "x" * (spec.max_length + 1)},
    )
    assert resp.status_code == 400


async def test_env_settings_requires_session(app_settings: AppSettings) -> None:
    """未登录访问 → 401。"""
    app = create_app(app_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/api/v1/env-settings")
    assert resp.status_code == 401


async def test_missing_signing_secret_blocks_startup(
    app_settings: AppSettings, settings_copy: AppSettings
) -> None:
    """SIGNING_SECRET 为空必须 fail-fast，不能带病启动。"""
    settings_copy.signing_secret = None
    app = create_app(settings_copy)
    with pytest.raises(RuntimeError, match="SIGNING_SECRET"):
        async with app.router.lifespan_context(app):
            pass


async def test_metrics_requires_session(client: httpx.AsyncClient) -> None:
    """/metrics 与其他管理端点同样要求登录。"""
    client.cookies.clear()
    resp = await client.get("/metrics")
    assert resp.status_code == 401


async def test_metrics_with_session(client: httpx.AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200


async def test_readyz_reports_startup_degradations(client: httpx.AsyncClient) -> None:
    """启动降级必须在 /readyz 可见，但 HTTP 仍 200（不触发容器重启）。"""
    from app.services.startup_status import record, reset

    reset()
    record("watch_manager_start:unit", "监听目录不存在")
    try:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["degradations"]["watch_manager_start:unit"]["reason"] == (
            "监听目录不存在"
        )
    finally:
        reset()


async def test_readyz_ok_when_no_degradation(client: httpx.AsyncClient) -> None:
    """无降级且 DB 正常时 /readyz 应为 ok，且带 degradations 字段。"""
    from app.services.startup_status import reset

    reset()
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["degradations"] == {}


async def test_scheduler_status_with_enabled_setting(
    client: httpx.AsyncClient,
    app_settings: AppSettings,
    settings_copy: AppSettings,
) -> None:
    """生产默认 scheduler_enabled=True 时状态端点必须反映开启态。

    这里只验证配置分支的端点契约，不真跑 APScheduler——测试环境跑 lifespan
    会启动真实调度器并注册作业，可能在测试期间触发外部网络请求。
    """
    from app.core.config import set_settings

    enabled = settings_copy.model_copy(update={"scheduler_enabled": True})
    set_settings(enabled)
    try:
        resp = await client.get("/api/v1/scheduler")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
    finally:
        set_settings(app_settings)
