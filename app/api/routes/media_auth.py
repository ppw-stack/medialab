"""媒体库域的认证路由（``/api/auth/*``）。

**与辅种域的认证路由并存**（辅种域在 :mod:`app.api.auth`，前缀
``/api/v1/auth/*``）。两者共用同一套底层实现
（:mod:`app.core.auth`）——同一个 ``WEB_TOKEN`` 派生的 HMAC key、同一个
cookie 名、同一套密码校验，故**一次登录即可访问两个域的面板**。

为什么保留两套端点：

- 媒体库域的 ``/api/auth/me`` 是 strmmanger 时期的前端契约，已生成的
  脚本 / 收藏的 URL 依赖它
- 辅种域的 ``/api/v1/auth/status`` 是 ptHelper 时期的前端契约

两者都是薄封装，**没有各自的会话状态**——session cookie 相同。

本模块的服务层依赖在 :mod:`app.core.auth`，不碰 DB —— 符合分层约束
（``api`` 层不依赖 ``db``）。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Cookie, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from app.core.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    make_session,
    verify_bearer,
    verify_password,
    verify_session,
)
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool
    username: str


class MeResponse(BaseModel):
    authenticated: bool
    username: str = ""


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, response: Response) -> LoginResponse:
    """密码登录，成功后下发 session cookie。"""
    settings = get_settings()
    expected_user = settings.web_username
    stored_hash = (
        settings.web_password.get_secret_value() if settings.web_password else ""
    )

    # 用户名与密码**都要**校验，且用户名用常量时间比较。
    # 用户名错误时也走一遍密码校验路径，避免通过响应时间区分「用户不存在」
    # 与「密码错误」。
    user_ok = hmac.compare_digest(payload.username, expected_user)
    pass_ok = verify_password(payload.password, stored_hash) if stored_hash else False
    if not (user_ok and pass_ok):
        logger.warning("登录失败：用户 %s", payload.username)
        # detail 用**明确的中文文案**——前端会把 401 的 detail 显示给用户。
        # 不能用「未登录」：那是「会话过期」的文案，登录失败时显示它会误导人。
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    cookie = make_session()
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=settings.web_cookie_secure,
        path="/",
    )
    return LoginResponse(ok=True, username=expected_user)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    """登出（清 cookie）。"""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(
    medialab_session: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> MeResponse:
    """查询当前登录状态。

    同时支持 ``Authorization: Bearer <token>``——供 CLI / 脚本使用。
    Bearer 的 token 可以是 API Token（``WEB_TOKEN``）或 session cookie 值。
    """
    token = medialab_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if token and verify_session(token):
        return MeResponse(authenticated=True, username=get_settings().web_username)
    if token and verify_bearer(token):
        return MeResponse(authenticated=True, username=get_settings().web_username)
    return MeResponse(authenticated=False)
