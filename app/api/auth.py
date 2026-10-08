"""认证与静态前端托管（FR-6.1 + FR-8.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    username: str
    password: str


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
        if settings.web_token and provided == settings.web_token.get_secret_value():
            return StatusOut(authenticated=True, username="token")
    if session and verify_session(session):
        return StatusOut(authenticated=True, username=settings.web_username)
    return StatusOut(authenticated=False)


@router.post("/api/v1/auth/login")
async def login(
    payload: LoginIn, response: Response
) -> dict[str, str]:
    settings: AppSettings = get_settings()
    if (
        payload.username != settings.web_username
        or not settings.web_password
        or not verify_password(
            payload.password, settings.web_password.get_secret_value()
        )
    ):
        # 显式延迟以防计时攻击（M1 验证 → M5 实践）
        import asyncio

        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = make_session()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # secure=True  # 生产由反向代理加 HTTPS，前端 dev 不需要
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
        if token and auth[7:] == token:
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


#: ``auto_error=False`` 让 ``session`` 可能是 None，便于开发模式
Depends_session = Depends(require_session)
