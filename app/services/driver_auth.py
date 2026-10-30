"""存储源登录服务（扫码 / 短信）。

**为什么独立于 :mod:`app.services.driver_service`**：

登录流程的共同点是「拿凭据 → 加密落盘」，与 CRUD 职责不同。两者共享
底层能力（:func:`app.services.driver_service.store_credential`），
但对外接口与失败语义不同，故拆开。

**API 层不感知适配层**：扫码会话、凭据对象、驱动异常全部收在本模块，
路由层只拿到 dataclass（:class:`QRStartResult` / :class:`QRStatusResult`
/ :class:`SmsSendResult` / :class:`SmsVerifyResult`）。

**按存储源类型分派**：

- **115** —— p115client 的扫码（绑设备类型）
- **光鸭** —— Device Code（``account.guangyapan.com``）或短信验证码
"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.drivers import guangya_auth, guangya_credential, p115_qr
from app.core.logging import get_logger
from app.domain.device_types import DEFAULT_DEVICE_TYPE
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


async def _driver_type_and_credential(driver_id: int) -> tuple[str, str | None, str | None]:
    """取存储源的类型 / 凭据 / 设备类型。

    Raises:
        DriverServiceError: 存储源不存在（404）。
    """
    from app.db.session import session_scope

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        return row.type, row.credential, row.device_type


# ================================================================ 扫码登录


async def start_qr(driver_id: int) -> QRStartResult:
    """启动扫码登录。

    **按存储源类型分派**：115 走 p115client 的扫码（绑设备类型），
    光鸭走 Device Code（``account.guangyapan.com``）。
    """
    driver_type, _, device_type = await _driver_type_and_credential(driver_id)

    if driver_type == "guangya":
        try:
            gyp_result = await guangya_auth.start_qr_session()
        except guangya_auth.GuangyaAuthError as exc:
            raise DriverAuthError(str(exc), status_code=502) from exc
        except Exception as exc:
            raise DriverAuthError(f"启动扫码失败：{exc}", status_code=502) from exc
        return QRStartResult(
            session_id=gyp_result.session_id, qr_image_b64=gyp_result.qr_image_b64
        )

    if driver_type != "p115":
        raise DriverAuthError("该存储源不支持扫码登录", status_code=400, kind="invalid")

    resolved_device = device_type or DEFAULT_DEVICE_TYPE
    try:
        result = await p115_qr.start_qr_session(resolved_device)
    except p115_qr.QRCapabilityError as exc:
        # 设备类型不支持扫码——明确告知（400 而非 502：不是上游故障）
        raise DriverAuthError(str(exc), status_code=400, kind="invalid") from exc
    except Exception as exc:
        raise DriverAuthError(f"启动扫码失败：{exc}", status_code=502) from exc
    return QRStartResult(
        session_id=result.session_id, qr_image_b64=result.qr_image_b64
    )


async def qr_status(driver_id: int, session_id: str) -> QRStatusResult:
    """查询扫码状态。

    ``confirmed`` 时**自动落盘凭据**（加密存储）。
    """
    from app.db.session import session_scope

    driver_type, _, _ = await _driver_type_and_credential(driver_id)

    if driver_type == "guangya":
        gyp_status = await guangya_auth.get_qr_status(session_id)
        if gyp_status is None:
            raise DriverAuthError("扫码会话不存在或已过期", status_code=404, kind="not_found")
        confirmed = gyp_status.kind == "confirmed"
        if confirmed and gyp_status.credential:
            cred = guangya_credential.GuangyaCredential(
                access_token=str(gyp_status.credential.get("access_token") or ""),
                refresh_token=str(gyp_status.credential.get("refresh_token") or ""),
                device_id=str(gyp_status.credential.get("device_id") or ""),
                expires_at=int(gyp_status.credential.get("expires_at") or 0),
            )
            async with session_scope() as db:
                await store_credential(
                    db, driver_id, cred.to_json(), scheme=guangya_credential.SCHEME
                )
        return QRStatusResult(
            kind=gyp_status.kind,
            error_kind=gyp_status.error_kind or "",
            error_msg=gyp_status.error_msg or "",
            confirmed=confirmed,
        )

    p115_status = await p115_qr.get_qr_status(session_id)
    if p115_status is None:
        raise DriverAuthError("扫码会话不存在或已过期", status_code=404, kind="not_found")

    confirmed = p115_status.kind == "confirmed"
    if confirmed and p115_status.cookie_str:
        async with session_scope() as db:
            await store_credential(db, driver_id, p115_status.cookie_str)

    return QRStatusResult(
        kind=p115_status.kind,
        error_kind=p115_status.error_kind or "",
        error_msg=p115_status.error_msg or "",
        confirmed=confirmed,
    )


# ================================================================ 短信登录（光鸭）


async def send_sms_code(driver_id: int, phone: str) -> SmsSendResult:
    """发送短信验证码（光鸭盘）。

    短信**立即发出**，返回后续步骤要用的 ``captcha_token``。
    """
    driver_type, _, _ = await _driver_type_and_credential(driver_id)
    if driver_type != "guangya":
        raise DriverAuthError("该存储源不支持短信登录", status_code=400, kind="invalid")

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
    from app.db.session import session_scope

    driver_type, _, _ = await _driver_type_and_credential(driver_id)
    if driver_type != "guangya":
        raise DriverAuthError("该存储源不支持短信登录", status_code=400, kind="invalid")

    try:
        cred = await guangya_auth.verify_sms_code(phone, code, captcha_token)
    except guangya_auth.GuangyaAuthError as exc:
        return SmsVerifyResult(ok=False, message=str(exc))

    obj = guangya_credential.GuangyaCredential(
        access_token=str(cred.get("access_token") or ""),
        refresh_token=str(cred.get("refresh_token") or ""),
        device_id=str(cred.get("device_id") or ""),
        expires_at=int(cred.get("expires_at") or 0),
    )
    async with session_scope() as db:
        await store_credential(
            db, driver_id, obj.to_json(), scheme=guangya_credential.SCHEME
        )
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
