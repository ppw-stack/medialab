"""下载器适配层（FR-1）。

统一接口屏蔽 qBittorrent / Transmission / Deluge / aria2 的 API 差异。

设计约束（FR-1.3）：适配器**必须实现全部方法**，不支持的能力用
:class:`Capabilities` 声明并由业务层降级，**不允许**抛 ``NotImplementedError``
上抛业务层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Feature(StrEnum):
    """下载器能力枚举（FR-1 ``capabilities()``）。"""

    ADD_TORRENT = "add_torrent"
    CATEGORY = "category"
    TAGS = "tags"
    SAVE_PATH = "save_path"
    RATE_LIMIT_PER_TASK = "rate_limit_per_task"
    RATE_LIMIT_GLOBAL = "rate_limit_global"
    NATIVE_RSS = "native_rss"
    TORRENT_STATS_DETAIL = "torrent_stats_detail"
    #: 逐任务文件清单。**数据安全的硬依赖**（R12 / FR-5.2.5a）：
    #: 缺失则该客户端禁止启用任何数据删除能力。
    GET_FILES = "get_files"


#: 可删除数据所必需的能力集（FR-5.2.5a）
DATA_DELETION_REQUIRED: frozenset[Feature] = frozenset({Feature.GET_FILES})


class TorrentState(StrEnum):
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
class Capabilities:
    """能力声明（FR-1）。缺失能力由业务层降级，绝不抛异常。"""

    features: frozenset[Feature] = field(default_factory=frozenset)

    def has(self, feature: Feature) -> bool:
        return feature in self.features

    def supports_data_deletion(self) -> bool:
        """能否安全执行数据隔离（FR-5.2.5a / R12）。"""
        return self.features >= DATA_DELETION_REQUIRED

    def degrade_reason(self) -> str | None:
        missing = DATA_DELETION_REQUIRED - self.features
        if not missing:
            return None
        names = "、".join(m.value for m in sorted(missing, key=str))
        return f"缺少能力 [{names}]，已禁用数据删除，仅移除任务并保留数据"


@dataclass(frozen=True, slots=True)
class TorrentFilter:
    """任务列表过滤条件（FR-1 ``list_torrents(filter)``）。"""

    states: frozenset[TorrentState] = frozenset()
    category: str | None = None
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AddTorrentPayload:
    """添加任务入参。"""

    name: str
    urls: Sequence[str] = ()
    torrent_bytes: bytes | None = None
    save_path: str | None = None
    category: str | None = None
    tags: frozenset[str] = frozenset()
    paused: bool = False
    skip_checking: bool = False
    #: 幂等键：同一 hash 在同一站点不重复添加（非功能·幂等性）
    idem_key: str | None = None


@dataclass(frozen=True, slots=True)
class TorrentHandle:
    info_hash: str
    downloader: str
    raw: str | None = None


@dataclass(frozen=True, slots=True)
class GlobalStats:
    up_speed: int = 0
    down_speed: int = 0
    num_connections: int = 0
    upload_limit: int = 0
    download_limit: int = 0


class DownloaderError(RuntimeError):
    """下载器交互错误。``transient=True`` 表示可重试（FR-1.2 重试队列）。"""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class DownloaderAdapter(ABC):
    """下载器统一接口（FR-1）。

    子类**必须**实现全部抽象方法；不支持的能力返回安全默认值并在
    :meth:`capabilities` 中声明。
    """

    #: 客户端类型标识
    client_type: str = "unknown"
    #: 实例名（日志/审计用）
    name: str = "unnamed"

    @abstractmethod
    async def ping(self) -> bool:
        """连通性检测。地址/账号/密码任一不可用返回 False。"""

    @abstractmethod
    async def list_torrents(self, filt: TorrentFilter | None = None) -> list[TorrentInfo]:
        """列出任务，支持按状态/分类/标签过滤。"""

    @abstractmethod
    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        """添加任务（magnet / 文件 / URL）。"""

    @abstractmethod
    async def remove(self, hashes: Sequence[str], *, delete_data: bool) -> bool:
        """删除任务。``delete_data`` 必须独立可控（FR-5.2.3 先 false）。"""

    @abstractmethod
    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        """批量暂停/恢复。"""

    async def set_category_tags(
        self,
        hashes: Sequence[str],
        *,
        category: str | None = None,
        tags: frozenset[str] = frozenset(),
    ) -> bool:
        """设置分类标签。不支持时返回 False。"""
        return False

    async def set_save_path(self, hashes: Sequence[str], path: str) -> bool:
        """设置保存路径。不支持时返回 False。"""
        return False

    async def set_rate_limit(self, up: int | None = None, down: int | None = None) -> bool:
        """全局限速。不支持时返回 False。"""
        return False

    async def get_stats(self) -> GlobalStats:
        """全局统计。不支持时返回空统计。"""
        return GlobalStats()

    async def get_log(self, last_n: int = 200) -> list[str]:
        """排障日志。不支持时返回空列表。"""
        return []

    async def get_files(self, info_hash: str) -> list[TorrentFile]:
        """逐任务文件清单。**数据删除的硬依赖**（FR-5.2.5a）。

        不支持时返回空列表并由 :meth:`capabilities` 声明缺失，
        业务层据此禁用该客户端的数据删除能力（R12）。
        """
        return []

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """声明能力集合。"""

    async def close(self) -> None:
        """释放连接池。子类按需覆盖。"""

    @property
    def can_delete_data(self) -> bool:
        return self.capabilities().supports_data_deletion()
