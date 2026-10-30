"""WebDAV 端到端集成测试。

**这些用例是本次重做的核心验收** —— 它们逐条对应旧自研实现（被 `3a9e392`
回退）的协议缺陷：

| 用例 | 旧自研版的行为 |
|---|---|
| ``test_propfind_returns_207`` | — |
| ``test_propfind_href_is_url_encoded`` | href **未** URL 编码 → 爆米花报「网络异常」 |
| ``test_get_collection_returns_200`` | GET 目录返 **405** → 客户端判定「网络异常」 |
| ``test_get_file_returns_strm_content`` | — |

另含安全用例：伪造内部头无法越权、未认证 401、超限流 429、写方法 405。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from a2wsgi import WSGIMiddleware
from app.core.config import AppSettings
from app.db.models import DavUser
from app.db.session import session_scope
from app.services.dav_auth import hash_password
from app.webdav.app import build_dav_app
from app.webdav.guard import DavGuard
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

#: DAV 挂载路径
MOUNT = "/dav"


def _basic(user: str, pwd: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """造一棵媒体库目录树。"""
    root = tmp_path / "library"
    (root / "电影" / "阿凡达").mkdir(parents=True)
    (root / "剧集" / "西部世界").mkdir(parents=True)
    (root / "电影" / "阿凡达" / "阿凡达.strm").write_text(
        "http://example.com:8080/302/1/sig"
    )
    (root / "剧集" / "西部世界" / "西部世界.strm").write_text(
        "http://example.com:8080/302/2/sig"
    )
    (root / "电影" / "poster.jpg").write_bytes(b"\xff\xd8\xff")
    return root


@pytest.fixture
async def dav_client(settings: AppSettings, db_engine, library: Path):
    """已挂载 DAV 的测试客户端。"""
    async with session_scope() as db:
        db.add(
            DavUser(
                username="alice",
                password_hash=hash_password("secret"),
                is_active=True,
                last_login_at=None,
                created_at="2026-10-30 12:00:00",
                updated_at="2026-10-30 12:00:00",
            )
        )

    # 让 DAV 根指向测试库
    from app.webdav import paths

    paths._root_cache.set(str(library))

    dav_app = build_dav_app(settings, str(library))

    # 结构：守卫（ASGI）→ FastAPI → mount(/dav) → wsgidav（WSGI）
    inner = FastAPI()
    inner.mount(MOUNT, WSGIMiddleware(dav_app))
    outer = DavGuard(inner, settings=settings)

    transport = ASGITransport(app=outer)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --------------------------------------------------------------------------
# 协议行为（旧自研版的缺陷所在）
# --------------------------------------------------------------------------


#: 从 PROPFIND 响应里抽出所有 href。
#:
#: **前缀无关**——XML 命名空间前缀由 XML 序列化器决定，不是本项目的契约：
#:
#: - stdlib ``ElementTree`` → ``ns0:href``（Python < 3.13 的默认行为）
#: - ``lxml`` → ``D:href``（wsgidav 用 ``nsmap={"D": "DAV:"}`` 注册的）
#:
#: 装了 lxml 就用 lxml，没装就用 stdlib——两者输出**语义相同**，只是前缀不同。
#: 故断言必须**不依赖前缀**，否则「装没装 lxml」会让同一份代码测试红绿不定。
_HREF_RE = re.compile(r"<(?:\w+:)?href>(.*?)</(?:\w+:)?href>")


def _hrefs(resp) -> list[str]:
    """抽出响应里的所有 href。"""
    return _HREF_RE.findall(resp.text)


async def test_propfind_returns_207(dav_client):
    """PROPFIND 返回 207 Multi-Status。"""
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    assert resp.status_code == 207
    assert resp.headers.get("content-type", "").startswith("application/xml")


async def test_propfind_href_is_url_encoded(dav_client):
    """**关键回归**：href 是 URL 编码的。

    旧自研版直接返回中文路径（未编码），客户端解析 href 会失败 ——
    爆米花等客户端报「网络异常」。
    """
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    hrefs = _hrefs(resp)
    assert hrefs, "PROPFIND 应返回至少一条 href"

    # 中文目录名必须是 %XX 编码，不能是原始中文字符
    assert any("%" in h for h in hrefs), f"href 未编码: {hrefs}"
    for h in hrefs:
        assert not re.search(r"[一-鿿]", h), f"href 含原始中文: {h}"

    # 编码后能解回原名
    from urllib.parse import unquote

    assert any("电影" in unquote(h) for h in hrefs)


async def test_propfind_href_has_mount_prefix(dav_client):
    """**关键回归**：href 带挂载路径前缀。

    wsgidav 用 ``config["mount_path"]`` 拼 href。不配的话 href 是
    ``/电影/`` 而不是 ``/dav/电影/`` —— 客户端按 href 回请求会 **404**。
    （实测踩过：href 少前缀，客户端直接连不上。）
    """
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    hrefs = _hrefs(resp)
    assert hrefs
    for h in hrefs:
        assert h.startswith(MOUNT), f"href 缺少挂载前缀 {MOUNT}: {h}"


async def test_no_duplicate_date_header(dav_client):
    """响应里只有一个 Date 头。

    wsgidav 自己加一个，ASGI 服务器也加一个 → 客户端收到两个。
    HTTP 允许重复头，但某些客户端对重复 Date 的处理不一致。
    """
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    dates = resp.headers.get_list("date")
    assert len(dates) <= 1, f"Date 头重复：{dates}"


async def test_propfind_response_has_no_quota_properties(dav_client):
    """PROPFIND 响应里没有 quota 属性。

    **性能关键**：wsgidav 的 ``FolderResource.get_used_bytes`` 调
    ``shutil.disk_usage()``（``statvfs``）。PROPFIND 一个目录时会对
    **每个子目录**调 4 次（探测 2 次 + 取值 2 次）。

    在 CIFS 网络共享上 ``statvfs`` 是网络往返（实测 6~66 ms）——
    231 个子目录 = 928 次 statvfs = PROPFIND 单个目录 5.5 秒。
    完整扫库（971 个目录）要 366 秒，客户端会中途超时。

    这两个属性是 **RFC 4331 的可选扩展**（存储配额），只读 DAV 不需要。
    """
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    assert resp.status_code == 207
    assert "quota-used-bytes" not in resp.text
    assert "quota-available-bytes" not in resp.text


async def test_get_collection_returns_200(dav_client):
    """**关键回归**：GET 目录返回 200（不是 405）。

    旧自研版对目录 GET 返 405 —— 客户端（Finder / 爆米花）浏览目录时
    直接报「网络异常」。
    """
    resp = await dav_client.get(f"{MOUNT}/", headers=_basic("alice", "secret"))
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


async def test_get_file_returns_strm_content(dav_client):
    """GET ``.strm`` 返回其中转 URL 原文。"""
    resp = await dav_client.get(
        f"{MOUNT}/%E7%94%B5%E5%BD%B1/%E9%98%BF%E5%87%A1%E8%BE%BE/%E9%98%BF%E5%87%A1%E8%BE%BE.strm",
        headers=_basic("alice", "secret"),
    )
    assert resp.status_code == 200
    assert resp.text == "http://example.com:8080/302/1/sig"


async def test_propfind_depth_infinity_rejected(dav_client):
    """显式 Depth: infinity 被拒（3 万文件会炸）。"""
    resp = await dav_client.request(
        "PROPFIND",
        f"{MOUNT}/",
        headers={**_basic("alice", "secret"), "Depth": "infinity"},
    )
    assert resp.status_code == 403


async def test_propfind_default_depth_is_1(dav_client):
    """缺省 Depth 被降级为 1（不遍历全树）。

    根目录下只有「电影」「剧集」两个子目录 —— 若 Depth 被当成 infinity，
    响应里会多出「电影/阿凡达」等更深层条目。
    """
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers=_basic("alice", "secret")
    )
    assert resp.status_code == 207
    hrefs = _hrefs(resp)
    # 根 + 2 个一级子目录
    assert len(hrefs) == 3, f"depth 未降级为 1: {hrefs}"
    # 不含更深层（去掉挂载前缀后，一级子目录的路径里不应有中间的分隔符）
    for h in hrefs:
        rel = h[len(MOUNT) :] if h.startswith(MOUNT) else h
        # 去掉首尾斜杠后不应还有斜杠（那说明是更深一层）
        assert "/" not in rel.strip("/"), f"depth 未降级为 1: {hrefs}"


# --------------------------------------------------------------------------
# 只读
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["PUT", "DELETE", "MKCOL", "POST"])
async def test_write_methods_rejected(dav_client, method: str):
    """写方法一律 405（只读暴露）。"""
    resp = await dav_client.request(
        method,
        f"{MOUNT}/%E7%94%B5%E5%BD%B1/new.txt",
        headers=_basic("alice", "secret"),
        content=b"x",
    )
    assert resp.status_code == 405


async def test_lock_rejected(dav_client):
    """LOCK 被拒（只读）。"""
    resp = await dav_client.request(
        "LOCK",
        f"{MOUNT}/%E7%94%B5%E5%BD%B1/poster.jpg",
        headers=_basic("alice", "secret"),
        content=b'<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:">'
        b"<D:lockscope><D:exclusive/></D:lockscope>"
        b"<D:locktype><D:write/></D:locktype></D:lockinfo>",
    )
    assert resp.status_code == 405


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


async def test_unauthenticated_returns_401(dav_client):
    """未认证返回 401 + WWW-Authenticate。"""
    resp = await dav_client.request("PROPFIND", f"{MOUNT}/", headers={"Depth": "1"})
    assert resp.status_code == 401
    assert "www-authenticate" in resp.headers


async def test_wrong_password_returns_401(dav_client):
    """密码错返回 401。"""
    resp = await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "wrong"), "Depth": "1"}
    )
    assert resp.status_code == 401


async def test_forged_internal_header_cannot_bypass_auth(dav_client):
    """**关键安全用例**：伪造 ``X-Dav-User`` 头无法绕过认证。

    客户端直接发 ``X-Dav-User: alice`` 不带 Authorization —— 必须被拒。
    """
    resp = await dav_client.request(
        "PROPFIND",
        f"{MOUNT}/",
        headers={"Depth": "1", "X-Dav-User": "alice"},
    )
    assert resp.status_code == 401


async def test_forged_root_header_cannot_escape(dav_client, tmp_path: Path):
    """**关键安全用例**：伪造 ``X-Dav-Root`` 头无法读根外文件。

    客户端发 ``X-Dav-Root: /etc`` 想读系统目录 —— 必须被剥离并忽略。
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")

    resp = await dav_client.get(
        f"{MOUNT}/secret.txt",
        headers={**_basic("alice", "secret"), "X-Dav-Root": str(tmp_path)},
    )
    # 剥离后用的是真正的 DAV 根，那里没有 secret.txt → 404
    assert resp.status_code == 404
    assert "top secret" not in resp.text


