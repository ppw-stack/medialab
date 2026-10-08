"""凭据对称加密（FR-7.1）。

AES-256-GCM，每条记录独立随机 IV，AAD 绑定记录归属以防记录替换攻击。
密钥只来自环境变量，永不落库、永不进日志。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: 密文格式版本号，便于未来算法升级时区分历史数据
CIPHER_VERSION: Final = b"\x01"
#: nonce 长度（GCM 标准 96bit）
NONCE_LEN: Final = 12


class DecryptionError(RuntimeError):
    """密文无法解密：AAD 不匹配、密钥错误或数据被篡改。"""


class CredentialCipher:
    """加解密门面。以 AAD 绑定 ``owner_type:owner_id``，防止跨记录搬运密文。"""

    __slots__ = ("_aead", "_key", "_key_version")

    def __init__(self, key_b64: str, key_version: int = 1) -> None:
        self._key = self._decode_key(key_b64)
        self._key_version = key_version
        self._aead = AESGCM(self._key)

    @property
    def key_version(self) -> int:
        return self._key_version

    @staticmethod
    def _decode_key(key_b64: str) -> bytes:
        """解码密钥。

        ``secrets.token_urlsafe(32)`` 产出的是**无填充**的 URL-safe base64
        （43 字符），而严格的 ``urlsafe_b64decode`` 要求补齐 ``=``。因此这里
        先补齐填充再解码——这也是文档里给出的生成命令能直接用的原因。
        """
        raw = key_b64.strip().encode()
        padded = raw + b"=" * (-len(raw) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded)
        except Exception as exc:
            raise ValueError("ENCRYPTION_KEY 非法 base64") from exc
        if len(decoded) != 32:
            raise ValueError(f"ENCRYPTION_KEY 需为 32 字节，实际 {len(decoded)}")
        return decoded

    @staticmethod
    def _aad_for(owner_type: str, owner_id: int | str) -> bytes:
        """附加认证数据：记录归属。改归属即解密失败。"""
        return f"seedhelper:{owner_type}:{owner_id}".encode()

    def encrypt(self, plaintext: str, *, owner_type: str, owner_id: int | str) -> str:
        nonce = os.urandom(NONCE_LEN)
        ct = self._aead.encrypt(
            nonce, plaintext.encode(), self._aad_for(owner_type, owner_id)
        )
        return base64.urlsafe_b64encode(CIPHER_VERSION + nonce + ct).decode()

    def decrypt(
        self, token: str, *, owner_type: str, owner_id: int | str
    ) -> str:
        try:
            blob = base64.urlsafe_b64decode(token.encode())
        except Exception as exc:
            raise DecryptionError("密文非法 base64") from exc
        if len(blob) < 1 + NONCE_LEN + 16 or blob[:1] != CIPHER_VERSION:
            raise DecryptionError("密文版本或长度非法")
        nonce = blob[1 : 1 + NONCE_LEN]
        ct = blob[1 + NONCE_LEN :]
        try:
            pt = self._aead.decrypt(
                nonce, ct, self._aad_for(owner_type, owner_id)
            )
        except InvalidTag as exc:
            raise DecryptionError(
                "解密失败：密钥不匹配、记录归属被篡改或数据损坏"
            ) from exc
        return pt.decode()

    def try_decrypt(
        self, token: str, *, owner_type: str, owner_id: int | str
    ) -> str | None:
        """宽容版：解不开返回 ``None``，用于轮换期间回退到旧密钥。"""
        try:
            return self.decrypt(token, owner_type=owner_type, owner_id=owner_id)
        except DecryptionError:
            return None


def build_cipher(settings: Any) -> CredentialCipher:
    """从配置对象构造加密器。

    接受任何带 ``encryption_key`` / ``key_version`` 属性的对象，避免
    ``core.config`` ↔ ``core.crypto`` 的循环导入。
    """
    key = settings.encryption_key
    key_str = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    return CredentialCipher(key_str, key_version=int(settings.key_version))


def mask_secret(value: str | None, *, keep: int = 0) -> str:
    """界面脱敏（FR-7）。默认全掩码；``keep`` 控制两端保留位数。"""
    if not value:
        return ""
    if keep <= 0 or len(value) <= keep * 2:
        return "*" * 8
    return f"{value[:keep]}{'*' * 6}{value[-keep:]}"