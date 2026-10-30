"""服务单例工厂。

集中管理进程级单例（直链缓存、中转服务）。**不放在 ``app.core``**——
这些服务依赖 DB 与配置，属于业务层。
"""

from __future__ import annotations

from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.services.link_cache import LinkCache
from app.services.redirect import RedirectService

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
    logger.info("服务初始化完成 ua_policy=%s", resolved.ua_policy)


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
