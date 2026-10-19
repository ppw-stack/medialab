"""下载器适配层（FR-1）。

5 个核心契约方法：
1. ping: 连通性检测
2. add_torrent: 新增种子
3. list_torrents: 查询种子列表
4. set_pause: 修改状态（暂停/恢复）
5. get_files: 获取种子详情（文件清单）

附加可选：
- close: 释放连接池（优雅停机用）

设计约束（FR-1.3）：子类**必须**实现全部抽象方法，不支持的能力返回安全默认值。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TorrentState(StrEnum):
    """下载器任务状态。"""

    DOWNLOADING = "downloading"
    STALLED_DL = "stalledDL"
    PAUSED_DL = "pausedDL"
    CHECKING = "checking"
    SEEDING = "seeding"
    UPLOADING = "uploading"
    PAUSED_UP = "pausedUP"
    ERROR = "error"
    MISSING_FILES = "missingFiles"
    UNKNOWN = "unknown"

    @property
    def is_completed(self) -> bool:
        return self in {TorrentState.SEEDING, TorrentState.UPLOADING}

    @property
    def is_downloading(self) -> bool:
        return self in {
            TorrentState.DOWNLOADING,
            TorrentState.STALLED_DL,
            TorrentState.CHECKING,
        }

    @property
    def is_paused(self) -> bool:
        return self in {TorrentState.PAUSED_DL, TorrentState.PAUSED_UP}

    @property
    def is_fatal(self) -> bool:
        """FR-3.4 表：不等窗口，立即中止。"""
        return self in {TorrentState.ERROR, TorrentState.MISSING_FILES}


@dataclass(frozen=True, slots=True)
class TorrentFile:
    """下载器上报的文件条目。删除目标只能来自此处（FR-5.2.5a）。"""

    path: str
    size: int = 0
    priority: int = 0


@dataclass(frozen=True, slots=True)
class TorrentInfo:
    """归一化的任务视图。"""

    hash: str
    name: str
    state: TorrentState = TorrentState.UNKNOWN
    progress: float = 0.0
    size: int = 0
    downloaded: int = 0
    uploaded: int = 0
    save_path: str | None = None
    category: str | None = None
    tags: frozenset[str] = frozenset()
    dlspeed: int = 0
    upspeed: int = 0
    num_seeds: int = 0
    num_connections: int = 0
    ratio: float = 0.0
    added_at: int | None = None
    completed_at: int | None = None
    eta: int | None = None

    @property
    def remaining_bytes(self) -> int:
        return max(self.size - self.downloaded, 0)


@dataclass(frozen=True, slots=True)
class TorrentFilter:
    """任务列表过滤条件（list_torrents 入参）。"""

    states: frozenset[TorrentState] = frozenset()
    category: str | None = None
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AddTorrentPayload:
    """添加任务入参。"""

    name: str
    urls: Sequence[str] = ()
    torrent_bytes: bytes | None = None
    category: str | None = None
    tags: frozenset[str] = frozenset()
    #: 保存路径（下载器视角）。None = 用下载器默认路径。
    #: qB → savepath；TR → download_dir
    save_path: str | None = None
    paused: bool = False
    skip_checking: bool = False


@dataclass(frozen=True, slots=True)
class TorrentHandle:
    info_hash: str
    downloader: str
    raw: str | None = None


class DownloaderError(RuntimeError):
    """下载器交互错误。``transient=True`` 表示可重试（FR-1.2 重试队列）。"""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class DownloaderAdapter(ABC):
    """下载器统一接口（FR-1）。"""

    #: 客户端类型标识
    client_type: str = "unknown"
    #: 实例名（日志/审计用）
    name: str = "unnamed"

    @abstractmethod
    async def ping(self) -> bool:
        """1. 连通性检测。地址/账号/密码任一不可用返回 False。"""

    @abstractmethod
    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        """2. 添加任务（magnet / 文件 / URL）。"""

    @abstractmethod
    async def list_torrents(self, filt: TorrentFilter | None = None) -> list[TorrentInfo]:
        """3. 列出任务，支持按状态/分类/标签过滤。"""

    @abstractmethod
    async def remove(self, hashes: Sequence[str], *, delete_data: bool) -> bool:
        """4. 删除任务。``delete_data`` 必须独立可控（FR-5.2.3 先 false）。"""

    @abstractmethod
    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        """5. 批量暂停/恢复。"""

    async def get_files(self, info_hash: str) -> list[TorrentFile]:
        """6. 获取种子详情（文件清单）。

        不支持时返回空列表，由调用方决定是否启用数据删除。
        """
        return []

    async def list_torrents_with_errors(self) -> list[dict[str, Any]]:
        """7. 拉取全量种子，附带**规范化**的错误信息（辅种巡查用）。

        返回的每项至少含：

        - ``id``: int
        - ``hash``: str（infohash）
        - ``name``: str
        - ``size``: int（字节）
        - ``status``: int（下载器原生 status 码）
        - ``error_code``: int（0 = 无错；非 0 见下载器语义）
        - ``error_string``: str（人类可读错误描述，空串表示无错）

        与 :meth:`list_torrents` 的区别：本方法**不做状态归一**，
        保留下载器原生的错误码/错误描述，供巡查逻辑按具体错误类型
        （tracker 未注册 / 数据丢失等）判定。

        不支持时抛 :class:`NotImplementedError`。
        """
        raise NotImplementedError(
            f"{self.client_type} 适配器暂不支持 list_torrents_with_errors"
        )

    async def close(self) -> None:
        """释放连接池。子类按需覆盖。"""