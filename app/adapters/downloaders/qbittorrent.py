"""qBittorrent 适配器（附录 A）。

基于 :class:`Qb5ApiClient`（WebUI API v2）实现 :class:`DownloaderAdapter` 契约。
qBittorrent 5.x：pause=/torrents/stop, resume=/torrents/start（v4 端点 /pause /resume 已废弃）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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
from app.adapters.downloaders.qbittorrent_api import Qb5ApiClient, Qb5ApiError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: **批量**操作（拉全量种子列表）的单次调用超时（秒）。
#:
#: 默认的 15s 是给普通 API 调用用的——那些都该快速失败。但辅种站的
#: ``/torrents/info`` 要返回**几千个**种子（实测 7000+ 个），响应体很大，
#: qB 繁忙时更慢——用 15s 会**必然超时**。
#:
#: 故批量路径显式用这个更长的超时。
BULK_TIMEOUT = 180.0

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

#: qB 原生 state → TR 语义 status 码（仅供巡查展示，不做业务判定）。
#: 0=stopped 1=check_pending 2=checking 3=download_pending 4=downloading
#: 5=seed_pending 6=seeding
_QB_STATUS_CODE: dict[str, int] = {
    "pausedUP": 0,
    "stoppedUP": 0,
    "pausedDL": 0,
    "stoppedDL": 0,
    "error": 0,
    "missingFiles": 0,
    "queuedUP": 5,
    "forcedUP": 6,
    "stalledUP": 6,
    "uploading": 6,
    "queuedDL": 3,
    "forcedDL": 4,
    "stalledDL": 4,
    "downloading": 4,
    "metaDL": 2,
    "allocating": 2,
    "checkingDL": 2,
    "checkingUP": 2,
    "checkingResumeData": 2,
    "moving": 2,
}


def _completion_to_ts(value: object) -> int | None:
    """qB ``completion_on`` → 完成时间戳；未完成返回 ``None``。

    qBittorrent 对**未完成**的种子返回 ``-1``（不是 0 / None）。
    ``-1`` 是真值，若直接写入，下游 ``if info.completed_at:`` 会把
    未完成的种子误判为"已完成"。

    qB 该字段可能是 int 或 float（不同版本 / 平台不一致），统一转 int；
    字符串数字也接受。非法值一律按"未完成"处理（None）。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        ts = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def _qb_top_names(content_path: str, save_path: str) -> list[str]:
    """从 qB 的 ``content_path`` 提取**顶层**名字（目录名或单文件名）。

    qB 的 ``content_path`` 是绝对路径：
    - 目录型种子：``<save_path>/<根目录>/<子目录>/<文件>`` → 顶层是根目录名
    - 单文件种子：``<save_path>/<文件>`` → 顶层就是文件名

    取**相对 save_path 的首段**（实测 7296/7296 全部可取）。不能用
    ``content_path`` 的倒数第二段——单文件种子时那是父目录名，会把
    同目录的多个种子混成一个（实测 3665 个种子会错）。

    与 TR 的 ``files[].name`` 顶层段语义一致（见
    :func:`app.adapters.downloaders.transmission._top_names`）。
    """
    if not content_path:
        return []
    path = content_path.rstrip("/")
    base = save_path.rstrip("/")
    if base and path.startswith(base + "/"):
        path = path[len(base) + 1 :]
    top = path.split("/", 1)[0]
    return [top] if top else []


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
            rows: list[dict[str, Any]] = await self._qb.list_torrents(
                http_timeout=BULK_TIMEOUT
            )
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
                # completed_bytes 与 downloaded 同源（都是 total_wanted_done），
                # 但语义独立：completed_bytes = 已完成字节数，downloaded 历史上
                # 被前端当作「已下载」列展示。两者当前都取 row["completed"]。
                completed_bytes=int(row.get("completed") or 0),
                save_path=row.get("save_path") or None,
                category=row.get("category") or None,
                tags=tags,
                dlspeed=int(row.get("dlspeed") or 0),
                upspeed=int(row.get("upspeed") or 0),
                num_seeds=int(row.get("num_seeds") or 0),
                num_connections=int(row.get("num_connections") or 0),
                ratio=float(row.get("ratio") or 0),
                added_at=row.get("added_on"),
                # qB completion_on：未完成时返 -1（epoch 时间戳），不能直接写入。
                # 下游 `if info.completed_at:` 会把 -1 当真值误判为已完成。
                completed_at=_completion_to_ts(row.get("completion_on")),
                eta=row.get("eta"),
            )
            if filt is not None and not self._matches(info, filt):
                continue
            out.append(info)
        return out

    async def list_torrents_with_errors(self) -> list[dict[str, Any]]:
        """拉取全量种子，规范化错误信息（巡查用）。

        qBittorrent 没有 TR 那种 tracker 错误码——它的错误体现在
        ``state`` 上（``error`` / ``missingFiles``）。

        映射到统一契约：
        - ``error_code`` 0 = 无错；1 = tracker 警告；2 = tracker 错误；3 = 数据丢失
        - ``error_string`` 人类可读描述
        - ``state`` 归一化的 :class:`TorrentState` 值（巡查据此判定是否已暂停）

        注意：qB ``/torrents/info`` **不返回** tracker 字段，故不能从 row
        直接取 tracker 错误文案。如需 tracker 级别详情，需按 hash 单独调
        ``/api/v2/torrents/trackers``（开销大，本巡查场景不需要）。
        本方法仅按 ``state`` 给出归一化错误码与中文描述。
        """
        try:
            rows: list[dict[str, Any]] = await self._qb.list_torrents(
                http_timeout=BULK_TIMEOUT
            )
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc

        out: list[dict[str, Any]] = []
        for row in rows:
            state = str(row.get("state", ""))
            error_code = 0
            error_string = ""
            if state == "missingFiles":
                error_code = 3
                error_string = "本地数据丢失"
            elif state == "error":
                error_code = 2
                error_string = "做种错误"
            out.append({
                "id": int(row.get("id") or 0),
                "hash": str(row.get("hash", "")),
                "name": str(row.get("name", "")),
                "size": int(row.get("size") or 0),
                # qB 无原生 status 码，用 state 字符串映射到 TR 语义（仅供展示）
                "status": _QB_STATUS_CODE.get(state, -1),
                "error_code": error_code,
                "error_string": error_string,
                # 归一化状态（巡查判定「是否已暂停」用）
                "state": _STATE_MAP.get(state, TorrentState.UNKNOWN).value,
                # 已校验有效的字节数（辅种空间统计用）。
                # ⚠️ qB 的 ``completed`` 是 libtorrent 的
                # ``total_wanted_done``——**不含** unwanted 文件与冗余块，
                # 故它会**小于**磁盘上的真实占用。TR 的 haveValid 才是
                # 精确口径。两者不可直接横向比较。
                "completed_bytes": int(row.get("completed") or 0),
                # 下载目录（按目录统计占用用）
                "save_path": row.get("save_path") or None,
                # 顶层名（孤儿文件统计用）：相对 save_path 的首段
                "top_names": _qb_top_names(
                    str(row.get("content_path") or ""),
                    str(row.get("save_path") or ""),
                ),
            })
        return out

    async def get_disk_space(self, path: str | None = None) -> DiskSpace:
        """查询磁盘剩余空间（辅种空间统计用）。

        qBittorrent 的 ``sync/maindata`` 里 ``server_state`` 带
        ``free_space_on_disk``——**只有剩余空间，没有总量**。

        故 ``total_bytes`` 返回 None（前端据此不画进度条，而不是画成
        「磁盘已满」）。

        注：``sync/maindata`` 的响应体含全部种子信息（较大），但它只
        请求一次、可复用 rid 做增量同步，开销可接受。
        """
        try:
            data = await self._qb.get_main_data()
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        state = data.get("server_state") or {}
        free_bytes = int(state.get("free_space_on_disk") or 0)
        return DiskSpace(free_bytes=free_bytes, total_bytes=None)

    async def add_torrent(self, payload: AddTorrentPayload) -> TorrentHandle:
        """附录 A：``urls`` / ``category`` / ``tags`` / ``skip_checking``。

        qBittorrent 5.1+ /api/v2/torrents/add 实际**返 dict**（Python repr
        格式），含 ``added_torrent_ids`` 列表（首个元素就是真 info_hash）。
        5.0 旧版只返 "Ok." 字符串，无法直接拿 hash，需调用方 fallback 到
        ``_reconcile_hash`` 按 name 匹配。

        返回 :class:`TorrentHandle`：
        - ``info_hash`` 写真 hash（5.1+）或空（5.0 旧版）
        - ``raw`` dict 完整响应（debug 用）
        """
        kwargs: dict[str, Any] = {
            "category": payload.category,
            "skip_checking": payload.skip_checking,
            "paused": payload.paused,
        }
        if payload.save_path:
            kwargs["savepath"] = payload.save_path
        if payload.tags:
            kwargs["tags"] = sorted(payload.tags)
        result: dict[str, object] = {
            "added_torrent_ids": [],
            "failure_count": 0,
            "pending_count": 0,
            "success_count": 0,
            "ok": False,
            "raw": "",
        }
        try:
            if payload.torrent_bytes is not None:
                mimetype = "application/bittorrent"
                files = [(f"{payload.name}.torrent", payload.torrent_bytes, mimetype)]
                result = await self._qb.add_torrent(torrents=files, **kwargs)
            elif payload.urls:
                result = await self._qb.add_torrent(urls=list(payload.urls), **kwargs)
            else:
                raise DownloaderError("添加任务缺少 urls 或 torrent_bytes")
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc

        # 失败识别：qB 5.x 失败时以 200 + failure_count 返回而非异常。
        # 不在这里抛错，调用方会把失败当"已推送"写 push_status（游离种）。
        failure_count = result.get("failure_count")
        success_count = result.get("success_count")
        if (
            isinstance(failure_count, int)
            and isinstance(success_count, int)
            and failure_count > 0
            and success_count == 0
        ):
            raw = str(result.get("raw") or "")[:200]
            raise DownloaderError(f"qBittorrent 添加任务失败：{raw}")

        # 解析真 hash（5.1+ 返 added_torrent_ids[0]）
        info_hash = ""
        added_ids = result.get("added_torrent_ids")
        if isinstance(added_ids, list) and added_ids:
            first = added_ids[0]
            if isinstance(first, str) and len(first) == 40:
                info_hash = first
        raw_value = result.get("raw")
        return TorrentHandle(
            info_hash=info_hash,
            downloader=self.name,
            raw=raw_value if isinstance(raw_value, str) else None,
        )

    async def remove(self, hashes: Sequence[str], *, delete_data: bool) -> bool:
        """附录 A：``deleteFiles`` 独立控制数据删除。"""
        if not hashes:
            return True
        validate_infohashes(hashes)
        try:
            await self._qb.delete_torrents(list(hashes), delete_files=delete_data)
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        return True

    async def set_pause(self, hashes: Sequence[str], paused: bool) -> bool:
        if not hashes:
            return True
        validate_infohashes(hashes)
        # qBittorrent v5：pause=/torrents/stop, resume=/torrents/start
        try:
            if paused:
                await self._qb.pause_torrents(list(hashes))
            else:
                await self._qb.resume_torrents(list(hashes))
        except Qb5ApiError as exc:
            raise DownloaderError(str(exc), transient=exc.transient) from exc
        return True

    async def close(self) -> None:
        await self._qb.close()
