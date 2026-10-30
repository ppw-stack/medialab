"""把 WebDAV 挂到 FastAPI 应用上。

**挂载顺序很关键**：``app.mount()`` 的路由按**注册顺序**匹配，而 SPA
兜底路由（``/{full_path:path}``）会吞掉一切路径。所以 DAV 必须在
SPA 兜底**之前**挂载 —— 即在 :func:`app.main.create_app` 里、
:func:`_mount_frontend` 之前调用本模块。

**为什么这里能同步挂载**：DAV 根目录是**每请求**由守卫解析并注入的
（见 :mod:`app.webdav.guard`），provider 的构造根只是兜底。所以
挂载时不需要查库 —— 不必为了拿根目录而把挂载推迟到 lifespan（那样就
来不及排在 SPA 路由前面了）。

**wsgidav 跑在线程池里**：wsgidav 是同步 WSGI 应用，用
``a2wsgi.WSGIMiddleware`` 包一层接到 ASGI。它的线程池与主 event loop
分离 —— 主循环不会被 DAV 请求阻塞。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.webdav.app import build_dav_app
from app.webdav.guard import DavGuard
from app.core.config import AppSettings
from app.core.logging import get_logger

logger = get_logger(__name__)


def mount_dav(app: FastAPI, settings: AppSettings) -> bool:
    """挂载 WebDAV（若已启用）。

    Args:
        app: FastAPI 应用。
        settings: 应用配置。

    Returns:
        True = 已挂载；False = 未启用。
    """
    if not settings.dav_enabled:
        logger.info("WebDAV 未启用（DAV_ENABLED=false）")
        return False

    mount_path = settings.dav_mount_path

    # 兜底根目录：真实根由守卫每请求解析并注入。
    # 这里给一个必定存在的目录，只为满足 wsgidav 构造时的校验。
    fallback_root = tempfile.gettempdir()

    dav_app = build_dav_app(settings, fallback_root)

    # 守卫套在 wsgidav 外面：认证 / 限流 / 审计都在主 event loop 里做
    #
    # 类型标注用 Any —— a2wsgi 有自己的 scope 类型（HTTPScope 等），
    # 与 starlette 的 Scope 不完全一致，强行标注会让 mypy 报不兼容。
    wsgi_app: Any = WSGIMiddleware(dav_app, workers=settings.dav_wsgi_workers)
    guarded = DavGuard(wsgi_app, settings=settings)

    app.mount(mount_path, guarded)

    # **补一条 `/dav` → `/dav/` 的重定向**。
    #
    # Starlette 的 ``Mount`` 把路径编译成 ``/dav/{path:path}`` —— 要求
    # ``/dav`` 后面**必须有**斜杠。所以裸的 ``/dav`` 匹配不上挂载点，
    # 会落到别处 → **405 Method Not Allowed**。
    #
    # 部分客户端会先请求不带尾斜杠的路径（生产日志里实测到：
    # ``PROPFIND /dav 405``）。重定向到带斜杠的版本即可。
    #
    # 用 308 保持方法（**不能**用 301/302 —— 浏览器会把 PROPFIND 改成 GET）。
    @app.api_route(mount_path, methods=["GET", "HEAD", "PROPFIND", "OPTIONS"], include_in_schema=False)
    async def _dav_root_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{mount_path}/", status_code=308)

    logger.info("WebDAV 已挂载于 %s（只读）", mount_path)
    return True


def dav_root_hint(settings: AppSettings) -> str:
    """给日志用的根目录提示（不查库）。"""
    return os.path.abspath(settings.data_dir)
