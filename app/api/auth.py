"""认证路由（FR-6.1 + FR-8.2）。

**前端静态托管已移出本模块**——合并 strmmanger 后，SPA 的静态资源挂载
统一由 :func:`app.main._mount_frontend` 负责（它对带 hash 的构建产物做
不可变缓存、对不存在的资源返**真 404**，比 ``StaticFiles(html=True)``
更正确）。本模块只管认证。
"""

from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    make_session,
    verify_bearer,
    verify_password,
    verify_session,
)
from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    #: 多余字段直接拒（防 typo 字段名静默失效——比如前端写成
    #: ``passwd``，少了 ``password`` 却因为默认值而"登录成功"）
    model_config = ConfigDict(extra="forbid")

    #: 长度上限防超大请求体拖垮 PBKDF2 校验
    username: str = Field(..., min_length=1, max_length=64)
    #: **允许空串**（长度下限 0）——空密码是「密码错误」而非
    #: 「参数格式错」，返回 401 更贴合语义（前端显示"用户名或密码错误"）。
    #: 但**不能是缺字段**（缺字段由 pydantic 拦下 → 422）。
    password: str = Field(..., max_length=1024)


class StatusOut(BaseModel):
    authenticated: bool
    username: str | None = None


class LoginOut(BaseModel):
    """登录结果。"""

    ok: bool
    username: str


class LogoutOut(BaseModel):
    """登出结果。"""

    ok: bool


#: 时序均衡用的虚拟 PBKDF2 哈希（用户名不存在时也跑同样迭代）。
#: 进程级缓存——hash_password 本身要跑 600k 次 PBKDF2，每次登录现算
#: 会让"均衡"多出一倍 CPU 开销。
_dummy_hash: str | None = None
#: 首次计算虚拟哈希的锁——并发登录时只算一次，其余等待。
_dummy_hash_lock = threading.Lock()


def _dummy_password_hash() -> str:
    """返回（并缓存）一个恒定的虚拟密码哈希，仅用于时序均衡。"""
    global _dummy_hash
    if _dummy_hash is None:
        with _dummy_hash_lock:
            # 双检：拿到锁时可能已被其他线程算好
            if _dummy_hash is None:
                from app.core.auth import hash_password

                _dummy_hash = hash_password("dummy-password-for-timing-equalization")
    return _dummy_hash


@router.get("/api/v1/auth/status", response_model=StatusOut)
async def auth_status(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> StatusOut:
    """轻量级状态查询——前端启动时拉一次。

    **这是唯一的登录态查询端点**（合并前 strmmanger 有个同功能的
    ``/api/auth/me``，现已并入本端点）。
    """
    settings: AppSettings = get_settings()
    # 也接受 Bearer Token（CLI / 脚本入口，FR-6.1）——token 可以是
    # API Token（WEB_TOKEN 本身）或 session cookie 值。
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and verify_bearer(auth[7:].strip()):
        return StatusOut(authenticated=True, username="token")
    if session and verify_session(session):
        return StatusOut(authenticated=True, username=settings.web_username)
    return StatusOut(authenticated=False)


@router.post("/api/v1/auth/login", response_model=LoginOut)
async def login(payload: LoginIn, response: Response) -> LoginOut:
    settings: AppSettings = get_settings()
    # 防用户名枚举时序侧信道：无论用户名是否正确，都跑一次 PBKDF2 验证
    # （用户名不存在时对一个虚拟哈希跑同样 600k 迭代），让两条失败路径
    # 耗时接近。成功路径也跑 PBKDF2，与失败路径耗时差仅"成功返回"本身。
    #
    # 注意：PBKDF2 验证必须**无条件**执行，不能写成
    #   `username_ok and ... and await verify_password(...)`
    # ——Python 的 and 会短路，用户名错误时 await 根本不执行，
    # 虚拟哈希就白算了，恰好留下"用户名错误比密码错误快"的时序差。
    username_ok = payload.username == settings.web_username
    stored = (
        settings.web_password.get_secret_value()
        if settings.web_password is not None and username_ok
        else _dummy_password_hash()
    )
    # 先无条件跑验证（耗时路径一致），再与用户名/密码配置做逻辑与
    password_ok = await asyncio.to_thread(
        verify_password, payload.password, stored
    )
    ok = username_ok and settings.web_password is not None and password_ok
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
        # **SameSite=Strict**——面板是同源部署，且所有写操作都走
        # fetch + 自定义 header（没有 form 提交），Strict 不会误伤。
        # 它挡住的是「别的站点拿浏览器发跨站请求」——CSRF 的主路径。
        samesite="strict",
        # 生产由反向代理加 HTTPS 时通过 WEB_COOKIE_SECURE=true 开启
        secure=settings.web_cookie_secure,
        path="/",
    )
    return LoginOut(ok=True, username=settings.web_username)


@router.post("/api/v1/auth/logout", response_model=LogoutOut)
async def logout(response: Response) -> LogoutOut:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return LogoutOut(ok=True)


async def require_session(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    """依赖：保护所有管理端点（FR-6.1 面板需登录）。

    **两种鉴权口径**（合并说明）：

    - ``Authorization: Bearer <token>`` —— token 可以是 **API Token**
      （``WEB_TOKEN`` 本身，供 CLI / 脚本长期使用），也可以是
      **session cookie 值**（媒体库域的约定：同一份凭据两种用法）
    - ``Cookie: <session cookie>`` —— 面板用

    两种口径的判定都走 :func:`app.core.auth.verify_bearer` / 同名 cookie
    校验，实现只有一份。
    """
    settings: AppSettings = get_settings()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and verify_bearer(auth[7:].strip()):
        return "token"
    if session and verify_session(session):
        return settings.web_username
    raise HTTPException(status_code=401, detail="未登录")

