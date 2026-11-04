"""离线下载 API 的测试（源目录浏览的**路径穿越防护**是重点）。

⚠️ **为什么源目录浏览要单独限权**：「数据同步」的 ``/upload-tasks/local-dirs``
能列服务能看到的**任何**目录（含 ``/app/.env`` 的明文密钥）。而离线下载的
文件要经**公网**暴露给光鸭拉——暴露面更大，故必须收窄到
``OFFLINE_FILES_ROOT`` 之下。
"""

from __future__ import annotations

import pytest
from app.api.auth import require_session
from app.api.routes import offline_downloads
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """把源根目录指到 tmp_path 的 app（**绕过登录鉴权**）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.get_settings(), "offline_files_root", str(tmp_path))
    app = FastAPI()
    # 路由带 require_session 依赖——测试里替换成恒真
    app.dependency_overrides[require_session] = lambda: "tester"
    app.include_router(offline_downloads.router)
    return TestClient(app)


def _mk(root, rel: str, size: int = 10) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def test_lists_subdirectories(client: TestClient, tmp_path) -> None:
    """列出子目录。"""
    _mk(tmp_path, "sub/a.txt")
    _mk(tmp_path, "other/b.txt")
    r = client.get("/api/offline-downloads/local-dirs")
    assert r.status_code == 200
    names = {d["name"] for d in r.json()["dirs"]}
    assert {"sub", "other"} <= names


def test_ids_are_relative_to_root(client: TestClient, tmp_path) -> None:
    """``id`` 是**相对 root** 的路径（前端拿它当「下一层」直接请求）。"""
    _mk(tmp_path, "sub/a.txt")
    r = client.get("/api/offline-downloads/local-dirs")
    ids = {d["id"] for d in r.json()["dirs"]}
    assert "sub" in ids
    assert not any(i.startswith("/") for i in ids)


def test_navigates_into_subdirectory(client: TestClient, tmp_path) -> None:
    """逐层下钻。"""
    _mk(tmp_path, "a/b/c.txt")
    r = client.get("/api/offline-downloads/local-dirs?path=a")
    assert r.status_code == 200
    names = {d["name"] for d in r.json()["dirs"]}
    assert "b" in names


def test_blocks_path_traversal(client: TestClient, tmp_path) -> None:
    """**路径穿越被拦**——不能列出 root 之外的目录。"""
    r = client.get("/api/offline-downloads/local-dirs?path=../../etc")
    assert r.status_code == 400
    assert "超出" in r.json()["detail"]


def test_blocks_absolute_escape(client: TestClient, tmp_path) -> None:
    """绝对路径逃逸也被拦。"""
    r = client.get("/api/offline-downloads/local-dirs?path=/etc")
    assert r.status_code == 400


def test_missing_root_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 ``OFFLINE_FILES_ROOT`` → 503（功能未启用）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.get_settings(), "offline_files_root", "")
    r = client.get("/api/offline-downloads/local-dirs")
    assert r.status_code == 503


def test_lists_files_with_size(client: TestClient, tmp_path) -> None:
    """列出文件 + 大小（前端展示「将要传什么」）。"""
    _mk(tmp_path, "movie.mkv", 2048)
    _mk(tmp_path, "sub/nested.mkv", 1024)
    r = client.get("/api/offline-downloads/local-files")
    assert r.status_code == 200
    files = {f["name"]: f["size"] for f in r.json()["files"]}
    assert files["movie.mkv"] == 2048
    # 不递归（子目录里的文件不在列表里）
    assert "nested.mkv" not in files


def test_local_files_blocks_traversal(client: TestClient, tmp_path) -> None:
    """文件列举同样限权。"""
    r = client.get("/api/offline-downloads/local-files?path=../../etc")
    assert r.status_code == 400


def test_capabilities_reports_disabled_without_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配密钥 → ``enabled: false``（前端据此说明怎么开，而不是给个坏入口）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.get_settings(), "offline_hmac_secret", "")
    monkeypatch.setattr(config_mod.get_settings(), "offline_public_base", "")
    r = client.get("/api/offline-downloads/capabilities")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert data["hint"]
