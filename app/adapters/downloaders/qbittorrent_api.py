"""qBittorrent 5.0 WebUI API 工具类。

基于 qBittorrent Wiki「WebUI API (qBittorrent 5.0)」文档
（https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)）。

与 ``app.adapters.downloaders.qbittorrent.QbittorrentAdapter`` 解耦：
- 直接接受 base_url + 用户名密码，不依赖 ``DownloaderAdapter`` 契约
- 仅作为后端服务层工具使用（如 cleanup、future stats 收集等）
- 不暴露前端 API（前端路由只走 ``QbittorrentAdapter``）

排除章节（业务不使用）：RSS、Search、Log、Application preferences/cookies、shutdown。

包含章节：
- Authentication (2 endpoints)
- Application 核心 (3 endpoints)
- Transfer info (8 endpoints)
- Sync (2 endpoints)
- Torrent management (46 endpoints)

合计 61 个端点。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class Qb5ApiError(RuntimeError):
    """qBittorrent 5.0 API 调用错误。

    transient=True：调用方可重试（5xx / 网络错误 / 401/403 重登后仍失败）。
    transient=False：调用方不应重试（415 torrent 无效、409 队列未启用、404 hash 找不到等）。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


class Qb5ApiClient:
    """qBittorrent 5.0 WebUI API 客户端（独立工具类）。

    用法::

        async with Qb5ApiClient(base_url, username=..., password=...) as qb:
            info = await qb.get_transfer_info()
            await qb.pause_torrents([hash])

    内部维护认证状态（首次调用自动 login；401/403 自动重登一次）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float = 15.0,
        verify_tls: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._initial_cookies = dict(cookies or {})
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._authed = False
        self._auth_lock = asyncio.Lock()
        # qB 5.x 失败登录 5 次后 ban IP 30 分钟。记录 ban 截止时间，避免重复触发 ban 计数。
        # 详见 https://github.com/qbittorrent/qBittorrent/wiki/REST-API-(5.0)#login
        self._ban_until: float = 0.0
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            verify=verify_tls,
            cookies=self._initial_cookies or None,
            headers={"User-Agent": "SeedHelper/0.1 (qbittorrent-api)"},
        )

    # ============================================================ 内部
    async def _ensure_auth(self) -> None:
        """确保已登录：首次调用自动 login；401/403 自动重登一次。"""
        async with self._auth_lock:
            if self._authed:
                return
            await self._login()

    async def _login(self) -> None:
        if not (self._username and self._password):
            raise Qb5ApiError(
                "未提供用户名/密码",
                status_code=0,
                transient=False,
            )
        # ban 中直接 raise，不发请求（避免延长 ban 计时器）
        now = asyncio.get_event_loop().time()
        if now < self._ban_until:
            remaining = int(self._ban_until - now)
            raise Qb5ApiError(
                f"qBittorrent IP 已被封禁（剩余 {remaining}s，不要继续尝试）",
                status_code=403,
                transient=False,
            )
        try:
            resp = await self._client.post(
                "/api/v2/auth/login",
                data={"username": self._username, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise Qb5ApiError(
                f"qBittorrent 网络错误：{exc}", transient=True
            ) from exc

        # qB 5.x 错误分类：
        #   200 + body="Ok."     → 成功（qB 4.x 旧行为）
        #   204 No Content       → 成功（qB 5.0+ 新行为）
        #   200 + body="Fails."  → 密码错（qB 4.x 旧行为）
        #   401 + body="..."     → 密码错（qB 5.x 新行为）
        #   403 + body 含"IP"    → 累计 5 次错后 ban 30 分钟
        if resp.status_code in (200, 204):
            # 200/204 都算成功；body 可空或 "Ok."（旧版）
            if resp.text and "Fails" in resp.text:
                raise Qb5ApiError(
                    "qBittorrent 登录失败：密码错误",
                    status_code=resp.status_code,
                    transient=False,
                )
            self._authed = True
            return
        if resp.status_code == 403:
            # 检查是否 ban（qB 中文 qB 5.x 返"您的 IP 地址已被封禁"）
            body = resp.text
            if "IP" in body or "ip" in body or "封禁" in body or "banned" in body.lower():
                # 设本地 ban_until = now + 30 分钟（qB 5.x 默认 ban 时长）
                self._ban_until = asyncio.get_event_loop().time() + 30 * 60
                raise Qb5ApiError(
                    "qBittorrent IP 已被封禁 30 分钟（密码可能错，请先确认）",
                    status_code=403,
                    transient=False,
                )
            # 403 + 非 ban body：可能 Host header validation 失败
            raise Qb5ApiError(
                f"qBittorrent 拒绝请求（403）：{body[:200]}",
                status_code=403,
                transient=False,
            )
        if resp.status_code == 401:
            # 401 = 密码错（qB 5.x 新行为）
            raise Qb5ApiError(
                "qBittorrent 登录失败：密码错误（401）",
                status_code=401,
                transient=False,
            )
        # 其它未知状态
        raise Qb5ApiError(
            f"qBittorrent 登录异常：HTTP {resp.status_code} body={resp.text[:200]}",
            status_code=resp.status_code,
            transient=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry_on_auth: bool = True,
        **kw: Any,
    ) -> Any:
        """通用请求：401/403 重登一次重试；错误码按语义抛 Qb5ApiError。"""
        await self._ensure_auth()
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise Qb5ApiError(
                f"qBittorrent 网络错误：{exc}", transient=True
            ) from exc

        # 401/403：可能 cookie 失效，重登一次
        # 仅在 login 成功过（self._authed=True）才重试；login 失败的 401 是密码错
        if resp.status_code in (401, 403) and retry_on_auth and self._authed:
            self._authed = False
            await self._ensure_auth()
            return await self._request(method, path, retry_on_auth=False, **kw)

        # 4xx（已重登过）/ 5xx → raise
        if resp.status_code >= 400:
            transient = resp.status_code >= 500 or (
                resp.status_code in (401, 403) and not retry_on_auth
            )
            raise Qb5ApiError(
                f"qBittorrent HTTP {resp.status_code}：{resp.text[:200]}",
                status_code=resp.status_code,
                transient=transient,
            )

        # 2xx：尝试返回 JSON；空 body 返回 None
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def _get(self, path: str, **kw: Any) -> Any:
        return await self._request("GET", path, **kw)

    async def _post(self, path: str, **kw: Any) -> Any:
        return await self._request("POST", path, **kw)

    @staticmethod
    def _join_hashes(hashes: list[str] | tuple[str, ...] | str) -> str:
        """hash 列表 → qBittorrent 要求的 "|" 分隔字符串。"""
        if isinstance(hashes, str):
            return hashes
        return "|".join(hashes)

    # ============================================================ 生命周期
    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Qb5ApiClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ============================================================ Authentication (2)
    async def login(self) -> bool:
        """POST /api/v2/auth/login — 显式登录（一般自动调）。"""
        await self._login()
        return True

    async def logout(self) -> None:
        """POST /api/v2/auth/logout。"""
        await self._post("/api/v2/auth/logout")

    # ============================================================ Application (3)
    async def get_app_version(self) -> str:
        """GET /api/v2/app/version — 例如 "v5.2.4"。"""
        result = await self._get("/api/v2/app/version")
        return str(result).strip()

    async def get_webapi_version(self) -> str:
        """GET /api/v2/app/webapiVersion — 例如 "2.0"。"""
        result = await self._get("/api/v2/app/webapiVersion")
        return str(result).strip()

    async def get_default_save_path(self) -> str:
        """GET /api/v2/app/defaultSavePath。"""
        result = await self._get("/api/v2/app/defaultSavePath")
        return str(result)

    # ============================================================ Transfer info (8)
    async def get_transfer_info(self) -> dict[str, Any]:
        """GET /api/v2/transfer/info — 全局传输信息。

        返回字段：dl_info_speed / dl_info_data / up_info_speed / up_info_data /
        dl_rate_limit / up_rate_limit / dht_nodes / connection_status /
        queueing / use_alt_speed_limits / refresh_interval。
        """
        from typing import cast
        result = await self._get("/api/v2/transfer/info")
        return cast("dict[str, Any]", result)

    async def get_speed_limits_mode(self) -> bool:
        """GET /api/v2/transfer/speedLimitsMode — 是否启用备选速度限制。"""
        result = await self._get("/api/v2/transfer/speedLimitsMode")
        return bool(result)

    async def toggle_speed_limits_mode(self) -> None:
        """POST /api/v2/transfer/toggleSpeedLimitsMode — 切换备选速度限制。"""
        await self._post("/api/v2/transfer/toggleSpeedLimitsMode")

    async def get_global_download_limit(self) -> int:
        """GET /api/v2/transfer/downloadLimit — 全局下载限速（bytes/s，0=无限）。"""
        return int(await self._get("/api/v2/transfer/downloadLimit"))

    async def set_global_download_limit(self, limit: int) -> None:
        """POST /api/v2/transfer/setDownloadLimit — limit=-1 表示无限制。"""
        await self._post(
            "/api/v2/transfer/setDownloadLimit", data={"limit": str(limit)}
        )

    async def get_global_upload_limit(self) -> int:
        """GET /api/v2/transfer/uploadLimit — 全局上传限速（bytes/s，0=无限）。"""
        return int(await self._get("/api/v2/transfer/uploadLimit"))

    async def set_global_upload_limit(self, limit: int) -> None:
        """POST /api/v2/transfer/setUploadLimit — limit=-1 表示无限制。"""
        await self._post(
            "/api/v2/transfer/setUploadLimit", data={"limit": str(limit)}
        )

    async def ban_peers(self, peers: list[str]) -> None:
        """POST /api/v2/transfer/banPeers — peers=["host:port", ...]。"""
        await self._post("/api/v2/transfer/banPeers", data={"peers": "|".join(peers)})

    # ============================================================ Sync (2)
    async def get_main_data(self, rid: int = 0) -> dict[str, Any]:
        """GET /api/v2/sync/maindata?rid=N — 增量同步（rid=0 全量；rid>0 增量）。

        返回字段：rid / full_update / torrents (dict[hash, info]) /
        torrents_removed (list) / categories / categories_removed / tags /
        tags_removed / server_state。
        """
        from typing import cast
        result = await self._get(
            "/api/v2/sync/maindata", params={"rid": str(rid)}
        )
        return cast("dict[str, Any]", result)

    async def get_torrent_peers(self, hash_: str, rid: int = 0) -> dict[str, Any]:
        """GET /api/v2/sync/torrentPeers?hash=...&rid=N — 种子连接器列表。"""
        from typing import cast
        result = await self._get(
            "/api/v2/sync/torrentPeers",
            params={"hash": hash_, "rid": str(rid)},
        )
        return cast("dict[str, Any]", result)

    # ============================================================ Torrent 列表/属性 (8 GET)
    async def list_torrents(
        self,
        *,
        hashes: list[str] | None = None,
        filter_: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        sort: str | None = None,
        reverse: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v2/torrents/info — 列出种子。

        支持筛选：hashes / filter (downloading/completed/paused/active/error 等)
        / category / tag / sort (字段名) / reverse / limit / offset。
        """
        params: dict[str, str] = {}
        if hashes:
            params["hashes"] = self._join_hashes(hashes)
        if filter_:
            params["filter"] = filter_
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        if sort:
            params["sort"] = sort
        if reverse:
            params["reverse"] = "true"
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        result = await self._get("/api/v2/torrents/info", params=params or None)
        if not result:
            return []
        return list(result)

    async def get_torrent_properties(self, hash_: str) -> dict[str, Any]:
        """GET /api/v2/torrents/properties — 种子详细属性（save_path/comment/...）。"""
        from typing import cast
        result = await self._get(
            "/api/v2/torrents/properties", params={"hash": hash_}
        )
        return cast("dict[str, Any]", result)

    async def get_torrent_trackers(self, hash_: str) -> list[dict[str, Any]]:
        """GET /api/v2/torrents/trackers — 种子 tracker 列表。

        返回元素字段：url / status / tier / num_peers / msg。
        """
        result = await self._get(
            "/api/v2/torrents/trackers", params={"hash": hash_}
        )
        if not result:
            return []
        return list(result)

    async def get_torrent_webseeds(self, hash_: str) -> list[dict[str, Any]]:
        """GET /api/v2/torrents/webseeds — 种子 HTTP 来源列表。"""
        result = await self._get(
            "/api/v2/torrents/webseeds", params={"hash": hash_}
        )
        if not result:
            return []
        return list(result)

    async def get_torrent_files(self, hash_: str) -> list[dict[str, Any]]:
        """GET /api/v2/torrents/files?hash=... — 文件列表。

        返回元素字段：index / name / size / progress / priority / is_seed / padding。
        """
        result = await self._get(
            "/api/v2/torrents/files", params={"hash": hash_}
        )
        if not result:
            return []
        return list(result)

    async def get_piece_states(self, hash_: str) -> list[list[int]]:
        """GET /api/v2/torrents/pieceStates?hash=... — 各 piece 下载状态。

        返回元素：单 piece 的 peer 状态（list[int]，每个 peer 一个数字）。
        """
        result = await self._get(
            "/api/v2/torrents/pieceStates", params={"hash": hash_}
        )
        if not result:
            return []
        return result  # type: ignore[no-any-return]

    async def get_piece_hashes(self, hash_: str) -> list[str]:
        """GET /api/v2/torrents/pieceHashes?hash=... — 各 piece 的 SHA1 哈希。

        返回元素：单 piece 的 SHA1 哈希字符串（40 字符 hex）。
        """
        result = await self._get(
            "/api/v2/torrents/pieceHashes", params={"hash": hash_}
        )
        if not result:
            return []
        return list(result)

    async def get_categories(self) -> dict[str, Any]:
        """GET /api/v2/torrents/categories — 所有分类（键=分类名，值=save_path）。"""
        result = await self._get("/api/v2/torrents/categories")
        if not result:
            return {}
        return dict(result)

    async def get_tags(self) -> list[str]:
        """GET /api/v2/torrents/tags — 所有标签列表。"""
        result = await self._get("/api/v2/torrents/tags")
        if not result:
            return []
        return list(result)

    # ============================================================ Torrent 状态变更 (5)
    async def pause_torrents(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/stop — 暂停（qBittorrent v5 端点）。"""
        await self._post(
            "/api/v2/torrents/stop", data={"hashes": self._join_hashes(hashes)}
        )

    async def resume_torrents(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/start — 恢复。"""
        await self._post(
            "/api/v2/torrents/start", data={"hashes": self._join_hashes(hashes)}
        )

    async def delete_torrents(
        self, hashes: list[str] | str, delete_files: bool = False
    ) -> None:
        """POST /api/v2/torrents/delete — 删除（delete_files=True 连同数据）。"""
        await self._post(
            "/api/v2/torrents/delete",
            data={
                "hashes": self._join_hashes(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )

    async def recheck_torrents(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/recheck — 强制重新校验。"""
        await self._post(
            "/api/v2/torrents/recheck", data={"hashes": self._join_hashes(hashes)}
        )

    async def reannounce_torrents(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/reannounce — 强制重新汇报 tracker。"""
        await self._post(
            "/api/v2/torrents/reannounce", data={"hashes": self._join_hashes(hashes)}
        )

    # ============================================================ Add new torrent
    async def add_torrent(
        self,
        *,
        urls: list[str] | None = None,
        torrents: list[tuple[str, bytes, str]] | None = None,
        savepath: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        skip_checking: bool = False,
        paused: bool = False,
        root_folder: str | None = None,  # None/true/false/unset
        rename: str | None = None,
        up_limit: int | None = None,
        dl_limit: int | None = None,
        ratio_limit: float | None = None,
        seeding_time_limit: int | None = None,
        auto_tmm: bool | None = None,
        sequential_download: bool | None = None,
        first_last_piece_prio: bool | None = None,
    ) -> str:
        """POST /api/v2/torrents/add — 添加种子（multipart/form-data）。

        入参（互斥）：
        - urls: magnet/HTTP URL 列表（按 \\n 分隔）
        - torrents: [(filename, content, mimetype), ...]

        返回 qBittorrent 响应（字符串 hash 或 "Ok."）。
        """
        data: dict[str, str] = {}
        if urls:
            data["urls"] = "\n".join(urls)
        if savepath is not None:
            data["savepath"] = savepath
        if category is not None:
            data["category"] = category
        if tags is not None:
            data["tags"] = ",".join(tags)
        if skip_checking:
            data["skip_checking"] = "true"
        if paused:
            data["paused"] = "true"
        if root_folder is not None:
            data["root_folder"] = root_folder
        if rename is not None:
            data["rename"] = rename
        if up_limit is not None:
            data["upLimit"] = str(up_limit)
        if dl_limit is not None:
            data["dlLimit"] = str(dl_limit)
        if ratio_limit is not None:
            data["ratioLimit"] = str(ratio_limit)
        if seeding_time_limit is not None:
            data["seedingTimeLimit"] = str(seeding_time_limit)
        if auto_tmm is not None:
            data["autoTMM"] = "true" if auto_tmm else "false"
        if sequential_download:
            data["sequentialDownload"] = "true"
        if first_last_piece_prio:
            data["firstLastPiecePrio"] = "true"

        files: list[tuple[str, tuple[str, bytes, str]]] | None = None
        if torrents:
            files = [
                ("torrents", (fn, content, mt))
                for fn, content, mt in torrents
            ]
        result = await self._post(
            "/api/v2/torrents/add",
            data=data or None,
            files=files,
        )
        if result is None:
            return ""
        return str(result).strip()

    # ============================================================ 优先级 (4)
    async def increase_priority(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/increasePrio — 队列优先级上升（需启用 queueing）。"""
        await self._post(
            "/api/v2/torrents/increasePrio", data={"hashes": self._join_hashes(hashes)}
        )

    async def decrease_priority(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/decreasePrio。"""
        await self._post(
            "/api/v2/torrents/decreasePrio", data={"hashes": self._join_hashes(hashes)}
        )

    async def top_priority(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/topPrio — 最高优先级。"""
        await self._post(
            "/api/v2/torrents/topPrio", data={"hashes": self._join_hashes(hashes)}
        )

    async def bottom_priority(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/bottomPrio — 最低优先级。"""
        await self._post(
            "/api/v2/torrents/bottomPrio", data={"hashes": self._join_hashes(hashes)}
        )

    # ============================================================ Tracker (4)
    async def add_trackers(self, hash_: str, urls: list[str]) -> None:
        """POST /api/v2/torrents/addTrackers — 添加 tracker（urls 按 \\n 分隔）。"""
        await self._post(
            "/api/v2/torrents/addTrackers",
            data={"hash": hash_, "urls": "\n".join(urls)},
        )

    async def edit_tracker(
        self, hash_: str, orig_url: str, new_url: str
    ) -> None:
        """POST /api/v2/torrents/editTracker — 替换 tracker。

        注：qBittorrent 实际参数名是 ``url``（文档写作 ``origUrl``，实测是 ``url``）。
        """
        await self._post(
            "/api/v2/torrents/editTracker",
            data={"hash": hash_, "url": orig_url, "newUrl": new_url},
        )

    async def remove_trackers(self, hash_: str, urls: list[str]) -> None:
        """POST /api/v2/torrents/removeTrackers — 删除 tracker。"""
        await self._post(
            "/api/v2/torrents/removeTrackers",
            data={"hash": hash_, "urls": "\n".join(urls)},
        )

    async def add_peers(self, hash_: str, peers: list[str]) -> None:
        """POST /api/v2/torrents/addPeers — 添加 peer（peers 格式 "host:port" 列表）。

        注：qBittorrent 实际参数名是 ``hashes``（文档写作 ``hash``）。
        """
        await self._post(
            "/api/v2/torrents/addPeers",
            data={"hashes": hash_, "peers": "\n".join(peers)},
        )

    # ============================================================ 限速 (4)
    async def get_torrent_download_limits(self, hashes: list[str] | str) -> dict[str, int]:
        """POST /api/v2/torrents/downloadLimit — 返回 {hash: limit_bytes_per_s}。

        注意：文档将本端点标为 GET，实际 qBittorrent 实现要求 POST。
        """
        result = await self._post(
            "/api/v2/torrents/downloadLimit",
            data={"hashes": self._join_hashes(hashes)},
        )
        if not result:
            return {}
        return dict(result)

    async def set_torrent_download_limit(self, hashes: list[str] | str, limit: int) -> None:
        """POST /api/v2/torrents/setDownloadLimit — 单种子限速（bytes/s，0=无限）。"""
        await self._post(
            "/api/v2/torrents/setDownloadLimit",
            data={"hashes": self._join_hashes(hashes), "limit": str(limit)},
        )

    async def get_torrent_upload_limits(self, hashes: list[str] | str) -> dict[str, int]:
        """POST /api/v2/torrents/uploadLimit — 返回 {hash: limit}。"""
        result = await self._post(
            "/api/v2/torrents/uploadLimit",
            data={"hashes": self._join_hashes(hashes)},
        )
        if not result:
            return {}
        return dict(result)

    async def set_torrent_upload_limit(self, hashes: list[str] | str, limit: int) -> None:
        """POST /api/v2/torrents/setUploadLimit — limit=0=无限。"""
        await self._post(
            "/api/v2/torrents/setUploadLimit",
            data={"hashes": self._join_hashes(hashes), "limit": str(limit)},
        )

    # ============================================================ Share limits (1)
    async def set_share_limits(
        self,
        hashes: list[str] | str,
        *,
        ratio_limit: float | None = None,
        seeding_time_limit: int | None = None,
        inactive_seeding_time_limit: int | None = None,
        share_limit_action: int = 0,
    ) -> None:
        """POST /api/v2/torrents/setShareLimits。

        参数语义：
        - ratio_limit: -2 用全局、-1 无限制、正数=限制值
        - seeding_time_limit / inactive_seeding_time_limit: 单位分钟，-2/-1 同上
        - share_limit_action: 达限后 0=暂停、1=删除

        注：qBittorrent 实际接口要求 4 个值都提供（含 -2 用全局的）。
        """
        data: dict[str, str] = {
            "hashes": self._join_hashes(hashes),
            "ratioLimit": str(ratio_limit if ratio_limit is not None else -2),
            "seedingTimeLimit": str(
                seeding_time_limit if seeding_time_limit is not None else -2
            ),
            "inactiveSeedingTimeLimit": str(
                inactive_seeding_time_limit
                if inactive_seeding_time_limit is not None
                else -2
            ),
            "shareLimitAction": str(share_limit_action),
        }
        await self._post("/api/v2/torrents/setShareLimits", data=data)

    # ============================================================ 行为开关 (5)
    async def toggle_sequential_download(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/toggleSequentialDownload — 切换顺序下载。"""
        await self._post(
            "/api/v2/torrents/toggleSequentialDownload",
            data={"hashes": self._join_hashes(hashes)},
        )

    async def toggle_first_last_piece_prio(self, hashes: list[str] | str) -> None:
        """POST /api/v2/torrents/toggleFirstLastPiecePrio — 切换首尾 piece 优先。"""
        await self._post(
            "/api/v2/torrents/toggleFirstLastPiecePrio",
            data={"hashes": self._join_hashes(hashes)},
        )

    async def set_force_start(self, hashes: list[str] | str, value: bool = True) -> None:
        """POST /api/v2/torrents/setForceStart — 强制做种（绕过分享率限制）。"""
        await self._post(
            "/api/v2/torrents/setForceStart",
            data={
                "hashes": self._join_hashes(hashes),
                "value": "true" if value else "false",
            },
        )

    async def set_super_seeding(self, hashes: list[str] | str, value: bool = True) -> None:
        """POST /api/v2/torrents/setSuperSeeding — 超级做种模式。"""
        await self._post(
            "/api/v2/torrents/setSuperSeeding",
            data={
                "hashes": self._join_hashes(hashes),
                "value": "true" if value else "false",
            },
        )

    async def set_auto_management(
        self, hashes: list[str] | str, enable: bool = True
    ) -> None:
        """POST /api/v2/torrents/setAutoManagement — 自动 Torrent 管理。"""
        await self._post(
            "/api/v2/torrents/setAutoManagement",
            data={
                "hashes": self._join_hashes(hashes),
                "enable": "true" if enable else "false",
            },
        )

    # ============================================================ 位置/分类/标签 (10)
    async def set_location(self, hashes: list[str] | str, location: str) -> None:
        """POST /api/v2/torrents/setLocation — 修改保存位置。"""
        await self._post(
            "/api/v2/torrents/setLocation",
            data={"hashes": self._join_hashes(hashes), "location": location},
        )

    async def set_name(self, hash_: str, name: str) -> None:
        """POST /api/v2/torrents/rename — 修改种子名（仅 GUI 显示）。"""
        await self._post(
            "/api/v2/torrents/rename", data={"hash": hash_, "name": name}
        )

    async def set_category(self, hashes: list[str] | str, category: str) -> None:
        """POST /api/v2/torrents/setCategory。"""
        await self._post(
            "/api/v2/torrents/setCategory",
            data={"hashes": self._join_hashes(hashes), "category": category},
        )

    async def create_category(self, name: str, save_path: str | None = None) -> None:
        """POST /api/v2/torrents/createCategory。"""
        data: dict[str, str] = {"category": name}
        if save_path:
            data["savePath"] = save_path
        await self._post("/api/v2/torrents/createCategory", data=data)

    async def edit_category(self, name: str, save_path: str | None = None) -> None:
        """POST /api/v2/torrents/editCategory。"""
        data: dict[str, str] = {"category": name}
        if save_path:
            data["savePath"] = save_path
        await self._post("/api/v2/torrents/editCategory", data=data)

    async def remove_categories(self, names: list[str]) -> None:
        """POST /api/v2/torrents/removeCategories — categories 按 | 分隔。"""
        await self._post(
            "/api/v2/torrents/removeCategories",
            data={"categories": "|".join(names)},
        )

    async def add_tags(self, hashes: list[str] | str, tags: list[str]) -> None:
        """POST /api/v2/torrents/addTags。"""
        await self._post(
            "/api/v2/torrents/addTags",
            data={
                "hashes": self._join_hashes(hashes),
                "tags": ",".join(tags),
            },
        )

    async def remove_tags(self, hashes: list[str] | str, tags: list[str]) -> None:
        """POST /api/v2/torrents/removeTags。"""
        await self._post(
            "/api/v2/torrents/removeTags",
            data={
                "hashes": self._join_hashes(hashes),
                "tags": ",".join(tags),
            },
        )

    async def create_tags(self, tags: list[str]) -> None:
        """POST /api/v2/torrents/createTags — 全局标签创建。"""
        await self._post(
            "/api/v2/torrents/createTags", data={"tags": ",".join(tags)}
        )

    async def delete_tags(self, tags: list[str]) -> None:
        """POST /api/v2/torrents/deleteTags。"""
        await self._post(
            "/api/v2/torrents/deleteTags", data={"tags": ",".join(tags)}
        )

    # ============================================================ 文件操作 (3)
    async def set_file_priority(
        self, hash_: str, file_ids: list[int], priority: int
    ) -> None:
        """POST /api/v2/torrents/filePrio。

        priority：0=不下载、1=普通、2-7=高、7=最高（具体值参考 torrent contents API）。
        """
        await self._post(
            "/api/v2/torrents/filePrio",
            data={
                "hash": hash_,
                "id": "|".join(str(i) for i in file_ids),
                "priority": str(priority),
            },
        )

    async def rename_file(self, hash_: str, old_path: str, new_path: str) -> None:
        """POST /api/v2/torrents/renameFile — 重命名单个文件。"""
        await self._post(
            "/api/v2/torrents/renameFile",
            data={"hash": hash_, "oldPath": old_path, "newPath": new_path},
        )

    async def rename_folder(self, hash_: str, old_path: str, new_path: str) -> None:
        """POST /api/v2/torrents/renameFolder — 重命名文件夹。"""
        await self._post(
            "/api/v2/torrents/renameFolder",
            data={"hash": hash_, "oldPath": old_path, "newPath": new_path},
        )


__all__ = ["Qb5ApiClient", "Qb5ApiError"]