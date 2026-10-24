"""Transmission RPC 适配器（FR-1）。

协议：HTTP/HTTPS POST /transmission/rpc，header ``X-Transmission-Session-Id``。
认证：HTTP Basic（username/password）。
契约：基类 5(含抽象 remove/set_pause) + list_torrents_with_errors + close 全部实现；不引入能力声明。
差异（与 qBittorrent）：
    - add_torrent **不支持同时多个** URL/magnet（决策 1，硬拒收）
    - 无原生 RSS（FR-1 由本系统自拉补齐）
    - 无逐任务限速（FR-3.6 整局限速）
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from typing import Any

import httpx

from app.adapters.downloaders.base import (
    AddTorrentPayload,
    DiskSpace,
    DownloaderAdapter,
    DownloaderError,
    TorrentFilter,
    TorrentHandle,
    TorrentInfo,
    TorrentState,
    validate_infohashes,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: **批量**操作（拉全量种子列表）的单次调用超时（秒）。
#:
#: 默认的 15s 是给普通 RPC（增删改、查状态）用的——那些都该快速失败。
#: 但辅种站的 ``torrent-get`` 要返回**几千个**种子（实测 4600+ 个、
#: 响应体 ~1.5MB），TR 繁忙时更慢——用 15s 会**必然超时**
#: （实测辅种站全量拉取要 20s+，TR 卡时超过 120s）。
#:
#: 故批量路径显式用这个更长的超时。
BULK_TIMEOUT = 180.0

#: list_torrents 一次拉取的字段全集
_FIELDS_ALL: tuple[str, ...] = (
    "id",
    "hashString",
    "name",
    "status",
    "errorString",
    "totalSize",
    "percentDone",
    "downloadedEver",
    "uploadedEver",
    # 已校验有效的字节数——用于「是否已完成」的字节口径判定。
    # 不能用 downloadedEver（累计下载量，含重复下载）。
    "haveValid",
    "downloadDir",
    "rateDownload",
    "rateUpload",
    "secondsDownloading",
    "secondsSeeding",
    "files",
    "fileStats",
    # 与 qBittorrent 视图对齐（FR-1.1 统一契约）。
    "labels",                # TR 用「labels」承载原自定义标签
    "uploadRatio",           # 上传/下载比
    "eta",                   # 预计剩余秒数；完成时 TR 返 0
    "addedDate",             # 入队 unix 秒
    "doneDate",              # 完成 unix 秒；未完成返 0
    "peersConnected",        # qB 字段 num_connections 对应值
    "isFinished",            # 用于 completed_at 兜底
)

#: list_torrents_with_errors 一次拉取的字段（含 error/errorString）
#:
#: ``percentDone`` / ``rateUpload`` 是为了把 TR 原生 status 码换算成
#: 归一化的 :class:`TorrentState`（巡查据此判定「是否已暂停」）。
#: 缺这两个字段时 status=6 只能粗判成 SEEDING，无法区分 UPLOADING。
#:
#: ``haveValid`` / ``downloadDir`` 用于辅种空间统计（实际占用 + 按目录
#: 分布）——与 :meth:`list_torrents` 的 ``_FIELDS_ALL`` 口径一致。
_FIELDS_WITH_ERRORS: tuple[str, ...] = (
    "id",
    "hashString",
    "name",
    "status",
    "sizeWhenDone",
    "error",           # 0=OK 1=tracker警告 2=tracker错误 3=本地数据丢失
    "errorString",     # 人类可读错误描述
    "percentDone",
    "rateUpload",
    "haveValid",       # 已校验有效的字节数（= 磁盘实际占用）
    "downloadDir",     # 下载目录（按目录统计占用用）
)


class TransmissionAdapter(DownloaderAdapter):
    """Transmission RPC 适配器。"""

    client_type = "transmission"

    def __init__(
        self,
        name: str,
        url: str,
        username: str,
        password: str,
        *,
        timeout: float = 15.0,
        verify_tls: bool = True,
    ) -> None:
        self.name = name
        self._rpc_url = url.rstrip("/") + "/transmission/rpc"
        auth: httpx.BasicAuth | None = (
            httpx.BasicAuth(username, password) if username else None
        )
        self._client = httpx.AsyncClient(
            auth=auth,
            timeout=timeout,
            verify=verify_tls,
        )
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()

    # -------------------------------------------------- 内部
    async def _ensure_session_id(self) -> str:
        """第一次握手拿 session-id。缓存后由 _rpc 在 409 时刷新一次。"""
        async with self._session_lock:
            if self._session_id:
                return self._session_id
            try:
                resp = await self._client.post(
                    self._rpc_url,
                    json={"method": "session-get", "tag": "seedhelper"},
                )
            except httpx.HTTPError as exc:
                raise DownloaderError(
                    f"TR 网络错误：{exc}", transient=True
                ) from exc
            if resp.status_code == 409:
                hdr = resp.headers.get("X-Transmission-Session-Id")
                if not hdr:
                    raise DownloaderError(
                        "TR 409 响应缺少 X-Transmission-Session-Id",
                        transient=False,
                    )
                self._session_id = hdr
                assert self._session_id is not None
                return self._session_id
            if resp.status_code in (401, 403):
                raise DownloaderError(
                    f"TR 认证失败 {resp.status_code}", transient=False
                )
            if resp.status_code >= 500:
                raise DownloaderError(
                    f"TR 服务端错误 {resp.status_code}", transient=True
                )
            raise DownloaderError(
                f"TR session-get {resp.status_code}：{resp.text[:200]}",
                transient=False,
            )

    async def _rpc(
        self,
        method: str,
        arguments: dict[str, Any] | None = None,
        *,
        http_timeout: float | None = None,
    ) -> dict[str, Any]:
        """通用 RPC 调用；处理 session-id 409 重取。

        :param http_timeout: **单次 HTTP 请求的超时**（秒）。None = 用
            client 的默认超时。批量操作（如 ``torrent-get`` 拉几千个种子）
            需要更长的超时——辅种站实测全量拉取要 20s+，默认 15s 会必然
            超时。
        """
        body: dict[str, Any] = {"method": method, "tag": "seedhelper"}
        if arguments:
            body["arguments"] = arguments
        sid = await self._ensure_session_id()
        headers = {"X-Transmission-Session-Id": sid}
        try:
            resp = await self._client.post(
                self._rpc_url,
                json=body,
                headers=headers,
                timeout=http_timeout,
            )
        except httpx.HTTPError as exc:
            raise DownloaderError(
                f"TR 网络错误：{exc}", transient=True
            ) from exc
        if resp.status_code == 409:
            # session-id 过期：取响应头里的新值，重试一次
            new_sid = resp.headers.get("X-Transmission-Session-Id")
            if new_sid:
                self._session_id = new_sid
                try:
                    resp = await self._client.post(
                        self._rpc_url,
                        json=body,
                        headers={"X-Transmission-Session-Id": new_sid},
                    )
                except httpx.HTTPError as exc:
                    raise DownloaderError(
                        f"TR 重试网络错误：{exc}", transient=True
                    ) from exc
        if resp.status_code in (401, 403):
            raise DownloaderError(
                f"TR 认证失败 {resp.status_code}", transient=False
            )
        if resp.status_code >= 500:
            raise DownloaderError(
                f"TR 服务端错误 {resp.status_code}", transient=True
            )
        if resp.status_code >= 400:
            raise DownloaderError(
                f"TR {resp.status_code}：{resp.text[:200]}", transient=False
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise DownloaderError(
                f"TR 响应非 JSON：{exc}", transient=False
            ) from exc
        if data.get("result") != "success":
            raise DownloaderError(
                f"TR {method} 失败：{data}", transient=False
            )
        return data.get("arguments") or {}

    @staticmethod
    def _status_to_state(
        status: int, percent: float, rate_ul: int
    ) -> TorrentState:
        """TR status 码 → TorrentState。

        TR RPC spec：
        0=stopped, 1=queued verify, 2=verifying,
        3=queued download, 4=downloading,
        5=queued seed, 6=seeding

        ``rate_ul`` 为上传速率（``rateUpload``）：status=6 表示已完成在做种，
        有上传活动（``rate_ul > 0``）时归为 UPLOADING，否则 SEEDING。
        之前误用 ``rateDownload``（下载速率），做种阶段下载速率本就接近 0，
        会导致几乎所有做种种子都被判为 SEEDING 而非 UPLOADING。
        """
        if status == 0:
            if percent >= 1.0:
                return TorrentState.PAUSED_UP
            return TorrentState.PAUSED_DL
        if status in (1, 2):
            return TorrentState.CHECKING
        if status in (3, 4):
            return TorrentState.DOWNLOADING
        if status == 5:
            return TorrentState.SEEDING
        if status == 6:
            return TorrentState.UPLOADING if rate_ul > 0 else TorrentState.SEEDING
        return TorrentState.UNKNOWN

    @staticmethod
    def _row_to_info(row: dict[str, Any]) -> TorrentInfo:
        percent = float(row.get("percentDone") or 0)
        rate_dl = int(row.get("rateDownload") or 0)
        rate_ul = int(row.get("rateUpload") or 0)
        raw_status = row.get("status")
        if raw_status is None:
            raw_status = -1
        state = TransmissionAdapter._status_to_state(
            int(raw_status), percent, rate_ul
        )
        if row.get("errorString") and state in (
            TorrentState.DOWNLOADING,
            TorrentState.STALLED_DL,
            TorrentState.SEEDING,
            TorrentState.UPLOADING,
        ):
            state = TorrentState.ERROR  # FR-3.4 兜底
        # TR 没有「category」字段；把第一个 label 当 category（FR-1.1 统一视图
        # 在下载器控制台改标签即可表达分组），其余当 tags。
        labels = list(row.get("labels") or [])
        tags = frozenset(labels)
        category = labels[0] if labels else None
        done_date = int(row.get("doneDate") or 0)
        completed_at = done_date if done_date and row.get("isFinished") else None
        eta_raw = row.get("eta")
        eta = int(eta_raw) if isinstance(eta_raw, (int, float)) and eta_raw else None
        return TorrentInfo(
            hash=row.get("hashString", ""),
            name=row.get("name", ""),
            state=state,
            progress=percent,
            size=int(row.get("totalSize") or 0),
            downloaded=int(row.get("downloadedEver") or 0),
            uploaded=int(row.get("uploadedEver") or 0),
            # 已校验有效的字节数（TR 的「已完成」字节口径）。
            # 不能用 downloadedEver——那是累计下载量，含重复下载。
            completed_bytes=int(row.get("haveValid") or 0),
            save_path=row.get("downloadDir") or None,
            category=category,
            tags=tags,
            dlspeed=rate_dl,
            upspeed=int(row.get("rateUpload") or 0),
            num_seeds=0,  # ponytail: TR RPC 无 seed 数聚合，UI 显示「-」
            num_connections=int(row.get("peersConnected") or 0),
            ratio=float(row.get("uploadRatio") or 0),
            added_at=int(row.get("addedDate") or 0) or None,
            completed_at=completed_at,
            eta=eta,
        )

    @staticmethod
    def _matches(info: TorrentInfo, filt: TorrentFilter) -> bool:
        if filt.states and info.state not in filt.states:
            return False
        if filt.category and info.category != filt.category:
            return False
        return not (filt.tags and not filt.tags.issubset(info.tags))

    # -------------------------------------------------- 接口
    async def ping(self) -> bool:
        try:
            await self._rpc("session-get")
            return True
        except DownloaderError as exc:
            logger.warning("TR %s 连通性检测失败：%s", self.name, exc)
            return False

    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        # 决策 1：硬约束拒收多个 URL（结构性错误，不进重试队列）
        if len(payload.urls) > 1:
            raise DownloaderError(
                "Transmission add_torrent 不支持同时多个 URL/magnet",
                transient=False,
            )
        if payload.torrent_bytes is not None and payload.urls:
            raise DownloaderError(
                "torrent_bytes 与 urls 互斥",
                transient=False,
            )
        if payload.torrent_bytes is None and not payload.urls:
            raise DownloaderError(
                "添加任务缺少 urls 或 torrent_bytes", transient=False
            )

        arguments: dict[str, Any] = {"paused": payload.paused}
        if payload.save_path:
            arguments["download_dir"] = payload.save_path
        # TR 无 category 字段，把 payload.category 与 tags 合并到 labels（第一个
        # 为 category，保持 _row_to_info 反向解析一致）。
        labels: list[str] = []
        if payload.category:
            labels.append(payload.category)
        if payload.tags:
            for t in payload.tags:
                if t and t not in labels:
                    labels.append(t)
        if labels:
            arguments["labels"] = labels
        if payload.torrent_bytes is not None:
            arguments["metainfo"] = base64.b64encode(
                payload.torrent_bytes
            ).decode()
        elif payload.urls:
            arguments["filename"] = payload.urls[0]

        resp = await self._rpc("torrent-add", arguments)
        torrent_obj = (
            resp.get("torrent-added") or resp.get("torrent-duplicate") or {}
        )
        return TorrentHandle(
            info_hash=torrent_obj.get("hashString", "") or "",
            downloader=self.name,
            raw=str(torrent_obj) if torrent_obj else None,
        )

    async def list_torrents(
        self, filt: TorrentFilter | None = None
    ) -> list[TorrentInfo]:
        """列出任务（批量路径，用 :data:`BULK_TIMEOUT`）。

        ``_FIELDS_ALL`` 比 ``_FIELDS_WITH_ERRORS`` 多（files / fileStats 等），
        辅种站的响应体更大——更需要长超时。
        """
        resp = await self._rpc(
            "torrent-get",
            {"fields": list(_FIELDS_ALL)},
            http_timeout=BULK_TIMEOUT,
        )
        rows = resp.get("torrents") or []
        out: list[TorrentInfo] = []
        for row in rows:
            info = self._row_to_info(row)
            if filt is not None and not self._matches(info, filt):
                continue
            out.append(info)
        return out

    async def remove(
        self, hashes: Sequence[str], *, delete_data: bool
    ) -> bool:
        if not hashes:
            return True
        validate_infohashes(hashes)
        await self._rpc(
            "torrent-remove",
            {"ids": list(hashes), "delete-local-data": bool(delete_data)},
        )
        return True

    async def set_pause(
        self, hashes: Sequence[str], paused: bool
    ) -> bool:
        if not hashes:
            return True
        validate_infohashes(hashes)
        method = "torrent-stop" if paused else "torrent-start"
        await self._rpc(method, {"ids": list(hashes)})
        return True

    async def list_torrents_with_errors(self) -> list[dict[str, Any]]:
        """拉取全量种子，保留 TR 原生 error / errorString（巡查用）。

        额外给出归一化的 ``state``（:class:`TorrentState` 值）——巡查
        判定「是否已暂停」时要用。注意 TR 的 ``errorString`` 表示
        tracker 报错，种子的**传输状态**仍由 ``status`` 决定（两者是
        正交的），故 state 只按 status 换算，不被 errorString 覆盖。

        **超时用 :data:`BULK_TIMEOUT`**：辅种站全量 4600+ 个种子的响应
        实测要 20s+（TR 卡时更久），默认的 15s 会必然超时。
        """
        resp = await self._rpc(
            "torrent-get",
            {"fields": list(_FIELDS_WITH_ERRORS)},
            http_timeout=BULK_TIMEOUT,
        )
        out: list[dict[str, Any]] = []
        for row in resp.get("torrents") or []:
            raw_status = row.get("status")
            status = int(raw_status) if raw_status is not None else -1
            state = self._status_to_state(
                status,
                float(row.get("percentDone") or 0),
                int(row.get("rateUpload") or 0),
            )
            out.append({
                "id": int(row.get("id") or 0),
                "hash": str(row.get("hashString") or ""),
                "name": str(row.get("name") or ""),
                "size": int(row.get("sizeWhenDone") or row.get("totalSize") or 0),
                "status": status,
                "error_code": int(row.get("error") or 0),
                "error_string": str(row.get("errorString") or ""),
                # 归一化状态（巡查判定「是否已暂停」用）
                "state": state.value,
                # 已校验有效的字节数（= 磁盘实际占用，TR 口径精确）
                "completed_bytes": int(row.get("haveValid") or 0),
                # 下载目录（按目录统计占用用）
                "save_path": row.get("downloadDir") or None,
            })
        return out

    async def get_disk_space(self, path: str | None = None) -> DiskSpace:
        """查询磁盘剩余空间（辅种空间统计用）。

        TR 的 ``free-space`` RPC 需要一个**存在的目录**——它返回该目录
        所在**分区**的剩余/总量。

        :param path: 参考目录。None 时用 ``/``（根分区）。传辅种种子的
            ``downloadDir`` 更准确（能定位到辅种数据实际所在的那个分区）。
        """
        target = path or "/"
        try:
            resp = await self._rpc("free-space", {"path": target})
        except DownloaderError as exc:
            raise DownloaderError(
                f"TR free-space 失败（{target}）：{exc}", transient=exc.transient
            ) from exc
        free_bytes = int(resp.get("size-bytes") or 0)
        total_bytes = int(resp.get("total_size") or 0) or None
        return DiskSpace(free_bytes=free_bytes, total_bytes=total_bytes)

    async def close(self) -> None:
        await self._client.aclose()