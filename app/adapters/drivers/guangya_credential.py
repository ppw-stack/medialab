"""光鸭盘凭据的加解密适配。

光鸭的凭据不止一个 cookie，而是一组：

- ``access_token`` —— 业务接口的 Bearer
- ``refresh_token`` —— 换新 access_token 用
- ``device_id`` —— 设备标识（扫码 / 短信登录时绑定）
- ``expires_at`` —— access_token 过期时间（unix 秒）

故明文用 **JSON** 序列化，整体走与 115 相同的 encrypt-then-MAC 方案，
只是 scheme 前缀不同（``gypcipher:v2:``）——这样能一眼看出库里存的是哪家的
凭据，也避免两种凭据被误当成同一种解密。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from app.adapters.drivers.credential_crypto import (
    CredentialCryptoError,
    decrypt_with_scheme,
    encrypt_with_scheme,
)

#: 加密方案前缀
SCHEME = "gypcipher:v2:"
#: 任意 gypcipher 前缀（用于识别「这是密文」）
SCHEME_PREFIX = "gypcipher:"

#: 解密密钥的环境变量名（与 115 共用同一个密钥）
SECRET_ENV = "CREDENTIAL_ENCRYPTION_SECRET"


@dataclass
class GuangyaCredential:
    """光鸭凭据。"""

    access_token: str
    refresh_token: str = ""
    device_id: str = ""
    expires_at: int = 0

    # -- 客户端覆盖层（可选）--
    #: macOS / Windows 客户端登录拿到的 access_token。
    #:
    #: **为什么要单独存**：服务端按 token 的 ``aud``（客户端 clientId）
    #: 判定「是不是客户端」，客户端 token 才能取非会员的大文件直链
    #: （Web token 返回 ``code=254``）。覆盖层只用于换直链，扫描 / 列目录
    #: 等仍走 Web token——客户端 token 只有 2 小时有效期，过期回退即可，
    #: 不会把整个驱动的登录态拖死。
    client_access_token: str = ""
    #: 客户端 token 的过期时刻（unix 秒）
    client_expires_at: int = 0
    #: 客户端设备 id（取自客户端 ``app-lifecycle/state.json`` 的 deviceId）
    client_device_id: str = ""

    def to_json(self) -> str:
        """序列化成 JSON（加密前的明文）。"""
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "device_id": self.device_id,
                "expires_at": self.expires_at,
                "client_access_token": self.client_access_token,
                "client_expires_at": self.client_expires_at,
                "client_device_id": self.client_device_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> GuangyaCredential:
        """从 JSON 反序列化。

        Raises:
            CredentialCryptoError: JSON 非法。
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialCryptoError("光鸭凭据不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise CredentialCryptoError("光鸭凭据格式错误（不是对象）")
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            device_id=str(data.get("device_id") or ""),
            expires_at=int(data.get("expires_at") or 0),
            client_access_token=str(data.get("client_access_token") or ""),
            client_expires_at=int(data.get("client_expires_at") or 0),
            client_device_id=str(data.get("client_device_id") or ""),
        )


def encode_credential(cred: GuangyaCredential, secret: str) -> str:
    """加密凭据，返 ``gypcipher:v2:<base64(iv || ct || hmac)>``。

    Raises:
        CredentialCryptoError: 加密失败。
    """
    return encrypt_with_scheme(cred.to_json(), secret, SCHEME)


def decode_credential(credential: str | None) -> GuangyaCredential | None:
    """解密凭据。

    无凭据 → 返回 None（驱动据此报「未配置凭据」）。

    明文（无前缀）→ 按 JSON 解析（兼容手工导入 / 测试 fixture）。

    Raises:
        CredentialCryptoError: 密文但未配置密钥 / 解密失败 / 明文非法。
    """
    if not credential:
        return None
    if not credential.startswith(SCHEME_PREFIX):
        return GuangyaCredential.from_json(credential)

    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise CredentialCryptoError(
            f"凭据是密文但未配置 {SECRET_ENV}，无法解密"
        )

    plain = decrypt_with_scheme(credential, secret, SCHEME, SCHEME_PREFIX)
    return GuangyaCredential.from_json(plain)


def credential_to_str(cred: GuangyaCredential | None) -> str:
    """把凭据转成可存库的字符串（无凭据 → 空串）。"""
    if cred is None:
        return ""
    return cred.to_json()


def credential_from_str(raw: str | None) -> GuangyaCredential | None:
    """从字符串读出凭据（无内容 → None）。"""
    if not raw:
        return None
    return GuangyaCredential.from_json(raw)
