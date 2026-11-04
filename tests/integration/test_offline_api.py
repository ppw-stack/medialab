"""离线下载 API 的测试。

**配置全部落库**（``app_env_settings``），页面上改完立即生效——故这里
覆盖三块：目录浏览、能力查询、配置保存。

⚠️ **目录浏览不再限权**：「数据同步」的 ``/upload-tasks/local-dirs`` 能列
服务能看到的任何目录，离线下载现在与它一致（面板本身已是管理员权限，
用户明确要求「所有目录都能选」）。**暴露面靠「三项没配齐时不暴露文件」
控制**——文件服务在未配置时返回 403。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from app.api.auth import require_session
from app.api.routes import offline_downloads
from app.services.env_settings import get_env_settings
from app.services.offline_config import (
    KEY_FILES_ROOT,
    KEY_PUBLIC_BASE,
    KEY_SECRET,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """不打 DB 的 app（**绕过登录鉴权**）——只测目录浏览。"""
    app = FastAPI()
    app.dependency_overrides[require_session] = lambda: "tester"
    app.include_router(offline_downloads.router)
    yield TestClient(app)


@pytest_asyncio.fixture()
async def aclient(db: None) -> AsyncIterator[httpx.AsyncClient]:
    """异步客户端（**走 DB**）——配置保存要落库。"""
    app = FastAPI()
    app.dependency_overrides[require_session] = lambda: "tester"
    app.include_router(offline_downloads.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _mk(root: Path, rel: str, size: int = 10) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


# ---- 目录浏览（不限权，从任意绝对路径开始）----


def test_lists_subdirectories(client: TestClient, tmp_path: Path) -> None:
    """列出子目录。``id`` 是**完整路径**（前端拿它当下一层直接请求）。"""
    _mk(tmp_path, "sub/a.txt")
    _mk(tmp_path, "other/b.txt")
    r = client.get(f"/api/offline-downloads/local-dirs?path={tmp_path}")
    assert r.status_code == 200
    entries = {d["name"]: d["id"] for d in r.json()["dirs"]}
    assert entries["sub"] == str(tmp_path / "sub")
    assert entries["other"] == str(tmp_path / "other")


def test_navigates_into_subdirectory(client: TestClient, tmp_path: Path) -> None:
    """逐层下钻。"""
    _mk(tmp_path, "a/b/c.txt")
    r = client.get(f"/api/offline-downloads/local-dirs?path={tmp_path / 'a'}")
    assert r.status_code == 200
    assert "b" in {d["name"] for d in r.json()["dirs"]}


def test_relative_path_rejected(client: TestClient) -> None:
    """相对路径 → 400（目录浏览一律用绝对路径）。"""
    r = client.get("/api/offline-downloads/local-dirs?path=relative/dir")
    assert r.status_code == 400
    assert "绝对路径" in r.json()["detail"]


def test_missing_dir_rejected(client: TestClient, tmp_path: Path) -> None:
    """不存在的目录 → 400。"""
    r = client.get(f"/api/offline-downloads/local-dirs?path={tmp_path / 'nope'}")
    assert r.status_code == 400


# ---- 文件列举 ----


def test_lists_files_with_size(client: TestClient, tmp_path: Path) -> None:
    """列出文件 + 大小（前端展示「将要传什么」）。"""
    _mk(tmp_path, "movie.mkv", 2048)
    _mk(tmp_path, "sub/nested.mkv", 1024)
    r = client.get(f"/api/offline-downloads/local-files?path={tmp_path}")
    assert r.status_code == 200
    files = {f["name"]: f["size"] for f in r.json()["files"]}
    assert files["movie.mkv"] == 2048
    # 不递归（子目录里的文件不在列表里）
    assert "nested.mkv" not in files


def test_local_files_relative_path_rejected(client: TestClient) -> None:
    """文件列举同样要绝对路径。"""
    r = client.get("/api/offline-downloads/local-files?path=relative")
    assert r.status_code == 400


# ---- 能力查询 ----


def test_capabilities_reports_disabled_without_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配密钥 → ``enabled: false``（前端据此给配置表单，而不是坏入口）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.get_settings(), "offline_hmac_secret", "")
    monkeypatch.setattr(config_mod.get_settings(), "offline_public_base", "")
    monkeypatch.setattr(config_mod.get_settings(), "offline_files_root", "")
    r = client.get("/api/offline-downloads/capabilities")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert data["has_secret"] is False
    assert data["has_public_base"] is False
    assert data["has_files_root"] is False
    assert data["hint"]


def test_capabilities_never_leaks_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**密钥永不回显**——只报「是否已配置」。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.get_settings(), "offline_hmac_secret", "s3cr3t")
    r = client.get("/api/offline-downloads/capabilities")
    assert r.status_code == 200
    assert "s3cr3t" not in r.text
    assert r.json()["has_secret"] is True


# ---- 配置保存（落库 + 立即生效）----


async def test_config_saves_and_enables(
    aclient: httpx.AsyncClient, tmp_path: Path
) -> None:
    """保存三项 → 落库 + 内存生效 + capabilities 变启用。"""
    r = await aclient.put(
        "/api/offline-downloads/config",
        json={
            "secret": "abc123",
            "public_base": "http://example.test:8092/",
            "files_root": str(tmp_path),
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is True
    assert data["has_secret"] is True
    # 尾斜杠被去掉（光鸭拼 URL 时不能出现 //）
    assert data["public_base"] == "http://example.test:8092"
    # 落库（内存里也是同一份）
    assert get_env_settings().get(KEY_SECRET) == "abc123"
    assert get_env_settings().get(KEY_FILES_ROOT) == str(tmp_path)


async def test_config_rejects_bad_public_base(
    aclient: httpx.AsyncClient, tmp_path: Path
) -> None:
    """公网入口必须是 http(s)——光鸭要能直接拉。"""
    r = await aclient.put(
        "/api/offline-downloads/config",
        json={"public_base": "example.test", "files_root": str(tmp_path)},
    )
    assert r.status_code == 400
    assert "http" in r.json()["detail"]


async def test_config_rejects_missing_root(aclient: httpx.AsyncClient, tmp_path: Path) -> None:
    """源根目录必须存在（否则任务必然失败，不如当场报错）。"""
    r = await aclient.put(
        "/api/offline-downloads/config",
        json={"files_root": str(tmp_path / "nope")},
    )
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


async def test_config_secret_omitted_keeps_old(
    aclient: httpx.AsyncClient, tmp_path: Path
) -> None:
    """**留空 = 不修改密钥**（密钥永不回显，不能用「空串 = 清除」语义）。"""
    await aclient.put(
        "/api/offline-downloads/config",
        json={"secret": "keep-me", "public_base": "http://a.test", "files_root": str(tmp_path)},
    )
    r = await aclient.put(
        "/api/offline-downloads/config", json={"public_base": "http://b.test"}
    )
    assert r.status_code == 200, r.text
    assert get_env_settings().get(KEY_SECRET) == "keep-me"
    assert get_env_settings().get(KEY_PUBLIC_BASE) == "http://b.test"


async def test_config_empty_body_rejected(aclient: httpx.AsyncClient) -> None:
    """什么都没传 → 400。"""
    r = await aclient.put("/api/offline-downloads/config", json={})
    assert r.status_code == 400


# ---- /run：源目录越界防线（2026-11-04 prod 事故）----


async def test_run_rejects_local_root_outside_files_root(
    aclient: httpx.AsyncClient, tmp_path: Path
) -> None:
    """源目录不在源根目录之下 → 400（文件服务物理上暴露不了它）。"""
    from app.services.offline_config import (
        KEY_FILES_ROOT,
        KEY_PUBLIC_BASE,
        KEY_SECRET,
    )

    mgr = get_env_settings()
    mgr.set_memory(KEY_SECRET, "s3cr3t")
    mgr.set_memory(KEY_PUBLIC_BASE, "http://example.test:8092")
    mgr.set_memory(KEY_FILES_ROOT, str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    r = await aclient.post(
        "/api/offline-downloads/run",
        json={
            "target_driver_id": 1,
            "target_file_id": "",
            "target_path": "",
            "local_root": str(outside),
            "include_subdirs": True,
            "exts": "",
        },
    )
    assert r.status_code == 400
    assert "源根目录" in r.json()["detail"]
