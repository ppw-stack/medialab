"""服务单例工厂。

集中管理进程级单例（直链缓存、中转服务）。**不放在 ``app.core``**——
这些服务依赖 DB 与配置，属于业务层。
"""

from __future__ import annotations

from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.services.link_cache import LinkCache
from app.services.redirect import RedirectResult, RedirectService

logger = get_logger(__name__)

_link_cache: LinkCache | None = None
_redirect_service: RedirectService | None = None


def init_services(settings: AppSettings | None = None) -> None:
    """初始化服务单例（幂等）。"""
    global _link_cache, _redirect_service
    if _link_cache is not None:
        return
    resolved = settings or get_settings()
    _link_cache = LinkCache(resolved)
    _redirect_service = RedirectService(resolved, _link_cache)
    logger.info(
        "服务初始化完成：%s",
        "link_cache+redirect",
        extra={
            "op": "svc_init",
            "components": ["link_cache", "redirect"],
            "link_cache_l1_size": resolved.link_cache_l1_size,
            "link_cache_ttl_margin_sec": resolved.link_cache_ttl_margin_sec,
        },
    )


def get_link_cache() -> LinkCache:
    """获取直链缓存单例。"""
    if _link_cache is None:
        raise RuntimeError("服务未初始化：必须在 lifespan 中先调用 init_services()")
    return _link_cache


def get_redirect_service() -> RedirectService:
    """获取中转服务单例。"""
    if _redirect_service is None:
        raise RuntimeError("服务未初始化：必须在 lifespan 中先调用 init_services()")
    return _redirect_service


def reset_services() -> None:
    """清空服务单例（测试用）。"""
    global _link_cache, _redirect_service
    _link_cache = None
    _redirect_service = None


async def resolve_redirect(
    media_id: int, sig: str, request_ua: str
) -> RedirectResult:
    """解析中转请求，返回重定向目标（**自开事务**）。

    API 层不碰 DB——它只调本函数。原先路由里直接 ``session_scope()``
    再调 ``service.resolve(db, ...)``，违反了「``api`` 层不依赖 ``db``」
    的分层约束（见 ``tests/unit/test_layering.py``）。
    """
    from app.db.session import session_scope

    service = get_redirect_service()
    async with session_scope() as db:
        return await service.resolve(db, media_id, sig, request_ua)
