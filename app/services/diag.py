"""115 驱动的直链诊断。

**为什么需要**：115 直链 URL 里的 ``f`` 参数语义文档有述但**实际取值未实测**
（见 ``docs/design.md`` §11）：

- ``f=0`` —— 无要求
- ``f=1`` —— 请求直链时的 UA 必须与访问直链时的 UA 一致
- ``f=3`` —— 还要额外携带 ``Set-Cookie``

``f=1`` 决定了缓存**必须按 UA 分桶**（本项目已如此实现）；
若实际是 ``f=3``，中转层还得回吐 Cookie 头。所以这个值必须实测确认，
不能靠猜。

本模块提供「换一条直链并解析其参数」的诊断能力。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.adapters.drivers.base import DriverError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class DirectLinkDiag:
    """直链诊断结果。"""

    #: 直链 URL（**脱敏**：query 里的凭据类参数打码）
    url_masked: str
    #: ``t`` —— 过期时间戳
    expires_at: int | None
    #: ``f`` —— 请求时要求携带的请求头（0=无 / 1=要 UA / 3=要 UA+Cookie）
    flag: str | None
    #: ``c`` —— 允许同时打开次数（0=无限）
    concurrent: str | None
    #: ``u`` —— 用户 id
    user_id: str | None
    #: 直链是否绑定 UA（由 f 推出）
    ua_required: bool
    #: 是否需要 Cookie（由 f 推出）
    cookie_required: bool
    #: 剩余有效秒数（相对当前时刻）
    ttl_seconds: int | None


def parse_direct_link(url: str, *, now: float | None = None) -> DirectLinkDiag:
    """解析直链 URL 的参数。

    Args:
        url: 直链 URL。
        now: 当前时间戳（默认取系统时间）。

    Returns:
        诊断结果。
    """
    import time

    current = int(now if now is not None else time.time())
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))

    flag = query.get("f")
    ua_required = flag == "1" or flag == "3"
    cookie_required = flag == "3"

    expires_raw = query.get("t", "")
    expires_at: int | None = None
    ttl_seconds: int | None = None
    if expires_raw.isascii() and expires_raw.isdigit():
        expires_at = int(expires_raw)
        ttl_seconds = expires_at - current

    return DirectLinkDiag(
        url_masked=_mask_url(url),
        expires_at=expires_at,
        flag=flag,
        concurrent=query.get("c"),
        user_id=query.get("u"),
        ua_required=ua_required,
        cookie_required=cookie_required,
        ttl_seconds=ttl_seconds,
    )


def _mask_url(url: str) -> str:
    """脱敏直链 URL。

    直链里的 ``t`` / ``u`` / ``c`` 本身不算密钥，但完整 URL **可以分享**，
    因此诊断输出里打码，避免截图泄露。
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    masked = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in ("t", "u", "c"):
            masked.append(f"{key}=***")
        else:
            masked.append(f"{key}={value}")
    query = "&".join(masked)
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{query}"


async def diagnose_direct_link(
    driver: Any,
    remote_id: str,
    user_agent: str,
) -> DirectLinkDiag:
    """换一条直链并诊断其参数。

    Args:
        driver: 115 驱动实例。
        remote_id: pickcode。
        user_agent: 用于换直链的 UA。

    Raises:
        DriverError: 换直链失败。
    """
    try:
        link = await driver.get_direct_link(remote_id, user_agent)
    except DriverError:
        raise
    except Exception as exc:
        raise DriverError(f"诊断失败：{exc}", kind="network") from exc
    return parse_direct_link(link.url)
