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

**全量列表**（2026-11-12 生产实测，改写扫描遍历）：``fs_files`` 支持
``parent_id="*"``（全账号）+ ``res_type`` 过滤，条目带
``fullParentIds``（从账号根到父目录的完整 fileId 链）与 ``parentId``。
``iter_tree_by_cid`` 据此用约 16 个只读请求替代逐目录递归（生产
970 目录 ≈ 1100+ 请求、限速下 5-9 分钟）。同日实测还确认：
``page_size`` 支持到 1000+；**目录 ``utime`` 不可靠**（建子目录会变，
改名/删除都不变）——不能做「目录 mtime 剪枝」。
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
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

#: 限速器里光鸭**换直链**的桶名
#:
#: 官方限频是 5 次/秒（列目录）/ **2 次/秒（换直链）**——后者更严，
#: 单独一个桶。共用列目录的桶（0.2 秒 = 5/s）会超官方限频：实测附件
#: 并发 4 路后换直链峰值 5/s → 光鸭软限频 → 扫描速率从 45 掉到 21
#: 个/分钟、30 分钟内 96 次下载重试（2026-11-12 生产实测）。
PROVIDER_LINK = "guangya_link"

#: 光鸭 Web 端的 client_id（官方固定值，不是用户申请的）
DEFAULT_CLIENT_ID = "aMe-8VSlkrbQXpUR"

#: 业务域名
DEFAULT_API_BASE_URL = "https://api.guangyapan.com"

#: 每页拉多少条（``list_dir_by_cid`` 用——浏览目录树时单目录条目少，
#: 100 足够；实测 2026-11-12 ``fs_files`` 的 ``page_size`` 服务端支持
#: 1000+，全量列表走 :data:`LIST_PAGE_SIZE`）。
PAGE_SIZE = 100

#: **全量列表**模式的每页条数（2026-11-12 生产实测：``page_size``
#: 100/200/500/1000 全部生效，``total=15573`` 无截断）。
#:
#: ``fs_files`` 支持 ``parent_id="*"``（全账号）+ ``res_type`` 过滤，
#: 条目带 ``fullParentIds``（从账号根到父目录的完整 fileId 链）——
#: 两个只读调用（目录 + 文件各一次翻页）即可替代逐目录递归：
#: 生产 726 目录 + ~1.5 万文件，旧递归 ≈ 1100+ 请求（限速 0.2s/请求
#: ≈ 5-9 分钟），全量列表 ≈ 16 请求（≈ 10 秒）。
LIST_PAGE_SIZE = 1000

#: 全量列表的翻页防御上限。
#:
#: ``cloud_task_list`` 实测会**完全忽略** ``page`` 参数（返回相同列表，
#: 终止条件永远不满足 → 无限循环，1.5 秒打出 2 万+ 请求）。``fs_files``
#: 的翻页在生产扫描中一直正常，但防御同一类故障：超过上限按接口异常
#: 报错，而不是把光鸭打到风控。
MAX_LIST_PAGES = 1000

#: 目录名映射允许缺失的上限。
#:
#: 文件条目的 ``fullParentIds`` 需要用目录列表换算成目录名；正常情况
#: 缺失为 0（目录列表覆盖全账号）。少量缺失（两次调用间的竞态删除）
#: 用 fileId 占位继续；缺失过多说明接口行为变了——宁可扫描失败可见。
MISSING_DIR_NAME_LIMIT = 10

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


def share_url_id(share_url: str, share_id: str) -> str:
    """从 ``shareUrl`` 解析出**URL 形态**的分享 id。

    光鸭的公开接口（``share_summary`` / ``share_access_token``）只认
    ``{shareId}_{账号分享key}``——裸 ``shareId`` 一律 code=112「参数错误」。

    ⚠️ **必须剥掉查询串**：``shareUrl`` 形如::

        https://www.guangyapan.com/s/{shareId}_{key}?code=xxxx

    查询串里带着提取码，不剥掉会让 id 变成「…_key?code=xxxx」→
    取 token 报「分享链接错误」（2026-11-16 本地真机联调踩到）。

    Args:
        share_url: 创建分享时返回的 ``shareUrl``。
        share_id: 创建分享时返回的 ``shareId``（URL 异常时的兜底）。

    Returns:
        URL 形态的分享 id。
    """
    if "/" not in share_url:
        return share_id
    tail = share_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "_" not in tail:
        return share_id
    return tail


