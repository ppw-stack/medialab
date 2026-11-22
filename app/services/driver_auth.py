"""存储源登录服务（扫码 / 短信）。

**为什么独立于 :mod:`app.services.driver_service`**：

登录流程的共同点是「拿凭据 → 加密落盘」，与 CRUD 职责不同。两者共享
底层能力（:func:`app.services.driver_service.store_credential`），
但对外接口与失败语义不同，故拆开。

**API 层不感知适配层**：扫码会话、凭据对象、驱动异常全部收在本模块，
路由层只拿到 dataclass（:class:`QRStartResult` / :class:`QRStatusResult`
/ :class:`SmsSendResult` / :class:`SmsVerifyResult`）。

**唯一的存储源类型是光鸭盘**：Device Code 扫码
（``account.guangyapan.com``）或短信验证码。其它类型一律 400。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.drivers import guangya_auth, guangya_credential
from app.adapters.drivers.credential_crypto import CredentialCryptoError
from app.core.logging import get_logger
from app.services.driver_service import (
    get_driver,
    store_credential,
)

logger = get_logger(__name__)


class DriverAuthError(Exception):
    """登录流程失败。

    ``status_code`` 区分「用户能改的错」（400）与「上游故障」（502）。
    """

    def __init__(self, message: str, *, status_code: int = 502, kind: str = "upstream"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


# ================================================================ 数据载体


@dataclass(slots=True)
class QRStartResult:
    """扫码会话已启动。"""

    session_id: str
    #: base64 PNG（前端直接 <img src="data:image/png;base64,...">）
    qr_image_b64: str


@dataclass(slots=True)
class QRStatusResult:
    """扫码状态查询结果。"""

    kind: str
    #: 错误分类（``None`` = 无错误）。适配层给的是 ``Literal | None``，
    #: 这里统一成 ``str``（空串 = 无错误）——API 契约是字符串。
    error_kind: str = ""
    error_msg: str = ""
    confirmed: bool = False


@dataclass(slots=True)
class SmsSendResult:
    """短信验证码已发出。"""

    captcha_token: str
    captcha_url: str = ""


@dataclass(slots=True)
class SmsVerifyResult:
    """短信验证码校验结果。"""

    ok: bool
    message: str = ""


# ================================================================ 内部辅助


async def _driver_type(driver_id: int) -> str:
    """取存储源类型（存储源不存在时抛 ``DriverServiceError`` 404）。"""
    from app.db.session import session_scope

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        return row.type


async def _require_guangya(driver_id: int) -> None:
    """登录只支持光鸭盘（其它类型一律 400，不是上游故障）。

    Raises:
        DriverAuthError: 存储源类型不是 ``guangya``（``status_code=400``、
            ``kind="invalid"``）。存储源不存在时由 :func:`_driver_type`
            转发的 ``DriverServiceError``（404）先冒泡，路由层统一映射。
    """
    if await _driver_type(driver_id) != "guangya":
        raise DriverAuthError("该存储源不支持登录", status_code=400, kind="invalid")


# ================================================================ 扫码登录


async def start_qr(driver_id: int, *, client_mode: bool = False) -> QRStartResult:
    """启动扫码登录。

    光鸭走 Device Code（``account.guangyapan.com``）。

    Args:
        client_mode: 仅光鸭有效。``True`` = 用**客户端 client_id** 申领，
            签发出的 token ``aud = 客户端``（能取非会员大文件直链），
            且 scope 带 ``offline``（有 ``refresh_token`` 可自续期）。
            ``False`` = 原来的 Web 身份登录。
    """
    await _require_guangya(driver_id)
    try:
        gyp_result = await guangya_auth.start_qr_session(client_mode=client_mode)
    except guangya_auth.GuangyaAuthError as exc:
        raise DriverAuthError(str(exc), status_code=502) from exc
    except Exception as exc:
        raise DriverAuthError(f"启动扫码失败：{exc}", status_code=502) from exc
    return QRStartResult(
        session_id=gyp_result.session_id, qr_image_b64=gyp_result.qr_image_b64
    )


async def qr_status(driver_id: int, session_id: str) -> QRStatusResult:
    """查询扫码状态。

    ``confirmed`` 时**自动落盘凭据**（加密存储）。
    """
    await _require_guangya(driver_id)

    gyp_status = await guangya_auth.get_qr_status(session_id)
    if gyp_status is None:
        raise DriverAuthError("扫码会话不存在或已过期", status_code=404, kind="not_found")
    confirmed = gyp_status.kind == "confirmed"
    if confirmed and gyp_status.credential:
        await _store_guangya_credential(
            driver_id,
            gyp_status.credential,
            client_mode=gyp_status.client_mode,
        )
    return QRStatusResult(
        kind=gyp_status.kind,
        error_kind=gyp_status.error_kind or "",
        error_msg=gyp_status.error_msg or "",
        confirmed=confirmed,
    )


async def _store_guangya_credential(
    driver_id: int,
    payload: dict[str, Any],
    *,
    client_mode: bool,
) -> None:
    """把光鸭登录结果写回凭据（**合并**，不覆盖另一层）。

    Args:
        payload: 登录返回的凭据 dict（``guangya_auth`` 的格式）。
        client_mode: ``True`` = 本次签发的是**客户端身份** token
            （``aud = CLIENT_ID_MAC``）。

    **为什么要合并**：光鸭凭据是一个 JSON，含 Web 层与客户端覆盖层。
    直接新建对象会把另一层抹掉——Web 扫码每 1~2 小时刷一次 token，
    客户端 token 会被顺手清掉（2026-11-19 记录过的坑）。

    client 模式下主 ``access_token`` 也用同一把：实测客户端 aud 的 token
    对扫描 / 列目录 / 上传全部生效，一套比两套少一个失效点。
    """
    from app.db.session import session_scope

    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    device = str(payload.get("device_id") or "")
    expires_at = int(payload.get("expires_at") or 0)

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        try:
            cred = guangya_credential.decode_credential(row.credential)
        except (
            CredentialCryptoError,
            ValueError,
            TypeError,
        ):
            # **只降级「数据坏了」**（密文解不开 / 明文不是合法 JSON /
            # 字段类型不对）——编程缺陷（AttributeError 等）让它冒泡。
            # 降级必须留痕：否则解密层出问题没人知道，客户端 token
            # 覆盖层会被无声清空。
            logger.warning(
                "光鸭凭据解密失败，按新建凭据降级处理 driver_id=%s",
                driver_id,
                exc_info=True,
            )
            cred = None
        cred = cred or guangya_credential.GuangyaCredential(access_token="")

        if client_mode:
            cred.access_token = access
            cred.expires_at = expires_at
            cred.device_id = device
            # 主路径不再走 Web 刷新（那是另一个 aud），交给客户端刷新
            cred.refresh_token = ""
            cred.client_access_token = access
            cred.client_refresh_token = refresh
            cred.client_expires_at = expires_at
            cred.client_device_id = device
        else:
            cred.access_token = access
            cred.refresh_token = refresh
            cred.device_id = device
            cred.expires_at = expires_at

        await store_credential(
            db, driver_id, cred.to_json(), scheme=guangya_credential.SCHEME
        )


# ================================================================ 短信登录（光鸭）


async def send_sms_code(driver_id: int, phone: str) -> SmsSendResult:
    """发送短信验证码（光鸭盘）。

    短信**立即发出**，返回后续步骤要用的 ``captcha_token``。
    """
    await _require_guangya(driver_id)

    try:
        result = await guangya_auth.send_sms_code(phone)
    except guangya_auth.GuangyaAuthError as exc:
        raise DriverAuthError(str(exc), status_code=502) from exc
    return SmsSendResult(
        captcha_token=result.captcha_token, captcha_url=result.captcha_url
    )


async def verify_sms_code(
    driver_id: int,
    phone: str,
    code: str,
    captcha_token: str,
) -> SmsVerifyResult:
    """校验短信验证码并登录（光鸭盘）。

    校验失败返回 ``ok=False``（**不抛异常**）——验证码输错是**用户可
    重试**的正常情况，不是服务端故障；用异常表达会让前端误判成故障。
    """
    await _require_guangya(driver_id)

    try:
        cred = await guangya_auth.verify_sms_code(phone, code, captcha_token)
    except guangya_auth.GuangyaAuthError as exc:
        return SmsVerifyResult(ok=False, message=str(exc))

    # **合并**写入：短信登录只更新 Web 层，客户端覆盖层原样保留
    await _store_guangya_credential(driver_id, cred, client_mode=False)
    return SmsVerifyResult(ok=True, message="登录成功，凭据已加密存储")


__all__ = [
    "DriverAuthError",
    "QRStartResult",
    "QRStatusResult",
    "SmsSendResult",
    "SmsVerifyResult",
    "qr_status",
    "send_sms_code",
    "start_qr",
    "verify_sms_code",
]
