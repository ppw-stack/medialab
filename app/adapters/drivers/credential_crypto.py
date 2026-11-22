"""凭据的加密 / 解密。

文件或 DB 字段里的网盘凭据 **必须加密存储**——凭据等价于账号密码。

格式（encrypt-then-MAC）::

    p115cipher:v2:<base64(iv || ciphertext || hmac)>

  - iv       = 16 字节随机 CBC IV
  - ciphertext = AES-128-CBC(plaintext)，key = SHA256(secret)[:16]
  - hmac     = HMAC-SHA256(mac_key, iv || ciphertext)，前 32 字节
  - mac_key  = SHA256(secret + ":mac")[:16]

**为什么用 encrypt-then-MAC 而不是裸 CBC**：

裸 CBC 解密会**先解密再验证填充**，攻击者能根据「填充是否合法」的差异
做 padding oracle 攻击——逐字节猜出明文。加上 HMAC 后先验 MAC 再解密，
篡改的密文直接被拒（MAC 不匹配），不存在填充预言机。

**版本号**：v2 = 带 HMAC 的格式。``p115cipher:v1:`` 是无认证的旧格式
（与 mediamanager 兼容），读到时按明文处理但**不再产出**——
本项目没有历史密文，直接从 v2 起步。

无前缀视为明文（手工导入 / 测试 fixture）。

**其它驱动的凭据**（如光鸭的 token 三件套）也复用这里的底层原语，
只是 scheme 前缀不同——见 :func:`encrypt_with_scheme` /
:func:`decrypt_with_scheme`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 解密密钥的环境变量名（加密 / 解密都要用它）
SECRET_ENV = "CREDENTIAL_ENCRYPTION_SECRET"

#: 当前方案。
#:
#: 前缀里的 ``p115`` 是**历史命名**（最初只为 115 实现），现作为默认方案
#: 保留：存量密文都带这个前缀，改名会让它们解不开。新代码按驱动传自己的
#: scheme（如光鸭的 ``gypcipher:v2:``）。
_SCHEME = "p115cipher:v2:"
#: 任意 p115cipher 前缀（用于识别「这是密文」）
_SCHEME_PREFIX = "p115cipher:"
_IV_LEN = 16
_KEY_LEN = 16  # AES-128
_MAC_LEN = 32  # HMAC-SHA256 截断
_AES_BLOCK = 16


class CredentialCryptoError(RuntimeError):
    """凭据加密 / 解密失败。"""


def _derive_keys(secret: str) -> tuple[bytes, bytes]:
    """从密钥串派生 (加密 key, MAC key)。

    两个 key 必须独立——复用同一个 key 会让 MAC 与加密互相泄漏信息。
    """
    if not secret:
        raise CredentialCryptoError("加密密钥未配置，无法加密 / 解密凭据")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    enc_key = digest[:_KEY_LEN]
    mac_key = hashlib.sha256(secret.encode("utf-8") + b":mac").digest()[:_KEY_LEN]
    return enc_key, mac_key


def _pad(data: bytes) -> bytes:
    """PKCS#7 填充。"""
    pad_len = _AES_BLOCK - (len(data) % _AES_BLOCK)
    return data + bytes([pad_len]) * pad_len


def _unpad(data: bytes) -> bytes:
    """去除 PKCS#7 填充。"""
    if not data or len(data) % _AES_BLOCK != 0:
        raise CredentialCryptoError("密文长度非法")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > _AES_BLOCK or pad_len > len(data):
        raise CredentialCryptoError("填充字节非法")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise CredentialCryptoError("填充字节不一致")
    return data[:-pad_len]