@dataclass(frozen=True, slots=True)
class ShareInfo:
    """一次分享创建的结果。

    ⚠️ **``url_id`` 才是公开接口认的 id**（``{shareId}_{账号分享key}``）——
    裸 ``share_id`` 调 ``share_summary`` / ``share_access_token`` 一律
    code=112「参数错误」（2026-11-16 生产实测）。
    """

    #: 分享 id（``share_delete`` 用它）
    share_id: str
    #: **URL 形态**的分享 id（公开接口用）
    url_id: str
    #: 提取码（转存取 token 时必需）
    code: str
    #: 分享标题
    title: str


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
            # **必须放线程池**——``refresh_token()`` 是同步 HTTP 调用
            # （含 MD5 计算 + 网络往返），直接跑会阻塞事件循环：
            # 实测 prod 启动时 healthcheck 连续 3 次超时（5s），
            # 容器被判 unhealthy、API 不可达。
            result = await asyncio_to_thread(self.client.refresh_token)
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

    async def _call(
        self,
        method: str,
        *args: Any,
        bucket: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调一次业务接口（自动限速 + Token 刷新 + 错误码映射）。

        Args:
            bucket: 限速桶名。默认光鸭业务接口桶；换直链走
                :data:`PROVIDER_LINK`（官方限频更严，2 次/秒）。
        """
        await self._ensure_token()
        await rate_limiter.acquire(bucket or PROVIDER)
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
        """按 fileId 遍历媒体库子树，**流式**产出文件（目录不产出）。

        **全量列表模式**（2026-11-12 生产实测后重写）：``fs_files``
        支持 ``parent_id="*"`` 且条目带 ``fullParentIds``——先用一次
        ``res_type=2`` 拉全账号目录建「fileId → 目录名」映射，再翻页拉
        ``res_type=1`` 文件，按 ``fullParentIds`` 过滤出 ``cid`` 子树并
        把链上的 fileId 换成目录名拼 ``relative_dir``。约 16 个请求替代
        旧逐目录递归的成百上千次（见 :data:`LIST_PAGE_SIZE`）。

        ``relative_dir`` 语义与旧实现一致：**相对媒体库根（``cid``）的
        父目录**，顶层文件为空串——消费端
        :func:`app.services.media.target_relative_dir` 靠它算 STRM 落盘
        目录，目录结构不能丢。

        ``parent_path`` 只用于拼 ``path``（日志与展示可读），光鸭接口
        不返回路径字符串。

        Args:
            cid: 子树根 fileId。**必须非空**。
                扫描入口传媒体库根（``media_cid``）；迁移的「光鸭 → 光鸭」
                源目录传用户选中的任意目录——**两种都用同一套实现**：
                ``fullParentIds`` 链是「账号根的直接子目录 → 父目录」的
                完整链（**不含账号根本身**），故链里任意深度都能定位到
                ``cid``，取其后的部分即相对目录（实测：链首是账号根的
                子目录，不是媒体库根）。
            include_subdirs: False 时只产出 ``cid`` 直接子文件。

        Yields:
            子树内每个文件。
        """
        root = str(cid or "")
        if not root:
            raise DriverError(
                "光鸭遍历需要非空的根 fileId（媒体库根 media_cid 或选中的源目录）",
                kind="config",
            )

        dir_names = await self._fetch_dir_name_map()
        missing_ids: set[str] = set()

        async for item in self._iter_all_items(res_type=1):
            # 防御：服务端忽略 res_type 时把目录也混进来——跳过
            if int(item.get("resType") or 1) == 2:
                continue
            file_id = str(item.get("fileId") or "")
            name = str(item.get("fileName") or "")
            if not file_id or not name:
                continue

            raw_chain = str(item.get("fullParentIds") or "")
            chain = [seg for seg in raw_chain.split("/") if seg]
            # 条目缺 fullParentIds = 接口改版——**必须报错**，静默跳过
            # 会把故障伪装成「媒体库为空」（拉取失败必须抛错的契约）
            if not chain:
                raise DriverError(
                    f"光鸭全量列表条目缺 fullParentIds（fileId={file_id}），"
                    "接口可能已改版",
                    kind="unknown",
                )
            if root not in chain:
                continue  # 不在 cid 子树（全账号列表里有其它内容）
            root_idx = chain.index(root)
            if not include_subdirs and (len(chain) - root_idx) > 1:
                continue  # 仅顶层

            rel_parts: list[str] = []
            for ancestor in chain[root_idx + 1 :]:
                dir_name = dir_names.get(ancestor)
                if dir_name is None:
                    # 目录映射缺 id（两次调用间的竞态删除/接口异常）。
                    # 用 fileId 占位——路径确定可复现，不静默跳过（跳过
                    # 会让 prune 把「源端还在」的文件当成孤儿删掉）。
                    missing_ids.add(ancestor)
                    dir_name = ancestor
                rel_parts.append(dir_name)

            if len(missing_ids) > MISSING_DIR_NAME_LIMIT:
                raise DriverError(
                    f"光鸭目录映射缺失过多（{len(missing_ids)} 个 fileId 无名），"
                    "接口可能已改版",
                    kind="unknown",
                )

            rel = "/".join(rel_parts)
            parent = (parent_path or "/").rstrip("/")
            path = f"{parent}/{rel}/{name}" if rel else f"{parent}/{name}"

            yield CloudFile(
                remote_id=file_id,
                path=path,
                name=name,
                size=int(item.get("fileSize") or 0),
                is_dir=False,
                mtime=int(item.get("utime") or item.get("ctime") or 0),
                relative_dir=rel,
            )

        if missing_ids:
            logger.warning(
                "光鸭目录映射缺失 %d 个 fileId（已用 fileId 占位路径）：%s",
                len(missing_ids),
                ", ".join(sorted(missing_ids)[:10]),
            )

    async def _fetch_dir_name_map(self) -> dict[str, str]:
        """拉全账号目录（``res_type=2``），返回 ``{fileId: 目录名}``。

        生产实测 726 个目录 = 1 个请求（:data:`LIST_PAGE_SIZE`=1000）。
        """
        names: dict[str, str] = {}
        async for item in self._iter_all_items(res_type=2):
            file_id = str(item.get("fileId") or "")
            name = str(item.get("fileName") or "")
            if file_id and name:
                names[file_id] = name
        return names

    async def _iter_all_items(self, res_type: int) -> AsyncIterator[dict[str, Any]]:
        """全账号列表翻页（``parent_id="*"``），逐条 yield 原始 item。

        **翻页防御**：超过 :data:`MAX_LIST_PAGES` 按接口异常报错——
        ``cloud_task_list`` 实测会忽略 ``page`` 参数导致无限循环，
        ``fs_files`` 一旦出现同款故障必须可见地失败，而不是把光鸭
        打到风控。
        """
        page = 0
        total: int | None = None
        seen = 0
        while True:
            payload = await self._call(
                "fs_files",
                parent_id="*",
                page=page,
                page_size=LIST_PAGE_SIZE,
                order_by=0,
                sort_type=0,
                file_types=[],
                res_type=res_type,
            )
            code = payload.get("code")
            if code not in (None, 0):
                raise DriverError(
                    f"光鸭全量列表失败：{payload.get('msg') or payload}",
                    kind=_map_code(code),
                )
            data = payload.get("data") or {}
            items = data.get("list") or []
            if not items:
                return
            if total is None:
                total = int(data.get("total") or 0)
            for item in items:
                seen += 1
                yield item
            if len(items) < LIST_PAGE_SIZE or (total and seen >= total):
                return
            page += 1
            if page >= MAX_LIST_PAGES:
                raise DriverError(
                    f"光鸭全量列表翻页超过 {MAX_LIST_PAGES} 页仍未取完，"
                    "疑似服务端忽略了 page 参数",
                    kind="unknown",
                )

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

    async def rename_file(self, file_id: str, new_name: str) -> None:
        """重命名文件（**不移动位置**，只改名）。

        用途：光鸭「云下载」的落盘名取自 URL 末段——115 直链末段是文件名
        （落名正确），而**光鸭直链末段是 ``download``**（实测 2026-11-13），
        故「光鸭 → 光鸭」迁移必须把落盘文件改回原名。

        Args:
            file_id: 目标文件 fileId。
            new_name: 新文件名（不含路径）。

        Raises:
            DriverError: 重命名失败（接口返回非 0 业务码）。
        """
        payload = self.client.fs_rename(str(file_id), str(new_name))
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭重命名失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )

    async def copy_files(self, file_ids: list[str], parent_id: str) -> None:
        """批量复制文件到 ``parent_id`` 目录下。

        **id 与 parentId 一律转字符串再发**——光鸭的 ``fs_copy`` /
        ``fs_delete`` 收到数字 id 会返回 ``code=112 参数错误``（实测
        2026-11-13），而第三方客户端 ``guangyaclient`` 的类型签名却写着
        ``Optional[int]``，照签名传 int 必炸。这里在驱动层统一 ``str()``，
        调用方传什么都安全。

        **同名不覆盖**：目标目录已有同名文件时，光鸭会自动改名成
        ``xxx(1).xxx``（实测）——要真正覆盖必须**先删掉旧的那份**。

        Args:
            file_ids: 待复制的 fileId 列表（可传 int，内部转 str）。
            parent_id: 目标目录 fileId（可传 int，内部转 str）。

        Raises:
            DriverError: 复制失败（接口返回非 0 业务码）。
        """
        payload = await self._call(
            "fs_copy", [str(fid) for fid in file_ids], parent_id=str(parent_id)
        )
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭复制文件失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )

    async def delete_files(self, file_ids: list[str]) -> None:
        """批量删除文件（**进回收站**，非永久删除）。

        **id 一律转字符串再发**（同 :meth:`copy_files`：数字 id → code=112）。

        **删除是异步任务**（接口返回 taskId）：实测单文件 <0.5 s 生效，但
        批量删 20+ 个时要**重列目录确认**——曾有 6 个文件到 60 s 后才从
        列表里消失。

        Args:
            file_ids: 待删 fileId 列表（可传 int，内部转 str）。

        Raises:
            DriverError: 删除失败（接口返回非 0 业务码）。
        """
        payload = await self._call("fs_delete", [str(fid) for fid in file_ids])
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭删除文件失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )

    async def delete_file(self, file_id: str) -> None:
        """删除一个文件（**进回收站**，非永久删除）。

        用于「搬完清理源」——那个开关默认关闭，且只删**已校验成功**（目标
        侧已确认落盘同名同大小）的文件。

        Args:
            file_id: 待删文件的 fileId。

        Raises:
            DriverError: 删除失败（接口返回非 0 业务码）。
        """
        await self.delete_files([file_id])

    # -- 离线下载任务 --

    # -- 分享 / 转存（小文件归拢通道）--

    async def create_share(self, file_ids: list[str], title: str) -> ShareInfo:
        """创建分享（**小文件归拢通道**用）。

        分享是**服务端内部复制**的另一种搬运方式：源账号建分享 → 目标账号
        「转存」到指定目录。与 ``cloud_create_task``（URL 离线下载）是**不同
        的接口与配额**——转存不消耗云添加额度（2026-11-16 生产实测：小文件
        nfo 建分享 → 转存 → 目标目录出现同名同大小文件，全流程零云添加）。

        Args:
            file_ids: 要分享的文件 id（列表）。
            title: 分享标题（用户可见）。

        Returns:
            :class:`ShareInfo`（含 ``share_id`` / 提取码 / **URL 形态 id**）。

        Raises:
            DriverError: 建分享失败（含分享审核拒绝）。
        """
        payload = await self._call(
            "share_create",
            [str(fid) for fid in file_ids],
            title,
            0,  # validate_duration=0 → 永久有效
            1,  # share_type=1
            "",  # code（空 → 服务端自动生成）
            True,  # auto_fill_code：提取码是**必需的**（转存要带）
            "0",  # traffic_limit 不限
            0,  # max_restore_count 不限
            1,  # download_type
        )
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭创建分享失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        data = payload.get("data") or {}
        share_id = str(data.get("shareId") or "")
        if not share_id:
            raise DriverError(f"光鸭创建分享未返回 shareId：{payload}")
        url_id = share_url_id(str(data.get("shareUrl") or ""), share_id)
        return ShareInfo(
            share_id=share_id,
            url_id=url_id,
            code=str(data.get("code") or ""),
            title=title,
        )

    async def share_access_token(self, url_id: str, code: str) -> str:
        """取分享的 access token（**公开接口**，无需登录）。

        Args:
            url_id: **URL 形态**的分享 id（``{shareId}_{key}``）。
            code: 提取码（分享带码时必需，否则 code=209「提取码错误」）。

        Raises:
            DriverError: 取 token 失败（id/码不对 → 112 / 209）。
        """
        try:
            resp = await asyncio_to_thread(
                self.client.share_access_token, url_id, code
            )
        except Exception as exc:
            raise DriverError(f"光鸭取分享访问令牌失败：{exc}") from exc
        payload = resp if isinstance(resp, dict) else {}
        biz = payload.get("code")
        if biz not in (None, 0):
            raise DriverError(
                f"光鸭取分享访问令牌失败：{payload.get('msg') or payload}",
                kind="unknown",
            )
        data = payload.get("data") or {}
        if isinstance(data, str):
            return data
        token = str(data.get("accessToken") or data.get("token") or "")
        if not token:
            raise DriverError(f"光鸭取分享访问令牌未返回 token：{payload}")
        return token

    async def restore_share(
        self, access_token: str, file_ids: list[str], parent_id: str
    ) -> str:
        """把分享内容**转存**到指定目录（服务端内部复制）。

        Args:
            access_token: 分享访问令牌。
            file_ids: 要转存的文件 id（分享里的那些）。
            parent_id: 目标目录 id（**可以精确指定**——不像分享落地那样只能
                进「来自：分享」）。

        Returns:
            转存任务 id（异步任务，完成与否靠列目录确认）。
        """
        payload = await self._call(
            "share_restore", access_token, [str(fid) for fid in file_ids], parent_id
        )
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭转存分享失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        data = payload.get("data") or {}
        return str(data.get("taskId") or "")

    async def delete_share(self, share_ids: list[str]) -> None:
        """删除分享（转存完成后清理，别在账号里堆分享）。"""
        payload = await self._call("share_delete", [str(sid) for sid in share_ids])
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭删除分享失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )

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
        payload = await self._call(
            "download_url", str(remote_id), bucket=PROVIDER_LINK
        )
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
