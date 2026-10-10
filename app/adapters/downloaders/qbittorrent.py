"""qBittorrent 适配器（附录 A）。

WebUI API v2，见 https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

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
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 客户端 → 状态映射。未知状态归 UNKNOWN，由 FR-3.4 兜底行按红线中止。
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
        self._base = url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            verify=verify_tls,
            follow_redirects=True,
            headers={"Referer": self._base, "User-Agent": "SeedHelper/0.1"},
        )
        self._authed = False
        self._auth_lock = asyncio.Lock()

    # ------------------------------------------------------------ 内部
    async def _ensure_auth(self) -> None:
        """登录取 Cookie（附录 A）。401/403 自动重登一次。"""
        if self._authed:
            return
        async with self._auth_lock:
            if self._authed:  # double-check：其他协程可能已登录
                return
            try:
                resp = await self._client.post(
                    "/api/v2/auth/login",
                    data={"username": self._username, "password": self._password},
                )
                # qBittorrent 5.x 登录成功返回 200 或 204（No Content）；
                # 失败时返回 200 但 body 含 "Fails"/"Ok."。
                if resp.status_code not in (200, 204) or "Fails" in resp.text:
                    raise DownloaderError("qBittorrent 登录失败", transient=False)
                self._authed = True
            except httpx.HTTPError as exc:
                raise DownloaderError(f"qBittorrent 网络错误：{exc}", transient=True) from exc

    async def _post_retry(self, path: str, **kw: Any) -> httpx.Response:
        await self._ensure_auth()
        try:
            resp = await self._client.post(path, **kw)
        except httpx.HTTPError as exc:
            raise DownloaderError(f"qBittorrent 网络错误：{exc}", transient=True) from exc
        if resp.status_code in {401, 403}:
            self._authed = False
            await self._ensure_auth()
            try:
                resp = await self._client.post(path, **kw)
            except httpx.HTTPError as exc:
                raise DownloaderError(
                    f"qBittorrent 重试失败：{exc}", transient=True
                ) from exc
        if resp.status_code >= 500:
            raise DownloaderError(
                f"qBittorrent 服务端错误 {resp.status_code}", transient=True
            )
        if resp.status_code >= 400:
            raise DownloaderError(
                f"qBittorrent 请求失败 {resp.status_code}：{resp.text[:200]}",
                transient=False,
            )
        return resp

    async def _get_retry(self, path: str, **kw: Any) -> httpx.Response:
        await self._ensure_auth()
        try:
            resp = await self._client.get(path, **kw)
        except httpx.HTTPError as exc:
            raise DownloaderError(f"qBittorrent 网络错误：{exc}", transient=True) from exc
        if resp.status_code in {401, 403}:
            self._authed = False
            await self._ensure_auth()
            try:
                resp = await self._client.get(path, **kw)
            except httpx.HTTPError as exc:
                raise DownloaderError(
                    f"qBittorrent 重试失败：{exc}", transient=True
                ) from exc
        if resp.status_code >= 500:
            raise DownloaderError(
                f"qBittorrent 服务端错误 {resp.status_code}", transient=True
            )
        if resp.status_code >= 400:
            raise DownloaderError(
                f"qBittorrent 请求失败 {resp.status_code}：{resp.text[:200]}",
                transient=False,
            )
        return resp

    @staticmethod
    def _ts(value: int | None) -> datetime | None:
        """qBittorrent 的 Unix 秒 → naive UTC。"""
        if not value or value < 0:
            return None
        return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)

    # ------------------------------------------------------------ 接口
    async def ping(self) -> bool:
        try:
            resp = await self._get_retry("/api/v2/app/version")
            return bool(resp.text.strip())
        except DownloaderError as exc:
            logger.warning("qBittorrent %s 连通性检测失败：%s", self.name, exc)
            return False

    async def list_torrents(self, filt: TorrentFilter | None = None) -> list[TorrentInfo]:
        resp = await self._get_retry("/api/v2/torrents/info")
        try:
            rows: list[dict[str, Any]] = resp.json()
        except ValueError as exc:
            raise DownloaderError(f"qBittorrent 响应非 JSON：{exc}", transient=False) from exc
        out: list[TorrentInfo] = []
        for row in rows:
            state = _STATE_MAP.get(row.get("state", ""), TorrentState.UNKNOWN)
            tags = frozenset(
                t.strip() for t in (row.get("tags") or "").split(",") if t.strip()
            )
            info = TorrentInfo(
                hash=row.get("hash", ""),
                name=row.get("name", ""),
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
            if filt is not None and not _matches(info, filt):
                continue
            out.append(info)
        return out

    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        """附录 A：``urls`` / ``savepath`` / ``category`` / ``tags`` / ``skip_checking``。"""
        data: dict[str, str] = {
            "savepath": payload.save_path or "",
            "skip_checking": "true" if payload.skip_checking else "false",
        }
        if payload.paused:
            data["paused"] = "true"
        if payload.tags:
            data["tags"] = ",".join(sorted(payload.tags))
        files: dict[str, Any] = {}
        if payload.torrent_bytes is not None:
            files = {"torrents": (f"{payload.name}.torrent", payload.torrent_bytes)}
        elif payload.urls:
            data["urls"] = "\n".join(payload.urls)
        else:
            raise DownloaderError("添加任务缺少 urls 或 torrent_bytes")
        resp = await self._post_retry("/api/v2/torrents/add", data=data, files=files or None)
        # qBittorrent 不返回 hash；调用方需按 idem_key 或随后 list 查询
        return TorrentHandle(info_hash="", downloader=self.name, raw=resp.text)

    async def remove(self, hashes: Sequence[str], *, delete_data: bool) -> bool:
        """附录 A：``deleteFiles`` 独立控制数据删除。"""
        if not hashes:
            return True
        await self._post_retry(
            "/api/v2/torrents/delete",
            data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_data else "false"},
        )
        return True

    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        if not hashes:
            return True
        action = "pause" if paused else "resume"
        await self._post_retry(
            f"/api/v2/torrents/{action}", data={"hashes": "|".join(hashes)}
        )
        return True

    async def get_files(self, info_hash: str) -> list[TorrentFile]:
        """FR-5.2.5a 的数据来源。删除目标只能来自此返回值。"""
        try:
            resp = await self._get_retry("/api/v2/torrents/files", params={"hash": info_hash})
        except DownloaderError:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [
            TorrentFile(path=row.get("name", ""), size=int(row.get("size") or 0))
            for row in data
            if row.get("name")
        ]

    async def close(self) -> None:
        await self._client.aclose()