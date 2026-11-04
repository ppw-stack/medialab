"""离线下载文件服务的 HTTP 层测试。

用 FastAPI 的 ``TestClient`` 打真实的 HTTP 请求——验证「光鸭能不能
拉到文件」这件事的**每一环**：鉴权、路径穿越、Range、内容长度。

⚠️ **路由是常驻的**（FastAPI 不支持运行时增删路由，而配置现在可以在
页面上改）。故「未启用」表现为 **403**，而不是「路由不存在」——
比让 SPA 兜底吞掉更诚实（明确「这里有个服务但没开」）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api.routes.offline_files import mount_offline_files
from app.services import env_settings
from app.services.env_settings import EnvSettingsManager
from app.services.offline_config import (
    KEY_FILES_ROOT,
    KEY_PUBLIC_BASE,
    KEY_SECRET,
)
from app.services.offline_files import URL_PREFIX
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "http-test-secret"


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """一个挂了文件服务的 app（根目录 = tmp_path，三项配置齐全）。"""
    env_settings.set_env_settings(
        EnvSettingsManager(
            {
                KEY_SECRET: SECRET,
                KEY_PUBLIC_BASE: "http://example.test:8092",
                KEY_FILES_ROOT: str(tmp_path),
            }
        )
    )
    app = FastAPI()
    mount_offline_files(app)
    yield TestClient(app)
    env_settings.set_env_settings(None)


def _write(root: Path, rel: str, size: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def _token(rel: str) -> str:
    from app.services.offline_files import make_token

    return make_token(SECRET, rel)


def test_serves_file_with_valid_token(client: TestClient, tmp_path: Path) -> None:
    """正确 token → 200 + 完整内容。"""
    _write(tmp_path, "a.mkv", 1000)
    r = client.get(f"{URL_PREFIX}/{_token('a.mkv')}/a.mkv")
    assert r.status_code == 200
    assert len(r.content) == 1000


def test_rejects_bad_token(client: TestClient, tmp_path: Path) -> None:
    """错误 token → 403（**不泄露文件是否存在**）。"""
    _write(tmp_path, "a.mkv", 1000)
    r = client.get(f"{URL_PREFIX}/deadbeef/a.mkv")
    assert r.status_code == 403


def test_token_of_another_file_rejected(client: TestClient, tmp_path: Path) -> None:
    """**拿 a 的 token 要 b** → 403（token 与路径绑定）。"""
    _write(tmp_path, "a.mkv", 1000)
    _write(tmp_path, "b.mkv", 1000)
    r = client.get(f"{URL_PREFIX}/{_token('a.mkv')}/b.mkv")
    assert r.status_code == 403


def test_blocks_path_traversal(client: TestClient, tmp_path: Path) -> None:
    """路径穿越 → 403（token 对不上）或 404——**绝不能返回内容**。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    # token 按穿越路径算（攻击者算不出正确 token 就被 403）
    r = client.get(f"{URL_PREFIX}/{_token('../secret.txt')}/../secret.txt")
    assert r.status_code in (403, 404)
    assert b"top secret" not in r.content


def test_missing_file_returns_404(client: TestClient, tmp_path: Path) -> None:
    """token 正确但文件不存在 → 404。"""
    r = client.get(f"{URL_PREFIX}/{_token('nope.mkv')}/nope.mkv")
    assert r.status_code == 404


def test_supports_range_request(client: TestClient, tmp_path: Path) -> None:
    """Range 请求 → 206 + Content-Range（光鸭可能断点续传）。"""
    _write(tmp_path, "big.mkv", 10000)
    r = client.get(
        f"{URL_PREFIX}/{_token('big.mkv')}/big.mkv",
        headers={"Range": "bytes=100-199"},
    )
    assert r.status_code == 206
    assert len(r.content) == 100
    assert r.headers["content-range"] == "bytes 100-199/10000"
    assert r.headers["accept-ranges"] == "bytes"


