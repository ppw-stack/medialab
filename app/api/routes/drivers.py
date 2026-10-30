"""存储源路由：列出 / 创建 / 删除 / 扫码登录。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers import guangya_auth, guangya_credential, p115_qr
from app.adapters.drivers.base import DriverError
from app.adapters.drivers.device_types import (
    ALL_DEVICE_TYPES,
    DEFAULT_DEVICE_TYPE,
    DeviceTypeLiteral,
)
from app.api.auth import require_session
from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import session_scope
from app.services.driver_service import (
    DriverServiceError,
    create_driver,
    delete_driver,
    get_driver,
    mask_credential,
    store_credential,
)

router = APIRouter(
    prefix="/api/drivers", tags=["drivers"], dependencies=[Depends(require_session)]
)


async def _db() -> AsyncIterator[AsyncSession]:
    """DB session 依赖。"""
    async with session_scope() as db:
        yield db


class DriverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    #: 115 设备类型。**留空则用默认值**（115android）。
    #: 用 Literal 枚举做校验——裸 str 会让拼错的值一路溜到 115 API 才炸。
    device_type: DeviceTypeLiteral | None = None


class DriverConfigIn(BaseModel):
    """媒体库配置（每个存储源一套）。

    字段对应参考图：115媒体库cid / 本地媒体库目录 / 三类后缀 / 最小视频。
    """

    model_config = ConfigDict(extra="forbid")

    #: 115 媒体库 cid（**字符串**）。用 cid 而非路径：目录改名后 id 不变。
    media_cid: str | None = Field(default=None, max_length=32)
    #: 本地媒体库目录（STRM 输出根）
    output_dir: str | None = Field(default=None, max_length=1024)
    #: 视频后缀（逗号分隔）——**扫描依据**
    video_exts: str | None = Field(default=None, max_length=1024)
    #: 媒体图片后缀（逗号分隔）——当前忽略
    image_exts: str | None = Field(default=None, max_length=1024)
    #: 媒体数据后缀（逗号分隔）——当前忽略
    data_exts: str | None = Field(default=None, max_length=1024)
    #: 最小视频体积（MB）——0 = 不过滤
    min_size_mb: int = Field(default=0, ge=0)
    #: 是否包含子目录
    include_subdirs: bool = True


class DeviceTypeOut(BaseModel):
    """设备类型清单项。"""

    value: str
    label: str
    #: 能否用于扫码登录（实测：18 个里只有 10 个能取到二维码）
    qr_capable: bool
    #: 补充说明
    note: str = ""


class DriverUpdate(BaseModel):
    """修改存储源的请求体（只传要改的字段）。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    device_type: DeviceTypeLiteral | None = None


class DriverOut(BaseModel):
    id: int
    type: str
    name: str
    device_type: str | None
    enabled: bool
    credential_masked: str

    # -- 媒体库配置 --
    media_cid: str | None = None
    media_path: str | None = None
    output_dir: str | None = None
    video_exts: str = ""
    image_exts: str = ""
    data_exts: str = ""
    min_size_mb: int = 0
    include_subdirs: bool = True
    last_scan_at: int | None = None
    last_synced_count: int = 0


class QRStartOut(BaseModel):
    session_id: str
    qr_image_b64: str


class QRStatusOut(BaseModel):
    kind: str
    error_kind: str | None = None
    error_msg: str | None = None
    confirmed: bool = False


class SmsSendIn(BaseModel):
    """发送短信验证码的请求体。"""

    model_config = ConfigDict(extra="forbid")

    #: 手机号（含国际区号，如 ``+86 13800138000``）
    phone: str = Field(..., min_length=5, max_length=32)


class SmsSendOut(BaseModel):
    """发送短信的结果。

    ``captcha_url`` 非空表示服务端要求先过人机验证。
    """

    #: 后续步骤要用的人机验证 token
    captcha_token: str
    #: 需要人机验证时给出 URL
    captcha_url: str = ""


class SmsVerifyIn(BaseModel):
    """校验短信验证码的请求体。"""

    model_config = ConfigDict(extra="forbid")

    phone: str = Field(..., min_length=5, max_length=32)
    code: str = Field(..., min_length=4, max_length=12)
    captcha_token: str = Field(..., min_length=1)


class SmsVerifyOut(BaseModel):
    """校验结果。"""

    ok: bool
    message: str = ""


