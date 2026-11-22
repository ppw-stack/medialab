"""测试用假驱动。

**Phase 0/1 用它跑通全链路**，不依赖真实网盘账号。

假驱动复刻真实驱动的关键行为：

- 直链带 ``?t=`` 过期时间戳与 ``f`` 参数
- **UA 绑定**：UA 不符时拒绝（模拟「直链绑定 UA」的驱动语义）
- 可配置直链有效期，用于验证缓存 TTL 提前失效
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.adapters.drivers.base import CloudFile, DirectLink, DriverError


@dataclass
class FakeDriver:
    """内存假驱动。

    Attributes:
        files: ``remote_id -> CloudFile``。
        link_ttl_sec: 生成的直链有效期（秒）。
        ua_required: 直链是否绑定 UA。
        call_counts: 各 remote_id 的换直链次数（用于断言单飞/缓存命中）。
    """

    files: dict[str, CloudFile] = field(default_factory=dict)
    link_ttl_sec: int = 3600
    ua_required: bool = True
    call_counts: dict[str, int] = field(default_factory=dict)
    #: 各 remote_id 绑定的 UA（为空表示不绑定）
    bound_ua: dict[str, str] = field(default_factory=dict)

    name = "fake"

    def add_file(
        self,
        remote_id: str,
        name: str,
        path: str | None = None,
        size: int = 1024,
        is_dir: bool = False,
        mtime: int = 0,
    ) -> CloudFile:
        """登记一个文件/目录。"""
        cf = CloudFile(
            remote_id=remote_id,
            path=path or f"/media/{name}",
            name=name,
            size=size,
            is_dir=is_dir,
            mtime=mtime,
        )
        self.files[remote_id] = cf
        return cf

    async def list_dir(self, path: str) -> list[CloudFile]:
        """按路径列出目录的直接子项。"""
        if path == "/__missing__":
            raise DriverError("目录不存在", kind="not_found")
        children = [
            cf
            for cf in self.files.values()
            if cf.is_dir is False and cf.path.rsplit("/", 1)[0] == path
        ]
        return children

    async def list_dir_by_cid(
        self, cid: str | int, parent_path: str = ""
    ) -> list[CloudFile]:
        """按 cid 列出目录的直接子项。

        假驱动的 cid = 路径本身（测试里省事）。
        """
        return await self.list_dir(parent_path or "/")

    async def iter_tree_by_cid(
        self,
        cid: str | int,
        parent_path: str = "",
        include_subdirs: bool = True,
    ) -> AsyncIterator[CloudFile]:
        """递归遍历（假实现：按路径前缀匹配）。"""
        if parent_path == "/__missing__":
            raise DriverError("目录不存在", kind="not_found")
        root = (parent_path or "").rstrip("/")
        for cf in self.files.values():
            if cf.is_dir:
                continue
            if root and not cf.path.startswith(root):
                continue
            yield cf

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        """换直链。UA 不符时抛错（模拟「直链绑定 UA」的驱动）。"""
        self.call_counts[remote_id] = self.call_counts.get(remote_id, 0) + 1
        if remote_id not in self.files:
            raise DriverError(f"文件不存在：{remote_id}", kind="not_found")
        if self.ua_required:
            bound = self.bound_ua.get(remote_id)
            if bound is not None and bound != user_agent:
                raise DriverError(
                    "UA 不匹配，直链拒绝访问", kind="auth"
                )
        expires = int(time.time()) + self.link_ttl_sec
        url = (
            f"https://cdn.example.com/{remote_id}"
            f"?t={expires}&f={'1' if self.ua_required else '0'}"
        )
        return DirectLink(url=url, expires_at=expires, ua_required=self.ua_required)


def build_fake_driver(
    credential: str | None = None,
) -> FakeDriver:
    """工厂函数（签名与注册表一致）。"""
    return FakeDriver()
