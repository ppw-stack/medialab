"""存储源驱动抽象。

接口先抽象好：新增阿里/夸克/WebDAV 只新增实现，
**中转层与缓存层完全不改**。

契约（所有驱动必须遵守）：

- :meth:`CloudDriver.list_dir` 返回**直接子项**，不递归。
- :meth:`CloudDriver.get_direct_link` 返回的直链必须能被播放器直接 GET。
- 拉取失败**必须抛** :class:`DriverError`——返回空列表会让上层误判为
  「该目录确实没有文件」，把故障伪装成正常状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class DriverError(RuntimeError):
    """驱动调用失败（网络 / 凭据 / 协议错误）。

    上层据此把作业标记为 failed，**不写 0 兜底**——写 0 等于把故障
    伪装成「上传 0 / 做种 0」。
    """

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        #: 错误分类：``network`` / ``auth`` / ``not_found`` / ``rate_limit`` / ``unknown``
        self.kind = kind


@dataclass(frozen=True)
class CloudFile:
    """存储源内的文件或目录。"""

    #: 存储源内的文件标识（光鸭为 fileId）。不出现在对外 URL 里。
    remote_id: str
    #: 存储源内的完整路径（绝对）
    path: str
    #: 文件名（不含路径）
    name: str
    #: 字节数（目录为 0）
    size: int = 0
    #: 是否为目录
    is_dir: bool = False
    #: 源文件 mtime（unix 秒），用于增量扫描
    mtime: int = 0
    #: SHA1（部分存储源可提供，用于校验）
    sha1: str | None = None
    #: **相对媒体库根**的路径（目录部分），用于算落盘位置。
    #:
    #: 为什么单独有这个字段：不同驱动**拿到根路径的方式不同**——
    #: 有的驱动能把目录 id 反解成路径；光鸭**没有**反查接口，根路径只有
    #: ``fileId``，拿不到「/影视」这样的字符串。
    #: 若靠 ``path`` 减去根路径字符串，光鸭就永远减不掉 → 目录结构全丢
    #: （附件全平铺在输出根下）。
    #:
    #: 驱动在遍历时**自己**知道当前位置相对根有多深，直接填这里即可。
    #: ``None`` 表示驱动不提供（此时回退到按 ``media_path`` 剥离）。
    relative_dir: str | None = None


@dataclass(frozen=True)
class DirectLink:
    """一条可直接访问的直链。

    Attributes:
        url: 播放器可直接 GET 的 URL。
        expires_at: 过期时间戳（unix 秒）。``None`` 表示驱动认为它不过期。
        extra_headers: 访问该 URL **必须**携带的额外请求头——中转层会
            把它们透传给播放器。
        ua_required: 该直链是否绑定了 UA。True 时播放器必须用同一 UA
            访问（缓存因此按 UA 分桶）。
    """

    url: str
    expires_at: int | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    ua_required: bool = True


@runtime_checkable
class CloudDriver(Protocol):
    """存储源驱动协议。"""

    #: 驱动类型标识（``guangya`` 等）
    name: str

    async def list_dir(self, path: str) -> list[CloudFile]:
        """列出目录的直接子项。失败抛 :class:`DriverError`。"""
        ...

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        """换取直链。失败抛 :class:`DriverError`。"""
        ...


@runtime_checkable
class AuthenticatedDriver(CloudDriver, Protocol):
    """支持扫码登录 / 凭据管理的驱动。"""

    async def check_login(self) -> bool:
        """检查凭据是否仍然有效。"""
        ...

    async def list_root(self) -> list[CloudFile]:
        """列出存储源根目录（用于登录态探测）。"""
        ...