@router.get("", response_model=list[DriverOut])
async def list_drivers(db: AsyncSession = Depends(_db)) -> list[DriverOut]:
    """列出存储源。"""
    from sqlalchemy import select

    from app.db.models import Driver

    rows = (await db.execute(select(Driver).order_by(Driver.id))).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=DriverOut, status_code=201)
async def add_driver(payload: DriverIn, db: AsyncSession = Depends(_db)) -> DriverOut:
    """创建存储源。"""
    try:
        row = await create_driver(
            db,
            driver_type=payload.type,
            name=payload.name,
            device_type=payload.device_type,
        )
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_out(row)


def _to_out(row: Driver) -> DriverOut:
    """ORM → 输出模型。"""
    return DriverOut(
        id=row.id,
        type=row.type,
        name=row.name,
        device_type=row.device_type,
        enabled=row.enabled,
        credential_masked=mask_credential(row.credential),
        media_cid=row.media_cid,
        media_path=row.media_path,
        output_dir=row.output_dir,
        video_exts=row.video_exts or "",
        image_exts=row.image_exts or "",
        data_exts=row.data_exts or "",
        min_size_mb=row.min_size_mb or 0,
        include_subdirs=bool(row.include_subdirs),
        last_scan_at=row.last_scan_at,
        last_synced_count=row.last_synced_count or 0,
    )


@router.patch("/{driver_id}", response_model=DriverOut)
async def update_driver(
    driver_id: int,
    payload: DriverUpdate,
    db: AsyncSession = Depends(_db),
) -> DriverOut:
    """修改存储源。

    改 ``device_type`` 后，下次扫码登录会绑定新设备类型。
    """
    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if payload.name is not None:
        row.name = payload.name
    if payload.device_type is not None:
        # device_type 是 115 专属——光鸭没有这个概念
        if row.type != "p115":
            raise HTTPException(
                status_code=400, detail="该存储源不支持设备类型设置"
            )
        row.device_type = payload.device_type
    row.updated_at = int(time.time())
    await db.commit()
    await db.refresh(row)

    return _to_out(row)


@router.put("/{driver_id}/config", response_model=DriverOut)
async def update_config(
    driver_id: int,
    payload: DriverConfigIn,
    db: AsyncSession = Depends(_db),
) -> DriverOut:
    """保存媒体库配置。

    ``media_cid`` 保存时会**实时解析出路径**填入 ``media_path``（只读展示）。
    cid 无效 → 400（**不做路径降级**——按设计，cid 失效就是配置错误）。
    """

    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    media_path = row.media_path
    if payload.media_cid:
        media_path = await _resolve_media_path(row, payload.media_cid)

    row.media_cid = payload.media_cid
    row.media_path = media_path
    row.output_dir = payload.output_dir
    row.video_exts = payload.video_exts or ""
    row.image_exts = payload.image_exts or ""
    row.data_exts = payload.data_exts or ""
    row.min_size_mb = payload.min_size_mb
    row.include_subdirs = payload.include_subdirs
    row.updated_at = int(time.time())
    await db.commit()
    await db.refresh(row)

    return _to_out(row)


async def _resolve_media_path(row: Driver, media_cid: str) -> str:
    """用 cid 解析出路径（只读展示用）。

    **光鸭没有这个能力**（接口只接受 fileId，没有反查）→ 直接返回空串，
    路径栏留空即可，**不算错误**。

    Raises:
        HTTPException: 115 的 cid 无效（400）。
    """
    if row.type == "guangya":
        # 光鸭的 fileId 无法反查路径——接口只接受 ID，不接受路径
        return ""

    from app.adapters.drivers.p115 import P115Driver
    from app.adapters.drivers.p115_credential import decode_credential

    try:
        credential = decode_credential(row.credential)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"凭据解密失败：{exc}") from exc
    if not credential:
        raise HTTPException(
            status_code=400, detail="尚未配置凭据，请先扫码登录"
        )

    driver = P115Driver(credential, row.device_type)
    try:
        return await driver.resolve_path(media_cid)
    except DriverError as exc:
        raise HTTPException(
            status_code=400, detail=f"cid 无效：{exc}"
        ) from exc


