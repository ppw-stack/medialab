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
- list_torrent_file_names: 拉取每个种子的顶层文件名（孤儿文件统计用）
- get_disk_space: 查询磁盘空间（辅种空间统计用）

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
    def display_meta(cls) -> list[dict[str, str]]:
        """全部状态的**展示元信息**（中文名 + 颜色）——供前端直接渲染。

        **为什么在后端定义**：枚举的**值**与前端必须**逐字一致**
        （``pausedDL`` / ``missingFiles`` 是 camelCase，转小写后端不认）。
        以前前端硬编码一份 → 后端加状态就得改前端，漏改显示原始英文值。

        与 :data:`app.services.job_registry.JOB_META` 同一思路：**展示元信息
        随数据一起下发**，前端不再维护映射表。

        Returns:
            ``[{"value": "downloading", "label": "下载中", "color": "blue"}, ...]``
        """
        return [
            {"value": s.value, "label": label, "color": color}
            for s, (label, color) in _STATE_DISPLAY.items()
        ]

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


#: 状态 → (中文名, 颜色)。**与枚举同源**——前端不再硬编码这份映射。
#:
#: 颜色取 Ant Design Vue 的 tag 颜色名（blue / orange / cyan / green / red /
#: default）。定义在类**之后**：类内的 :meth:`TorrentState.display_meta`
#: 运行时才读它。
_STATE_DISPLAY: dict[TorrentState, tuple[str, str]] = {
    TorrentState.DOWNLOADING: ("下载中", "blue"),
    TorrentState.STALLED_DL: ("下载停滞", "orange"),
    TorrentState.PAUSED_DL: ("暂停下载", "orange"),
    TorrentState.CHECKING: ("校验中", "cyan"),
    TorrentState.SEEDING: ("做种中", "green"),
    TorrentState.UPLOADING: ("上传中", "green"),
    TorrentState.PAUSED_UP: ("暂停上传", "orange"),
    TorrentState.ERROR: ("错误", "red"),
    TorrentState.MISSING_FILES: ("文件丢失", "red"),
    TorrentState.UNKNOWN: ("未知", "default"),
}


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
class TorrentFileNames:
    """一个种子的顶层文件名（孤儿文件统计用）。

    :attr:`save_path` 是该种子的下载目录（TR 的 ``downloadDir`` /
    qB 的 ``save_path``）——孤儿判定按它把任务分组到各自的目录下。
    """

    #: infohash（40 位 hex）
    hash: str
    #: 下载目录（= downloadDir）
    save_path: str | None = None
    #: 顶层文件名列表（相对 save_path 的第一段）
    top_names: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class DiskSpace:
    """下载器所在磁盘的空间信息。

    ``total_bytes`` 可为 ``None``——qBittorrent 的 ``sync/maindata`` 只给
    ``free_space_on_disk``（剩余空间），拿不到总量。**不猜、不填 0**：
    0 会被前端画成「磁盘已满」，NULL 才能表达"不知道"。
    """

    #: 剩余空间（字节）
    free_bytes: int
    #: 磁盘总量（字节）；None = 该下载器不提供
    total_bytes: int | None = None

    @property
    def used_bytes(self) -> int | None:
        """已用空间（总量 - 剩余）。总量未知时返回 None。"""
        if self.total_bytes is None:
            return None
        return max(self.total_bytes - self.free_bytes, 0)

    @property
    def used_ratio(self) -> float | None:
        """已用占比（0.0–1.0）。总量未知时返回 None。"""
        if not self.total_bytes:
            return None
        return self.used_bytes / self.total_bytes if self.used_bytes else 0.0


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
        - ``completed_bytes``: int（**已校验有效**的字节数——磁盘实际
          占用。qB 取 ``completed``、TR 取 ``haveValid``）
        - ``save_path``: str | None（下载目录，用于按目录统计占用）
        - ``added_at``: int | None（**加入下载器的 unix 秒**；TR
          ``addedDate`` / qB ``added_on``。``0`` 与缺失统一归一为
          ``None``——下有人会写 0 表示"未知"）。disk_rotation 用它在
          同一价值档（count 相同）内**先删加入更早的种子**。

        与 :meth:`list_torrents` 的区别：本方法**不做状态归一**（保留
        下载器原生的错误码/错误描述，供巡查逻辑按具体错误类型判定），
        但**额外**给出归一化的 ``state``——巡查需要它来判定「是否已
        暂停」（避免对已暂停的种子重复发暂停指令）。

        不支持时抛 :class:`NotImplementedError`。
        """
        raise NotImplementedError(
            f"{self.client_type} 适配器暂不支持 list_torrents_with_errors"
        )

    async def list_torrent_file_names(self) -> list[TorrentFileNames]:
        """7. 拉取每个种子的**顶层文件名**（孤儿文件统计专用）。

        顶层名是相对该种子 ``save_path``（= downloadDir）的第一段路径：

        - 目录型种子：``"目录名/子目录/文件.mkv"`` → ``["目录名"]``
        - 单文件种子：``"文件.mkv"`` → ``["文件.mkv"]``

        **为什么单独一个方法**：顶层名需要下载器的**文件列表**才能算出，
        而文件列表的响应体极大——Transmission 的 ``files`` 字段在辅种站
        有 15.8 万个文件条目，占响应体 98%（27.5 MB / 27.5 MB）。把它
        混进 :meth:`list_torrents_with_errors` 会让**每次**辅种巡查
        （30 分钟一次）都白拉 27 MB，而巡查本身只要 status / 占用等。

        故拆成独立方法：只有孤儿扫描作业（每天一次）才付这个代价。

        本方法**自带** ``save_path``——孤儿扫描据此按 downloadDir 分组，
        无需再调 :meth:`list_torrents_with_errors`（省一次全量拉取）。

        不支持时抛 :class:`NotImplementedError`。
        """
        raise NotImplementedError(
            f"{self.client_type} 适配器暂不支持 list_torrent_file_names"
        )

    async def get_disk_space(self, path: str | None = None) -> DiskSpace:
        """8. 查询下载器所在磁盘的空间信息（辅种空间统计用）。

        :param path: 参考目录——用于定位**哪个分区**（同一个盘上不同
            挂载点可能不同）。None = 用下载器默认位置。

        :returns: :class:`DiskSpace`（``total_bytes`` 可能为 None——
            qBittorrent 只提供剩余空间）。

        不支持时抛 :class:`NotImplementedError`。
        """
        raise NotImplementedError(
            f"{self.client_type} 适配器暂不支持 get_disk_space"
        )

    async def close(self) -> None:
        """释放连接池。子类按需覆盖。"""