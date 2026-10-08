"""Cookie-based session (轻量级；M6 引入 JWT + refresh 之前先用 secrets.token_urlsafe).

为什么不是纯 JWT：M5 阶段我们已有 1 个用户的最小可行面板；JWT 在单用户
场景下增加复杂度而收益有限。先用 ``Set-Cookie: HttpOnly; SameSite=Strict;
Secure`` + 服务端 hash 比对 + 24h 过期，对接前端。

FR-6.1 认证：单用户密码 + API Token (备用)。单用户场景下用 token 作为
``Authorization: Bearer`` 兼容 CLI/脚本登录。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Final

SESSION_COOKIE: Final = "seedhelper_session"
SESSION_MAX_AGE: Final = 24 * 3600  # 24h
HMAC_SALT: Final = b"seedhelper-v0.1-salt"  # 演示用；生产应来自 ENCRYPTION_KEY 派生


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256：盐 + 迭代。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"pbkdf2$120000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(dk_hex)
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
    return hmac.compare_digest(got, expected)


def make_session() -> str:
    """生成 cookie value。"""
    nonce = secrets.token_urlsafe(32)
    expires = int(time.time()) + SESSION_MAX_AGE
    sig = _sign(nonce, expires)
    return f"{nonce}.{expires}.{sig}"


def verify_session(token: str) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, expires, sig = parts
    try:
        exp = int(expires)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = _sign(nonce, exp)
    return hmac.compare_digest(sig, expected)


def _sign(nonce: str, expires: int) -> str:
    msg = f"{nonce}:{expires}".encode()
    return hmac.new(HMAC_SALT, msg, hashlib.sha256).hexdigest()