@router.post("/{driver_id}/incremental")
async def incremental_driver_route(
    driver_id: int,
    db: AsyncSession = Depends(_db),
) -> dict[str, object]:
    """启动增量同步（**立即返回 job_id**，后台跑）。

    与全量扫描的区别：只处理「上次同步之后」的变化（基于 115 生活事件流），
    不遍历目录树。

    **必须先跑过一次全量扫描**建立基线——没有基线就无法判断哪些是新增。

    进度用 ``GET /api/incremental-jobs/{job_id}`` 轮询。
    """
    from app.services import incremental_jobs

    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if row.type != "p115":
        raise HTTPException(
            status_code=400, detail="增量同步目前只支持 115 存储源"
        )

    running = await incremental_jobs.find_running(driver_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该存储源已有增量任务在跑（job={running.id}）",
        )

    job = await incremental_jobs.start_incremental_job(driver_id, row.name)
    return {"job_id": job.id, "state": job.state}


@router.post("/{driver_id}/incremental/baseline")
async def set_incremental_baseline_route(
    driver_id: int,
    db: AsyncSession = Depends(_db),
) -> dict[str, object]:
    """把**今天**记为增量基线（**不扫任何文件**）。

    用途：已经跑过全量扫描、但当时没记游标时，用它手动补一个基线。

    语义：**从此刻起**的事件才算变化——之前的事件一律不处理。
    """
    from app.adapters.drivers import p115_events
    from app.services import settings_store
    from app.services.driver_factory import build_driver

    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if row.type != "p115":
        raise HTTPException(
            status_code=400, detail="增量同步目前只支持 115 存储源"
        )

    driver_impl = build_driver(row.type, row.credential, row.device_type)
    client = getattr(driver_impl, "_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="115 客户端不可用")

    today = p115_events.today_str()
    await settings_store.set_value(
        db, settings_store.cursor_key(driver_id), today
    )
    await db.commit()
    return {"cursor": today}



@router.post("/{driver_id}/scan")
async def scan_driver_route(
    driver_id: int,
    db: AsyncSession = Depends(_db),
    prune: bool = False,
) -> dict[str, object]:
    """启动扫描任务（**立即返回 job_id**，扫描在后台跑）。

    为什么要异步：限速 1s/请求下扫一棵大树要几分钟，同步 HTTP 会让用户
    只看到转圈，且反代/网关的 60s 超时会把请求掐断。

    进度用 ``GET /api/scan-jobs/{job_id}`` 轮询。

    Args:
        prune: 是否删除源端已消失条目的 STRM 文件。
    """
    from app.services.scan_jobs import find_running
    from app.services.scan_runner import start_scan_job

    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # 同一存储源不允许并发扫描（限速下并发只会互相拖慢，且会打爆 115）
    running = await find_running(driver_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该存储源已有扫描任务在跑（job={running.id}）",
        )

    job = start_scan_job(driver_id, row.name, prune=prune)
    return {"job_id": job.id, "state": job.state}


@router.delete("/{driver_id}")
async def remove_driver(driver_id: int, db: AsyncSession = Depends(_db)) -> dict[str, bool]:
    """删除存储源。"""
    try:
        await delete_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/device-types", response_model=list[DeviceTypeOut])
async def device_types() -> list[DeviceTypeOut]:
    """115 设备类型清单（**单一真源**）。

    前端下拉**从这里取**，不手写——本项目踩过「前端手写 4 个、后端
    另一份、扫码时压根没传」的坑。

    ``qr_capable=false`` 的类型**不能用于扫码**（实测：18 个里只有 10 个
    能取到二维码，其余 8 个返回 ``state=1`` 却无 ``qrcode`` 字段）。
    """
    return [
        DeviceTypeOut(
            value=d.value,
            label=d.label,
            qr_capable=d.qr_capable,
            note=d.note,
        )
        for d in ALL_DEVICE_TYPES
    ]


@router.post("/{driver_id}/qr/start", response_model=QRStartOut)
async def start_qr(driver_id: int, db: AsyncSession = Depends(_db)) -> QRStartOut:
    """启动扫码登录。

    **按存储源类型分派**：115 走 p115client 的扫码（绑设备类型），
    光鸭走 Device Code（``account.guangyapan.com``）。
    """
    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if row.type == "guangya":
        try:
            gyp_result = await guangya_auth.start_qr_session()
        except guangya_auth.GuangyaAuthError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"启动扫码失败：{exc}") from exc
        return QRStartOut(
            session_id=gyp_result.session_id, qr_image_b64=gyp_result.qr_image_b64
        )

    if row.type != "p115":
        raise HTTPException(status_code=400, detail="该存储源不支持扫码登录")

    device_type = row.device_type or DEFAULT_DEVICE_TYPE
    try:
        result = await p115_qr.start_qr_session(device_type)
    except p115_qr.QRCapabilityError as exc:
        # 设备类型不支持扫码——明确告知（400 而非 502：不是上游故障）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"启动扫码失败：{exc}") from exc
    return QRStartOut(session_id=result.session_id, qr_image_b64=result.qr_image_b64)


