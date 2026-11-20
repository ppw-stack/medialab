"""站点流控的轻量适配（移植 torrent/parser 的依赖）。

MoviePilot 的流控由站点配置 + 资源模块的远端冷却共同决定；本项目已有
站点熔断（``BreakerTripped``）。这里实现**本地最小间隔 + 冷却**，保证移植
代码可以同步/异步调用，不引入 DB 查询。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Mapping

from app.adapters.sites.indexer.compat.exceptions import RateLimitExceededException

_LOCK = threading.Lock()
#: domain -> 上次请求时刻
_LAST: dict[str, float] = {}
#: domain -> 冷却截止时刻
_COOLDOWN: dict[str, float] = {}

_RATE_HINT_RE = re.compile(
    r"(429|too many requests|rate limit|频繁|請稍後|请稍后|稍后再试|稍後再試)",
    re.IGNORECASE,
)


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or url).lower()
    except ValueError:
        # urlsplit 对非法 IPv6 之类会抛 ValueError。
        return str(url)


def site_request_config(url: str, site_id: int | None = None) -> dict[str, object]:
    """返回一个只含身份与流控字段的配置快照。"""
    return {
        "id": site_id,
        "domain": _domain_of(url),
        "limit_seconds": 0,
        "limit_interval": 0,
        "limit_count": 0,
    }


def site_request_limits(site: Mapping[str, object]) -> dict[str, object]:
    """构造兼容层共用的流控配置（不含凭据）。"""
    return {key: site.get(key) for key in ("id", "domain", "limit_seconds", "limit_interval", "limit_count")}


def _cooldown_reason(domain: str) -> str | None:
    until = _COOLDOWN.get(domain, 0.0)
    remaining = until - time.time()
    if remaining > 0:
        return f"站点 {domain} 冷却中，剩余 {int(remaining)} 秒"
    return None


def wait_for_site_request(site: Mapping[str, object]) -> None:
    """同步等待站点额度；冷却期内直接抛限流异常。"""
    domain = str(site.get("domain") or "")
    reason = _cooldown_reason(domain)
    if reason:
        raise RateLimitExceededException(reason)
    interval = float(site.get("limit_seconds") or 0)
    with _LOCK:
        last = _LAST.get(domain, 0.0)
        wait = max(0.0, last + interval - time.time())
        if wait <= 0:
            _LAST[domain] = time.time()
    if wait > 0:
        time.sleep(wait)
        with _LOCK:
            _LAST[domain] = time.time()


async def async_wait_for_site_request(site: Mapping[str, object]) -> None:
    """异步等待站点额度。"""
    domain = str(site.get("domain") or "")
    reason = _cooldown_reason(domain)
    if reason:
        raise RateLimitExceededException(reason)
    interval = float(site.get("limit_seconds") or 0)
    with _LOCK:
        last = _LAST.get(domain, 0.0)
        wait = max(0.0, last + interval - time.time())
        if wait <= 0:
            _LAST[domain] = time.time()
    if wait > 0:
        await asyncio.sleep(wait)
        with _LOCK:
            _LAST[domain] = time.time()


def defer_site_request(site: Mapping[str, object], reason: str) -> None:
    """把远端限流提示转成同域冷却。"""
    domain = str(site.get("domain") or "")
    seconds = 3600.0 if _RATE_HINT_RE.search(reason or "") else 300.0
    with _LOCK:
        _COOLDOWN[domain] = max(_COOLDOWN.get(domain, 0.0), time.time() + seconds)
