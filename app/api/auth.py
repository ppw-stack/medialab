"""认证与静态前端托管（FR-6.1 + FR-8.2）。"""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.core.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    make_session,
    verify_password,
    verify_session,
)
from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    #: 长度上限防超大请求体拖垮 PBKDF2 校验
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=1024)


class StatusOut(BaseModel):
    authenticated: bool
    username: str | None = None


@router.get("/api/v1/auth/status", response_model=StatusOut)
async def auth_status(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> StatusOut:
    """轻量级状态查询——前端启动时拉一次。"""
    settings: AppSettings = get_settings()
    # 也接受 Bearer Token（CLI / 脚本入口，FR-6.1）
    if request.headers.get("Authorization", "").startswith("Bearer "):
        provided = request.headers["Authorization"][7:]
        if settings.web_token and hmac.compare_digest(
            provided, settings.web_token.get_secret_value()
        ):
            return StatusOut(authenticated=True, username="token")
    if session and verify_session(session):
        return StatusOut(authenticated=True, username=settings.web_username)
    return StatusOut(authenticated=False)


@router.post("/api/v1/auth/login")
async def login(
    payload: LoginIn, response: Response
) -> dict[str, str]:
    settings: AppSettings = get_settings()
    ok = (
        payload.username == settings.web_username
        and settings.web_password is not None
        and await asyncio.to_thread(
            verify_password,
            payload.password,
            settings.web_password.get_secret_value(),
        )
    )
    if not ok:
        # 显式延迟以防计时攻击（M1 验证 → M5 实践）
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = make_session()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # 生产由反向代理加 HTTPS 时通过 WEB_COOKIE_SECURE=true 开启
        secure=settings.web_cookie_secure,
        path="/",
    )
    return {"status": "ok", "username": settings.web_username}


@router.post("/api/v1/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


async def require_session(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    """依赖：保护所有管理端点（FR-6.1 面板需登录）。"""
    settings: AppSettings = get_settings()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = settings.web_token.get_secret_value() if settings.web_token else ""
        if token and hmac.compare_digest(auth[7:], token):
            return "token" 
    if session and verify_session(session):
        return settings.web_username
    raise HTTPException(status_code=401, detail="未登录")


def install_static(app, *, path: str = "/app/web/dist") -> None:
    """挂载前端构建产物到 ``/``。

    SPA fallback：未知路径返回 ``index.html``，由前端路由接管。
    API 路径（``/api/*``、``/healthz``、``/metrics``）由 FastAPI 自身处理。
    """
    import os

    from fastapi.responses import FileResponse

    if not os.path.isdir(path):
        logger.info("前端构建产物不存在于 %s，跳过静态托管", path)
        return

    # 资产目录（js/css/img）直接挂载
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(path, "assets")),
        name="assets",
    )

    async def spa_fallback(full_path: str):  # type: ignore[no-untyped-def]
        # API 与运维路径不接管
        if full_path.startswith(
            ("/api/", "/healthz", "/metrics", "/assets", "/docs", "/openapi")
        ):
            return JSONResponse({"detail": "not found"}, status_code=404)
        index = os.path.join(path, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return JSONResponse({"detail": "frontend not built"}, status_code=404)

    # 任何其他路径都返回 index.html

    # 直接挂到根：当 FastAPI 路由表中没有该路径时，触发 SPA fallback
    async def root_handler(request: Request) -> FileResponse:
        return await spa_fallback(request.url.path)

    app.add_route("/", root_handler, methods=["GET"])
    app.add_route("/{full_path:path}", root_handler, methods=["GET"])
    logger.info("前端 SPA 静态托管已挂载（来源：%s）", path)

