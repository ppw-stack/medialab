"""WebDAV 的文件系统 Provider。

在 :class:`wsgidav.fs_dav_provider.FilesystemProvider` 基础上加三件事：

1. **只读** —— ``is_readonly()`` 返回 True，wsgidav 会把 PUT / DELETE /
   MKCOL / LOCK 等写方法全部返回 **405**。这既是安全要求，也是
   AGENTS.md 的「不碰媒体库本体」。
2. **路径安全** —— 解析后的真实路径必须仍在根目录内（防软链逃逸）。
3. **Depth 限制** —— 见 :meth:`DavProvider.custom_request_handler`。

**根目录可运行时切换**：根路径从 ``environ`` 里的内部头读，而不是写死在
构造函数里 —— 面板改配置后无需重启 wsgidav。
"""

from __future__ import annotations

import os
from typing import Any

from wsgidav.dav_error import HTTP_FORBIDDEN, DAVError
from wsgidav.fs_dav_provider import FileResource, FilesystemProvider, FolderResource

from app.core.logging import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# 内部头的两套名字
#
# **必须分清**：ASGI 的 scope header 是**原始 HTTP 头名**（小写连字符），
# 而 WSGI 的 environ 键是**大写下划线**（``HTTP_`` 前缀）。WSGI 服务器会把
# ASGI 头转成 environ 键 —— 所以往 scope 里塞 ``HTTP_X_DAV_USER`` 的话，
# 到 wsgidav 那里会变成 ``HTTP_HTTP_X_DAV_USER``，取不到。
# --------------------------------------------------------------------------

#: ASGI 头名 —— 已认证的用户名（由守卫注入）
HEADER_USER = "x-dav-user"

#: ASGI 头名 —— DAV 根目录的**真实路径**（由守卫注入）
HEADER_ROOT = "x-dav-root"

#: WSGI environ 键 —— 已认证的用户名
#:
#: wsgidav 的 ``trusted_auth_header`` 配置项要的是**这个**格式。
ENV_USER = "HTTP_X_DAV_USER"

#: WSGI environ 键 —— DAV 根目录的真实路径
ENV_ROOT = "HTTP_X_DAV_ROOT"


class _NoQuotaMixin:
    """遮掉 ``quota-used-bytes`` / ``quota-available-bytes`` 属性。

    **为什么必须遮**（生产实测，差 85 倍）：

    wsgidav 的 ``FolderResource.get_used_bytes`` / ``get_available_bytes``
    调 :func:`shutil.disk_usage`（即 ``statvfs``）。PROPFIND 一个目录时，
    wsgidav 会对**每个子目录**调这两个方法各 **2 次**
    （一次在 ``get_property_names`` 里探测属性是否存在，一次在
    ``get_property_value`` 里取值）。

    在 **CIFS 网络共享**上，``statvfs`` 是**网络往返**（实测 6~66 ms）：

    ```
    /电影/外语电影 有 231 个子目录
      → 231 × 4 = 928 次 statvfs
      → PROPFIND 单个目录耗时 5.5 秒
    ```

    完整扫库（971 个目录）因此要 **366 秒**，客户端会中途超时。

    遮掉后实测：PROPFIND ``/电影/外语电影`` 从 **4077 ms → 48 ms**。

    **代价**：响应里不再有 ``quota-used-bytes`` /
    ``quota-available-bytes``。这两个是 **RFC 4331 的可选扩展**，
    描述的是「存储配额」—— 对只读 DAV 毫无意义，主流客户端
    （Infuse / 网易爆米花 / Finder）都不依赖。

    注意：``_DAVResource`` 基类本来就返回 ``None``（即默认不提供），
    只有 ``FolderResource`` 覆写成了 ``disk_usage``。这里把行为改回去。
    """

    def get_used_bytes(self) -> int | None:
        """不提供 ``quota-used-bytes``（见类文档）。"""
        return None

    def get_available_bytes(self) -> int | None:
        """不提供 ``quota-available-bytes``（见类文档）。"""
        return None


class DavFolderResource(_NoQuotaMixin, FolderResource):  # type: ignore[misc]
    """目录资源。

    覆写 :meth:`get_member_names` 的目的只有一个：**过滤掉不该被看到的
    条目**（如隐藏文件）。

    注意：该方法定义在 **Resource** 上而不是 Provider 上 —— 覆写
    ``FilesystemProvider.get_member_names`` 是**无效**的（wsgidav 的
    ``get_member_list`` 调的是 resource 上的方法）。
    """

    #: 不对外暴露的文件名前缀
    HIDDEN_PREFIXES = (".",)

    def get_member_names(self) -> list[str]:
        names = super().get_member_names()
        return [n for n in names if not n.startswith(self.HIDDEN_PREFIXES)]


class DavFileResource(_NoQuotaMixin, FileResource):  # type: ignore[misc]
    """文件资源。

    同样遮掉 quota 属性 —— 原因见 :class:`_NoQuotaMixin`。

    ``FileResource`` 理论上继承的是基类的 ``None`` 实现，但实测在 CIFS 上
    仍会被调用（wsgidav 对每个属性都要探测 getter 是否存在）。显式遮掉
    最稳妥。
    """


