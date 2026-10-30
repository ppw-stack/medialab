"""WebDAV 的 DomainController。

wsgidav 自带的 ``SimpleDomainController`` 要求配置 ``simple_dc.user_mapping``，
密码存在**配置文件**里 —— 我们的账号在**数据库**，且要走自己的认证流程
（PBKDF2 + 失败锁定 + 缓存）。

故自建一个：认证已在 ASGI 中间件（:mod:`app.webdav.guard`）做完，
这里只负责把「已认证的用户名」告诉 wsgidav，并**拒绝一切匿名访问**。

**关键**：:meth:`require_authentication` 恒返回 True —— 即使 wsgidav 没收到
用户名头（说明中间件没生效），也不会退化成匿名访问。
"""

from __future__ import annotations

from typing import Any

from wsgidav.dc.base_dc import BaseDomainController

from app.webdav.provider import ENV_USER


class DavDomainController(BaseDomainController):  # type: ignore[misc]
    """信任上游注入的用户名，拒绝匿名。

    **用户名从哪来**：ASGI 中间件在认证通过后注入 ``X-Dav-User`` 头；
    wsgidav 的 ``trusted_auth_header`` 会把它放到
    ``environ["wsgidav.auth.user_name"]``。
    """

    def get_domain_realm(self, path_info: str, environ: dict[str, Any] | None) -> str:
        """realm 用 share 路径（只有一个 share，就是根）。"""
        return "/"

    def require_authentication(self, realm: str, environ: dict[str, Any] | None) -> bool:
        """恒为 True —— 绝不匿名放行。

        启动时（``environ is None``）wsgidav 会调它来检查「这个 share 是否
        允许匿名」。返回 True 表示需要认证，wsgidav 就会打日志警告
        anonymous share —— 那是**期望行为**，不是配置错误。
        """
        return True

    def basic_auth_user(
        self, realm: str, user_name: str, password: str, environ: dict[str, Any]
    ) -> bool:
        """恒 False —— 我们不走 wsgidav 的 Basic Auth 流程。

        真正的认证在 ASGI 中间件里（能访问异步 DB）。wsgidav 的
        ``HTTPAuthenticator`` 配了 ``accept_basic=False``，正常不会走到这里；
        万一走到，直接拒。
        """
        return False

    def supports_http_digest_auth(self) -> bool:
        """不支持 Digest —— 客户端会用 Basic。"""
        return False

    def digest_auth_user(self, realm: str, user_name: str, environ: dict[str, Any]) -> str:
        """不支持 Digest（见 :meth:`supports_http_digest_auth`）。"""
        return ""

    def is_share_anonymous(self, path_info: str) -> bool:
        """不匿名 —— 避免 wsgidav 启动时打「允许匿名」的误导性警告。"""
        return False

    def _trusted_user(self, environ: dict[str, Any] | None) -> str:
        """取上游注入的用户名（调试用）。"""
        if environ is None:
            return ""
        return str(environ.get(ENV_USER, ""))
