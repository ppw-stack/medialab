"""光鸭盘登录（扫码 + 短信验证码）。

**用的不是官方 OpenAPI**，而是光鸭 Web 端自己的接口
（``account.guangyapan.com``）。原因（实测 2026-10-27）：

- 官方 OpenAPI 要求 ``client_id`` / ``project_id`` / ``sign_secret``
  三件套，还要报备服务器出口 IP（否则 ``code=111``）
- 而 Web 端用的 ``client_id`` 是光鸭**官方自己**的固定值
  （``aMe-8VSlkrbQXpUR``），实测可直接拿到 device_code
- 官方那套域名对未注册的 client_id 返回 ``client_id not registered``

两种登录方式：

1. **Device Code 扫码**（:func:`start_qr_session`）——与 115 的 UX 一致。
   服务端直接给 ``verification_uri_complete``，本地渲染二维码即可
   （不像 115 要自己拼 canonical URL）。
2. **短信验证码**（:func:`send_sms_code` / :func:`verify_sms_code`）——
   四步：``captcha/init`` → ``verification`` → ``verification/verify``
   → ``signin``。

两种方式最终都拿到 ``access_token`` + ``refresh_token``，落盘格式一致。
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from app.adapters.drivers.qr_render import render_qr_png
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 账号域名（登录 / 刷新 Token）
DEFAULT_ACCOUNT_BASE_URL = "https://account.guangyapan.com"

#: 光鸭 Web 端的 client_id（官方固定值，不是用户申请的）
DEFAULT_CLIENT_ID = "aMe-8VSlkrbQXpUR"

#: 轮询间隔（秒）——服务端返回的 interval 优先
POLL_INTERVAL_SEC = 2
#: 扫码会话超时（秒）
POLL_TIMEOUT_SEC = 180

#: 扫码成功后结果保留时长（秒）
#:
#: 前端扫码成功后还会再轮询一次才能拿到 token——若此时会话已被清理，
#: 前端会拿到 404，把「登录成功」误显示成「二维码过期」。
RESULT_TTL_SEC = 120

QRSessionKind = Literal["waiting", "scanned", "confirmed", "expired", "error"]
QRErrorKind = Literal["rate_limit", "captcha_required", "unknown"]


class GuangyaAuthError(RuntimeError):
    """光鸭授权失败。

    Attributes:
        kind: 错误分类（``rate_limit`` / ``captcha_required`` / ``unknown``）。
    """

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class QRSession:
    """一个扫码会话。"""

    session_id: str
    device_id: str
    device_code: str
    qr_image_b64: str
    kind: QRSessionKind = "waiting"
    error_kind: QRErrorKind | None = None
    error_msg: str | None = None
    #: 授权成功后拿到的凭据
    credential: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass
class QRStartResult:
    """扫码启动结果。"""

    qr_image_b64: str
    session_id: str
    #: 用户码（部分平台需要手动输入）
    user_code: str = ""


@dataclass
class QRStatusResult:
    """扫码状态查询结果。"""

    kind: QRSessionKind
    credential: dict[str, Any] | None = None
    error_kind: QRErrorKind | None = None
    error_msg: str | None = None


_SESSIONS: dict[str, QRSession] = {}
_FINISHED_SESSIONS: dict[str, QRSession] = {}
_FINISHED_EXPIRY: dict[str, float] = {}
_LOCK = asyncio.Lock()


def _client_id() -> str:
    """取 client_id（配置优先，兜底为光鸭 Web 端的固定值）。"""
    from app.core.config import get_settings

    return get_settings().guangya_client_id.strip() or DEFAULT_CLIENT_ID


def _account_base_url() -> str:
    """取账号域名（配置优先）。"""
    from app.core.config import get_settings

    return (
        get_settings().guangya_account_base_url.strip().rstrip("/")
        or DEFAULT_ACCOUNT_BASE_URL
    )


def _account_headers(device_id: str) -> dict[str, str]:
    """账号域名的请求头。

    实测只需要 ``X-Client-Id`` + ``X-Device-Id``（服务端不校验其余头）。
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Client-Id": _client_id(),
        "X-Device-Id": device_id,
    }


def _map_error(payload: dict[str, Any]) -> tuple[QRErrorKind, str]:
    """把服务端的错误响应归类。"""
    code = payload.get("code")
    msg = str(payload.get("msg") or payload.get("error") or payload.get("message") or "")
    text = f"{code}: {msg}" if code is not None else msg
    if code in (429, 4050) or "频繁" in msg or "too many" in msg.lower():
        return "rate_limit", text[:200]
    if "captcha" in str(payload).lower() or "验证" in msg:
        return "captcha_required", text[:200]
    return "unknown", text[:200]


