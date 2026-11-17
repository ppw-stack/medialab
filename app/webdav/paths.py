"""DAV 根目录解析的协议层兼容入口。

实现已上移到 :mod:`app.services.dav_root`（services 层）—— 本模块只做
re-export，避免 ``services → webdav`` 的逆向依赖（见
``tests/unit/test_layering.py`` 的分层约束）。

这里保留 ``_root_cache`` 等私有符号的转发，是因为既有的 WebDAV 集成测试
直接操作它来注入根目录；转发的是 services 层的**同一个对象**，语义不变。
"""

from app.services.dav_root import (
    CACHE_TTL_SEC,
    KEY_ROOT_DRIVER_ID,
    _Cache,
    _get_configured_driver_id,
    _resolve_uncached,
    _root_cache,
    invalidate_root_cache,
    resolve_dav_root,
    set_root_driver_id,
)

__all__ = [
    "CACHE_TTL_SEC",
    "KEY_ROOT_DRIVER_ID",
    "_Cache",
    "_get_configured_driver_id",
    "_resolve_uncached",
    "_root_cache",
    "invalidate_root_cache",
    "resolve_dav_root",
    "set_root_driver_id",
]
