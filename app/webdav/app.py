"""构造 wsgidav 应用。

**认证不在这一层**：wsgidav 自带的 ``HTTPAuthenticator`` 会自己解析
Basic Auth 并比对密码 —— 但它比对的是它自己的 ``user_mapping``，而我们的
账号在**数据库**里。

故用 ``trusted_auth_header``：让 wsgidav **信任上游注入的用户名头**，
认证本身由 ASGI 中间件（:mod:`app.webdav.guard`）完成 —— 那里能
访问异步 DB。

这样切分的额外好处：认证、限流、审计全在**主 event loop** 里，
wsgidav 跑在独立的线程池里，两边互不阻塞。
"""

from __future__ import annotations

from wsgidav.dav_error import HTTP_FORBIDDEN, DAVError
from wsgidav.dir_browser import WsgiDavDirBrowser
from wsgidav.error_printer import ErrorPrinter
from wsgidav.http_authenticator import HTTPAuthenticator
from wsgidav.request_resolver import RequestResolver
from wsgidav.wsgidav_app import WsgiDAVApp

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.webdav.domain_controller import DavDomainController
from app.webdav.provider import ENV_USER, DavProvider

logger = get_logger(__name__)


def build_dav_app(settings: AppSettings, root_folder: str) -> WsgiDAVApp:
    """构造 wsgidav 应用实例。

    Args:
        settings: 应用配置。
        root_folder: **初始** DAV 根目录。实际生效的根由请求头决定
            （见 :mod:`app.webdav.paths`），这里只是启动时的占位。

    Returns:
        构造好的 :class:`WsgiDAVApp`。
    """
    provider = DavProvider(root_folder)

    config: dict[str, object] = {
        # provider 直接传**实例** —— wsgidav 的 `{"class": ...}` 形式
        # 只接受字符串路径（传类对象会报 TypeError）。
        "provider_mapping": {"/": provider},
        # **挂载路径** —— wsgidav 用它拼 PROPFIND 响应里的 href。
        #
        # 不配的话 href 会是 `/电影/` 而不是 `/dav/电影/` —— 客户端按 href
        # 回请求会 404（实测踩过）。它必须与 FastAPI 侧的 mount 路径一致。
        "mount_path": settings.dav_mount_path,
        "http_authenticator": {
            # 自建 DomainController：认证已在 ASGI 中间件完成，这里只负责
            # 把「已认证的用户名」传给 wsgidav 并拒绝一切匿名访问。
            #
            # 不能用 wsgidav 自带的 SimpleDomainController —— 它要求配置
            # `simple_dc.user_mapping`（密码存在配置文件里），且缺配置时
            # 会因为「realm 不可访问」把请求打成 400。
            "domain_controller": DavDomainController,
            # 只信上游注入的用户名头
            "trusted_auth_header": ENV_USER,
            # **必须 accept_basic=True** —— wsgidav 的校验是
            # `accept_digest or default_to_digest or not accept_basic`，
            # 三者全 False 会被判成「配置自相矛盾」而拒绝启动。
            #
            # 这不代表会用 wsgidav 的 Basic Auth：认证已在 ASGI 中间件做完，
            # 且 DomainController 的 basic_auth_user 恒返回 False。
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "middleware_stack": [
            ErrorPrinter,
            HTTPAuthenticator,
            # 目录浏览：**必须在栈里**，否则 GET 目录会走到
            # `do_GET` 的 `res.is_collection` 分支 → 403 / 400，
            # 客户端（Finder / 爆米花）浏览目录时直接报「网络异常」。
            #
            # 这是旧自研版的第二个缺陷（GET 目录返 405）。
            WsgiDavDirBrowser,
            RequestResolver,
        ],
        # 只读 DAV 不需要锁与属性存储
        "lock_storage": False,
        "property_manager": False,
        # 目录浏览：客户端（如 Finder）GET 目录时需要 HTML 列表。
        # **旧自研版在这里返 405**，导致爆米花等客户端报「网络异常」。
        "dir_browser": {"enable": True},
        "verbose": 1,
        "logging": {"enable": False},
        "hotfixes": {
            # 中文路径必须 URL 编码后再返回 —— 旧自研版漏了这步，
            # 客户端拿到未编码的中文 href 会解析失败。
            "re_encode_path_info": True,
        },
    }

    app = WsgiDAVApp(config)
    logger.info("WebDAV 应用已构造：根=%s（只读）", root_folder)
    return app


def ensure_root_exists(root: str) -> None:
    """DAV 根目录不存在时抛错。

    根目录不存在通常是「存储源还没配 output_dir」或「库还没同步」。
    这时返回 404 比返回 500 好 —— 客户端会提示「目录不存在」而不是
    「服务出错」。
    """
    if not root:
        raise DAVError(HTTP_FORBIDDEN, "DAV root is not configured")
