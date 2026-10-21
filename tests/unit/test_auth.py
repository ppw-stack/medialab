"""认证工具测试（Phase 0）。

认证是安全底线：PBKDF2 迭代数、session 签名、过期判定、时序均衡
路径都不能被后续重构改坏。
"""

from __future__ import annotations

import time

import pytest
from app.core.auth import (
    PBKDF2_ITERATIONS,
    SESSION_MAX_AGE,
    hash_password,
    init_hmac_key,
    make_session,
    verify_password,
    verify_session,
)


@pytest.fixture(autouse=True)
def _hmac_key() -> None:
    """每个用例用固定 key 派生 session HMAC（否则会抛「未初始化」）。"""
    init_hmac_key("unit-test-token-32-chars-abcdefgh")


def test_hash_password_pbkdf2_format() -> None:
    hashed = hash_password("secret-pass")
    parts = hashed.split("$")
    assert len(parts) == 4
    algo, iters, salt_hex, dk_hex = parts
    assert algo == "pbkdf2"
    assert int(iters) == PBKDF2_ITERATIONS
    assert len(salt_hex) == 32  # 16 字节盐的 hex
    assert len(dk_hex) == 64  # SHA-256 32 字节


def test_hash_password_salt_is_random() -> None:
    a = hash_password("same-pass")
    b = hash_password("same-pass")
    assert a != b, "盐应随机，同密码两次哈希不应相同"
    assert verify_password("same-pass", a)
    assert verify_password("same-pass", b)


def test_verify_password_correct() -> None:
    assert verify_password("correct", hash_password("correct")) is True


def test_verify_password_wrong() -> None:
    assert verify_password("wrong", hash_password("correct")) is False


def test_verify_password_malformed_storage_no_raise() -> None:
    for bad in ("", "garbage", "pbkdf2$notanint$aa$bb", "a$b$c", "md5$1$aa$bb"):
        assert verify_password("x", bad) is False


def test_verify_password_unknown_algorithm_rejected() -> None:
    stored = "md5$600000$" + "00" * 16 + "$" + "00" * 32
    assert verify_password("x", stored) is False


def test_make_session_verifiable() -> None:
    token = make_session()
    assert verify_session(token) is True


def test_make_session_has_three_segments() -> None:
    token = make_session()
    assert len(token.split(".")) == 3


def test_verify_session_rejects_empty_and_malformed() -> None:
    assert verify_session("") is False
    assert verify_session("garbage") is False
    assert verify_session("a.b") is False
    assert verify_session("a.b.c") is False  # 签名不匹配


def test_verify_session_rejects_expired() -> None:
    token = make_session()
    nonce, _expires, sig = token.split(".")
    expired = f"{nonce}.{int(time.time()) - 10}.{sig}"
    assert verify_session(expired) is False


def test_verify_session_rejects_tampered_signature() -> None:
    token = make_session()
    nonce, expires, _sig = token.split(".")
    tampered = f"{nonce}.{expires}.{'0' * 64}"
    assert verify_session(tampered) is False


def test_verify_session_invalid_after_key_rotation() -> None:
    token = make_session()
    assert verify_session(token) is True
    init_hmac_key("a-completely-different-token-32c")
    assert verify_session(token) is False


def test_session_max_age_is_24h() -> None:
    assert SESSION_MAX_AGE == 24 * 3600
