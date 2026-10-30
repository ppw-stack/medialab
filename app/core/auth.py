"""Cookie-based session（轻量级；M6 引入 JWT + refresh 之前先用 secrets.token_urlsafe）。

为什么不是纯 JWT：单用户场景下 JWT 增加复杂度而收益有限。用
``Set-Cookie: HttpOnly; SameSite=Strict; Secure`` + 服务端 hash 比对
+ 24h 过期，对接前端。

FR-6.1 认证：单用户密码 + API Token (备用)。单用户场景下用 token 作为
``Authorization: Bearer`` 兼容 CLI/脚本登录。

**合并说明（strmmanger 并入）**：

- cookie 名统一为 :data:`SESSION_COOKIE`（原 ptHelper 为 ``seedhelper_session``，
  strmmanger 为 ``strmmanger_session``）。**旧 cookie 会失效**，用户需重新登录
  一次——这是刻意的：两个来源的 cookie 签名算法不同（pt 用 ``nonce:expires``，
  strm 用 ``nonce.expires``），无法兼容解析。
- 签名 payload 统一为 ``f"{nonce}.{expires}"``（点分隔）——与 strmmanger 一致。
- 密码校验新增**明文兼容分支**：存的不是 ``pbkdf2$`` 开头时按明文常量时间
  比较，供首次启动时从 env 明文自动升级为哈希（见 :func:`is_hashed`）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Final

SESSION_COOKIE: Final = "medialab_session"
SESSION_MAX_AGE: Final = 24 * 3600  # 24h
#: OWASP 2023+ 推荐 PBKDF2-HMAC-SHA256 ≥ 600,000 次
PBKDF2_ITERATIONS: Final = 600_000

#: HMAC key 由 init_hmac_key() 派生；未派生时为 None → make/verify 直接抛错
_hmac_key: bytes | None = None


def init_hmac_key(web_token: str) -> None:
    """从 WEB_TOKEN 派生 session HMAC 密钥（防硬编码盐伪造 cookie）。"""
    global _hmac_key
    _hmac_key = hashlib.sha256(web_token.encode() + b"session-hmac").digest()


def _require_key() -> bytes:
    if _hmac_key is None:
        raise RuntimeError(
            "HMAC key 未初始化：必须在 lifespan 中先调用 init_hmac_key()"
        )
    return _hmac_key


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256：盐 + 迭代（OWASP 推荐 600k）。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与存储的值。

    支持两种存储形态：

    - ``pbkdf2$...`` —— 已哈希（推荐）
    - 其它 —— **明文**，直接常量时间比较（首次启动会用明文，
      启动时自动升级为 pbkdf2 哈希）

    明文分支用 :func:`hmac.compare_digest` 而非 ``==``——避免时序攻击
    逐字符猜密码。
    """
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        # 明文密码：常量时间比较
        return hmac.compare_digest(password, stored)
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
    except (ValueError, TypeError):
        return False
    if algo != "pbkdf2":
        return False
    return hmac.compare_digest(got, expected)


def is_hashed(stored: str) -> bool:
    """判断存储值是否已是 pbkdf2 哈希。"""
    return stored.startswith("pbkdf2$")


def make_session() -> str:
    """生成 cookie value：``<nonce>.<expires>.<sig>``。"""
    nonce = secrets.token_urlsafe(32)
    expires = int(time.time()) + SESSION_MAX_AGE
    sig = _sign(nonce, expires)
    return f"{nonce}.{expires}.{sig}"


def verify_session(token: str) -> bool:
    """校验 session cookie 值。格式不符 / 过期 / 签名不符一律 False。"""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, expires_raw, sig = parts
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    try:
        expected = _sign(nonce, expires)
    except RuntimeError:
        # HMAC 未初始化 = 服务端配置错误 → 拒绝所有 session
        return False
    return hmac.compare_digest(sig, expected)


def _sign(nonce: str, expires: int) -> str:
    payload = f"{nonce}.{expires}".encode()
    return hmac.new(_require_key(), payload, hashlib.sha256).hexdigest()


def verify_bearer(token: str) -> bool:
    """校验 ``Authorization: Bearer <token>`` 里的 token。

    **两种口径都接受**（合并说明）：

    1. **API Token**（``WEB_TOKEN`` 本身）——辅种域的约定，供 CLI / 脚本
       长期使用
    2. **Session cookie 值**（``<nonce>.<expires>.<sig>``）——媒体库域的
       约定，同一份凭据两种用法

    返回 True 表示鉴权通过。
    """
    if not token:
        return False
    # 口径 1：API Token（常量时间比较）
    from app.core.config import get_settings

    settings = get_settings()
    api_token = settings.web_token.get_secret_value() if settings.web_token else ""
    if api_token and hmac.compare_digest(token, api_token):
        return True
    # 口径 2：session cookie 值
    return verify_session(token)
