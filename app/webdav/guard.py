"""WebDAV 的 ASGI 守卫中间件。

**这是唯一碰数据库的 DAV 组件。** wsgidav 跑在线程池里（同步 WSGI），
在里面用 ``AsyncEngine`` 会崩 —— 所以认证、限流、审计全放这里，在
**主 event loop** 里跑。

处理顺序：

1. **剥离客户端伪造的内部头** —— ``X-Dav-User`` / ``X-Dav-Root`` 等是
   我们自己注入的，客户端发来的同名头必须先扔掉，否则就是越权。
2. **限流** —— 按 IP + 按用户，超了返 429
3. **认证** —— Basic Auth，失败返 401
4. **注入身份与根目录** —— 供 wsgidav 的 provider 使用
5. **审计** —— 记录「谁在什么时候读了什么」
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from app.webdav.paths import resolve_dav_root
from app.webdav.provider import HEADER_ROOT, HEADER_USER
from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.services.dav_audit import dav_auditor
from app.services.dav_auth import AUTH_LOCKED, dav_authenticator
from app.services.dav_ratelimit import DavRateLimiter, dav_rate_limiter

logger = get_logger(__name__)

#: **客户端不得设置**的内部头 —— 出现即剥离
INTERNAL_HEADERS = frozenset(
    {
        b"x-dav-user",
        b"x-dav-root",
        b"x-dav-path",
    }
)

#: 审计里记的方法（其余方法不记 —— 避免 OPTIONS 之类的噪声）
AUDITED_METHODS = frozenset({"GET", "HEAD", "PROPFIND"})


class DavGuard:
    """DAV 请求守卫。

    **为什么是纯 ASGI 中间件**（而不是 ``BaseHTTPMiddleware``）：
    ``BaseHTTPMiddleware`` 会把响应**完整缓冲**后再发给客户端 ——
    DAV 的 GET 响应体虽小（``.strm`` 几 KB），但目录列表可能有几百 KB。
    纯 ASGI 中间件可以流式透传，不占额外内存。
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        settings: AppSettings | None = None,
        rate_limiter: DavRateLimiter | None = None,
    ) -> None:
        self.app = app
        self.settings = settings or get_settings()
        self.rate_limiter = rate_limiter or dav_rate_limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        remote_addr = _client_ip(scope)
        client = request.headers.get("user-agent", "")

        # -- ① 剥离客户端伪造的内部头 --
        scope["headers"] = [
            (k, v) for k, v in scope.get("headers", []) if k.lower() not in INTERNAL_HEADERS
        ]

        # -- ② 限流（先于认证 —— 不花 CPU 就能挡掉大部分流量）--
        if not self.rate_limiter.allow(
            DavRateLimiter.DIM_IP, remote_addr, self.settings.dav_rate_limit_per_min
        ):
            await self._reject(
                scope, receive, send, 429, "Too Many Requests", method, path, remote_addr,
                client, username="", user_id=0,
            )
            return

        # -- ③ 认证 --
        authorization = request.headers.get("authorization")
        result = await dav_authenticator.authenticate(
            self.settings, authorization, remote_addr
        )
        if not result.ok:
            status, message = _auth_error_response(result.status)
            await self._reject(
                scope, receive, send, status, message, method, path, remote_addr,
                client, username=result.username, user_id=0,
            )
            return

        # -- ④ 按用户限流 --
        if not self.rate_limiter.allow(
            DavRateLimiter.DIM_USER, result.username, self.settings.dav_user_rate_limit_per_min
        ):
            await self._reject(
                scope, receive, send, 429, "Too Many Requests", method, path, remote_addr,
                client, username=result.username, user_id=result.user_id,
            )
            return

        # -- ⑤ 解析 DAV 根 --
        root = await resolve_dav_root()
        if not root:
            await self._reject(
                scope, receive, send, 404, "DAV root is not configured", method, path,
                remote_addr, client, username=result.username, user_id=result.user_id,
            )
            return

        # -- ⑥ 注入身份与根目录 --
        #
        # 注意头名是**原始 HTTP 形式**（小写连字符）—— WSGI 服务器会把它
        # 转成 ``HTTP_X_DAV_USER`` 这样的 environ 键。若这里直接塞
        # ``HTTP_X_DAV_USER``，到 wsgidav 那里会变成
        # ``HTTP_HTTP_X_DAV_USER``，取不到。
        scope["headers"] = [
            *scope["headers"],
            (HEADER_USER.encode(), result.username.encode()),
            (HEADER_ROOT.encode(), root.encode()),
        ]

        # -- ⑦ 放行 + 审计 --
        if method not in AUDITED_METHODS:
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # wsgidav 会自己加一个 ``Date`` 头，而 ASGI 服务器（uvicorn）
                # 也会加 —— 客户端会收到两个 Date 头。HTTP 允许重复头
                # （服务端会合并），但不够干净，且某些客户端对重复 Date
                # 的处理不一致。
                #
                # 这里去掉 wsgidav 那个，保留 ASGI 服务器的。
                message = {
                    **message,
                    "headers": [
                        (k, v) for k, v in message["headers"] if k.lower() != b"date"
                    ],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            self._audit(
                user_id=result.user_id,
                username=result.username,
                method=method,
                path=path,
                status=status_holder["status"],
                client=client,
                remote_addr=remote_addr,
                duration_ms=duration_ms,
            )

    # -- 内部 --

    def _audit(
        self,
        *,
        user_id: int,
        username: str,
        method: str,
        path: str,
        status: int,
        client: str,
        remote_addr: str,
        duration_ms: int,
    ) -> None:
        """记一条审计（PROPFIND 汇总，GET/HEAD 明细）。

        PROPFIND 记的是「谁扫了哪个目录」—— 客户端扫库一次几百条，
        逐条记会把表撑爆，故汇总成一条（``item_count`` 由 wsgidav 那边
        补不上，这里记 0）。
        """
        dav_auditor.record(
            user_id=user_id,
            username=username,
            method=method,
            path=path,
            status=status,
            client=client,
            remote_addr=remote_addr,
            duration_ms=duration_ms,
            item_count=0,
        )

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        message: str,
        method: str,
        path: str,
        remote_addr: str,
        client: str,
        *,
        username: str,
        user_id: int,
    ) -> None:
        """拒绝请求（并记审计）。

        认证失败**也要记审计** —— 「谁在尝试爆破」是安全审计的关键部分。
        """
        headers = [(b"content-type", b"text/plain; charset=utf-8")]
        if status == 401:
            headers.append((b"www-authenticate", b'Basic realm="/dav"'))

        body = message.encode("utf-8")
        headers.append((b"content-length", str(len(body)).encode()))

        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

        self._audit(
            user_id=user_id,
            username=username,
            method=method,
            path=path,
            status=status,
            client=client,
            remote_addr=remote_addr,
            duration_ms=0,
        )


def _auth_error_response(status: str) -> tuple[int, str]:
    """把认证状态码映射成 HTTP 响应。

    锁定（``AUTH_LOCKED``）返 **429** —— 让客户端知道「等一会儿再试」，
    而不是无意义的 401（会让客户端反复重试，徒增失败计数）。

    其余（凭据错 / 没带头 / 头格式错）一律 401。
    """
    if status == AUTH_LOCKED:
        return 429, "Too Many Requests"
    return 401, "Unauthorized"


def _client_ip(scope: Scope) -> str:
    """取客户端 IP。

    **不信任 ``X-Forwarded-For``** —— 那是我们自己没设的反代加的。
    在同源部署（无反代）下它就是客户端自己填的，采信它等于让攻击者
    随便伪造 IP 绕过限流。

    若将来要放在反代后面，应改为只信任**已知反代网段**传来的头。
    """
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"
