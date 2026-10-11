"""Transmission 4.1.0 JSON-RPC 2.0 工具类。

基于 Transmission 文档「rpc-spec.md」（GitHub: transmission/transmission）。

与 ``app.adapters.downloaders.qbittorrent_api.Qb5ApiClient`` 同模式：独立工具类，
供后端服务层使用，与现有 ``QbittorrentAdapter`` 解耦（不动 ``DownloaderAdapter`` 契约）。

覆盖 24 个 RPC 方法：
- Torrent (11): start / start_now / stop / verify / reannounce / set / get /
  add / remove / set_location / rename_path
- Queue (4): move_top / move_up / move_down / move_bottom
- Session (4): get / set / stats / close
- Misc (5): blocklist_update / port_test / free_space / group_set / group_get
"""

from __future__ import annotations

import asyncio
import base64
from enum import StrEnum
from typing import Any, cast

import httpx


class TrApiError(RuntimeError):
    """Transmission RPC 调用错误。

    transient=True：调用方可重试（5xx / 网络错误 / CSRF 重发后仍失败）。
    transient=False：调用方不应重试（参数错、JSON-RPC error.code 等）。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: int = 0,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.transient = transient


# ============================================================ 枚举
class TorrentField(StrEnum):
    """torrent-get 返回字段（§3.3 文档全集）。"""

    ACTIVITY_DATE = "activityDate"
    ADDED_DATE = "addedDate"
    AVAILABILITY = "availability"
    BANDWIDTH_PRIORITY = "bandwidthPriority"
    BYTES_COMPLETED = "bytesCompleted"
    COMMENT = "comment"
    CORRUPT_EVER = "corruptEver"
    CREATOR = "creator"
    DATE_CREATED = "dateCreated"
    DESIRED_AVAILABLE = "desiredAvailable"
    DONE_DATE = "doneDate"
    DOWNLOAD_DIR = "downloadDir"
    DOWNLOADED_EVER = "downloadedEver"
    DOWNLOAD_LIMIT = "downloadLimit"
    DOWNLOAD_LIMITED = "downloadLimited"
    EDIT_DATE = "editDate"
    ERROR = "error"
    ERROR_STRING = "errorString"
    ETA = "eta"
    ETA_IDLE = "etaIdle"
    FILE_COUNT = "fileCount"
    FILES = "files"
    FILE_STATS = "fileStats"
    GROUP = "group"
    HASH_STRING = "hashString"
    HAVE_UNCHECKED = "haveUnchecked"
    HAVE_VALID = "haveValid"
    HONORS_SESSION_LIMITS = "honorsSessionLimits"
    ID = "id"
    IS_FINISHED = "isFinished"
    IS_PRIVATE = "isPrivate"
    IS_STALLED = "isStalled"
    LABELS = "labels"
    LEFT_UNTIL_DONE = "leftUntilDone"
    MAGNET_LINK = "magnetLink"
    MANUAL_ANNOUNCE_TIME = "manualAnnounceTime"  # DEPRECATED
    MAX_CONNECTED_PEERS = "maxConnectedPeers"
    METADATA_PERCENT_COMPLETE = "metadataPercentComplete"
    NAME = "name"
    PEER_LIMIT = "peerLimit"
    PEERS = "peers"
    PEERS_CONNECTED = "peersConnected"
    PEERS_FROM = "peersFrom"
    PEERS_GETTING_FROM_US = "peersGettingFromUs"
    PEERS_SENDING_TO_US = "peersSendingToUs"
    PERCENT_COMPLETE = "percentComplete"
    PERCENT_DONE = "percentDone"
    PIECE_COUNT = "pieceCount"
    PIECE_SIZE = "pieceSize"
    PIECES = "pieces"
    PRIORITIES = "priorities"
    PRIMARY_MIME_TYPE = "primaryMimeType"
    QUEUE_POSITION = "queuePosition"
    RATE_DOWNLOAD = "rateDownload"
    RATE_UPLOAD = "rateUpload"
    RECHECK_PROGRESS = "recheckProgress"
    SECONDS_DOWNLOADING = "secondsDownloading"
    SECONDS_SEEDING = "secondsSeeding"
    SEED_IDLE_LIMIT = "seedIdleLimit"
    SEED_IDLE_MODE = "seedIdleMode"
    SEED_RATIO_LIMIT = "seedRatioLimit"
    SEED_RATIO_MODE = "seedRatioMode"
    SEQUENTIAL_DOWNLOAD = "sequentialDownload"
    SEQUENTIAL_DOWNLOAD_FROM_PIECE = "sequentialDownloadFromPiece"
    SIZE_WHEN_DONE = "sizeWhenDone"
    START_DATE = "startDate"
    STATUS = "status"
    TORRENT_FILE = "torrentFile"
    TOTAL_SIZE = "totalSize"
    TRACKERS = "trackers"
    TRACKER_LIST = "trackerList"
    TRACKER_STATS = "trackerStats"
    UPLOADED_EVER = "uploadedEver"
    UPLOAD_LIMIT = "uploadLimit"
    UPLOAD_LIMITED = "uploadLimited"
    UPLOAD_RATIO = "uploadRatio"
    WANTED = "wanted"
    WEBSEEDS = "webseeds"  # DEPRECATED
    WEBSEEDS_EX = "webseedsEx"
    WEBSEEDS_SENDING_TO_US = "webseedsSendingToUs"


class SessionField(StrEnum):
    """session-get 返回字段（§4.1 文档全集，排除只读）。"""

    ALT_SPEED_DOWN = "alt-speed-down"
    ALT_SPEED_ENABLED = "alt-speed-enabled"
    ALT_SPEED_TIME_BEGIN = "alt-speed-time-begin"
    ALT_SPEED_TIME_DAY = "alt-speed-time-day"
    ALT_SPEED_TIME_ENABLED = "alt-speed-time-enabled"
    ALT_SPEED_TIME_END = "alt-speed-time-end"
    ALT_SPEED_UP = "alt-speed-up"
    ANTI_BRUTE_FORCE_ENABLED = "anti-brute-force-enabled"
    BLOCKLIST_ENABLED = "blocklist-enabled"
    BLOCKLIST_URL = "blocklist-url"
    CACHE_SIZE_MIB = "cache-size-mib"  # DEPRECATED 5.0 移除
    DEFAULT_TRACKERS = "default-trackers"
    DOWNLOAD_DIR = "downloadDir"
    DOWNLOAD_DIR_FREE_SPACE = "download-dir-free-space"
    DOWNLOAD_QUEUE_ENABLED = "download-queue-enabled"
    DOWNLOAD_QUEUE_SIZE = "download-queue-size"
    ENCRYPTION = "encryption"
    IDLE_SEEDING_LIMIT = "idle-seeding-limit"
    IDLE_SEEDING_LIMIT_ENABLED = "idle-seeding-limit-enabled"
    INCOMPLETE_DIR = "incomplete-dir"
    INCOMPLETE_DIR_ENABLED = "incomplete-dir-enabled"
    LPD_ENABLED = "lpd-enabled"
    PEER_LIMIT = "peerLimit"
    PEER_PORT = "peer-port"
    PEER_PORT_RANDOM_ON_START = "peer-port-random-on-start"
    PEX_ENABLED = "pex-enabled"
    PORT_FORWARDING_ENABLED = "port-forwarding-enabled"
    QUEUE_STALLED_ENABLED = "queue-stalled-enabled"
    QUEUE_STALLED_MINUTES = "queue-stalled-minutes"
    RENAME_PARTIAL_FILES = "rename-partial-files"
    RPC_VERSION = "rpc-version"
    RPC_VERSION_MINIMUM = "rpc-version-minimum"
    RPC_VERSION_SEMVER = "rpc-version-semver"
    SEED_QUEUE_ENABLED = "seed-queue-enabled"
    SEED_QUEUE_SIZE = "seed-queue-size"
    SEED_RATIO_LIMIT = "seedRatioLimit"
    SEED_RATIO_LIMITED = "seed-ratio-limited"
    SESSION_ID = "session-id"
    SPEED_LIMIT_DOWN = "speed-limit-down"
    SPEED_LIMIT_DOWN_ENABLED = "speed-limit-down-enabled"
    SPEED_LIMIT_UP = "speed-limit-up"
    SPEED_LIMIT_UP_ENABLED = "speed-limit-up-enabled"
    START_ADDED_TORRENTS = "start-added-torrents"
    TRASH_ORIGINAL_TORRENT_FILES = "trash-original-torrent-files"
    UNITS = "units"
    UTP_ENABLED = "utp-enabled"
    VERSION = "version"


class TrApiClient:
    """Transmission 4.x JSON-RPC 2.0 客户端（独立工具类）。

    用法::

        async with TrApiClient(base_url, username=..., password=...) as tr:
            torrents = await tr.list_torrents()
            await tr.stop_torrents([hash])

    内部处理：CSRF token（409 自动重发）、HTTP Basic Auth、JSON-RPC 2.0 错误码。
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        path: str = "/transmission/rpc",
        timeout: float = 15.0,
        verify_tls: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._path = path
        self._username = username
        self._password = password
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._session_id: str | None = None
        self._next_req_id = 1
        self._auth_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            verify=verify_tls,
            headers={"User-Agent": "SeedHelper/0.1 (transmission-rpc)"},
        )

    # ============================================================ 内部
    @property
    def _rpc_url(self) -> str:
        return f"{self._base}{self._path}"

    @property
    def _auth(self) -> httpx.BasicAuth | None:
        if not (self._username and self._password):
            return None
        return httpx.BasicAuth(self._username, self._password)

    def _next_id(self) -> int:
        v = self._next_req_id
        self._next_req_id += 1
        return v

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | list[Any] | None = None,
        *,
        retry_on_csrf: bool = True,
    ) -> Any:
        """发送 RPC 请求（兼容旧版 bespoke + 4.1.0 JSON-RPC 2.0）。

        错误码语义：
        - HTTP 409（CSRF）→ 自动重发（更新 X-Transmission-Session-Id）
        - HTTP 4xx → transient=False
        - HTTP 5xx / 网络错误 / CSRF 重发失败 → transient=True
        - JSON-RPC result != "success" → transient=False
        """
        # Transmission 旧版 bespoke 协议：method + arguments + tag
        # 4.1.0+ 也支持 JSON-RPC 2.0：jsonrpc + method + params + id
        # 实际上旧版 4.0.x 仍广泛使用，且 4.1+ 同时支持两种。
        # 旧版 bespoke 格式更稳定，所有 4.x 都支持。
        body: dict[str, Any] = {
            "method": method,
            "arguments": params if isinstance(params, dict) else {},
            "tag": self._next_id(),
        }

        headers: dict[str, str] = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        try:
            resp = await self._client.post(
                self._rpc_url,
                json=body,
                headers=headers or None,
                auth=cast(Any, self._auth),
            )
        except httpx.HTTPError as exc:
            raise TrApiError(
                f"Transmission 网络错误：{exc}", transient=True
            ) from exc

        # CSRF：409 含新 session id
        if resp.status_code == 409 and retry_on_csrf:
            new_sid = resp.headers.get("X-Transmission-Session-Id")
            if not new_sid:
                raise TrApiError(
                    "409 但缺少 X-Transmission-Session-Id",
                    status_code=409,
                    transient=False,
                )
            self._session_id = new_sid
            return await self._request(method, params, retry_on_csrf=False)

        # 4xx（已重试过）/ 5xx → raise
        if resp.status_code >= 400:
            transient = resp.status_code >= 500 or (
                resp.status_code == 409 and not retry_on_csrf
            )
            raise TrApiError(
                f"Transmission HTTP {resp.status_code}：{resp.text[:200]}",
                status_code=resp.status_code,
                transient=transient,
            )

        # 2xx：解析 JSON
        try:
            data = resp.json()
        except ValueError as exc:
            raise TrApiError(
                f"响应非 JSON：{resp.text[:200]}",
                status_code=resp.status_code,
                transient=False,
            ) from exc

        # JSON-RPC 2.0 error（4.1.0+）
        if isinstance(data, dict) and "error" in data and data["error"]:
            err = data["error"]
            message = err.get("message", "")
            code = err.get("code", 0)
            raise TrApiError(
                f"JSON-RPC error code={code}: {message}",
                code=code,
                status_code=resp.status_code,
                transient=False,
            )

        # 旧版 bespoke：检查 result 字段
        if isinstance(data, dict):
            result_str = data.get("result", "")
            if result_str and result_str != "success":
                raise TrApiError(
                    f"Transmission result: {result_str}",
                    status_code=resp.status_code,
                    transient=False,
                )
            # 返回 arguments（旧版）或 result（JSON-RPC 2.0）
            if "arguments" in data:
                return data["arguments"]
            if "result" in data and isinstance(data["result"], (dict, list)):
                return data["result"]
            return data

    # ============================================================ 生命周期
    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TrApiClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ============================================================ Session (4)
    async def get_session(
        self, fields: list[SessionField | str] | None = None
    ) -> dict[str, Any]:
        """session-get — 取会话参数（accessor）。

        fields: 指定返回字段（None=全部）。支持 SessionField 枚举或字符串。
        """
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = [str(f) for f in fields]
        return cast(dict[str, Any], await self._request("session-get", params))

    async def set_session(self, **kwargs: Any) -> None:
        """session-set — 修改会话参数。

        字段名与文档 §4.1 一致（如 alt_speed_enabled / download_dir 等）。
        session-id / version / rpc-version 等只读字段忽略。
        """
        if not kwargs:
            return
        await self._request("session-set", kwargs)

    async def get_session_stats(self) -> dict[str, Any]:
        """session-stats — 取累计 + 当前流量统计。

        返回字段：
        - active_torrent_count / paused_torrent_count / torrent_count
        - download_speed / upload_speed (B/s)
        - cumulative_stats: {uploaded_bytes, downloaded_bytes, files_added,
          seconds_active, session_count}
        - current_stats: 同上
        """
        return cast(dict[str, Any], await self._request("session-stats"))

    async def close_session(self) -> None:
        """session-close — 关闭 Transmission 守护进程。

        警告：执行后 daemon 进程退出，所有下载停止。
        """
        await self._request("session-close", {})

    # ============================================================ Torrent - 状态变更 (5)
    async def start_torrents(
        self, ids: list[int | str] | None = None
    ) -> None:
        """torrent-start — 启动种子（受队列位置约束）。

        ids: 整数 id 或 SHA1 哈希列表；None 表示所有种子。
        """
        params: dict[str, Any] = {}
        if ids is not None:
            params["ids"] = ids
        await self._request("torrent-start", params)

    async def start_torrents_now(
        self, ids: list[int | str] | None = None
    ) -> None:
        """torrent-start-now — 无视队列立即启动。"""
        params: dict[str, Any] = {}
        if ids is not None:
            params["ids"] = ids
        await self._request("torrent-start-now", params)

    async def stop_torrents(
        self, ids: list[int | str] | None = None
    ) -> None:
        """torrent-stop — 停止种子。"""
        params: dict[str, Any] = {}
        if ids is not None:
            params["ids"] = ids
        await self._request("torrent-stop", params)

    async def verify_torrents(
        self, ids: list[int | str] | None = None
    ) -> None:
        """torrent-verify — 校验已下载数据。"""
        params: dict[str, Any] = {}
        if ids is not None:
            params["ids"] = ids
        await self._request("torrent-verify", params)

    async def reannounce_torrents(
        self, ids: list[int | str] | None = None
    ) -> None:
        """torrent-reannounce — 立即向所有 tracker 重新宣告。"""
        params: dict[str, Any] = {}
        if ids is not None:
            params["ids"] = ids
        await self._request("torrent-reannounce", params)

    # ============================================================ Torrent - mutator (1)
    async def set_torrent(
        self,
        *,
        ids: list[int | str] | None = None,
        **kwargs: Any,
    ) -> None:
        """torrent-set — 修改一个或多个种子属性。

        关键字参数与文档 §3.2 字段一致：
        - bandwidth_priority / download_limit / download_limited
        - files_unwanted / files_wanted（文件 index 列表）
        - group / honors_session_limits / labels
        - location（下载目录）/ peer_limit
        - priority_high / priority_low / priority_normal
        - queue_position
        - seed_idle_limit / seed_idle_mode / seed_ratio_limit / seed_ratio_mode
        - sequential_download / sequential_download_from_piece
        - tracker_list / tracker_add / tracker_remove / tracker_replace
        - upload_limit / upload_limited
        """
        if not kwargs:
            return
        params: dict[str, Any] = dict(kwargs)
        if ids is not None:
            params["ids"] = ids
        await self._request("torrent-set", params)

    # ============================================================ Torrent - accessor (1)
    async def list_torrents(
        self,
        *,
        ids: list[int | str] | None = None,
        fields: list[TorrentField | str] | None = None,
        format: str = "objects",
    ) -> list[dict[str, Any]]:
        """torrent-get — 取种子属性（accessor）。

        fields: 指定返回字段（None=Transmission 默认最小集）。支持 TorrentField 枚举或字符串。
        format: "objects"（默认，返回 dict 列表）或 "table"（返回 list of lists）。
        """
        params: dict[str, Any] = {"format": format}
        if ids is not None:
            params["ids"] = ids
        if fields is not None:
            params["fields"] = [str(f) for f in fields]
        result = await self._request("torrent-get", params)
        torrents = result.get("torrents", []) if isinstance(result, dict) else []
        # 移除 / 近期活跃 torrent 列表（ids="recently_active" 时）
        return list(torrents) if torrents else []

    # ============================================================ Torrent - 增删 (3)
    async def add_torrent(
        self,
        *,
        filename: str | None = None,
        metainfo: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """torrent-add — 添加种子。

        filename 或 metainfo 必须二选一：
        - filename: 磁链 / HTTP URL / 本地 .torrent 路径
        - metainfo: base64 编码的 .torrent 内容

        可选参数：cookies / download_dir / paused / peer_limit /
        bandwidth_priority / labels / files_wanted / files_unwanted /
        priority_high / priority_low / priority_normal /
        sequential_download / sequential_download_from_piece

        返回：torrent_added 或 torrent_duplicate 对象（id/name/hash_string）。
        """
        if not (filename or metainfo):
            raise TrApiError(
                "filename 和 metainfo 至少需要 1 个",
                transient=False,
            )
        params: dict[str, Any] = dict(kwargs)
        if filename is not None:
            params["filename"] = filename
        if metainfo is not None:
            params["metainfo"] = metainfo
        result = await self._request("torrent-add", params)
        if not isinstance(result, dict):
            return {}
        return result

    async def remove_torrents(
        self,
        ids: list[int | str],
        *,
        delete_local_data: bool = False,
    ) -> None:
        """torrent-remove — 移除种子。

        delete_local_data=True：连同磁盘数据一并删除。
        """
        await self._request(
            "torrent-remove",
            {"ids": ids, "delete_local_data": delete_local_data},
        )

    async def set_location(
        self,
        ids: list[int | str],
        location: str,
        *,
        move: bool = False,
    ) -> None:
        """torrent-set-location — 移动或重新定位下载目录。

        move=False（默认）：在新 location 中搜索已有文件。
        move=True：从原位置 mv 到新 location。
        """
        await self._request(
            "torrent-set-location",
            {"ids": ids, "location": location, "move": move},
        )

    # ============================================================ Torrent - 重命名 (1)
    async def rename_path(
        self,
        id_: int | str,
        path: str,
        name: str,
    ) -> dict[str, Any]:
        """torrent-rename-path — 重命名种子中的文件或文件夹。

        id_: 单个 torrent 的 id 或 hash。
        path: 待重命名路径（相对种子根目录）。
        name: 新名称。

        返回 {path, name, id}（id=integer 种子 id）。
        """
        result = await self._request(
            "torrent-rename-path",
            {"ids": [id_], "path": path, "name": name},
        )
        return result if isinstance(result, dict) else {}

    # ============================================================ Queue (4)
    async def queue_top(self, ids: list[int | str]) -> None:
        """queue-move-top — 队列置顶。"""
        await self._request("queue-move-top", {"ids": ids})

    async def queue_up(self, ids: list[int | str]) -> None:
        """queue-move-up — 队列上移 1 位。"""
        await self._request("queue-move-up", {"ids": ids})

    async def queue_down(self, ids: list[int | str]) -> None:
        """queue-move-down — 队列下移 1 位。"""
        await self._request("queue-move-down", {"ids": ids})

    async def queue_bottom(self, ids: list[int | str]) -> None:
        """queue-move-bottom — 队列置底。"""
        await self._request("queue-move-bottom", {"ids": ids})

    # ============================================================ Blocklist / Port / Free space (3)
    async def update_blocklist(self) -> int:
        """blocklist-update — 触发 blocklist 更新，返回新条目数。"""
        result = await self._request("blocklist-update", {})
        if isinstance(result, dict):
            return int(result.get("blocklist_size", 0))
        return 0

    async def test_port(
        self, *, ip_protocol: str | None = None
    ) -> dict[str, Any]:
        """port-test — 测试入站 peer 端口是否对外开放。

        ip_protocol: "ipv4" / "ipv6" / None。
        返回：{port_is_open: bool, ip_protocol: str}。
        """
        params: dict[str, Any] = {}
        if ip_protocol:
            params["ip_protocol"] = ip_protocol
        result = await self._request("port-test", params)
        return result if isinstance(result, dict) else {}

    async def get_free_space(self, path: str) -> dict[str, Any]:
        """free-space — 查询指定目录剩余空间。

        返回：{path, size_bytes, total_size}。
        """
        result = await self._request("free-space", {"path": path})
        return result if isinstance(result, dict) else {}

    # ============================================================ Bandwidth groups (2)
    async def set_group(self, name: str, **kwargs: Any) -> None:
        """group-set — 创建/修改带宽分组。

        关键字参数：honors_session_limits / speed_limit_down / speed_limit_down_enabled
        / speed_limit_up / speed_limit_up_enabled。
        """
        if not kwargs:
            return
        params: dict[str, Any] = {"name": name, **kwargs}
        await self._request("group-set", params)

    async def get_group(
        self, name: str | list[str] | None = None
    ) -> list[dict[str, Any]]:
        """group-get — 读取带宽分组。

        name: 单个分组名 / 分组名列表 / None（全部）。
        返回：group 对象数组。
        """
        params: dict[str, Any] = {}
        if name is not None:
            params["name"] = name
        result = await self._request("group-get", params)
        if isinstance(result, dict) and "group" in result:
            return list(result["group"])
        return [] if not isinstance(result, list) else result


__all__ = [
    "SessionField",
    "TorrentField",
    "TrApiClient",
    "TrApiError",
]


# 防止 ruff 警告 unused import（base64 供未来 metainfo 编码用）
_ = base64