@router.get("/{driver_id}/qr/status", response_model=QRStatusOut)
async def qr_status(
    driver_id: int,
    session_id: str,
    db: AsyncSession = Depends(_db),
) -> QRStatusOut:
    """查询扫码状态。

    ``confirmed`` 时**自动落盘凭据**（加密存储）。
    """
    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if row.type == "guangya":
        gyp_status = await guangya_auth.get_qr_status(session_id)
        if gyp_status is None:
            raise HTTPException(status_code=404, detail="扫码会话不存在或已过期")
        confirmed = gyp_status.kind == "confirmed"
        if confirmed and gyp_status.credential:
            cred = guangya_credential.GuangyaCredential(
                access_token=str(gyp_status.credential.get("access_token") or ""),
                refresh_token=str(gyp_status.credential.get("refresh_token") or ""),
                device_id=str(gyp_status.credential.get("device_id") or ""),
                expires_at=int(gyp_status.credential.get("expires_at") or 0),
            )
            try:
                await store_credential(
                    db,
                    driver_id,
                    cred.to_json(),
                    scheme=guangya_credential.SCHEME,
                )
            except DriverServiceError as exc:
                raise HTTPException(
                    status_code=exc.status_code, detail=str(exc)
                ) from exc
        return QRStatusOut(
            kind=gyp_status.kind,
            error_kind=gyp_status.error_kind,
            error_msg=gyp_status.error_msg,
            confirmed=confirmed,
        )

    p115_status = await p115_qr.get_qr_status(session_id)
    if p115_status is None:
        raise HTTPException(status_code=404, detail="扫码会话不存在或已过期")

    confirmed = p115_status.kind == "confirmed"
    if confirmed and p115_status.cookie_str:
        try:
            await store_credential(db, driver_id, p115_status.cookie_str)
        except DriverServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return QRStatusOut(
        kind=p115_status.kind,
        error_kind=p115_status.error_kind,
        error_msg=p115_status.error_msg,
        confirmed=confirmed,
    )


@router.post("/{driver_id}/login/sms/send", response_model=SmsSendOut)
async def send_sms_code(
    driver_id: int,
    payload: SmsSendIn,
    db: AsyncSession = Depends(_db),
) -> SmsSendOut:
    """发送短信验证码（光鸭盘）。

    短信**立即发出**，返回后续步骤要用的 ``captcha_token``。
    """
    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if row.type != "guangya":
        raise HTTPException(status_code=400, detail="该存储源不支持短信登录")

    try:
        result = await guangya_auth.send_sms_code(payload.phone)
    except guangya_auth.GuangyaAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SmsSendOut(
        captcha_token=result.captcha_token, captcha_url=result.captcha_url
    )


@router.post("/{driver_id}/login/sms/verify", response_model=SmsVerifyOut)
async def verify_sms_code(
    driver_id: int,
    payload: SmsVerifyIn,
    db: AsyncSession = Depends(_db),
) -> SmsVerifyOut:
    """校验短信验证码并登录（光鸭盘）。"""
    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if row.type != "guangya":
        raise HTTPException(status_code=400, detail="该存储源不支持短信登录")

    try:
        cred = await guangya_auth.verify_sms_code(
            payload.phone, payload.code, payload.captcha_token
        )
    except guangya_auth.GuangyaAuthError as exc:
        return SmsVerifyOut(ok=False, message=str(exc))

    obj = guangya_credential.GuangyaCredential(
        access_token=str(cred.get("access_token") or ""),
        refresh_token=str(cred.get("refresh_token") or ""),
        device_id=str(cred.get("device_id") or ""),
        expires_at=int(cred.get("expires_at") or 0),
    )
    try:
        await store_credential(
            db, driver_id, obj.to_json(), scheme=guangya_credential.SCHEME
        )
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SmsVerifyOut(ok=True, message="登录成功，凭据已加密存储")