def test_serves_unicode_path(client: TestClient, tmp_path: Path) -> None:
    """中文 / 空格路径能正常取到（URL 编码 → 服务端解码 → token 校验）。"""
    _write(tmp_path, "电影/星际迷航 (2009).mkv", 500)
    rel = "电影/星际迷航 (2009).mkv"
    from urllib.parse import quote

    r = client.get(f"{URL_PREFIX}/{_token(rel)}/{quote(rel)}")
    assert r.status_code == 200
    assert len(r.content) == 500


def test_serves_double_encoded_path(client: TestClient, tmp_path: Path) -> None:
    """**双重编码的路径也能取到**。

    实测（2026-11-04，真实光鸭账号）：光鸭的下载器有一条重试路径会把
    URL 里的 ``%`` 再编码成 ``%25``（``%E7%94%B5`` → ``%25E7%2594%25B5``），
    那种请求原本全部 403（token 校验失败）。
    """
    _write(tmp_path, "电影/星际迷航 (2009).mkv", 500)
    rel = "电影/星际迷航 (2009).mkv"
    from urllib.parse import quote

    r = client.get(f"{URL_PREFIX}/{_token(rel)}/{quote(quote(rel))}")
    assert r.status_code == 200
    assert len(r.content) == 500


def test_head_returns_size(client: TestClient, tmp_path: Path) -> None:
    """HEAD 返回文件大小（光鸭**先探大小**再决定怎么拉）。

    实测：光鸭会发 HEAD 请求；不支持 → 405 → 任务可能直接失败。
    """
    _write(tmp_path, "big.mkv", 12345)
    r = client.head(f"{URL_PREFIX}/{_token('big.mkv')}/big.mkv")
    assert r.status_code == 200
    assert r.headers["content-length"] == "12345"
    assert r.headers["accept-ranges"] == "bytes"


def test_head_rejects_bad_token(client: TestClient, tmp_path: Path) -> None:
    """HEAD 也要验 token（不能因为是 HEAD 就放行）。"""
    _write(tmp_path, "big.mkv", 12345)
    r = client.head(f"{URL_PREFIX}/deadbeef/big.mkv")
    assert r.status_code == 403


def test_not_configured_returns_403(tmp_path: Path) -> None:
    """**三项没配齐 → 403**（路由常驻，但不暴露任何文件）。"""
    env_settings.set_env_settings(EnvSettingsManager({}))
    try:
        app = FastAPI()
        mount_offline_files(app)
        client = TestClient(app)
        r = client.get(f"{URL_PREFIX}/{_token('a.mkv')}/a.mkv")
        assert r.status_code == 403
    finally:
        env_settings.set_env_settings(None)


def test_config_change_takes_effect_immediately(tmp_path: Path) -> None:
    """**改配置立即生效**——路由常驻，每次请求时读当前配置。

    这是「配置搬到页面上」的核心前提：FastAPI 不能运行时增删路由，
    所以只能让 handler 每次读配置。
    """
    mgr = EnvSettingsManager({})
    env_settings.set_env_settings(mgr)
    try:
        app = FastAPI()
        mount_offline_files(app)
        client = TestClient(app)

        _write(tmp_path, "a.mkv", 100)
        # 未配置 → 403
        assert client.get(f"{URL_PREFIX}/{_token('a.mkv')}/a.mkv").status_code == 403

        # 配齐三项（模拟页面上的「保存配置」）→ 立刻能取
        mgr.set_memory(KEY_SECRET, SECRET)
        mgr.set_memory(KEY_PUBLIC_BASE, "http://example.test:8092")
        mgr.set_memory(KEY_FILES_ROOT, str(tmp_path))
        r = client.get(f"{URL_PREFIX}/{_token('a.mkv')}/a.mkv")
        assert r.status_code == 200
    finally:
        env_settings.set_env_settings(None)


def test_directory_request_returns_404(client: TestClient, tmp_path: Path) -> None:
    """目录不是文件 → 404（不列目录）。"""
    (tmp_path / "sub").mkdir()
    r = client.get(f"{URL_PREFIX}/{_token('sub')}/sub")
    assert r.status_code == 404
