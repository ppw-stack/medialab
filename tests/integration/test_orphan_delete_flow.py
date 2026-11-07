"""孤儿删除链路的两个产品问题修复（2026-11-08 prod 实测暴露）。

1. **下载器拉取无兜底超时**：TR 适配器的 ``BULK_TIMEOUT = 180s``——
   下载器假死时（进程在、端口 LISTEN、accept 后不响应）孤儿扫描/删除
   端点会挂 3 分钟无响应。修复：``collect_owned_names`` 给每个下载器加
   兜底超时，超时按「拉取失败」处理并**点名**（判定不完整）。

2. **删除后的自动重扫拖死 HTTP 响应**：重扫要遍历整个辅种目录
   （NFS 上实测 175s）→ 删除请求必然超时，用户以为删除失败，**其实文件
   已经删了**（不可逆，用户会再点一次）。修复：重扫最多等 15s，超时
   转后台继续，响应立即返回并带 ``rescan_pending`` 标记。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.core.config import AppSettings
from app.db.models import DownloaderInstance
from app.db.session import session_scope
from app.main import create_app
from app.services import cross_seed_orphan
from app.services.cross_seed_orphan import collect_owned_names


@pytest_asyncio.fixture
async def client(
    app_settings: AppSettings, db: None
) -> AsyncIterator[httpx.AsyncClient]:
    """已登录的 API 客户端（走 lifespan 初始化）。"""
    from app.db.session import dispose_db

    app = create_app(app_settings)
    await dispose_db()

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


class _SlowDownloader:
    """模拟假死的下载器：``list_torrent_file_names`` 永远不返回。"""

    async def list_torrent_file_names(self) -> list[Any]:
        await asyncio.sleep(3600)
        return []  # pragma: no cover - 不会走到


class _FakeFactory:
    def __init__(self, downloader: object) -> None:
        self._downloader = downloader

    def downloader(self, row: object) -> object:
        return self._downloader


async def _add_downloader(name: str = "tr") -> None:
    async with session_scope() as session:
        session.add(
            DownloaderInstance(
                name=name,
                type="transmission",
                url="http://tr.test:9091",
                is_default=True,
                enabled=True,
            )
        )


async def test_collect_owned_names_times_out_slow_downloader(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """假死下载器 → 兜底超时后**点名**（判定不完整），不无限挂起。

    2026-11-08 prod：172 的 TR 进程在、端口 LISTEN，但 accept 后不响应
    ——适配器自身的 180s 超时没到之前，孤儿端点就一直无响应。
    """
    await _add_downloader()
    monkeypatch.setattr(
        cross_seed_orphan, "get_factory", lambda: _FakeFactory(_SlowDownloader())
    )
    monkeypatch.setattr(cross_seed_orphan, "_DOWNLOADER_PULL_TIMEOUT_SEC", 0.05)

    owned, by_downloader, failed = await collect_owned_names()

    assert owned == []
    assert by_downloader == {}
    assert failed == ["tr"], "假死的下载器必须被点名（判定不完整）"


class _FakeResult:
    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": 1,
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "freed_bytes": 0,
            "items": [],
            "error": None,
            "permission_denied": 0,
        }


async def _empty_owned() -> tuple[list[Any], dict[int, list[Any]], list[str]]:
    return [], {}, []


async def _slow_rescan() -> dict[str, Any]:
    await asyncio.sleep(3600)
    return {}  # pragma: no cover - 不会走到


async def test_delete_orphan_returns_before_rescan_finishes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除响应**不等**重扫（超过上限就转后台），带 ``rescan_pending``。

    否则用户看到超时以为删除失败，而文件其实已经删了（不可逆）。
    """
    from app.api.routes import cross_seed as cross_seed_route

    # 端点要求已配置扫描目录（否则 400）
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    set_env_settings(
        EnvSettingsManager({"cross_seed_scan_dirs": "/share/Public/home/downloads/辅种"})
    )
    try:
        monkeypatch.setattr(cross_seed_route, "_RESCAN_WAIT_SEC", 0.05)
        # 端点内部是**局部 import**（``from app.services.cross_seed_orphan import
        # run_scan_orphans``），故要打在 service 模块上
        monkeypatch.setattr(
            "app.services.cross_seed_orphan.run_scan_orphans", _slow_rescan
        )
        # 删除动作本身：直接返回成功（不真的碰磁盘）
        monkeypatch.setattr(
            "app.services.orphan_deleter.delete_orphans",
            lambda paths, owned, scan_dirs: _FakeResult(),
        )
        monkeypatch.setattr(
            "app.services.cross_seed_orphan.collect_owned_names", _empty_owned
        )

        resp = await client.post(
            "/api/v1/cross-seed-monitor/orphans/delete",
            json={"paths": ["/share/Public/home/downloads/辅种/x"]},
        )
    finally:
        set_env_settings(None)  # type: ignore[arg-type]

    assert resp.status_code == 200
    body = resp.json()
    assert body["rescan_pending"] is True
    assert body["rescan_error"] is None
    assert body["succeeded"] == 1
