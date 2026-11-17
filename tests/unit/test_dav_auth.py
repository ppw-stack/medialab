"""WebDAV 认证的单测。

覆盖三类安全关键行为：

1. **凭据校验** —— 正确 / 错误 / 不存在 / 停用 / 畸形头
2. **失败锁定** —— 按 IP 与按用户名**各自独立**计数
3. **认证缓存** —— 命中、过期、失败即清、改密码后失效
"""

from __future__ import annotations

import asyncio
import base64

from app.core.config import AppSettings
from app.db.models import DavUser
from app.db.session import session_scope
from app.services.dav_auth import (
    AUTH_DISABLED,
    AUTH_INVALID,
    AUTH_LOCKED,
    AUTH_MALFORMED,
    AUTH_MISSING,
    AUTH_OK,
    DavAuthenticator,
    hash_password,
    verify_password,
)


def _basic(user: str, pwd: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


async def _make_user(username: str = "alice", password: str = "secret", **kw) -> int:
    """建一个 DAV 账号，返回 id。"""
    async with session_scope() as db:
        row = DavUser(
            username=username,
            password_hash=hash_password(password),
            is_active=kw.get("is_active", True),
            last_login_at=None,
            created_at="2026-10-30 12:00:00",
            updated_at="2026-10-30 12:00:00",
        )
        db.add(row)
        await db.flush()
        return row.id


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------


def test_hash_verify_roundtrip():
    """哈希能校验自己，且不同密码不匹配。"""
    stored = hash_password("secret")
    assert stored.startswith("pbkdf2$")
    assert verify_password("secret", stored)
    assert not verify_password("wrong", stored)


def test_verify_password_rejects_garbage():
    """畸形存储值一律 False，不抛异常。"""
    assert not verify_password("x", "")
    assert not verify_password("x", "not-pbkdf2")
    assert not verify_password("x", "pbkdf2$notanint$aa$bb")
    assert not verify_password("x", "pbkdf2$600000$zz$bb")


def test_hash_is_salted():
    """同密码两次哈希不同（盐随机）。"""
    assert hash_password("same") != hash_password("same")


# --------------------------------------------------------------------------
# 凭据校验
# --------------------------------------------------------------------------


async def test_authenticate_ok(db_engine, settings: AppSettings):
    """正确凭据放行。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    result = await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert result.status == AUTH_OK
    assert result.username == "alice"
    assert result.user_id > 0


async def test_authenticate_wrong_password(db_engine, settings: AppSettings):
    """密码错 → invalid。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    result = await auth.authenticate(settings, _basic("alice", "bad"), "1.2.3.4")
    assert result.status == AUTH_INVALID


async def test_authenticate_unknown_user(db_engine, settings: AppSettings):
    """用户不存在 → invalid（不是 missing）。"""
    auth = DavAuthenticator()
    result = await auth.authenticate(settings, _basic("nobody", "x"), "1.2.3.4")
    assert result.status == AUTH_INVALID


async def test_authenticate_missing_header(db_engine, settings: AppSettings):
    """没带 Authorization 头 → missing，且**不计入失败次数**。"""
    auth = DavAuthenticator()
    result = await auth.authenticate(settings, None, "1.2.3.4")
    assert result.status == AUTH_MISSING
    assert auth.stats()["failed_ip"] == 0


async def test_authenticate_malformed_header(db_engine, settings: AppSettings):
    """畸形 Authorization 头 → malformed，且**不计入失败次数**。

    客户端发来的坏头不该消耗账号的失败配额 —— 否则一个配置错误的
    播放器就能把账号锁死。
    """
    auth = DavAuthenticator()
    for bad in ("Basic !!!not-base64", "Bearer xyz", "Basic", "Basic " + "x" * 5):
        result = await auth.authenticate(settings, bad, "1.2.3.4")
        assert result.status == AUTH_MALFORMED, bad
    assert auth.stats()["failed_ip"] == 0
    assert auth.stats()["failed_user"] == 0


async def test_authenticate_disabled_account(db_engine, settings: AppSettings):
    """停用账号 → disabled，且**不计入失败次数**。

    停用是管理员的动作，不是用户输错了密码 —— 不该计入失败配额。
    """
    await _make_user("alice", "secret", is_active=False)
    auth = DavAuthenticator()
    result = await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert result.status == AUTH_DISABLED
    assert auth.stats()["failed_ip"] == 0


async def test_authenticate_updates_last_login(db_engine, settings: AppSettings):
    """认证成功会更新 last_login_at。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    async with session_scope() as db:
        from sqlalchemy import select

        row = (
            await db.execute(select(DavUser).where(DavUser.username == "alice"))
        ).scalar_one()
        assert row.last_login_at is not None


# --------------------------------------------------------------------------
# 失败锁定
# --------------------------------------------------------------------------


async def test_lock_by_ip(db_engine, settings: AppSettings):
    """同一 IP 连续失败超阈值 → 该 IP 被锁（即使之后密码正确）。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    for _ in range(settings.dav_max_failures):
        result = await auth.authenticate(settings, _basic("alice", "bad"), "1.2.3.4")
        assert result.status == AUTH_INVALID

    # 密码正确也进不来
    result = await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert result.status == AUTH_LOCKED
    assert result.locked_by == "ip"


async def test_lock_by_username_across_ips(db_engine, settings: AppSettings):
    """同一用户名从**多个 IP** 试错达阈值 → 用户名被锁（挡分布式爆破）。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    # 两个不同 IP 各试若干次，合计达到阈值
    for i in range(settings.dav_max_failures):
        ip = f"10.0.0.{i}" if i % 2 == 0 else "10.0.0.100"
        result = await auth.authenticate(settings, _basic("alice", "bad"), ip)
        assert result.status == AUTH_INVALID

    # 从第三个 IP 用正确密码 → 用户名维度已锁
    result = await auth.authenticate(settings, _basic("alice", "secret"), "10.0.0.99")
    assert result.status == AUTH_LOCKED
    assert result.locked_by == "user"


async def test_lock_does_not_block_other_username(db_engine, settings: AppSettings):
    """用户名锁定不影响别的用户名。

    注意用**不同的 IP** —— IP 锁定是防火墙式的「整个 IP 封禁」，
    同 IP 上的其他用户本来就该一起被拒（那正是 IP 锁定的目的）。
    """
    await _make_user("alice", "secret")
    await _make_user("bob", "secret")
    auth = DavAuthenticator()
    # 两个不同 IP 打 alice —— 触发用户名维度锁定
    for i in range(settings.dav_max_failures):
        ip = "1.2.3.4" if i % 2 == 0 else "2.2.2.2"
        await auth.authenticate(settings, _basic("alice", "bad"), ip)

    # bob 从第三个 IP 认证 → 不受影响
    result = await auth.authenticate(settings, _basic("bob", "secret"), "3.3.3.3")
    assert result.status == AUTH_OK


async def test_ip_lock_blocks_whole_ip(db_engine, settings: AppSettings):
    """IP 锁定是「整个 IP 封禁」—— 同 IP 上的其他账号也进不来。

    这是防火墙式语义：来源 IP 已被判定为攻击源，不该还放它进来。
    """
    await _make_user("alice", "secret")
    await _make_user("bob", "secret")
    auth = DavAuthenticator()
    for _ in range(settings.dav_max_failures):
        await auth.authenticate(settings, _basic("alice", "bad"), "1.2.3.4")

    result = await auth.authenticate(settings, _basic("bob", "secret"), "1.2.3.4")
    assert result.status == AUTH_LOCKED
    assert result.locked_by == "ip"


async def test_single_ip_failure_does_not_lock_username(db_engine, settings: AppSettings):
    """单 IP 反复试错**不锁用户名** —— 不误伤其它设备上的同一账号。

    这是本次实现特意修掉的缺陷：若按用户名无条件计数，一个 IP 试错 5 次
    就会把该用户名锁上，电视上正在正常使用的同一账号会被一起拒掉。
    """
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    for _ in range(settings.dav_max_failures):
        await auth.authenticate(settings, _basic("alice", "bad"), "1.2.3.4")

    # 别的 IP 用正确密码 → 应当放行
    result = await auth.authenticate(settings, _basic("alice", "secret"), "9.9.9.9")
    assert result.status == AUTH_OK


async def test_success_clears_failure_count(db_engine, settings: AppSettings):
    """成功一次会清零失败计数。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    await auth.authenticate(settings, _basic("alice", "bad"), "1.2.3.4")
    await auth.authenticate(settings, _basic("alice", "bad"), "1.2.3.4")
    assert auth.stats()["failed_ip"] == 1

    await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert auth.stats()["failed_ip"] == 0
    assert auth.stats()["failed_user"] == 0


# --------------------------------------------------------------------------
# 认证缓存
# --------------------------------------------------------------------------


async def test_cache_hit_avoids_rehash(db_engine, settings: AppSettings):
    """缓存命中时不再查库（用改密码验证：改密码后缓存仍放行 → 说明命中了）。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    first = await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert first.status == AUTH_OK
    assert auth.stats()["cached_credentials"] == 1

    # 改密码（不清缓存）
    async with session_scope() as db:
        from sqlalchemy import select

        row = (
            await db.execute(select(DavUser).where(DavUser.username == "alice"))
        ).scalar_one()
        row.password_hash = hash_password("newpass")

    second = await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert second.status == AUTH_OK  # 命中缓存，没有重新验密码


async def test_cache_is_ip_scoped(db_engine, settings: AppSettings):
    """缓存按 IP 隔离 —— 换个 IP 要重新验密码。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")

    async with session_scope() as db:
        from sqlalchemy import select

        row = (
            await db.execute(select(DavUser).where(DavUser.username == "alice"))
        ).scalar_one()
        row.password_hash = hash_password("newpass")

    # 换 IP → 不命中缓存 → 重新验密码 → 失败
    result = await auth.authenticate(settings, _basic("alice", "secret"), "5.5.5.5")
    assert result.status == AUTH_INVALID


async def test_failure_purges_ip_cache(db_engine, settings: AppSettings):
    """认证失败立即清空该 IP 的全部缓存。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    assert auth.stats()["cached_credentials"] == 1

    # 同一 IP 用另一个账号失败 → 该 IP 缓存被清
    await auth.authenticate(settings, _basic("bob", "bad"), "1.2.3.4")
    assert auth.stats()["cached_credentials"] == 0


async def test_purge_all_cache(db_engine, settings: AppSettings):
    """改密码时清空全部缓存（跨 IP）。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    await auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4")
    await auth.authenticate(settings, _basic("alice", "secret"), "2.2.2.2")
    assert auth.stats()["cached_credentials"] == 2

    assert auth.purge_all_cache() == 2
    assert auth.stats()["cached_credentials"] == 0


async def test_cache_expires(db_engine, app_settings: AppSettings):
    """缓存过期后重新校验。"""
    await _make_user("alice", "secret")
    app_settings.dav_auth_cache_sec = 0.05
    auth = DavAuthenticator()
    await auth.authenticate(app_settings, _basic("alice", "secret"), "1.2.3.4")
    await asyncio.sleep(0.1)

    assert auth.purge_expired() == 1
    assert auth.stats()["cached_credentials"] == 0


# --------------------------------------------------------------------------
# 并发
# --------------------------------------------------------------------------


async def test_concurrent_auth_same_user(db_engine, settings: AppSettings):
    """同一账号并发认证全部成功。"""
    await _make_user("alice", "secret")
    auth = DavAuthenticator()
    results = await asyncio.gather(
        *[auth.authenticate(settings, _basic("alice", "secret"), "1.2.3.4") for _ in range(5)]
    )
    assert all(r.status == AUTH_OK for r in results)


# --------------------------------------------------------------------------
# 事务边界（PBKDF2 不得在事务内跑）
# --------------------------------------------------------------------------


async def test_pbkdf2_runs_outside_db_transaction(
    db_engine, settings: AppSettings, monkeypatch
):
    """验密码时不得持有 DB 事务（旧实现全程持有 ~42ms 写锁）。"""
    import contextlib

    from app.services import dav_auth as mod

    await _make_user("alice", "secret")
    active = {"count": 0}
    real_scope = mod.session_scope

    @contextlib.asynccontextmanager
    async def tracking_scope():
        active["count"] += 1
        try:
            async with real_scope() as scope:
                yield scope
        finally:
            active["count"] -= 1

    seen_active: list[int] = []
    real_verify = mod.verify_password

    def tracking_verify(pwd: str, hashed: str) -> bool:
        seen_active.append(active["count"])
        return real_verify(pwd, hashed)

    monkeypatch.setattr(mod, "session_scope", tracking_scope)
    monkeypatch.setattr(mod, "verify_password", tracking_verify)

    auth = DavAuthenticator()
    result = await auth.authenticate(settings, _basic("alice", "secret"), "9.9.9.9")
    assert result.status == AUTH_OK
    assert seen_active == [0], "PBKDF2 必须在事务外执行"


async def test_last_login_write_failure_still_authenticates(
    db_engine, settings: AppSettings, monkeypatch
):
    """last_login_at 是统计字段——写失败不能把成功认证变成 401。"""
    import contextlib

    from app.services import dav_auth as mod

    user_id = await _make_user("alice", "secret")
    real_scope = mod.session_scope
    calls: list[int] = []

    @contextlib.asynccontextmanager
    async def fail_second_scope():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("模拟 last_login_at 写失败")
        async with real_scope() as scope:
            yield scope

    monkeypatch.setattr(mod, "session_scope", fail_second_scope)

    auth = DavAuthenticator()
    result = await auth.authenticate(settings, _basic("alice", "secret"), "8.8.8.8")
    assert result.status == AUTH_OK
    assert len(calls) == 2  # 一次读 + 一次写

    async with real_scope() as db:
        row = await db.get(DavUser, user_id)
        assert row is not None and row.last_login_at is None
