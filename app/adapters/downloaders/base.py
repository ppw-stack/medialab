"""下载器适配层（FR-1）。

5 个核心契约方法：
1. ping: 连通性检测
2. add_torrent: 新增种子
3. list_torrents: 查询种子列表
4. set_pause: 修改状态（暂停/恢复）
5. remove: 删除任务（delete_data 独立控制是否连磁盘数据一起删）

附加可选：
- close: 释放连接池（优雅停机用）
- list_torrents_with_errors: 拉取全量种子 + 归一化错误信息（辅种巡查用）

设计约束（FR-1.3）：子类**必须**实现全部抽象方法，不支持的能力返回安全默认值。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: BTIH infohash：40 位 hex。拒绝 ``all`` / 通配等会批量误删的值——
#: qBittorrent 会把 ``all`` 解析为"应用到所有种子"，可一次删光全部任务及数据。
_VALID_INFOHASH_RE: re.Pattern[str] = re.compile(r"^[a-fA-F0-9]{40}$")


def validate_infohashes(hashes: Sequence[str]) -> None:
    """校验 hash 列表全部是 40 位 hex（FR 删除底线：拒绝 all/通配）。

    下载器适配器的 ``remove`` / ``set_pause`` 入口应调用本函数，避免
    上游（API 路由已校验，但 service / 测试 / 未来入口无法保证）传入
    ``all`` 等通配值导致批量误删。
    """
    bad = [h for h in hashes if not _VALID_INFOHASH_RE.fullmatch(h)]
    if bad:
        raise ValueError(
            f"非法 infohash：{bad[:5]}（必须是 40 位 hex，不支持 all/通配）"
        )


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

    @classmethod
    def parse(cls, value: str) -> TorrentState:
        """宽松解析：接受枚举值、枚举名（大小写不敏感）与 snake_case 别名。

        枚举的**值**是 camelCase（``pausedDL`` / ``missingFiles``，与
        qBittorrent 原生 state 一致），而**名**是 UPPER_SNAKE（``PAUSED_DL``）。
        前端下拉框用的是名，外部调用方常用 snake_case，故三种写法都接受。

        :raises ValueError: 无法识别时抛出（调用方据此返回 400）。
        """
        raw = (value or "").strip()
        if not raw:
            raise ValueError("state 不能为空")
        # 1) 精确值优先
        try:
            return cls(raw)
        except ValueError:
            pass
        # 2) 枚举名（大小写不敏感）：PAUSED_DL / paused_dl
        upper = raw.upper()
        for member in cls:
            if member.name == upper:
                return member
        # 3) snake_case ↔ camelCase 互转：paused_dl → pausedDL
        camel = "".join(
            part.capitalize() if i else part
            for i, part in enumerate(raw.replace("-", "_").split("_"))
        )
        for member in cls:
            if member.value.lower() == camel.lower():
                return member
            if member.name.lower() == raw.replace("-", "_").lower():
                return member
        raise ValueError(
            f"未知 state: {value!r}（可用："
            + ", ".join(sorted(m.value for m in cls))
            + "）"
        )

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

    @property
    def is_inflight(self) -> bool:
        """「在途」= 正在参与的任务：做种 / 下载 / 检查 / 停滞。

        **排除**已暂停（PAUSED_DL / PAUSED_UP）与致命错误（ERROR /
        MISSING_FILES）——这两类不参与传输。

        刻意**不排除**"已完成"（SEEDING / UPLOADING）：PT 辅种场景下
        做种正是主要活跃状态，排除它会让该指标恒为 0。

        口径单一来源：仪表盘的「在途任务」统计与辅种快照的
        ``inflight_count`` 都用它，避免两处各写一份判定而漂移。
        """
        return not (self.is_paused or self.is_fatal)


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
    #: **已完成（已校验有效）的字节数**——与 ``downloaded`` 语义不同。
    #:
    #: - qBittorrent：``completed``（libtorrent ``total_wanted_done``），
    #:   seed mode 下等于全量
    #: - Transmission：``haveValid``（已校验有效的字节数）
    #:
    #: 为什么需要它：``progress`` 在**校验中**返回的是「校验进度」而非
    #: 下载完成度（qBittorrent ``TorrentImpl::progress()`` 源码：
    #: ``if (isChecking()) return m_nativeStatus.progress;``），故
    #: ``progress >= 1.0`` 不能用来判断「已完成」。字节口径不受影响。
    #:
    #: 而 ``downloaded`` 也不能复用：qB 的口径正确，但 TR 的
    #: ``downloadedEver`` 是**累计**下载量（含重复下载），两站语义不一致。
    completed_bytes: int = 0
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
        """4. 删除任务。``delete_data`` 独立控制是否连磁盘数据一起删。"""

    @abstractmethod
    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        """5. 批量暂停/恢复。"""

    async def list_torrents_with_errors(self) -> list[dict[str, Any]]:
        """6. 拉取全量种子，附带**规范化**的错误信息与状态（辅种巡查用）。

        返回的每项至少含：

        - ``id``: int
        - ``hash``: str（infohash）
        - ``name``: str
        - ``size``: int（字节）
        - ``status``: int（下载器原生 status 码）
        - ``error_code``: int（0 = 无错；非 0 见下载器语义）
        - ``error_string``: str（人类可读错误描述，空串表示无错）
        - ``state``: str（**归一化**的 :class:`TorrentState` 值，如
          ``seeding`` / ``pausedUP`` / ``error``；未知状态给
          ``unknown``）

        与 :meth:`list_torrents` 的区别：本方法**不做状态归一**（保留
        下载器原生的错误码/错误描述，供巡查逻辑按具体错误类型判定），
        但**额外**给出归一化的 ``state``——巡查需要它来判定「是否已
        暂停」（避免对已暂停的种子重复发暂停指令）。

        不支持时抛 :class:`NotImplementedError`。
        """
        raise NotImplementedError(
            f"{self.client_type} 适配器暂不支持 list_torrents_with_errors"
        )

    async def close(self) -> None:
        """释放连接池。子类按需覆盖。"""