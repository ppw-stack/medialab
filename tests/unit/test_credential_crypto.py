"""凭据加密的契约测试。"""

from __future__ import annotations

import base64

import pytest
from app.adapters.drivers.credential_crypto import (
    CredentialCryptoError,
    decrypt,
    encrypt,
)

SECRET = "a-very-secret-key"


class TestEncryptDecrypt:
    def test_round_trip(self) -> None:
        """加密后能解回原文。"""
        plain = "UID=123; CID=456; SEID=789"
        assert decrypt(encrypt(plain, SECRET), SECRET) == plain

    def test_ciphertext_has_scheme_prefix(self) -> None:
        assert encrypt("x", SECRET).startswith("p115cipher:v2:")

    def test_tampering_is_detected(self) -> None:
        """密文被篡改 → 完整性校验失败（不是解密出垃圾）。

        这是 encrypt-then-MAC 的意义：篡改的密文**一定**被拒，
        不存在「解密成功但内容被改」的情况。
        """
        encrypted = encrypt("secret value", SECRET)
        raw = bytearray(base64.b64decode(encrypted[len("p115cipher:v2:") :]))
        # 翻转密文中间的一个字节
        raw[len(raw) // 2] ^= 0xFF
        tampered = "p115cipher:v2:" + base64.b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(CredentialCryptoError):
            decrypt(tampered, SECRET)

    def test_ciphertext_differs_each_time(self) -> None:
        """随机 IV —— 同一明文两次加密结果不同（防密文比对攻击）。"""
        a = encrypt("same", SECRET)
        b = encrypt("same", SECRET)
        assert a != b

    def test_plaintext_without_prefix_passes_through(self) -> None:
        """无前缀视为明文（兼容手工导入 / 测试 fixture）。"""
        assert decrypt("UID=1; CID=2", SECRET) == "UID=1; CID=2"

    def test_wrong_secret_fails(self) -> None:
        encrypted = encrypt("secret", SECRET)
        with pytest.raises(CredentialCryptoError):
            decrypt(encrypted, "wrong-key")

    def test_empty_secret_raises(self) -> None:
        with pytest.raises(CredentialCryptoError):
            encrypt("x", "")

    def test_empty_plaintext_round_trip(self) -> None:
        assert decrypt(encrypt("", SECRET), SECRET) == ""

    def test_unicode_round_trip(self) -> None:
        plain = "路径=/我的电影/测试.mkv"
        assert decrypt(encrypt(plain, SECRET), SECRET) == plain

    def test_long_credential_round_trip(self) -> None:
        """长凭据（跨多个 AES 块）。"""
        plain = "x" * 5000
        assert decrypt(encrypt(plain, SECRET), SECRET) == plain

    def test_corrupted_ciphertext_raises(self) -> None:
        encrypted = encrypt("secret", SECRET)
        corrupted = encrypted[:-4] + "AAAA"
        with pytest.raises(CredentialCryptoError):
            decrypt(corrupted, SECRET)

    def test_unsupported_scheme_raises(self) -> None:
        with pytest.raises(CredentialCryptoError):
            decrypt("p115cipher:v9:abc", SECRET)

    def test_legacy_v1_scheme_rejected(self) -> None:
        """v1（无认证的旧格式）不再接受——它有 padding oracle 风险。"""
        with pytest.raises(CredentialCryptoError):
            decrypt("p115cipher:v1:YWJjZGVm", SECRET)
