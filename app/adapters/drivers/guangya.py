"""光鸭盘驱动。

依赖 ``guangyaclient``（PyPI 上的开源客户端）。本模块是**唯一**直接
调用它的地方。

**用 Web 端接口而非官方 OpenAPI**（实测 2026-10-27）：

- 官方 OpenAPI 要 ``client_id`` / ``project_id`` / ``sign_secret`` 三件套
  还要报备出口 IP；Web 端用的 ``client_id`` 是光鸭官方自己的固定值
- 官方那套域名对未注册的 client_id 返回 ``client_id not registered``

**直链不绑 UA**（与 115 的 ``f=1`` 不同）——``get_res_download_url``
返回的 ``signedURL`` 直接可 GET。故 :attr:`DirectLink.ua_required` 为
False，中转层不必按 UA 分桶（但保留分桶也无害）。

**过期时间靠 ``urlDuration``**：``signedURL`` 里没有可解析的过期参数，
故由驱动直接给出 ``expires_at``。

**Token 会过期**：``access_token`` 有效期只有 1200~7200 秒。驱动在每次
请求前检查，过期就刷新；刷新成功后**必须把新 token 写回 DB**——否则
重启后又用旧 token → 401 → 刷新失败。写回通过 ``on_token_refresh``
回调注入。
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from app.adapters.drivers.base import CloudFile, DirectLink, DriverError
from app.adapters.drivers.guangya_credential import (
    GuangyaCredential,
    decode_credential,
)
from app.core.logging import get_logger
from app.core.rate_limit import rate_limiter

logger = get_logger(__name__)

#: 限速器里光鸭的桶名
PROVIDER = "guangya"

#: 光鸭 Web 端的 client_id（官方固定值，不是用户申请的）
DEFAULT_CLIENT_ID = "aMe-8VSlkrbQXpUR"

#: 业务域名
DEFAULT_API_BASE_URL = "https://api.guangyapan.com"

#: 每页拉多少条（光鸭列目录上限 5 次/秒/页）
PAGE_SIZE = 100

#: Token 过期前多少秒就刷新
TOKEN_REFRESH_MARGIN_SEC = 300

#: 光鸭离线下载器的固定 UA（实测 httpbin / httpbingo / postman-echo 三家一致）。
#:
#: **迁移 115 文件时必须用它去换直链**：115 的 ``f=1`` 绑定「换链时用的
#: UA」，用别的 UA 换来的链接交给光鸭下载器会返回 403。
GUANGYA_DOWNLOAD_UA = (
    "Mozilla/5.0 (Linux; Android) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/34.0.1847.131 Safari/537.36"
)

#: ``fs_create_dir`` 的「同名目录已存在」业务码。
#:
#: 配合 ``fail_if_name_exist=True``：新建成功返回 ``code=0``，已存在返回
#: ``159``——**两种情况的 ``data.fileId`` 都是目标目录的 id**，天然幂等。
CODE_NAME_EXISTS = 159

#: 离线下载任务的**活跃**状态（0=排队 / 1=进行中）。
#:
#: 注意：``cloud_task_list`` 默认 ``status=[0,1,3,4]``，**不含 2（完成）**，
#: 想看到完成任务必须显式传 2。判断「还在不在跑」时只关心 0/1。
TASK_ACTIVE_STATUS = (0, 1)

#: 离线下载任务的**失败**状态（3 = 失败）。
TASK_FAILED_STATUS = (3,)


def _client_id() -> str:
    """取 client_id（配置优先，兜底为光鸭 Web 端的固定值）。"""
    from app.core.config import get_settings

    return get_settings().guangya_client_id.strip() or DEFAULT_CLIENT_ID


def _api_base_url() -> str:
    """取业务域名（配置优先）。"""
    from app.core.config import get_settings

    return (
        get_settings().guangya_api_base_url.strip().rstrip("/")
        or DEFAULT_API_BASE_URL
    )


class GuangyaDriver:
    """光鸭盘驱动。"""

    name = "guangya"

    def __init__(
        self,
        credential: str | None,
        device_type: str | None = None,
        *,
        on_token_refresh: Callable[[GuangyaCredential], Any] | None = None,
    ) -> None:
        """构造驱动。

        Args:
            credential: 加密后的凭据（``gypcipher:v2:``）或明文 JSON。
            device_type: 未使用（光鸭没有设备类型概念）。
            on_token_refresh: Token 刷新后的回调（用于写回 DB）。
        """
        self._device_type = device_type
        self._on_token_refresh = on_token_refresh
        self._cred: GuangyaCredential | None = None
        self._client: Any = None

        if credential:
            try:
                self._cred = decode_credential(credential)
            except Exception as exc:
                raise DriverError(f"光鸭凭据解密失败：{exc}", kind="auth") from exc

    # -- 底层客户端 --

    @property
    def client(self) -> Any:
        """取（或创建）guangyaclient 实例。"""
        if self._client is not None:
            return self._client
        if self._cred is None or not self._cred.access_token:
            raise DriverError("未配置光鸭凭据", kind="auth")

        try:
            from guangyaclient import GuangyaClient
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise DriverError("guangyaclient 未安装", kind="config") from exc

        try:
            self._client = GuangyaClient(
                access_token=self._cred.access_token,
                refresh_token=self._cred.refresh_token,
                device_id=self._cred.device_id or None,
            )
        except Exception as exc:
            raise DriverError(f"光鸭客户端初始化失败：{exc}", kind="auth") from exc
        return self._client

    async def _ensure_token(self) -> None:
        """确保 access_token 有效（过期则刷新）。

        刷新成功后通过回调把新 token 写回 DB——**这一步不能省**，否则
        进程重启后又用旧 token，服务端返回 117 / 401。
        """
        cred = self._cred
        if cred is None:
            raise DriverError("未配置光鸭凭据", kind="auth")
        if not cred.expires_at:
            return
        if time.time() < cred.expires_at - TOKEN_REFRESH_MARGIN_SEC:
            return
        if not cred.refresh_token:
            raise DriverError("光鸭 Token 已过期且无 refresh_token", kind="auth")

        await rate_limiter.acquire(PROVIDER)
        try:
            result = self.client.refresh_token()
        except Exception as exc:
            raise DriverError(f"光鸭 Token 刷新失败：{exc}", kind="auth") from exc

        access = result.get("access_token") if isinstance(result, dict) else None
        if not access:
            raise DriverError("光鸭 Token 刷新失败：未返回 access_token", kind="auth")

        expires_in = result.get("expires_in") if isinstance(result, dict) else None
        cred.access_token = str(access)
        if isinstance(result, dict) and result.get("refresh_token"):
            cred.refresh_token = str(result["refresh_token"])
        cred.expires_at = int(time.time()) + int(expires_in) if expires_in else 0

        if self._on_token_refresh is not None:
            try:
                await self._on_token_refresh(cred)
            except Exception:
                logger.exception("光鸭 Token 写回失败（不影响本次使用）")

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """调一次业务接口（自动限速 + Token 刷新 + 错误码映射）。"""
        await self._ensure_token()
        await rate_limiter.acquire(PROVIDER)
        try:
            result = await asyncio_to_thread(
                getattr(self.client, method), *args, **kwargs
            )
        except Exception as exc:
            raise DriverError(f"光鸭接口 {method} 调用失败：{exc}") from exc
        return result if isinstance(result, dict) else {}

    # -- 协议实现 --

    async def list_dir(self, path: str) -> list[CloudFile]:
        """按路径列目录（光鸭没有路径↔id 反查，故仅支持根目录）。"""
        if path in ("", "/"):
            return await self.list_dir_by_cid("", parent_path="/")
        raise DriverError(
            "光鸭盘不支持按路径列目录（接口只接受 fileId）", kind="not_found"
        )

    async def list_dir_by_cid(
        self,
        cid: str | int,
        parent_path: str = "",
        relative_dir: str = "",
    ) -> list[CloudFile]:
        """按 fileId 列出目录的直接子项。

        **翻页**直到取完（``pageSize`` 是服务端上限，一次拉不完）。

        Args:
            cid: 父目录 fileId。
            parent_path: 绝对路径（拼 ``CloudFile.path`` 用）。
            relative_dir: **相对媒体库根**的目录（拼 ``relative_dir`` 用）。
        """
        files: list[CloudFile] = []
        page = 0
        total = 0
        while True:
            payload = await self._call(
                "fs_files",
                parent_id=str(cid or ""),
                page=page,
                page_size=PAGE_SIZE,
                order_by=0,
                sort_type=0,
                file_types=[],
            )
            code = payload.get("code")
            if code not in (None, 0):
                raise DriverError(
                    f"光鸭列目录失败：{payload.get('msg') or payload}",
                    kind=_map_code(code),
                )
            data = payload.get("data") or {}
            items = data.get("list") or []
            total = int(data.get("total") or 0)
            if not items:
                break
            for item in items:
                cloud_file = _to_cloud_file(
                    item, parent_path=parent_path, relative_dir=relative_dir
                )
                if cloud_file is not None:
                    files.append(cloud_file)
            if len(items) < PAGE_SIZE or (total and len(files) >= total):
                break
            page += 1
        return files

    async def iter_tree_by_cid(
        self,
        cid: str | int,
        parent_path: str = "",
        include_subdirs: bool = True,
    ) -> AsyncIterator[CloudFile]:
        """按 fileId 递归遍历目录树，**流式**产出文件。

        递归时用子项**自己的 fileId** 作为下一层 parentId——不需要再解析
        路径。``parent_path`` 往下传，只为让日志里的路径可读（光鸭接口
        不返回路径字符串）。

        **同时维护 ``relative_dir``**（相对媒体库根的目录）——光鸭没有
        「路径 ↔ id」反查接口，拿不到根路径字符串，只能在递归时自己
        累加相对深度。没有它，附件会全部平铺在输出根目录下（目录结构丢）。

        Yields:
            每个文件（目录不产出）。
        """
        stack: list[tuple[str, str, str]] = [
            (str(cid or ""), parent_path or "/", "")
        ]

        while stack:
            current_id, current_path, current_rel = stack.pop()
            children = await self.list_dir_by_cid(
                current_id,
                parent_path=current_path,
                relative_dir=current_rel,
            )
            for child in children:
                if child.is_dir:
                    if include_subdirs:
                        # 用子项 id 继续递归——目录改名不影响
                        stack.append(
                            (
                                child.remote_id,
                                child.path,
                                child.relative_dir or "",
                            )
                        )
                else:
                    yield child

    # -- 写能力（迁移用） --

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        """建**一层**目录，返回 ``(fileId, 是否新建)``。

        **只能一层**：``fs_create_dir`` 不接受带 ``/`` 的名字（实测报
        ``code 141 文件夹名包含非法字符``）。

        **必须 ``fail_if_name_exist=True``**：默认 ``False`` 会在重名时
        建出 ``xxx(1)`` 副本，迁移重跑会把目录树搞爆。传 True 后已存在
        返回 ``code=159``，但 ``data.fileId`` 仍是已存在目录的 id。
        """
        payload = await self._call(
            "fs_create_dir",
            name,
            parent_id=str(parent_id or ""),
            fail_if_name_exist=True,
        )
        code = payload.get("code")
        data = payload.get("data") or {}
        file_id = str(data.get("fileId") or "")
        if code not in (None, 0, CODE_NAME_EXISTS) or not file_id:
            raise DriverError(
                f"光鸭建目录失败：{name}"
                f"（code={code} msg={payload.get('msg')}）"
            )
        return file_id, code in (None, 0)

    async def ensure_dir(self, rel_path: str, root_id: str = "") -> str:
        """逐层确保 ``rel_path``（相对 ``root_id``）存在，返回最终 fileId。"""
        parent = str(root_id or "")
        for name in [p for p in rel_path.split("/") if p]:
            parent, _ = await self.create_dir(name, parent)
        return parent

    async def list_files(self, dir_id: str) -> dict[str, int]:
        """列出目录下的**文件**（不含子目录），返回 ``{文件名: 字节数}``。"""
        entries = await self.list_dir_by_cid(dir_id)
        return {f.name: f.size for f in entries if not f.is_dir}

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        """列出目录下的**文件**（含 fileId），返回 ``{文件名: (字节数, fileId)}``。

        与 :meth:`list_files` 的区别：**带 fileId**。删除接口只认 fileId，
        故上传流程（删旧覆盖）必须用它。
        """
        entries = await self.list_dir_by_cid(dir_id)
        return {
            f.name: (f.size, f.remote_id) for f in entries if not f.is_dir
        }

    async def list_subdirs(self, dir_id: str) -> list[CloudFile]:
        """列出目录下的**子目录**。"""
        entries = await self.list_dir_by_cid(dir_id)
        return [f for f in entries if f.is_dir]

    # -- 离线下载任务 --

    async def create_offline_task(self, url: str, parent_id: str = "") -> str:
        """提交一个离线下载任务，返回 taskId。

        不传 ``parent_id`` 会落到**根目录**（不是「来自：云添加」那个目录），
        所以调用方必须显式传目标目录的 fileId。
        """
        payload = await self._call(
            "cloud_create_task", url, parent_id=str(parent_id or "")
        )
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭创建离线任务失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        task_id = str((payload.get("data") or {}).get("taskId") or "")
        if not task_id:
            raise DriverError(f"光鸭创建离线任务未返回 taskId：{payload}")
        return task_id

    async def list_active_offline_tasks(self) -> list[dict[str, Any]]:
        """列出**活跃**离线任务（排队 + 进行中）。

        用来判断「任务还在不在跑」：任务从活跃列表消失即视为结束。
        实测任务项在活跃阶段**不带** ``fileId`` / ``parentId``，但 ``res``
        字段里的 115 直链带 ``d=`` 参数（其中含 pickcode），调用方可据此
        把任务匹配回源文件。

        **``page_size`` 上限是 100**：传 200/500 会返回 ``code 112 参数错误``
        （实测），故用 :data:`PAGE_SIZE` 并翻页。
        """
        return await self._list_tasks(TASK_ACTIVE_STATUS)

    async def list_failed_offline_tasks(self) -> list[dict[str, Any]]:
        """列出**失败**的离线任务（失败原因在 ``errMsg`` / ``errCode``）。

        只在「任务消失但目标目录没有文件」时调用一次——失败是少数情况，
        多一次请求换一条可读的原因值得。
        """
        return await self._list_tasks(TASK_FAILED_STATUS)

    async def _list_tasks(self, statuses: tuple[int, ...]) -> list[dict[str, Any]]:
        """按状态拉离线任务。

        **不能分页**——实测光鸭**完全忽略** ``page`` 参数（``page=0/1/2``
        返回完全相同的列表）。故只请求一次；若真按 ``len(items) < PAGE_SIZE``
        判断终止条件，任务数恰好 ≥ 100 时会**无限循环**（实测 1.5 秒打出
        22837 次请求），把光鸭打到风控、并让迁移的轮询协程永远卡住。

        一次返回全部任务：``page_size`` 取 100（服务端上限，超过报
        ``code 112 参数错误``），实测任务总数远小于该值。
        """
        payload = await self._call(
            "cloud_task_list",
            page=0,
            page_size=PAGE_SIZE,
            status=list(statuses),
        )
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭列离线任务失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        return list((payload.get("data") or {}).get("list") or [])

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        """换取直链。

        **不绑 UA**——光鸭的 ``signedURL`` 可直接 GET，与 115 的 ``f=1``
        语义不同。

        **过期时间用 ``urlDuration``**——``signedURL`` 里没有可解析的
        过期参数，故由驱动直接给出 ``expires_at``。
        """
        payload = await self._call("download_url", str(remote_id))
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭换直链失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        data = payload.get("data") or {}
        url = str(data.get("signedURL") or data.get("downloadUrl") or "")
        if not url:
            raise DriverError("光鸭换直链返回空", kind="not_found")

        duration = int(data.get("urlDuration") or 0)
        expires_at = int(time.time()) + duration if duration else None
        return DirectLink(url=url, expires_at=expires_at, ua_required=False)

    async def check_login(self) -> bool:
        """检查凭据是否仍然有效。"""
        try:
            payload = await self._call_assets()
        except DriverError:
            return False
        return payload.get("code") in (None, 0) and bool(payload.get("data"))

    async def get_space_info(self) -> dict[str, Any]:
        """取账号 id 与容量。

        **用 ``get_assets``**（``nd.bizassets.s``）——实测 ``user_info()``
        那个端点（``/v1/user/me``）返回 **501 Not Implemented**，不可用。

        账号 id 从 **access_token 的 JWT payload** 里取 ``sub``——``get_assets``
        只给容量和 VIP 状态，不给用户 id。

        ``get_assets`` 额外还带 ``vipStatus`` / ``vipLeftTime`` /
        ``freeDirectLinkTraffic``（直链流量额度），一并返回。
        """
        payload = await self._call_assets()
        data = payload.get("data") or {}
        total = int(data.get("totalSpaceSize") or 0)
        used = int(data.get("usedSpaceSize") or 0)
        return {
            "account_id": self._account_id(),
            "total_bytes": total,
            "used_bytes": used,
            "remain_bytes": max(total - used, 0),
            "vip_status": int(data.get("vipStatus") or 0),
            "vip_left_seconds": int(data.get("vipLeftTime") or 0),
            "direct_link_traffic": int(data.get("freeDirectLinkTraffic") or 0),
        }

    async def _call_assets(self) -> dict[str, Any]:
        """调 ``get_assets``（容量 / VIP 状态）。

        **``guangyaclient`` 里没有这个方法**（它只有 ``user_info()``，
        而那个端点已被平台禁用：实测返回 ``501 Not Implemented``），
        故自己发请求。

        不用库的私有 httpx 客户端——直接用 httpx，认证头自己拼
        （与库的 ``_get_auth_headers`` 一致）。
        """
        await self._ensure_token()
        await rate_limiter.acquire(PROVIDER)
        url = f"{_api_base_url()}/nd.bizassets.s/v1/get_assets"
        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                response = await http.post(
                    url, json={}, headers=self._auth_headers()
                )
        except httpx.HTTPError as exc:
            raise DriverError(f"光鸭获取容量失败：{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise DriverError(
                f"光鸭获取容量返回非 JSON（HTTP {response.status_code}）"
            ) from exc
        if not isinstance(payload, dict):
            raise DriverError("光鸭获取容量返回格式异常")
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭获取容量失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        return payload

    def _auth_headers(self) -> dict[str, str]:
        """业务接口的请求头（Bearer + 设备标识）。"""
        cred = self._cred
        return {
            "Authorization": f"Bearer {cred.access_token if cred else ''}",
            "X-Client-Id": _client_id(),
            "X-Device-Id": cred.device_id if cred else "",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        }

    def _account_id(self) -> str:
        """从 access_token 的 JWT payload 里取账号 id（``sub``）。

        光鸭的 access_token 是 **JWT**（三段 base64），payload 里带 ``sub``
        （用户 id）、``exp``（过期时间）、``scope`` 等。

        解不出来就返回空串——**不报错**（账号 id 只用于展示）。
        """
        token = self._cred.access_token if self._cred else ""
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        try:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, TypeError, json.JSONDecodeError):
            return ""
        return str(payload.get("sub") or "")

    async def list_root(self) -> list[CloudFile]:
        """列出根目录（登录态探测用）。"""
        return await self.list_dir_by_cid("", parent_path="/")


async def asyncio_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    """在线程里跑同步调用（guangyaclient 是同步的 httpx）。"""
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)


def _to_cloud_file(
    item: dict[str, Any],
    parent_path: str = "",
    relative_dir: str = "",
) -> CloudFile | None:
    """把光鸭的文件项转成 :class:`CloudFile`。

    字段（实测）：

    ==================  ==================================
    字段                含义
    ==================  ==================================
    ``fileId``          文件 id（字符串，19 位数字）
    ``fileName``        文件名
    ``fileSize``        字节数（目录为 0）
    ``resType``         1 = 文件 / 2 = 目录
    ``utime``           更新时间（unix 秒）
    ``ext``             扩展名（含点）
    ==================  ==================================
    """
    file_id = str(item.get("fileId") or "")
    name = str(item.get("fileName") or "")
    if not file_id or not name:
        return None

    is_dir = int(item.get("resType") or 1) == 2
    parent = (parent_path or "/").rstrip("/")
    path = f"{parent}/{name}" if parent else f"/{name}"

    # 相对媒体库根的目录——光鸭拿不到根路径字符串，只能自己累加
    rel = relative_dir.strip("/")
    rel_dir = f"{rel}/{name}" if rel else name

    return CloudFile(
        remote_id=file_id,
        path=path,
        name=name,
        size=int(item.get("fileSize") or 0),
        is_dir=is_dir,
        mtime=int(item.get("utime") or item.get("ctime") or 0),
        relative_dir=rel_dir if is_dir else rel,
    )


def _map_code(code: Any) -> str:
    """把光鸭业务错误码映射成 :class:`DriverError` 的 kind。

    常用码（官方文档 §9）：

    - ``117`` —— access_token 无效（应先刷新）
    - ``111`` —— 访问拒绝
    - ``123`` —— 应用未开通该能力
    - ``146`` —— 文件不存在
    - ``430`` —— 用户不是会员
    """
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if value == 117:
        return "auth"
    if value == 111:
        return "auth"
    if value == 123:
        return "config"
    if value == 146:
        return "not_found"
    if value == 430:
        return "auth"
    return "unknown"


def build_guangya_driver(
    credential: str | None,
    device_type: str | None = None,
    *,
    on_token_refresh: Callable[[GuangyaCredential], Any] | None = None,
) -> GuangyaDriver:
    """工厂函数（签名与注册表一致）。

    ``on_token_refresh`` 必须由工厂**透传**给驱动——否则
    :func:`app.services.driver_factory.build_driver` 传进来的回调会被
    ``TypeError`` 兜底逻辑静默丢弃，Token 刷新后就没人写回 DB 了。
    """
    return GuangyaDriver(
        credential, device_type, on_token_refresh=on_token_refresh
    )
