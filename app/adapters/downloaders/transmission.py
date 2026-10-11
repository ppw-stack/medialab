"""Transmission 适配器（附录 A）。

基于 ``TrApiClient``（rpc-spec 4.x JSON-RPC）实现 ``DownloaderAdapter`` 契约。
TR 原生 7 个 status 值（0~6）归一为 :class:`TorrentState`：
- 0 (stopped) → PAUSED_DL/PAUSED_UP（按 percentDone 判做种/下载）
- 1 (check wait) / 2 (checking) → CHECKING
- 3 (download wait) → STALLED_DL
- 4 (downloading) → DOWNLOADING
- 5 (seed wait) / 6 (seeding) → SEEDING

未知值归 UNKNOWN（由 FR-3.4 兜底行处理）。
"""

from __future__ import annotations

import base64
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
from app.adapters.downloaders.transmission_api import (
    TorrentField,
    TrApiClient,
    TrApiError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Transmission 原生 status → TorrentState。
# 0=stopped / 1=check-wait / 2=checking / 3=dl-wait / 4=downloading /
# 5=seed-wait / 6=seeding
_TR_STATUS_MAP: dict[int, TorrentState] = {
    1: TorrentState.CHECKING,
    2: TorrentState.CHECKING,
    3: TorrentState.STALLED_DL,
    4: TorrentState.DOWNLOADING,
    5: TorrentState.SEEDING,
    6: TorrentState.SEEDING,
}

#: list_torrents 默认拉取字段（覆盖 TorrentInfo 全部映射）。
_DEFAULT_FIELDS: list[TorrentField | str] = [
    TorrentField.ID,
    TorrentField.HASH_STRING,
    TorrentField.NAME,
    TorrentField.STATUS,
    TorrentField.PERCENT_DONE,
    TorrentField.TOTAL_SIZE,
    TorrentField.DOWNLOADED_EVER,
    TorrentField.UPLOADED_EVER,
    TorrentField.RATE_DOWNLOAD,
    TorrentField.RATE_UPLOAD,
    TorrentField.UPLOAD_RATIO,
    TorrentField.ETA,
    TorrentField.ADDED_DATE,
    TorrentField.DONE_DATE,
    TorrentField.DOWNLOAD_DIR,
    TorrentField.LABELS,
    TorrentField.PEERS_CONNECTED,
    TorrentField.IS_FINISHED,
    TorrentField.ERROR_STRING,
    TorrentField.FILES,
]


class TransmissionAdapter(DownloaderAdapter):
    """Transmission 4.x 适配器（FR-1）。"""

    client_type = "transmission"

    def __init__(
        self,
        name: str,
        url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        timeout: float = 15.0,
        verify_tls: bool = True,
    ) -> None:
        self.name = name
        self._tr = TrApiClient(
            url,
            username=username or None,
            password=password or None,
            timeout=timeout,
            verify_tls=verify_tls,
        )

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _map_state(row: dict[str, Any]) -> TorrentState:
        """TR 原生 status（0~6）→ TorrentState。"""
        status = int(row.get("status") or 0)
        if status == 0:
            # stopped：按完成度判做种/下载暂停
            if (
                float(row.get("percentDone") or 0) >= 1.0
                or bool(row.get("isFinished"))
            ):
                return TorrentState.PAUSED_UP
            return TorrentState.PAUSED_DL
        return _TR_STATUS_MAP.get(status, TorrentState.UNKNOWN)

    @staticmethod
    def _matches(info: TorrentInfo, filt: TorrentFilter) -> bool:
        """按 TorrentFilter 过滤：states/category/tags。"""
        if filt.states and info.state not in filt.states:
            return False
        if filt.category and info.category != filt.category:
            return False
        return not (filt.tags and not filt.tags.issubset(info.tags))

    # ------------------------------------------------------------ 接口
    async def ping(self) -> bool:
        try:
            await self._tr.get_session_stats()
            return True
        except TrApiError as exc:
            logger.warning("Transmission %s 连通性检测失败：%s", self.name, exc)
            return False

    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        kwargs: dict[str, Any] = {}
        if payload.save_path:
            kwargs["download_dir"] = payload.save_path
        if payload.paused:
            kwargs["paused"] = True
        if payload.tags:
            kwargs["labels"] = sorted(payload.tags)

        try:
            if payload.torrent_bytes is not None:
                metainfo = base64.b64encode(payload.torrent_bytes).decode()
                result = await self._tr.add_torrent(metainfo=metainfo, **kwargs)
            elif payload.urls:
                # Transmission 一次只接受一个 filename；多个 URL 顺序逐个加入
                result = {}
                for url in payload.urls:
                    single = await self._tr.add_torrent(filename=url, **kwargs)
                    if single:
                        result = single
                        break
            else:
                raise DownloaderError("添加任务缺少 urls 或 torrent_bytes")
        except TrApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc

        # result: {"torrent-added": {...}} 或 {"torrent-duplicate": {...}}
        added = result.get("torrent-added") or result.get("torrent-duplicate") or {}
        info_hash = str(added.get("hashString") or "")
        raw_id = str(added.get("id") or "")
        return TorrentHandle(info_hash=info_hash, downloader=self.name, raw=raw_id)

    async def list_torrents(
        self, filt: TorrentFilter | None = None
    ) -> list[TorrentInfo]:
        try:
            rows = await self._tr.list_torrents(fields=_DEFAULT_FIELDS)
        except TrApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc

        out: list[TorrentInfo] = []
        for row in rows:
            labels = row.get("labels") or []
            tags = frozenset(str(lbl) for lbl in labels if lbl)
            info = TorrentInfo(
                hash=str(row.get("hashString") or ""),
                name=str(row.get("name") or ""),
                state=self._map_state(row),
                progress=float(row.get("percentDone") or 0),
                size=int(row.get("totalSize") or 0),
                downloaded=int(row.get("downloadedEver") or 0),
                uploaded=int(row.get("uploadedEver") or 0),
                save_path=row.get("downloadDir") or None,
                category=None,  # TR 用 labels 替代 category
                tags=tags,
                dlspeed=int(row.get("rateDownload") or 0),
                upspeed=int(row.get("rateUpload") or 0),
                num_seeds=int(row.get("peersSendingToUs") or 0),
                num_connections=int(row.get("peersConnected") or 0),
                ratio=float(row.get("uploadRatio") or 0),
                added_at=row.get("addedDate"),
                completed_at=row.get("doneDate"),
                eta=row.get("eta"),
            )
            if filt is not None and not self._matches(info, filt):
                continue
            out.append(info)
        return out

    async def remove(self, hashes: Sequence[str], *, delete_data: bool) -> bool:
        if not hashes:
            return True
        try:
            await self._tr.remove_torrents(
                list(hashes), delete_local_data=delete_data
            )
        except TrApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        return True

    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        if not hashes:
            return True
        try:
            if paused:
                await self._tr.stop_torrents(list(hashes))
            else:
                await self._tr.start_torrents_now(list(hashes))
        except TrApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        return True

    async def get_files(self, info_hash: str) -> list[TorrentFile]:
        """FR-5.2.5a 的数据来源：取种子内文件清单。"""
        try:
            rows = await self._tr.list_torrents(
                ids=[info_hash],
                fields=[TorrentField.FILES, TorrentField.FILE_STATS],
            )
        except TrApiError as exc:
            logger.warning("Transmission %s 拉取文件清单失败：%s", self.name, exc)
            return []
        if not rows:
            return []
        files: list[dict[str, Any]] = rows[0].get("files") or []
        out: list[TorrentFile] = []
        for f in files:
            path = str(f.get("name") or "")
            if not path:
                continue
            out.append(
                TorrentFile(
                    path=path,
                    size=int(f.get("length") or 0),
                    priority=int(f.get("priority") or 0),
                )
            )
        return out

    async def close(self) -> None:
        await self._tr.close()
