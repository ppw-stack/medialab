"""115 扫码登录。

直调 p115client 的四个 QR 方法，编排成非阻塞的两段：

1. :func:`start_qr_session` —— ``login_qrcode_token()``（app 恒 ``web``）
   拿 ``data{uid,time,sign,qrcode}``，**本地**渲染 canonical scan URL 为 PNG，
   起后台轮询任务。
2. :func:`get_qr_status` —— 轮询 ``login_qrcode_scan_status()``（零 cookie）。

status 语义：``0`` 等待 / ``1`` 已扫 / ``2`` 登入 / ``-1`` 过期 / ``-2`` 取消。

``status == 2`` 时调 ``login_qrcode_scan_result(uid, app=device_type)``
拿 cookie 字典 → 序列化 → 加密落盘。

**扫码图必须本地渲染**：服务端返回的 PNG 内嵌 ``yun.115.com`` 域名，
115 APP 会拒扫。本地渲染 canonical URL 已人工验证可扫。
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.adapters.drivers.device_types import (
    DEFAULT_DEVICE_TYPE,
    describe_qr_incapable,
    is_qr_capable,
)
from app.adapters.drivers.qr_render import render_qr_png
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 轮询间隔（秒）
POLL_INTERVAL_SEC = 2
#: 扫码会话超时（秒）
POLL_TIMEOUT_SEC = 180

QRSessionKind = Literal["waiting", "scanned", "confirmed", "expired", "error"]
QRErrorKind = Literal["ip_limited", "rate_limit", "unknown"]


class QRCapabilityError(RuntimeError):
    """所选设备类型不支持扫码登录。

    18 个设备类型里只有 10 个能取到二维码（实测）——其余 8 个会返回
    ``state=1`` 却**没有 qrcode 字段**，属静默失败。故在**发起前**拦下，
    给用户明确提示而不是让人对着空白页发呆。
    """


@dataclass
class QRSession:
    """一个扫码会话。"""

    session_id: str
    app: str
    payload: dict[str, Any]
    qr_image_b64: str
    kind: QRSessionKind = "waiting"
    error_kind: QRErrorKind | None = None
    error_msg: str | None = None
    cookie_str: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass
class QRStartResult:
    """扫码启动结果。"""

    qr_image_b64: str
    session_id: str


@dataclass
class QRStatusResult:
    """扫码状态查询结果。"""

    kind: QRSessionKind
    cookie_str: str | None = None
    error_kind: QRErrorKind | None = None
    error_msg: str | None = None


_SESSIONS: dict[str, QRSession] = {}
#: 已完成的会话（扫码成功 / 失败）——保留一段时间供前端读取结果
_FINISHED_SESSIONS: dict[str, QRSession] = {}
#: 已完成会话的过期时刻（monotonic 基准）
_FINISHED_EXPIRY: dict[str, float] = {}
_LOCK = asyncio.Lock()

#: 扫码成功后结果保留时长（秒）
#:
#: 前端扫码成功后还会再轮询一次才能拿到 cookie——若此时会话已被清理，
#: 前端会拿到 404，把「登录成功」误显示成「二维码过期」。
RESULT_TTL_SEC = 120


def _qr_token() -> dict[str, Any]:
    """``login_qrcode_token(app="web")`` → ``{state, data:{uid,time,sign,qrcode}}``。"""
    from p115client import P115Client

    resp: dict[str, Any] = P115Client.login_qrcode_token()
    return resp


def _qr_status(payload: dict[str, Any]) -> dict[str, Any]:
    """``login_qrcode_scan_status(payload)`` → ``{..., data:{status:int}}``。零 cookie。"""
    from p115client import P115Client

    resp: dict[str, Any] = P115Client.login_qrcode_scan_status(payload)
    return resp


def _qr_result(uid: str, app: str) -> dict[str, Any]:
    """``login_qrcode_scan_result(uid, app)`` → ``{..., data:{cookie:{...}}}``。"""
    from p115client import P115Client

    resp: dict[str, Any] = P115Client.login_qrcode_scan_result(uid, app=app)
    return resp


def _render_qr_png(scan_url: str) -> bytes:
    """本地渲染 QR PNG（内容 = 115 APP 期望的 canonical scan URL）。"""
    return render_qr_png(scan_url)


def _map_p115_error(exc: Exception) -> tuple[QRErrorKind, str]:
    """把 p115client 抛的异常归到 3 类 error_kind（前端分别渲染）。"""
    msg = str(exc)
    if "IP" in msg and "登录" in msg:
        return "ip_limited", msg[:200]
    if "429" in msg or "Too Many Requests" in msg or "频繁" in msg:
        return "rate_limit", msg[:200]
    return "unknown", msg[:200]


async def start_qr_session(device_type: str = DEFAULT_DEVICE_TYPE) -> QRStartResult:
    """启动扫码会话。

    Args:
        device_type: 设备类型（``drivers.device_type``）。**必须能扫码**——
            18 个设备类型里只有 10 个能取到二维码（实测）。

    **关于 "IP登录异常"**：115 对 ``app="web"`` 的扫码登录最敏感，频繁
    触发「IP登录异常」时**要等到次日零点才解禁**（官方文档明确说了，
    用 VPN 可绕过）。所以：
    - **token 阶段**用 ``web``——这是 115 唯一稳定的取码路径（实测 10 个
      app 都能出码，但 web 最稳）
    - **绑定阶段**（``login_qrcode_scan_result``）用用户选的 device_type，
      决定「这次登录记成哪个设备」
    - 失败时明确提示用户「可能要等次日零点」，别让人干等

    Raises:
        QRCapabilityError: 选的设备类型不支持扫码。
    """
    if not is_qr_capable(device_type):
        raise QRCapabilityError(describe_qr_incapable(device_type))

    session_id = secrets.token_urlsafe(16)

    def _init() -> tuple[dict[str, Any], bytes]:
        # token 阶段固定用 web（最稳的取码路径）
        resp = _qr_token()
        data = resp["data"]
        scan_url = data["qrcode"]
        return data, _render_qr_png(scan_url)

    try:
        payload, png = await asyncio.to_thread(_init)
    except KeyError as exc:
        logger.error("115 QR token 返回结构异常：%s", exc)
        raise RuntimeError("115 返回的扫码信息不完整") from exc

    qr_b64 = base64.b64encode(png).decode("ascii")
    session = QRSession(
        session_id=session_id,
        app=device_type,
        payload=payload,
        qr_image_b64=qr_b64,
    )
    async with _LOCK:
        _SESSIONS[session_id] = session

    session.task = asyncio.create_task(_run_qr_session(session))
    logger.info("115 扫码会话已启动：%s device_type=%s", session_id, device_type)
    return QRStartResult(qr_image_b64=qr_b64, session_id=session_id)


async def _run_qr_session(session: QRSession) -> None:
    """后台轮询扫码状态。"""
    elapsed = 0
    try:
        while elapsed < POLL_TIMEOUT_SEC:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            elapsed += POLL_INTERVAL_SEC
            try:
                resp = await asyncio.to_thread(_qr_status, session.payload)
            except Exception as exc:
                kind, msg = _map_p115_error(exc)
                session.kind = "error"
                session.error_kind = kind
                session.error_msg = msg
                logger.warning("115 扫码轮询失败（%s）：%s", kind, msg)
                return

            status = int(resp.get("data", {}).get("status", 0))
            if status == 1:
                session.kind = "scanned"
            elif status == 2:
                await _confirm_session(session)
                return
            elif status == -1:
                session.kind = "expired"
                logger.info("115 扫码会话已过期：%s", session.session_id)
                await _retire_session(session)
                return
            elif status == -2:
                session.kind = "expired"
                logger.info("115 扫码会话被取消：%s", session.session_id)
                await _retire_session(session)
                return
    except asyncio.CancelledError:
        raise
    finally:
        # 异常退出（轮询报错）也要清理。
        #
        # **注意**：成功路径（status==2）**不**走这里——扫码成功后必须
        # 让会话在表里多活一会儿，否则前端下一次轮询会拿到 404，
        # 把「登录成功」误显示成「二维码过期」。
        if session.kind not in ("confirmed",):
            await _retire_session(session)


async def _retire_session(session: QRSession) -> None:
    """把会话移出活跃表。

    **但保留结果**供前端读取——扫码成功后前端还要再轮询一次才能拿到
    cookie。若立刻删除，前端拿到 404 → 误报「二维码过期」，而实际上
    登录已经成功（凭据都拿到了）。

    故：成功后把会话移到 ``_FINISHED_SESSIONS``，保留
    :data:`RESULT_TTL_SEC` 秒；失败/过期的直接删（无需保留结果）。
    """
    async with _LOCK:
        _SESSIONS.pop(session.session_id, None)
        if session.kind == "confirmed":
            _FINISHED_SESSIONS[session.session_id] = session
            expires_at = time.monotonic() + RESULT_TTL_SEC
            _FINISHED_EXPIRY[session.session_id] = expires_at
            logger.info(
                "115 扫码会话完成，结果保留 %d 秒供前端读取：%s",
                RESULT_TTL_SEC,
                session.session_id,
            )
            _purge_finished()


def _purge_finished() -> None:
    """清理过期的已完成会话（同步调用，调用方已持锁）。"""
    now = time.monotonic()
    stale = [sid for sid, exp in _FINISHED_EXPIRY.items() if exp <= now]
    for sid in stale:
        _FINISHED_EXPIRY.pop(sid, None)
        _FINISHED_SESSIONS.pop(sid, None)


async def _confirm_session(session: QRSession) -> None:
    """扫码确认 → 取 cookie → 序列化。"""
    uid = str(session.payload.get("uid", ""))
    try:
        resp = await asyncio.to_thread(_qr_result, uid, session.app)
    except Exception as exc:
        kind, msg = _map_p115_error(exc)
        session.kind = "error"
        session.error_kind = kind
        session.error_msg = msg
        logger.warning("115 扫码确认失败（%s）：%s", kind, msg)
        return

    raw_cookie = resp.get("data", {}).get("cookie", {})
    cookie: dict[str, str] = {}
    if isinstance(raw_cookie, list):
        # 115 有时返回 [[k, v], ...] 的列表形态
        cookie = {str(i): str(v) for i, v in raw_cookie}
    elif isinstance(raw_cookie, str):
        # 有些 app 形态直接返回 "UID=...; CID=..." 字符串
        for part in raw_cookie.split(";"):
            if "=" in part:
                key, _, value = part.partition("=")
                cookie[key.strip()] = value.strip()
    elif isinstance(raw_cookie, dict):
        cookie = {str(k): str(v) for k, v in raw_cookie.items()}
    if not cookie:
        session.kind = "error"
        session.error_kind = "unknown"
        session.error_msg = "115 未返回 cookie"
        return

    session.cookie_str = "; ".join(f"{k}={v}" for k, v in cookie.items())
    session.kind = "confirmed"
    logger.info("115 扫码确认成功：%s", session.session_id)


async def get_qr_status(session_id: str) -> QRStatusResult | None:
    """查询扫码状态。

    活跃会话与**已完成会话**都要查——扫码成功后前端还会再轮询一次
    才能拿到 cookie，此时会话已从活跃表移到已完成表。

    会话不存在（从未创建 / 已过保留期）返回 None。
    """
    async with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            session = _FINISHED_SESSIONS.get(session_id)
        if session is None:
            # 顺手清理过期的已完成会话
            _purge_finished()
    if session is None:
        return None
    return QRStatusResult(
        kind=session.kind,
        cookie_str=session.cookie_str,
        error_kind=session.error_kind,
        error_msg=session.error_msg,
    )


async def cancel_qr_session(session_id: str) -> bool:
    """取消扫码会话。"""
    async with _LOCK:
        session = _SESSIONS.pop(session_id, None)
    if session is None:
        return False
    if session.task is not None:
        session.task.cancel()
    return True
