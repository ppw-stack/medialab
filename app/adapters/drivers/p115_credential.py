"""115 凭据的解码适配（驱动工厂用）。

把 DB 里存的密文还原成驱动能用的凭据。解密密钥来自
``CREDENTIAL_ENCRYPTION_SECRET`` 环境变量。
"""

from __future__ import annotations

import os

from app.adapters.drivers.credential_crypto import CredentialCryptoError, decrypt
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 解密密钥的环境变量名
SECRET_ENV = "CREDENTIAL_ENCRYPTION_SECRET"


def decode_credential(credential: str | None) -> str | None:
    """解密凭据。

    无凭据 → 返回 None（驱动据此报「未配置凭据」）。

    密文但未配置密钥 → 抛错（**不**静默返回密文当明文用——那会让
    115 API 收到一串垃圾，报错信息还难以定位）。

    明文（无前缀）→ 直接返回（兼容手工导入 / 测试 fixture）。
    """
    if not credential:
        return None
    if not credential.startswith("p115cipher:"):
        return credential

    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise CredentialCryptoError(
            f"凭据是密文但未配置 {SECRET_ENV}，无法解密"
        )
    try:
        return decrypt(credential, secret)
    except CredentialCryptoError:
        logger.exception("凭据解密失败")
        raise
