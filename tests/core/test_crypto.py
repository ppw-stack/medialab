"""凭据加密测试（FR-7.1 / AC-17）。"""

from __future__ import annotations

import base64

import pytest
from app.core.config import AppSettings
from app.core.crypto import CredentialCipher, DecryptionError, build_cipher, mask_secret
from pydantic import ValidationError


def test_roundtrip(test_key: str) -> None:
    c = CredentialCipher(test_key)
    token = c.encrypt("passkey-abc123", owner_type="site", owner_id="example-pt")
    assert token != "passkey-abc123"
    assert c.decrypt(token, owner_type="site", owner_id="example-pt") == "passkey-abc123"


def test_ciphertext_differs_each_time(test_key: str) -> None:
    """随机 nonce：相同明文两次加密结果不同。"""
    c = CredentialCipher(test_key)
    a = c.encrypt("same", owner_type="site", owner_id="s")
    b = c.encrypt("same", owner_type="site", owner_id="s")
    assert a != b


def test_wrong_owner_fails(test_key: str) -> None:
    """AAD 绑定归属：密文不能搬到别的记录上。"""
    c = CredentialCipher(test_key)
    token = c.encrypt("secret", owner_type="site", owner_id="a")
    with pytest.raises(DecryptionError, match="归属"):
        c.decrypt(token, owner_type="site", owner_id="b")
    with pytest.raises(DecryptionError):
        c.decrypt(token, owner_type="downloader", owner_id="a")


def test_wrong_key_fails(test_key: str) -> None:
    a = CredentialCipher(test_key)
    b = CredentialCipher(base64.urlsafe_b64encode(b"z" * 32).decode())
    token = a.encrypt("secret", owner_type="site", owner_id="s")
    with pytest.raises(DecryptionError):
        b.decrypt(token, owner_type="site", owner_id="s")


def test_tamper_detected(test_key: str) -> None:
    """GCM 认证：篡改密文必被拒。"""
    c = CredentialCipher(test_key)
    token = c.encrypt("secret-value", owner_type="site", owner_id="s")
    raw = bytearray(base64.urlsafe_b64decode(token.encode()))
    raw[-1] ^= 0xFF
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(DecryptionError):
        c.decrypt(tampered, owner_type="site", owner_id="s")


def test_try_decrypt_returns_none_on_failure(test_key: str) -> None:
    c = CredentialCipher(test_key)
    assert c.try_decrypt("garbage", owner_type="site", owner_id="s") is None


def test_invalid_key_length_rejected() -> None:
    with pytest.raises(ValueError, match="32"):
        CredentialCipher(base64.urlsafe_b64encode(b"short").decode())


def test_unpadded_token_urlsafe_accepted() -> None:
    """``secrets.token_urlsafe(32)`` 产出 43 字符**无填充** base64。

    这是文档与 ``.env.example`` 推荐的生成方式，必须开箱可用——
    该缺陷由 docker 冒烟验证发现（严格 b64decode 因缺 ``=`` 报
    "Incorrect padding" 导致容器无法启动）。
    """
    import secrets

    for _ in range(20):
        key = secrets.token_urlsafe(32)
        assert "=" not in key
        cipher = CredentialCipher(key)  # 不应抛异常
        token = cipher.encrypt("x", owner_type="site", owner_id="s")
        assert cipher.decrypt(token, owner_type="site", owner_id="s") == "x"


def test_padded_and_unpadded_equivalent() -> None:
    raw = b"k" * 32
    padded = base64.urlsafe_b64encode(raw).decode()
    unpadded = padded.rstrip("=")
    a = CredentialCipher(padded)
    b = CredentialCipher(unpadded)
    token = a.encrypt("secret", owner_type="site", owner_id="s")
    assert b.decrypt(token, owner_type="site", owner_id="s") == "secret"


def test_settings_accept_unpadded_key() -> None:
    """启动配置的校验必须与加密器一致（FR-7.1 fail-fast 不得误伤）。"""
    import secrets

    key = secrets.token_urlsafe(32)
    s = AppSettings(encryption_key=key, env="test")  # type: ignore[call-arg]
    assert build_cipher(s).key_version == 1


# ------------------------------------------------------------ fail-fast FR-7.1
def test_missing_key_aborts_startup(test_key: str) -> None:
    """缺密钥必须启动失败，禁止明文降级（AC-17）。"""
    with pytest.raises(ValidationError):
        AppSettings(encryption_key="", env="test")  # type: ignore[call-arg]


def test_malformed_key_rejected(test_key: str) -> None:
    with pytest.raises(Exception, match="ENCRYPTION_KEY"):
        AppSettings(encryption_key="not-base64!!!", env="test")  # type: ignore[call-arg]


def test_wrong_length_key_rejected() -> None:
    bad = base64.urlsafe_b64encode(b"0" * 16).decode()
    with pytest.raises(Exception, match="32"):
        AppSettings(encryption_key=bad, env="test")  # type: ignore[call-arg]


def test_build_cipher_from_settings(test_key: str) -> None:
    s = AppSettings(encryption_key=test_key, key_version=3, env="test")  # type: ignore[call-arg]
    assert build_cipher(s).key_version == 3


def test_mask_secret_never_leaks_full_value() -> None:
    assert "abcdefgh" not in mask_secret("abcdefgh1234")
    assert mask_secret("") == ""
    assert mask_secret("short") == "*" * 8
    assert mask_secret("abcdefgh1234", keep=2) == "ab******34"