# --------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------


async def test_rate_limit_returns_429(dav_client, settings: AppSettings):
    """超限流返回 429。"""
    settings.dav_rate_limit_per_min = 3
    from app.services.dav_ratelimit import dav_rate_limiter

    dav_rate_limiter.reset()

    statuses = []
    for _ in range(6):
        resp = await dav_client.request(
            "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
        )
        statuses.append(resp.status_code)

    assert 429 in statuses
    dav_rate_limiter.reset()


async def test_user_rate_limit_returns_429(dav_client, settings: AppSettings):
    """按用户的限流生效（IP 维度不限）。"""
    settings.dav_rate_limit_per_min = 0  # IP 不限
    settings.dav_user_rate_limit_per_min = 2
    from app.services.dav_ratelimit import dav_rate_limiter

    dav_rate_limiter.reset()

    statuses = []
    for _ in range(5):
        resp = await dav_client.request(
            "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
        )
        statuses.append(resp.status_code)

    assert 429 in statuses
    dav_rate_limiter.reset()


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


async def test_audit_records_request(dav_client, db_engine):
    """请求被记入审计。"""
    from app.services.dav_audit import dav_auditor

    await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "secret"), "Depth": "1"}
    )
    await dav_auditor._flush()

    from app.db.models import DavAuditLog
    from sqlalchemy import select

    async with session_scope() as db:
        rows = (
            await db.execute(select(DavAuditLog).where(DavAuditLog.method == "PROPFIND"))
        ).scalars().all()

    assert rows, "PROPFIND 应被记入审计"
    assert rows[0].username == "alice"


async def test_audit_records_auth_failure(dav_client, db_engine):
    """**关键安全用例**：认证失败也要记审计。

    「谁在尝试爆破」是安全审计的核心部分。
    """
    from app.services.dav_audit import dav_auditor

    await dav_client.request(
        "PROPFIND", f"{MOUNT}/", headers={**_basic("alice", "wrong"), "Depth": "1"}
    )
    await dav_auditor._flush()

    from app.db.models import DavAuditLog
    from sqlalchemy import select

    async with session_scope() as db:
        rows = (
            await db.execute(select(DavAuditLog).where(DavAuditLog.status == 401))
        ).scalars().all()

    assert rows, "认证失败应被记入审计"
    assert rows[0].username == "alice"
    assert rows[0].remote_addr
