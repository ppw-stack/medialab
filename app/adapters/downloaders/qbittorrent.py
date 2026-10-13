"""qBittorrent 适配器（附录 A）。

基于 :class:`Qb5ApiClient`（WebUI API v2）实现 :class:`DownloaderAdapter` 契约。
qBittorrent 5.x：pause=/torrents/stop, resume=/torrents/start（v4 端点 /pause /resume 已废弃）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.adapters.downloaders.base import (
    AddTorrentPayload,
    DownloaderAdapter,
    DownloaderError,
    TorrentFile,
    TorrentFilter,
    TorrentHandle,
    TorrentInfo,
    TorrentState,
)
from app.adapters.downloaders.qbittorrent_api import Qb5ApiClient, Qb5ApiError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: qBittorrent 原生 state → TorrentState。未知状态归 UNKNOWN，由 FR-3.4 兜底行按红线中止。
_STATE_MAP: dict[str, TorrentState] = {
    "error": TorrentState.ERROR,
    "missingFiles": TorrentState.MISSING_FILES,
    "uploading": TorrentState.UPLOADING,
    "stalledUP": TorrentState.SEEDING,
    "queuedUP": TorrentState.SEEDING,
    "forcedUP": TorrentState.UPLOADING,
    "pausedUP": TorrentState.PAUSED_UP,
    "stoppedUP": TorrentState.PAUSED_UP,
    "queuedDL": TorrentState.DOWNLOADING,
    "stalledDL": TorrentState.STALLED_DL,
    "pausedDL": TorrentState.PAUSED_DL,
    "stoppedDL": TorrentState.PAUSED_DL,
    "checkingDL": TorrentState.CHECKING,
    "checkingUP": TorrentState.CHECKING,
    "checkingResumeData": TorrentState.CHECKING,
    "moving": TorrentState.CHECKING,
    "downloading": TorrentState.DOWNLOADING,
    "metaDL": TorrentState.CHECKING,
    "allocating": TorrentState.CHECKING,
    "forcedDL": TorrentState.DOWNLOADING,
}


class QbittorrentAdapter(DownloaderAdapter):
    """qBittorrent WebUI 适配器。"""

    client_type = "qbittorrent"

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
        self._qb = Qb5ApiClient(
            url,
            username=username,
            password=password,
            timeout=timeout,
            verify_tls=verify_tls,
        )

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _matches(info: TorrentInfo, filt: TorrentFilter) -> bool:
        """按 TorrentFilter 过滤：states/category/tags 三个维度。"""
        if filt.states and info.state not in filt.states:
            return False
        if filt.category and info.category != filt.category:
            return False
        return not (filt.tags and not filt.tags.issubset(info.tags))

    # ------------------------------------------------------------ 接口
    async def ping(self) -> bool:
        try:
            version = await self._qb.get_app_version()
            return bool(version)
        except Qb5ApiError as exc:
            logger.warning("qBittorrent %s 连通性检测失败：%s", self.name, exc)
            return False

    async def list_torrents(
        self, filt: TorrentFilter | None = None
    ) -> list[TorrentInfo]:
        try:
            rows: list[dict[str, Any]] = await self._qb.list_torrents()
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc

        out: list[TorrentInfo] = []
        for row in rows:
            state = _STATE_MAP.get(str(row.get("state", "")), TorrentState.UNKNOWN)
            tags = frozenset(
                t.strip()
                for t in (row.get("tags") or "").split(",")
                if t.strip()
            )
            info = TorrentInfo(
                hash=str(row.get("hash", "")),
                name=str(row.get("name", "")),
                state=state,
                progress=float(row.get("progress") or 0),
                size=int(row.get("size") or 0),
                downloaded=int(row.get("completed") or 0),
                uploaded=int(row.get("uploaded") or 0),
                save_path=row.get("save_path") or None,
                category=row.get("category") or None,
                tags=tags,
                dlspeed=int(row.get("dlspeed") or 0),
                upspeed=int(row.get("upspeed") or 0),
                num_seeds=int(row.get("num_seeds") or 0),
                num_connections=int(row.get("num_connections") or 0),
                ratio=float(row.get("ratio") or 0),
                added_at=row.get("added_on"),
                completed_at=row.get("completion_on"),
                eta=row.get("eta"),
            )
            if filt is not None and not self._matches(info, filt):
                continue
            out.append(info)
        return out

    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        """附录 A：``urls`` / ``category`` / ``tags`` / ``skip_checking``。"""
        kwargs: dict[str, Any] = {
            "category": payload.category,
            "skip_checking": payload.skip_checking,
            "paused": payload.paused,
        }
        if payload.tags:
            kwargs["tags"] = sorted(payload.tags)
        try:
            if payload.torrent_bytes is not None:
                mimetype = "application/x-bittorrent"
                files = [(f"{payload.name}.torrent", payload.torrent_bytes, mimetype)]
                await self._qb.add_torrent(torrents=files, **kwargs)
            elif payload.urls:
                await self._qb.add_torrent(urls=list(payload.urls), **kwargs)
            else:
                raise DownloaderError("添加任务缺少 urls 或 torrent_bytes")
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        # qBittorrent 不返回 hash；调用方需按 idem_key 或随后 list 查询
        return TorrentHandle(info_hash="", downloader=self.name, raw=None)

    async def remove(self, hashes: Sequence[str], *, delete_data: bool) -> bool:
        """附录 A：``deleteFiles`` 独立控制数据删除。"""
        if not hashes:
            return True
        try:
            await self._qb.delete_torrents(list(hashes), delete_files=delete_data)
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        return True

    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        if not hashes:
            return True
        # qBittorrent v5：pause=/torrents/stop, resume=/torrents/start
        try:
            if paused:
                await self._qb.pause_torrents(list(hashes))
            else:
                await self._qb.resume_torrents(list(hashes))
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        return True

    async def get_files(self, info_hash: str) -> list[TorrentFile]:
        """FR-5.2.5a 的数据来源。删除目标只能来自此返回值。"""
        try:
            rows: list[dict[str, Any]] = await self._qb.get_torrent_files(info_hash)
        except Qb5ApiError:
            return []
        out: list[TorrentFile] = []
        for row in rows:
            name = row.get("name")
            if not name:
                continue
            out.append(
                TorrentFile(
                    path=str(name),
                    size=int(row.get("size") or 0),
                    priority=int(row.get("priority") or 0),
                )
            )
        return out

    async def close(self) -> None:
        await self._qb.close()