class DavProvider(FilesystemProvider):  # type: ignore[misc]
    """只读 + 路径安全 + 可切换根的 DAV Provider。"""

    def __init__(self, root_folder: str) -> None:
        """构造。

        Args:
            root_folder: **兜底**根目录。必须是一个**存在的**目录 ——
                wsgidav 的 ``FilesystemProvider`` 在构造时校验根目录存在，
                不存在就抛 ``ValueError: Invalid root path``。

                真正的根目录由请求头 :data:`ENV_ROOT` 决定（守卫每请求
                注入），所以这里传什么只影响「根未配置」时的报错。

        Note:
            **启动时真实根目录可能还不存在** —— 存储源还没配 output_dir、
            或媒体库还没同步过。这种情况下服务应该正常起来（面板可用、
            DAV 返回 404），而不是整个应用起不来。故调用方
            （:mod:`app.webdav.mount`）传一个必定存在的占位目录。
        """
        # fs_opts 必须显式传 —— 不传的话 wsgidav 的 FilesystemProvider
        # 不会设置 `self.fs_opts`，而 `get_member_names` 会去读它 → AttributeError
        super().__init__(root_folder, readonly=True, fs_opts={"follow_symlinks": False})

    # -- 只读 --

    def is_readonly(self) -> bool:
        """只读 —— wsgidav 据此把写方法全部返回 405。"""
        return True

    # -- 根目录 --

    def _effective_root(self, environ: dict[str, Any]) -> str:
        """取本次请求生效的根目录。

        优先用请求头里的（ASGI 中间件注入的当前根），回退到构造时的根。
        """
        root = environ.get(ENV_ROOT)
        if root:
            return str(root)
        return str(self.root_folder_path)

    def _loc_to_file_path(self, path: str, environ: dict[str, Any] | None = None) -> str:
        """把资源路径解析成真实文件路径，并校验**没有逃出根目录**。

        校验用 ``realpath`` —— 软链指向根外时会被挡下。

        Raises:
            DAVError: 403 —— 路径逃出根目录。
        """
        root = self._effective_root(environ or {})
        path_parts = [p for p in path.strip("/").split("/") if p]
        file_path = os.path.abspath(os.path.join(root, *path_parts))

        # 根目录本身也要 realpath —— 否则根是个软链时，正常访问都会被挡
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(file_path)
        if not _is_within(real_root, real_path):
            raise DAVError(HTTP_FORBIDDEN, "Access outside root is not allowed")

        return file_path

    # -- 资源 --

    def get_resource_inst(self, path: str, environ: dict[str, Any]) -> Any:
        """取资源实例（用自定义的 Resource 子类，遮掉 quota 属性）。"""
        file_path = self._loc_to_file_path(path, environ)

        if not os.path.exists(file_path):
            return None
        if os.path.islink(file_path):
            raise DAVError(HTTP_FORBIDDEN, "Symlink support is disabled")
        if os.path.isdir(file_path):
            return DavFolderResource(path, environ, file_path)
        return DavFileResource(path, environ, file_path)

    # -- 请求拦截 --

    def custom_request_handler(
        self,
        environ: dict[str, Any],
        start_response: Any,
        default_handler: Any,
    ) -> Any:
        """拦截请求，处理 **Depth** 语义。

        **为什么必须拦**：RFC 4918 规定 PROPFIND 不带 ``Depth`` 头时
        **等同 ``Depth: infinity``**。wsgidav 忠实实现了这点
        （``request_server.py`` 里 ``environ.setdefault("HTTP_DEPTH",
        "infinity")``）。媒体库 3 万个文件时，一次请求会遍历整棵树 ——
        必然超时、爆内存。

        处理：

        - **PROPFIND** 且缺省 ``Depth`` → 改成 ``"1"``（只列一层，客户端
          逐层下钻）
        - **PROPFIND** 显式 ``Depth: infinity`` → **403**（明确拒绝，
          不静默降级）

        **只对 PROPFIND 生效**：``do_GET`` / ``do_HEAD`` 要求
        ``Depth: 0``，若把它们的缺省 Depth 也降级成 ``1``，wsgidav 会以
        「Only Depth: 0 supported」返 **400** —— 目录浏览直接不可用。
        """
        method = environ.get("REQUEST_METHOD", "").upper()
        depth = environ.get("HTTP_DEPTH", "")
        if isinstance(depth, str):
            depth = depth.strip().lower()

        if method == "PROPFIND":
            if depth == "infinity":
                raise DAVError(
                    HTTP_FORBIDDEN,
                    "PROPFIND 'Depth: infinity' is disabled for security reasons",
                )
            if depth == "":
                # 缺省 → 降级为 1（见模块说明）
                environ["HTTP_DEPTH"] = "1"
            elif depth not in ("0", "1"):
                raise DAVError(HTTP_FORBIDDEN, f"Invalid Depth header: {depth!r}")

        return default_handler(environ, start_response)


def _is_within(root: str, path: str) -> bool:
    """判断 ``path`` 是否在 ``root`` 之内（含相等）。"""
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        # 不同盘（Windows）或混合绝对/相对路径 → 不在根内
        return False
