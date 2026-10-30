"""115 驱动。

依赖 ``p115client``。本模块是**唯一**直接调用 p115client 的地方。

职责：

- 列出目录（``list_dir``）—— 走 ``p115client.tool.iterdir``
- 换取直链（``get_direct_link``）—— **必须透传 UA**（115 直链的 ``f=1``
  参数绑定 UA，服务端统一 UA 换直链再分发给所有客户端这条路走不通）
- 扫码登录（:mod:`app.adapters.drivers.p115_qr`）

**API 用法（实测 p115client 0.0.9 / Python 3.13 确认，勿凭记忆改）**：

============================  ==========================================
用途                          正确写法
============================  ==========================================
列目录                        ``tool.iterdir(client, cid, app="web")``
换直链                        ``client.download_url(pc, user_agent=...)``
登录态                        ``client.login_status()``
账号容量                      ``client.user_space_info()``
cid → 路径                    ``tool.get_path(client, cid)``
============================  ==========================================

**四个易错点（都曾写错过）**：

1. ``download_url`` 是**实例方法**（``self`` 在前）——写成
   ``P115Client.download_url(pc, ...)`` 会报
   ``TypeError: missing 1 required positional argument: 'pickcode'``
2. **没有** ``fs_iter`` 方法（列目录要用 ``tool.iterdir``），
   **没有** ``check_login`` 方法（是 ``login_status``）
3. ``tool.iterdir`` 归一化后的字段名是 ``id`` / ``name`` / ``size`` /
   ``pick_code`` / ``is_dir`` / ``mtime`` / ``sha1``——不是原始响应的
   ``n`` / ``s`` / ``pc`` / ``fc``
4. **列目录必须固定用 ``app="web"``**（不要用 ``device_type``）：

   - ``app="web"`` → ``client.fs_files``（webapi）——**正常**
   - 其它 app → ``client.fs_files_app``（proapi）——**405 Method Not Allowed**

   实测：``app="alipaymini"`` 时 ``/影视/电影`` 返回 405，改用 ``web`` 正常。
   ``device_type`` 只影响「这次登录记成哪台设备」，与列目录无关。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.adapters.drivers.base import CloudFile, DirectLink, DriverError
from app.core.logging import get_logger
from app.core.rate_limit import rate_limiter

logger = get_logger(__name__)

#: 列目录被限流时的最大尝试次数（含首次）
#:
#: 115 对高频列目录会限流（``405 Method Not Allowed``）。实测跑全量
#: 4.75 小时后触发一次——**整个扫描任务因此终止**，剩下的目录全没扫。
#: 退避重试让长任务能扛住临时限流。
LIST_RETRY_MAX = 5

#: 限流重试的退避基数（秒）——第 n 次失败后等 ``n * LIST_RETRY_BACKOFF_SEC``
LIST_RETRY_BACKOFF_SEC = 30.0

#: 115 客户端实例的惰性单例（按 cookie 区分）
_CLIENTS: dict[str, Any] = {}


def _get_client(cookie: str, device_type: str | None = None) -> Any:
    """取（或创建）p115client 实例。

    复用实例很重要——每次新建会让 cookie 状态、连接池全部重来。
    """
    if not cookie:
        raise DriverError("未配置 115 cookie", kind="auth")
    client = _CLIENTS.get(cookie)
    if client is not None:
        return client

    try:
        from p115client import P115Client
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise DriverError("p115client 未安装", kind="config") from exc

    try:
        client = P115Client(cookies=cookie, app=device_type or "115android")
    except Exception as exc:
        raise DriverError(f"115 客户端初始化失败：{exc}", kind="auth") from exc
    _CLIENTS[cookie] = client
    return client


class P115Driver:
    """115 网盘驱动。"""

    name = "p115"

    def __init__(self, cookie: str, device_type: str | None = None) -> None:
        self._cookie = cookie
        self._device_type = device_type

    @property
    def _client(self) -> Any:
        return _get_client(self._cookie, self._device_type)

    async def list_dir(self, path: str) -> list[CloudFile]:
        """按**路径**列出目录的直接子项。

        Args:
            path: 源路径（以 ``/`` 开头）。

        Raises:
            DriverError: 凭据失效 / 网络错误 / 目录不存在。
        """
        client = self._client
        cid = await self._resolve_cid(client, path)
        return await self.list_dir_by_cid(cid, parent_path=path)

    async def list_dir_by_cid(
        self,
        cid: str | int,
        parent_path: str = "",
    ) -> list[CloudFile]:
        """按 **cid** 列出目录的直接子项。

        这是主路径——用 cid 而非路径：115 的目录 id 在**改名后不变**，
        路径会失效（``dir_getid`` 抛 ``P115FileNotFoundError``）。

        Args:
            cid: 目录 id（数字字符串或整数）。
            parent_path: 父目录路径（用于拼出子项的完整路径）。

        Raises:
            DriverError: 凭据失效 / 网络错误 / 被限流。
        """
        try:
            import p115client  # noqa: F401 - 仅检测依赖是否安装
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise DriverError("p115client 未安装", kind="config") from exc

        client = self._client
        raw = await self._iterdir_with_retry(client, cid)

        files: list[CloudFile] = []
        for item in raw:
            cloud_file = _to_cloud_file(item, parent_path=parent_path)
            if cloud_file is not None:
                files.append(cloud_file)
        return files

    async def _iterdir_with_retry(self, client: Any, cid: str | int) -> list[Any]:
        """列目录，遇到**限流**时退避重试。

        **为什么需要**：115 对高频列目录会限流，表现为
        ``405 Method Not Allowed``。实测跑全量 4.75 小时后触发一次——
        **整个扫描任务因此终止**，剩下的目录全没扫。

        退避重试让长任务（附件同步要跑好几小时）能扛住临时限流。

        只对**限流**重试；其它错误（目录不存在 / 凭据失效）立即抛出——
        那些重试也没用。
        """
        from p115client import tool

        last_exc: Exception | None = None
        for attempt in range(1, LIST_RETRY_MAX + 1):
            await rate_limiter.acquire("p115")
            try:
                return list(
                    tool.iterdir(
                        client,
                        int(cid) if str(cid).isdigit() else cid,
                        # **必须固定用 app="web"**（不要用 device_type）：
                        #   app="web"  → client.fs_files（webapi，实测正常）
                        #   其它 app   → client.fs_files_app（proapi，**405**）
                        # 实测：app=alipaymini 时 /影视/电影 返回 405，
                        # 改用 web 正常。device_type 只影响「这次登录记成哪台设备」。
                        app="web",
                    )
                )
            except Exception as exc:
                last_exc = exc
                kind = _classify_list_error(exc)
                if kind != "rate_limit":
                    raise DriverError(
                        f"115 列目录失败：{exc}", kind=kind
                    ) from exc
                if attempt >= LIST_RETRY_MAX:
                    break
                delay = LIST_RETRY_BACKOFF_SEC * attempt
                logger.warning(
                    "115 列目录被限流（第 %d/%d 次），%ds 后重试：cid=%s",
                    attempt,
                    LIST_RETRY_MAX,
                    delay,
                    cid,
                )
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise DriverError(
            f"115 列目录失败（限流重试 {LIST_RETRY_MAX} 次仍失败）：{last_exc}",
            kind="rate_limit",
        ) from last_exc

    async def iter_tree_by_cid(
        self,
        cid: str | int,
        parent_path: str = "",
        include_subdirs: bool = True,
    ) -> AsyncIterator[CloudFile]:
        """按 cid **递归**遍历目录树，**流式**产出文件。

        递归时用子项**自己的 id** 作为下一层 cid——不需要再解析路径。

        **流式而非一次性收集**：一棵大树有几百个目录，限速 1s/请求下要跑
        几分钟。若先收集再返回，调用方在遍历期间**无法报告任何进度**。

        Yields:
            每个文件（目录不产出）。
        """
        stack: list[tuple[str | int, str]] = [(cid, parent_path)]

        while stack:
            current_cid, current_path = stack.pop()
            children = await self.list_dir_by_cid(current_cid, parent_path=current_path)
            for child in children:
                if child.is_dir:
                    if include_subdirs:
                        # 用子项 id 继续递归——目录改名不影响
                        stack.append((child.remote_id, child.path))
                else:
                    yield child

    async def resolve_cid(self, path: str) -> str:
        """把路径解析成 cid（供「保存时自动填充 media_path」用）。"""
        return str(await self._resolve_cid(self._client, path))

    async def resolve_path(self, cid: str | int) -> str:
        """把 cid 反解成路径（只读展示用）。

        用 ``tool.get_path``——实测：
        ``get_path(3116168171472551604)`` → ``'/影视'``
        """
        from p115client import tool

        client = self._client
        try:
            path = await tool.get_path(client, int(cid) if str(cid).isdigit() else cid, async_=True)
        except Exception as exc:
            raise DriverError(
                f"115 无法解析 cid={cid} 的路径：{exc}", kind="not_found"
            ) from exc
        return str(path or "")

    async def _resolve_cid(self, client: Any, path: str) -> int | str:
        """把路径解析成目录 id（cid）。

        115 的目录用**数字 id** 标识，路径只是给人看的。要按路径遍历必须先
        解析出 cid——``tool.dir_getid(client, path)`` 直接吃完整路径，
        返回该路径对应的目录 id（根目录返回 0）。
        """
        from p115client import tool

        try:
            return await tool.dir_getid(client, path, async_=True)
        except Exception as exc:
            raise DriverError(
                f"115 目录不存在或无权限：{path}（{exc}）",
                kind="not_found",
            ) from exc

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        """换取直链。

        **UA 透传**：115 直链的 ``f=1`` 表示「请求直链时的 UA 必须与访问
        直链时的 UA 一致」，因此这里必须用**播放器的 UA** 去换。

        **限流退避重试**：附件同步的直链请求量最大（每个附件一次），
        长时间跑必然撞上限流。重试能扛住临时限流。
        """
        client = self._client
        url: Any = None
        last_exc: Exception | None = None

        for attempt in range(1, LIST_RETRY_MAX + 1):
            await rate_limiter.acquire("p115")
            try:
                # 必须是**实例**调用——download_url 的第一个参数是 self。
                # app 用 "android"：官方 302 示例就是这么用的，且 web 端
                # 更容易触发「IP登录异常」。
                url = await client.download_url(
                    remote_id,
                    user_agent=user_agent,
                    app="android",
                    async_=True,
                )
                break
            except DriverError:
                raise
            except Exception as exc:
                last_exc = exc
                kind, message = _classify_download_error(exc)
                if kind != "rate_limit" or attempt >= LIST_RETRY_MAX:
                    raise DriverError(message, kind=kind) from exc
                delay = LIST_RETRY_BACKOFF_SEC * attempt
                logger.warning(
                    "115 换直链被限流（第 %d/%d 次），%ds 后重试：%s",
                    attempt,
                    LIST_RETRY_MAX,
                    delay,
                    remote_id,
                )
                await asyncio.sleep(delay)

        if url is None:
            assert last_exc is not None
            raise DriverError(
                f"115 换直链失败（限流重试 {LIST_RETRY_MAX} 次仍失败）：{last_exc}",
                kind="rate_limit",
            ) from last_exc

        if not url:
            raise DriverError("115 换直链返回空", kind="not_found")
        return DirectLink(url=str(url), ua_required=True)

    async def check_login(self) -> bool:
        """检查 cookie 是否仍然有效。

        用 ``login_status()``——**没有** ``check_login`` 方法。
        """
        try:
            result = await self._client.login_status(async_=True)
        except Exception:
            return False
        return bool(result)


def _to_cloud_file(item: object, parent_path: str = "") -> CloudFile | None:
    """把 ``tool.iterdir`` 的归一化结果转成 :class:`CloudFile`。

    ``tool.iterdir`` 的字段（经 ``normalize_attr`` 归一化）：

    ==============  ==========================================
    字段            含义
    ==============  ==========================================
    ``id``          文件 / 目录 id
    ``name``        文件名
    ``size``        字节数
    ``pick_code``   提取码（换直链用）
    ``is_dir``      是否目录
    ``mtime``       修改时间
    ``sha1``        SHA1
    ==============  ==========================================

    **``path`` 字段是相对当前目录的**（列 ``/影视/电影`` 时返回
    ``/阿凡达 (2009)`` 而非 ``/影视/电影/阿凡达 (2009)``）——不能直接用，
    否则递归时父目录层级会丢，导致子目录再也列不出来。故**自己拼**绝对路径。
    """
    if not isinstance(item, dict):
        return None

    is_dir = bool(item.get("is_dir", False))
    name = str(item.get("name") or "")
    pick_code = str(item.get("pick_code") or item.get("pickcode") or "")
    file_id = str(item.get("id") or "")

    # 目录没有 pickcode——用 id 占位（但不会被当作媒体文件）；
    # 文件优先用 pickcode（换直链要用），退回 id。
    remote_id = file_id if is_dir else (pick_code or file_id)

    if not remote_id:
        return None

    return CloudFile(
        remote_id=remote_id,
        path=_join_path(parent_path, name),
        name=name,
        size=int(item.get("size") or 0),
        is_dir=is_dir,
        mtime=int(item.get("mtime") or 0),
        sha1=item.get("sha1") or None,
    )


def _classify_list_error(exc: Exception) -> str:
    """把列目录的异常归类。

    - ``rate_limit`` —— 115 限流（``405 Method Not Allowed``）
    - ``not_found`` —— 目录不存在
    - ``network`` —— 其它
    """
    text = str(exc)
    if "405" in text or "Method Not Allowed" in text:
        return "rate_limit"
    if "not found" in text.lower() or "不存在" in text:
        return "not_found"
    return "network"


def _join_path(parent_path: str, name: str) -> str:
    """拼出绝对路径：``/影视/电影`` + ``阿凡达`` → ``/影视/电影/阿凡达``。"""
    base = parent_path.rstrip("/")
    if not base:
        return f"/{name}"
    return f"{base}/{name}"


#: 115 账号安全验证的提示文案
#:
#: 115 对「换直链」接口单独加了账号安全校验（``code 911`` /
#: ``errno 137``），列目录不受影响。触发时**必须**在手机 115 APP 里
#: 完成验证——扫码重新登录**没用**（社区共识：一天大约过一次验证）。
#:
#: 参考：https://github.com/cloud-fs/cloud-fs.github.io/issues/123
_VERIFY_HINT = (
    "115 账号需要安全验证：请用手机 115 APP 进入「文件」目录，"
    "按提示完成验证（大约每天一次）。重新扫码登录无效。"
)


def _classify_download_error(exc: Exception) -> tuple[str, str]:
    """把换直链的异常归类成 (kind, 可读提示)。

    kind 取值：

    - ``auth_verify`` —— 账号需安全验证（code 911 / errno 137）
    - ``auth`` —— 凭据失效
    - ``rate_limit`` —— 被限流
    - ``network`` —— 其它网络 / 协议错误
    """
    text = str(exc)
    lowered = text.lower()

    # code 911 / errno 137 —— 账号异常，需 APP 验证
    if "911" in text or "137" in text or "验证激活" in text:
        return "auth_verify", f"115 换直链被拒：账号需安全验证。{_VERIFY_HINT}"

    # 凭据失效
    if "cookie" in lowered and ("失效" in text or "expire" in lowered or "login" in lowered):
        return "auth", "115 凭据已失效，请重新扫码登录"

    # 限流
    #
    # 115 的限流有**两种**表现：
    # - ``429 Too Many Requests`` / 「频繁」
    # - ``405 Method Not Allowed``（实测跑长时间任务时列目录与换直链都会遇到）
    # 漏掉 405 会把限流误判成 ``network`` → **不触发退避重试** → 任务直接挂。
    if (
        "429" in text
        or "too many requests" in lowered
        or "频繁" in text
        or "405" in text
        or "method not allowed" in lowered
    ):
        return "rate_limit", "115 请求被限流，请稍后重试"

    return "network", f"115 换直链失败：{text}"


def build_p115_driver(
    credential: str | None = None,
    device_type: str | None = None,
) -> P115Driver:
    """工厂函数（签名与驱动注册表一致）。

    Args:
        credential: **已解密**的 115 cookie 字符串。
        device_type: 设备类型（如 ``115android``），扫码登录时绑定的 app。
    """
    return P115Driver(credential or "", device_type)


def reset_clients() -> None:
    """清掉缓存的客户端实例（重新扫码登录后调用）。"""
    _CLIENTS.clear()