class ProbeOut(BaseModel):
    """存储源探测结果（测连）。

    状态取值：

    - ``ok`` —— 一切正常
    - ``needs_verify`` —— 登录态正常但换直链被 115 账号安全验证挡住
      （需在手机 APP 里验证，**重新扫码无效**）
    - ``no_credential`` —— 尚未扫码登录
    - ``error`` —— 其它问题
    """
    logged_in: bool
    #: 状态
    status: str = "error"
    #: 可读说明（前端直接显示）
    message: str = ""
    #: 账号 id（UID）
    account_id: str = ""
    #: 总容量（字节）
    total_bytes: int = 0
    #: 已用容量（字节）
    used_bytes: int = 0
    #: 剩余容量（字节）
    remain_bytes: int = 0
    #: 能否换到直链（**最关键的一项**）
    can_get_link: bool = False


class DirectLinkDiagOut(BaseModel):
    """直链参数诊断。"""

    url_masked: str
    expires_at: int | None = None
    ttl_seconds: int | None = None
    flag: str | None = None
    concurrent: str | None = None
    user_id: str | None = None
    ua_required: bool = False
    cookie_required: bool = False


@router.get("/{driver_id}/probe", response_model=ProbeOut)
async def probe_driver(driver_id: int, db: AsyncSession = Depends(_db)) -> ProbeOut:
    """探测存储源状态（测连）。

    检查三项：**凭据有效** → **账号信息 / 容量** → **能否换到直链**。

    只读——不修改任何状态。第三项最关键：前两项通过但换直链被 115 的
    账号安全验证挡住，是**常见情况**，故单独报 ``needs_verify``。

    未配置凭据时返回 200 + ``status=no_credential``（而不是 400）——
    「还没登录」是正常状态，不是错误。
    """
    from app.services.account_status import STATUS_NO_CREDENTIAL, probe_account
    from app.services.driver_factory import build_driver

    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if row.type not in ("p115", "guangya"):
        raise HTTPException(status_code=400, detail="该存储源暂不支持探测")

    if not row.credential:
        return ProbeOut(
            logged_in=False,
            status=STATUS_NO_CREDENTIAL,
            message="尚未配置凭据，请先扫码登录",
        )

    try:
        driver = build_driver(row.type, row.credential, row.device_type)
    except DriverError as exc:
        raise HTTPException(status_code=500, detail=f"驱动初始化失败：{exc}") from exc

    status = await probe_account(driver)
    return ProbeOut(
        logged_in=status.logged_in,
        status=status.status,
        message=status.message,
        account_id=status.account_id,
        total_bytes=status.total_bytes,
        used_bytes=status.used_bytes,
        remain_bytes=status.remain_bytes,
        can_get_link=status.can_get_link,
    )


@router.get("/{driver_id}/diag/{remote_id}", response_model=DirectLinkDiagOut)
async def diag_direct_link(
    driver_id: int,
    remote_id: str,
    db: AsyncSession = Depends(_db),
    user_agent: str = "",
) -> DirectLinkDiagOut:
    """诊断直链参数（``f`` / ``t`` / ``c``）。

    **用途**：115 直链的 ``f`` 参数实际取值未实测确认（``docs/design.md`` §11）。
    ``f=1`` 决定缓存必须按 UA 分桶；``f=3`` 还要回吐 Cookie 头。

    直链 URL **脱敏后**返回（避免截图泄露可分享的链接）。
    """
    from app.adapters.drivers.p115 import P115Driver
    from app.adapters.drivers.p115_credential import decode_credential
    from app.services.diag import diagnose_direct_link

    try:
        row = await get_driver(db, driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if row.type != "p115":
        raise HTTPException(status_code=400, detail="该存储源暂不支持直链诊断")

    try:
        credential = decode_credential(row.credential)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"凭据解密失败：{exc}") from exc
    if not credential:
        raise HTTPException(status_code=400, detail="尚未配置凭据，请先扫码登录")

    driver = P115Driver(credential, row.device_type)
    ua = user_agent or get_settings().default_user_agent
    try:
        diag = await diagnose_direct_link(driver, remote_id, ua)
    except DriverError as exc:
        # 账号需安全验证 → 403（不是 502：那不是上游故障，是账号状态问题）
        if exc.kind == "auth_verify":
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return DirectLinkDiagOut(
        url_masked=diag.url_masked,
        expires_at=diag.expires_at,
        ttl_seconds=diag.ttl_seconds,
        flag=diag.flag,
        concurrent=diag.concurrent,
        user_id=diag.user_id,
        ua_required=diag.ua_required,
        cookie_required=diag.cookie_required,
    )