def _as_error_kind(kind: str) -> QRErrorKind:
    """把任意字符串收敛成合法的 :data:`QRErrorKind`。"""
    if kind in ("rate_limit", "captcha_required", "unknown"):
        return kind  # type: ignore[return-value]
    return "unknown"


def _post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """POST 一个 JSON 接口，返回解析后的 dict。

    Raises:
        GuangyaAuthError: 网络错误 / 响应不是 JSON。
    """
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=20.0)
    except httpx.HTTPError as exc:
        raise GuangyaAuthError(f"请求失败：{exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise GuangyaAuthError(
            f"响应不是 JSON（HTTP {response.status_code}）：{response.text[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise GuangyaAuthError("响应格式异常")
    return data


def _extract_tokens(payload: dict[str, Any]) -> dict[str, Any] | None:
    """从 token 响应里抽出凭据字段（没有就返回 None）。"""
    access = payload.get("access_token")
    if not access:
        return None
    expires_in = payload.get("expires_in")
    expires_at = int(time.time()) + int(expires_in) if expires_in else 0
    return {
        "access_token": str(access),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": expires_at,
        "sub": str(payload.get("sub") or ""),
    }


async def start_qr_session(device_id: str | None = None) -> QRStartResult:
    """启动扫码会话。

    Args:
        device_id: 设备标识。不传则新生成一个（32 位 hex）。

    Returns:
        二维码（base64 PNG）与会话 id。

    Raises:
        GuangyaAuthError: 取码失败（限流 / 其它）。
    """
    device = device_id or secrets.token_hex(16)
    url = f"{_account_base_url()}/v1/auth/device/code"
    payload = {"scope": "user", "client_id": _client_id()}

    data = await asyncio.to_thread(_post, url, payload, _account_headers(device))
    if data.get("error") or (data.get("code") not in (None, 0)):
        kind, msg = _map_error(data)
        raise GuangyaAuthError(f"获取二维码失败：{msg}", kind=kind)

    device_code = str(data.get("device_code") or "")
    scan_url = str(data.get("verification_uri_complete") or "")
    if not device_code or not scan_url:
        raise GuangyaAuthError("光鸭返回的扫码信息不完整")

    png = await asyncio.to_thread(render_qr_png, scan_url)
    qr_b64 = base64.b64encode(png).decode("ascii")

    session = QRSession(
        session_id=secrets.token_urlsafe(16),
        device_id=device,
        device_code=device_code,
        qr_image_b64=qr_b64,
    )
    async with _LOCK:
        _SESSIONS[session.session_id] = session

    session.task = asyncio.create_task(_run_qr_session(session))
    logger.info("光鸭扫码会话已启动：%s device_id=%s", session.session_id, device)
    return QRStartResult(
        qr_image_b64=qr_b64,
        session_id=session.session_id,
        user_code=str(data.get("user_code") or ""),
    )


async def _run_qr_session(session: QRSession) -> None:
    """后台轮询扫码状态。"""
    elapsed = 0
    try:
        while elapsed < POLL_TIMEOUT_SEC:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            elapsed += POLL_INTERVAL_SEC
            try:
                payload = await asyncio.to_thread(_poll_token, session)
            except GuangyaAuthError as exc:
                session.kind = "error"
                session.error_kind = _as_error_kind(exc.kind)
                session.error_msg = str(exc)
                logger.warning("光鸭扫码轮询失败：%s", exc)
                return

            error = str(payload.get("error") or "")
            if error == "authorization_pending":
                continue
            if error:
                kind, msg = _map_error(payload)
                session.kind = "error"
                session.error_kind = kind
                session.error_msg = msg
                logger.warning("光鸭扫码失败：%s", msg)
                return
            cred = _extract_tokens(payload)
            if cred is None:
                continue
            cred["device_id"] = session.device_id
            session.credential = cred
            session.kind = "confirmed"
            logger.info("光鸭扫码成功：%s", session.session_id)
            return
    except asyncio.CancelledError:
        raise
    finally:
        # **成功路径也要 retire**——把会话从活跃表移出（结果保留在
        # ``_FINISHED_SESSIONS`` 里供前端再读一次）。
        #
        # 只在失败时 retire 会留下僵尸会话：活跃表一直占着内存，
        # 前端再查也会拿到 confirmed（虽然不影响功能，但表会涨）。
        await _retire_session(session)


def _poll_token(session: QRSession) -> dict[str, Any]:
    """轮询一次 token（同步）。"""
    url = f"{_account_base_url()}/v1/auth/token"
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": session.device_code,
        "client_id": _client_id(),
    }
    return _post(url, payload, _account_headers(session.device_id))


