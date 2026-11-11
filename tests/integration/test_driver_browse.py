"""存储源目录浏览（媒体库配置页选目录）的集成测试。

覆盖：

1. 根 id 映射（p115 → ``"0"`` / guangya → ``""``）——浏览起点是账号根
2. 只返回目录（文件不进目录浏览器）
3. 存储源不存在 → 404
4. 驱动不支持按 id 浏览 → 400
5. 上游调用失败 → 400
6. HTTP 端点响应格式
7. ``DriverOut.media_cid_label`` 按驱动类型给出（前端零分支）
"""

from __future__ import annotations

import pytest
from app.adapters.drivers.base import CloudFile, DriverError
from app.services import driver_service
from httpx import ASGITransport, AsyncClient


class _FakeDriver:
    """假驱动：记录被请求的父 id，返回固定的子项。"""

    def __init__(self, entries: list[CloudFile] | None = None) -> None:
        self.entries = entries if entries is not None else []
        self.seen_parent: str | None = None
        self.raise_upstream = False

    async def list_dir_by_cid(self, cid: str | int, parent_path: str = "") -> list[CloudFile]:
        self.seen_parent = str(cid)
        if self.raise_upstream:
            raise DriverError("上游炸了", kind="upstream")
        return self.entries


class _NoBrowseDriver:
    """不实现 ``list_dir_by_cid`` 的驱动。"""

    name = "nobrowse"


@pytest.fixture
async def p115_id(db) -> int:
    row = await driver_service.add_driver(
        driver_type="p115", name="测试115", device_type="web"
    )
    return row.id


@pytest.fixture
async def guangya_id(db) -> int:
    row = await driver_service.add_driver(driver_type="guangya", name="测试光鸭")
    return row.id


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch):
    """把 ``build_driver`` 换成假驱动（不打云端 API）。

    ``holder["driver"]`` 是当前生效的实例——测试可先塞一个自定义实例进去
    （如让上游抛错），``build_driver`` 会复用它而不是新建。
    """
    holder: dict[str, _FakeDriver] = {}

    def _build(*args, **kwargs) -> _FakeDriver:
        driver = holder.get("driver")
        if driver is None:
            driver = _FakeDriver(
                [
                    CloudFile(remote_id="d1", path="/影视/电影", name="电影", is_dir=True),
                    CloudFile(remote_id="d2", path="/影视/剧集", name="剧集", is_dir=True),
                    CloudFile(
                        remote_id="f1", path="/影视/电影/a.mkv", name="a.mkv", size=1024
                    ),
                ]
            )
            holder["driver"] = driver
        return driver

    monkeypatch.setattr(driver_service, "build_driver", _build)
    return holder


@pytest.fixture
async def client(settings, db_engine):
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


# ---------------------------------------------------------------- 根 id 映射


async def test_p115_browse_starts_at_account_root(
    p115_id: int, fake_driver
) -> None:
    """p115 的账号根 cid 是 ``0``——``parent_id`` 留空时后端自己解析。"""
    dirs = await driver_service.browse_driver_dirs(p115_id)
    assert fake_driver["driver"].seen_parent == "0"
    assert dirs == [("d1", "电影"), ("d2", "剧集")]


async def test_guangya_browse_starts_at_empty_root(
    guangya_id: int, fake_driver
) -> None:
    """光鸭的账号根 fileId 就是空串。"""
    dirs = await driver_service.browse_driver_dirs(guangya_id)
    assert fake_driver["driver"].seen_parent == ""
    assert dirs == [("d1", "电影"), ("d2", "剧集")]


async def test_explicit_parent_id_is_passed_through(
    p115_id: int, fake_driver
) -> None:
    """显式给了 parent_id 就用它（不解析成根）。"""
    await driver_service.browse_driver_dirs(p115_id, "12345")
    assert fake_driver["driver"].seen_parent == "12345"


async def test_files_are_filtered_out(p115_id: int, fake_driver) -> None:
    """目录浏览器只列目录——文件不进列表。"""
    dirs = await driver_service.browse_driver_dirs(p115_id)
    assert all(name in {"电影", "剧集"} for _, name in dirs)
    assert "a.mkv" not in [name for _, name in dirs]


# ---------------------------------------------------------------- 错误分支


async def test_missing_driver_raises_404(db) -> None:
    from app.services.driver_service import DriverServiceError

    with pytest.raises(DriverServiceError) as exc:
        await driver_service.browse_driver_dirs(999999)
    assert exc.value.status_code == 404


async def test_driver_without_browse_support_raises_400(
    p115_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """驱动没实现 ``list_dir_by_cid`` → 400（不是 500）。"""
    from app.services.driver_service import DriverServiceError

    monkeypatch.setattr(
        driver_service, "build_driver", lambda *a, **k: _NoBrowseDriver()
    )
    with pytest.raises(DriverServiceError) as exc:
        await driver_service.browse_driver_dirs(p115_id)
    assert exc.value.status_code == 400


async def test_upstream_failure_raises_400(p115_id: int, fake_driver) -> None:
    """上游调用失败 → 400（不是 500）——网盘限流 / 凭据失效都归这一类。"""
    from app.services.driver_service import DriverServiceError

    driver = _FakeDriver()
    driver.raise_upstream = True
    fake_driver["driver"] = driver

    with pytest.raises(DriverServiceError) as exc:
        await driver_service.browse_driver_dirs(p115_id)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------- HTTP 端点


async def test_browse_dirs_endpoint(client, p115_id: int, fake_driver) -> None:
    """端点返回 ``{driver_id, parent_id, dirs:[{id,name}]}``（与上传任务同形）。"""
    resp = await client.get(f"/api/drivers/{p115_id}/browse-dirs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["driver_id"] == p115_id
    assert data["parent_id"] == ""
    assert data["dirs"] == [
        {"id": "d1", "name": "电影"},
        {"id": "d2", "name": "剧集"},
    ]


async def test_browse_dirs_endpoint_404(client) -> None:
    resp = await client.get("/api/drivers/999999/browse-dirs")
    assert resp.status_code == 404


async def test_driver_out_carries_media_cid_label(client, p115_id: int) -> None:
    """``media_cid_label`` 按驱动类型给出——前端不写类型分支。"""
    resp = await client.get("/api/drivers")
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()}
    assert rows[p115_id]["media_cid_label"] == "115 媒体库 cid"


async def test_driver_out_media_cid_label_guangya(client, guangya_id: int) -> None:
    resp = await client.get("/api/drivers")
    rows = {r["id"]: r for r in resp.json()}
    assert rows[guangya_id]["media_cid_label"] == "光鸭媒体库 fileId"
