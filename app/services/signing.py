"""中转 URL 的 HMAC 签名。

URL 形态::

    http://<host>:<port>/302/<media_id>/<sig>

    sig = base64url(HMAC-SHA256(secret, str(media_id)))[:22]

设计要点：

- URL 里**不暴露任何网盘语义**（只有数字 ``media_id`` + 签名），
  不泄露 pickcode。
- 只签 ``media_id``：它是 ``media_files`` 的**全局自增主键**，跨驱动唯一，
  因此签名天然绑定到具体条目，无需再掺 driver_id。
- **签名在查库之前校验**——拿一个不存在的 media_id 来试，返回的是
  403（签名不符）而非 404（条目不存在），不泄露条目是否存在。
- 比较用 :func:`hmac.compare_digest`（常量时间），防时序攻击。
- **无有效期**：签名不绑定时间，STRM 文件因此永不过期，无需定期重写。
  代价是泄露后长期可用——用「密钥轮换」止血（见 :mod:`app.services.media`）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

#: 签名的 base64url 长度（22 字符 ≈ 128 bit 熵，足够抗枚举）
SIG_LEN = 22


def _b64(raw: bytes) -> str:
    """base64url 编码（无 padding）。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign(secret: str, media_id: int) -> str:
    """计算中转 URL 的签名。"""
    payload = str(media_id).encode()
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64(digest)[:SIG_LEN]


def verify(secret: str, media_id: int, sig: str) -> bool:
    """校验签名（常量时间比较）。"""
    expected = sign(secret, media_id)
    return hmac.compare_digest(expected, sig)


def ua_hash(user_agent: str) -> str:
    """User-Agent 的哈希（缓存 key 用）。

    **不存明文 UA**：UA 可能含设备指纹，且表里存明文会随 UA 变动无限增长。
    空 UA（Infuse 等客户端不发 UA）也参与分桶——它与某个具体 UA 是不同的
    访问者，直链不通用。
    """
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()


def build_redirect_url(base_url: str, media_id: int, secret: str) -> str:
    """构造完整的中转 URL。"""
    sig = sign(secret, media_id)
    return f"{base_url.rstrip('/')}/302/{media_id}/{sig}"