async def get_qr_status(session_id: str) -> QRStatusResult | None:
    """查询扫码状态。"""
    session = await _get_session(session_id)
    if session is None:
        return None
    return QRStatusResult(
        kind=session.kind,
        credential=session.credential,
        error_kind=session.error_kind,
        error_msg=session.error_msg,
    )


async def _get_session(session_id: str) -> QRSession | None:
    """取会话（含已完成会话的保留期处理）。"""
    async with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is not None:
            return session
        session = _FINISHED_SESSIONS.get(session_id)
        if session is None:
            return None
        expiry = _FINISHED_EXPIRY.get(session_id, 0.0)
        if time.monotonic() > expiry:
            _FINISHED_SESSIONS.pop(session_id, None)
            _FINISHED_EXPIRY.pop(session_id, None)
            return None
        return session


async def _retire_session(session: QRSession) -> None:
    """把会话移出活跃表。

    成功时**保留**结果一段时间（见 :data:`RESULT_TTL_SEC`）——前端扫码
    成功后还会再轮询一次才能拿到凭据，立刻删掉会让它拿到 404，
    把「登录成功」误显示成「二维码过期」。
    """
    async with _LOCK:
        _SESSIONS.pop(session.session_id, None)
        if session.kind == "confirmed":
            _FINISHED_SESSIONS[session.session_id] = session
            _FINISHED_EXPIRY[session.session_id] = time.monotonic() + RESULT_TTL_SEC


# -- 短信登录 --


@dataclass
class SmsInitResult:
    """短信登录初始化结果。"""

    #: 人机验证 URL（服务端要求先过人机验证时返回）
    captcha_url: str = ""
    #: 人机验证 token（后续两步要用）
    captcha_token: str = ""


async def send_sms_code(phone_number: str) -> SmsInitResult:
    """第一步：初始化并**发送**短信验证码。

    实测调用即发送短信（返回 ``verification_id``）。

    Args:
        phone_number: 手机号，含国际区号（如 ``+86 13800138000``）。

    Returns:
        ``captcha_token``（后续步骤要用）。

    Raises:
        GuangyaAuthError: 失败 / 需要人机验证。
    """
    from guangyaclient import GuangyaClient

    client = GuangyaClient()
    try:
        init = await asyncio.to_thread(client.login_sms_init, phone_number)
        captcha_token = str(init.get("captcha_token") or "")
        if not captcha_token:
            # 有些情况下服务端要求先过人机验证
            url = str(init.get("url") or "")
            if url:
                raise GuangyaAuthError(
                    f"需要人机验证，请先打开：{url}", kind="captcha_required"
                )
            raise GuangyaAuthError("初始化短信登录失败：未返回 captcha_token")
        await asyncio.to_thread(client.login_sms_send, phone_number, captcha_token)
    except GuangyaAuthError:
        raise
    except Exception as exc:
        raise GuangyaAuthError(f"发送短信失败：{exc}") from exc
    finally:
        await asyncio.to_thread(client.close)

    return SmsInitResult(captcha_token=captcha_token)


async def verify_sms_code(
    phone_number: str, code: str, captcha_token: str
) -> dict[str, Any]:
    """第二、三步：校验短信验证码 → 换登录态。

    Args:
        phone_number: 手机号。
        code: 用户收到的验证码。
        captcha_token: :func:`send_sms_code` 返回的 token。

    Returns:
        凭据 dict（``access_token`` / ``refresh_token`` / ``expires_at``）。

    Raises:
        GuangyaAuthError: 验证码错误 / 其它失败。
    """
    from guangyaclient import GuangyaClient

    client = GuangyaClient()
    try:
        sent = await asyncio.to_thread(
            client.login_sms_send, phone_number, captcha_token
        )
        verification_id = str(sent.get("verification_id") or "")
        if not verification_id:
            raise GuangyaAuthError("发送验证码失败：未返回 verification_id")

        verified = await asyncio.to_thread(
            client.login_sms_verify, verification_id, code
        )
        verification_token = str(verified.get("verification_token") or "")
        if not verification_token:
            raise GuangyaAuthError("验证码校验失败")

        signed = await asyncio.to_thread(
            client.login_sms_signin,
            code,
            verification_token,
            phone_number,
            captcha_token,
        )
    except GuangyaAuthError:
        raise
    except Exception as exc:
        raise GuangyaAuthError(f"登录失败：{exc}") from exc
    finally:
        await asyncio.to_thread(client.close)

    cred = _extract_tokens(signed if isinstance(signed, dict) else {})
    if cred is None:
        raise GuangyaAuthError("登录失败：未拿到 access_token")
    cred["device_id"] = ""
    return cred


def clear_all() -> None:
    """清空会话表（测试用）。"""
    _SESSIONS.clear()
    _FINISHED_SESSIONS.clear()
    _FINISHED_EXPIRY.clear()