def _cipher() -> tuple[Any, Any, Any]:
    """取 AES-CBC cipher 对象工厂。

    返回 ``(Cipher, algorithms, modes)``——cryptography 的类型存根太松，
    这里用 Any 避免污染整个模块的类型信息。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise CredentialCryptoError(
            "cryptography 未安装：pip install cryptography"
        ) from exc
    return Cipher, algorithms, modes


def encrypt(plaintext: str, secret: str) -> str:
    """加密凭据，返 ``p115cipher:v2:<base64(iv || ct || hmac)>``。

    Args:
        plaintext: 明文凭据（cookie 字符串 / JSON 等）。
        secret: 加密密钥（任意串，内部派生 AES key 与 MAC key）。

    Raises:
        CredentialCryptoError: 加密失败。
    """
    Cipher, algorithms, modes = _cipher()
    enc_key, mac_key = _derive_keys(secret)

    iv = os.urandom(_IV_LEN)
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(_pad(plaintext.encode("utf-8"))) + encryptor.finalize()

    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()[:_MAC_LEN]
    return _SCHEME + base64.b64encode(iv + ct + tag).decode("ascii")


def decrypt(ciphertext: str, secret: str) -> str:
    """解密带前缀的密文；无前缀视为明文直接返回。

    Raises:
        CredentialCryptoError: 密钥不对 / 密文损坏 / 格式不支持。
    """
    if not ciphertext.startswith(_SCHEME_PREFIX):
        return ciphertext
    if not ciphertext.startswith(_SCHEME):
        raise CredentialCryptoError(
            f"不支持的加密方案：{ciphertext[:20]!r}（仅支持 v2）"
        )

    Cipher, algorithms, modes = _cipher()
    enc_key, mac_key = _derive_keys(secret)

    try:
        raw = base64.b64decode(ciphertext[len(_SCHEME) :])
    except (ValueError, TypeError) as exc:
        raise CredentialCryptoError("密文不是合法 base64") from exc
    if len(raw) <= _IV_LEN + _MAC_LEN:
        raise CredentialCryptoError("密文长度不足")

    iv = raw[:_IV_LEN]
    ct = raw[_IV_LEN : -_MAC_LEN]
    tag = raw[-_MAC_LEN:]

    # **先验 MAC 再解密**——encrypt-then-MAC，杜绝 padding oracle。
    expected_tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()[:_MAC_LEN]
    if not hmac.compare_digest(tag, expected_tag):
        raise CredentialCryptoError("完整性校验失败：密钥不匹配或密文被篡改")

    try:
        decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
        pt = decryptor.update(ct) + decryptor.finalize()
    except Exception as exc:
        raise CredentialCryptoError("解密失败：密钥不匹配或密文损坏") from exc
    try:
        return _unpad(pt).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialCryptoError("解密结果不是合法 UTF-8") from exc


def encrypt_with_scheme(plaintext: str, secret: str, scheme: str) -> str:
    """用指定 scheme 前缀加密（其它驱动复用同一套原语）。

    Args:
        plaintext: 明文。
        secret: 加密密钥。
        scheme: 方案前缀（如 ``gypcipher:v2:``）。

    Raises:
        CredentialCryptoError: 加密失败。
    """
    Cipher, algorithms, modes = _cipher()
    enc_key, mac_key = _derive_keys(secret)

    iv = os.urandom(_IV_LEN)
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(_pad(plaintext.encode("utf-8"))) + encryptor.finalize()

    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()[:_MAC_LEN]
    return scheme + base64.b64encode(iv + ct + tag).decode("ascii")


def decrypt_with_scheme(
    ciphertext: str, secret: str, scheme: str, scheme_prefix: str
) -> str:
    """用指定 scheme 前缀解密（其它驱动复用同一套原语）。

    无前缀（明文）→ 直接返回。

    Raises:
        CredentialCryptoError: 密钥不对 / 密文损坏 / 格式不支持。
    """
    if not ciphertext.startswith(scheme_prefix):
        return ciphertext
    if not ciphertext.startswith(scheme):
        raise CredentialCryptoError(
            f"不支持的加密方案：{ciphertext[:20]!r}（仅支持 v2）"
        )

    Cipher, algorithms, modes = _cipher()
    enc_key, mac_key = _derive_keys(secret)

    try:
        raw = base64.b64decode(ciphertext[len(scheme) :])
    except (ValueError, TypeError) as exc:
        raise CredentialCryptoError("密文不是合法 base64") from exc
    if len(raw) <= _IV_LEN + _MAC_LEN:
        raise CredentialCryptoError("密文长度不足")

    iv = raw[:_IV_LEN]
    ct = raw[_IV_LEN : -_MAC_LEN]
    tag = raw[-_MAC_LEN:]

    expected_tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()[:_MAC_LEN]
    if not hmac.compare_digest(tag, expected_tag):
        raise CredentialCryptoError("完整性校验失败：密钥不匹配或密文被篡改")

    try:
        decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
        pt = decryptor.update(ct) + decryptor.finalize()
    except Exception as exc:
        raise CredentialCryptoError("解密失败：密钥不匹配或密文损坏") from exc
    try:
        return _unpad(pt).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialCryptoError("解密结果不是合法 UTF-8") from exc
