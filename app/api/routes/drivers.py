"""存储源路由：列出 / 创建 / 删除 / 扫码登录。

**路由层只做参数校验与响应组装**——DB 与适配层的访问全在
:mod:`app.services.driver_service`（CRUD、扫描、探测、诊断）与
:mod:`app.services.driver_auth`（扫码 / 短信登录）。

分层约束（``api`` 不依赖 ``db`` / ``adapters``）由
``tests/unit/test_layering.py`` 固化。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.domain.device_types import DeviceTypeLiteral
from app.domain.driver_types import media_cid_label
from app.services import driver_auth
from app.services.driver_auth import DriverAuthError
from app.services.driver_service import (
    CachedProbe,
    DriverRow,
    DriverServiceError,
    add_driver,
    browse_driver_dirs,
    cached_probe,
    diagnose_driver_direct_link,
    list_device_types,
    list_drivers,
    probe_driver,
    remove_driver,
    set_incremental_baseline,
    start_incremental_task,
    start_scan_task,
    update_driver,
    update_driver_config,
)

router = APIRouter(
    prefix="/api/drivers", tags=["drivers"], dependencies=[Depends(require_session)]
)


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
    #: 扫描时是否**自动清理**「网盘上已消失」的条目（prune）。
    #:
    #: 默认关——清理会**永久删除**本地 strm（不进回收站），不可逆。
    auto_prune: bool = False


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


class DriverConnStatusOut(BaseModel):
    """存储源测连状态（来自进程内缓存，可能为 ``None`` = 尚未测连）。

    字段与 ``DriverStatus``（前端类型）一致，另有 ``checked_at``。
    """

    logged_in: bool
    status: str
    message: str = ""
    account_id: str = ""
    total_bytes: int = 0
    used_bytes: int = 0
    remain_bytes: int = 0
    can_get_link: bool = False
    #: 最近测连时间（北京时间字符串）
    checked_at: str | None = None


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
    #: 媒体库目录 id 的**展示名**（115 叫 cid、光鸭叫 fileId）。
    #: 后端按驱动类型给出——前端**不写** ``if type == "guangya"``。
    media_cid_label: str = ""
    output_dir: str | None = None
    video_exts: str = ""
    image_exts: str = ""
    data_exts: str = ""
    min_size_mb: int = 0
    include_subdirs: bool = True
    #: 扫描时是否自动清理「网盘上已消失」的条目（prune）
    auto_prune: bool = False
    last_scan_at: str | None = None
    last_synced_count: int = 0

    # -- 聚合（主从）配置 --
    #: 所属聚合组（None = 独立存储源）
    aggregate_group_id: int | None = None
    #: 组内角色（``master`` / ``secondary``；None = 不参与聚合）
    role: str | None = None

    # -- 测连状态（启动自动测连 / 手动测连的缓存；None = 未测连） --
    conn: DriverConnStatusOut | None = None


class QRStartOut(BaseModel):
    session_id: str
    qr_image_b64: str
    #: 本次登录模式（``web`` = Web 身份；``client`` = 客户端身份）
    mode: str = "web"


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
    #: 本次测连时间（北京时间字符串）
    checked_at: str | None = None


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


def _to_out(row: DriverRow, conn: CachedProbe | None = None) -> DriverOut:
    """service 层 dataclass → 输出模型。``conn`` = 测连缓存（可缺）。"""
    conn_out = None
    if conn:
        s = conn.status
        conn_out = DriverConnStatusOut(
            logged_in=s.logged_in,
            status=s.status,
            message=s.message,
            account_id=s.account_id,
            total_bytes=s.total_bytes,
            used_bytes=s.used_bytes,
            remain_bytes=s.remain_bytes,
            can_get_link=s.can_get_link,
            checked_at=conn.checked_at,
        )
    return DriverOut(
        id=row.id,
        type=row.type,
        name=row.name,
        device_type=row.device_type,
        enabled=row.enabled,
        credential_masked=row.credential_masked,
        media_cid=row.media_cid,
        media_path=row.media_path,
        media_cid_label=media_cid_label(row.type),
        output_dir=row.output_dir,
        video_exts=row.video_exts or "",
        image_exts=row.image_exts or "",
        data_exts=row.data_exts or "",
        min_size_mb=row.min_size_mb or 0,
        include_subdirs=bool(row.include_subdirs),
        auto_prune=bool(row.auto_prune),
        last_scan_at=row.last_scan_at,
        last_synced_count=row.last_synced_count or 0,
        aggregate_group_id=row.aggregate_group_id,
        role=row.role,
        conn=conn_out,
    )


@router.get("", response_model=list[DriverOut])
async def list_drivers_route() -> list[DriverOut]:
    """列出存储源（携带测连缓存——启动自动测连 / 手动测连的结果）。"""
    return [_to_out(r, cached_probe(r.id)) for r in await list_drivers()]


@router.post("", response_model=DriverOut, status_code=201)
async def add_driver_route(payload: DriverIn) -> DriverOut:
    """创建存储源。"""
    try:
        row = await add_driver(
            driver_type=payload.type,
            name=payload.name,
            device_type=payload.device_type,
        )
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_out(row)


@router.patch("/{driver_id}", response_model=DriverOut)
async def update_driver_route(driver_id: int, payload: DriverUpdate) -> DriverOut:
    """修改存储源。

    改 ``device_type`` 后，下次扫码登录会绑定新设备类型。
    """
    try:
        row = await update_driver(
            driver_id,
            name=payload.name,
            device_type=payload.device_type,
        )
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_out(row)


@router.put("/{driver_id}/config", response_model=DriverOut)
async def update_config_route(driver_id: int, payload: DriverConfigIn) -> DriverOut:
    """保存媒体库配置。

    ``media_cid`` 保存时会**实时解析出路径**填入 ``media_path``（只读展示）。
    cid 无效 → 400（**不做路径降级**——按设计，cid 失效就是配置错误）。
    """
    try:
        row = await update_driver_config(
            driver_id,
            media_cid=payload.media_cid,
            output_dir=payload.output_dir,
            video_exts=payload.video_exts,
            image_exts=payload.image_exts,
            data_exts=payload.data_exts,
            min_size_mb=payload.min_size_mb,
            include_subdirs=payload.include_subdirs,
            auto_prune=payload.auto_prune,
        )
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_out(row)


@router.get("/{driver_id}/browse-dirs")
async def browse_driver_dirs_route(driver_id: int, parent_id: str = "") -> dict[str, object]:
    """列出存储源某个目录下的**子目录**（媒体库配置页选目录用）。

    ``parent_id`` **留空 = 账号根目录**（按驱动类型解析：光鸭的根是空串、
    115 的根是 ``0``）——浏览起点只能是根，此时 ``media_cid`` 还没配。

    返回 ``{"driver_id", "parent_id", "dirs": [{"id", "name"}]}``，形状与
    ``GET /api/upload-tasks/targets`` 一致（前端同一个 ``DirBrowser`` 组件
    复用两处）。
    """
    try:
        dirs = await browse_driver_dirs(driver_id, parent_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": rid, "name": name} for rid, name in dirs],
    }


@router.post("/{driver_id}/incremental")
async def incremental_driver_route(driver_id: int) -> dict[str, object]:
    """启动增量同步（**立即返回 job_id**，后台跑）。

    与全量扫描的区别：只处理「上次同步之后」的变化（基于 115 生活事件流），
    不遍历目录树。

    **必须先跑过一次全量扫描**建立基线——没有基线就无法判断哪些是新增。

    进度用 ``GET /api/incremental-jobs/{job_id}`` 轮询。
    """
    try:
        job = await start_incremental_task(driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"job_id": job.job_id, "state": job.state}


@router.post("/{driver_id}/incremental/baseline")
async def set_incremental_baseline_route(driver_id: int) -> dict[str, object]:
    """把**今天**记为增量基线（**不扫任何文件**）。

    用途：已经跑过全量扫描、但当时没记游标时，用它手动补一个基线。

    语义：**从此刻起**的事件才算变化——之前的事件一律不处理。
    """
    try:
        cursor = await set_incremental_baseline(driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"cursor": cursor}


@router.post("/{driver_id}/scan")
async def scan_driver_route(
    driver_id: int, prune: bool | None = None
) -> dict[str, object]:
    """启动扫描任务（**立即返回 job_id**，扫描在后台跑）。

    为什么要异步：限速 1s/请求下扫一棵大树要几分钟，同步 HTTP 会让用户
    只看到转圈，且反代/网关的 60s 超时会把请求掐断。

    进度用 ``GET /api/scan-jobs/{job_id}`` 轮询。

    Args:
        prune: **不传** = 用存储源配置的 ``auto_prune``；
            ``true`` / ``false`` = 显式覆盖（手动单次强制清理 / 强制不清理）。
            清理会**永久删除**本地 strm（不进回收站），故配置默认为关。
    """
    try:
        job = await start_scan_task(driver_id, prune=prune)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"job_id": job.job_id, "state": job.state}


@router.delete("/{driver_id}")
async def remove_driver_route(driver_id: int) -> dict[str, bool]:
    """删除存储源。"""
    try:
        await remove_driver(driver_id)
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
        for d in await list_device_types()
    ]


@router.post("/{driver_id}/qr/start", response_model=QRStartOut)
async def start_qr(
    driver_id: int,
    mode: str = Query(
        "web",
        pattern="^(web|client)$",
        description="光鸭登录身份：client 用客户端 clientId（token 的 aud 即客户端）",
    ),
) -> QRStartOut:
    """启动扫码登录。

    **按存储源类型分派**：115 走 p115client 的扫码（绑设备类型），
    光鸭走 Device Code（``account.guangyapan.com``）。

    ``mode=client`` 时用客户端 clientId + ``offline`` scope 取码：
    签发出的 token ``aud = 客户端``（能取非会员大文件直链），且带
    ``refresh_token``——之后由驱动自己续期，不再依赖本机客户端。
    """
    try:
        result = await driver_auth.start_qr(
            driver_id, client_mode=(mode == "client")
        )
    except DriverAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return QRStartOut(
        session_id=result.session_id, qr_image_b64=result.qr_image_b64, mode=mode
    )


@router.get("/{driver_id}/qr/status", response_model=QRStatusOut)
async def qr_status(driver_id: int, session_id: str) -> QRStatusOut:
    """查询扫码状态。

    ``confirmed`` 时**自动落盘凭据**（加密存储）。
    """
    try:
        result = await driver_auth.qr_status(driver_id, session_id)
    except DriverAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return QRStatusOut(
        kind=result.kind,
        error_kind=result.error_kind,
        error_msg=result.error_msg,
        confirmed=result.confirmed,
    )


@router.post("/{driver_id}/login/sms/send", response_model=SmsSendOut)
async def send_sms_code(driver_id: int, payload: SmsSendIn) -> SmsSendOut:
    """发送短信验证码（光鸭盘）。

    短信**立即发出**，返回后续步骤要用的 ``captcha_token``。
    """
    try:
        result = await driver_auth.send_sms_code(driver_id, payload.phone)
    except DriverAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SmsSendOut(
        captcha_token=result.captcha_token, captcha_url=result.captcha_url
    )


@router.post("/{driver_id}/login/sms/verify", response_model=SmsVerifyOut)
async def verify_sms_code(driver_id: int, payload: SmsVerifyIn) -> SmsVerifyOut:
    """校验短信验证码并登录（光鸭盘）。"""
    try:
        result = await driver_auth.verify_sms_code(
            driver_id, payload.phone, payload.code, payload.captcha_token
        )
    except DriverAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SmsVerifyOut(ok=result.ok, message=result.message)


@router.get("/{driver_id}/probe", response_model=ProbeOut)
async def probe_driver_route(driver_id: int) -> ProbeOut:
    """探测存储源状态（测连）。

    检查三项：**凭据有效** → **账号信息 / 容量** → **能否换到直链**。

    只读——不修改任何状态。第三项最关键：前两项通过但换直链被 115 的
    账号安全验证挡住，是**常见情况**，故单独报 ``needs_verify``。

    未配置凭据时返回 200 + ``status=no_credential``（而不是 400）——
    「还没登录」是正常状态，不是错误。
    """
    try:
        status = await probe_driver(driver_id)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    # 探测时间从缓存取（probe_driver 刚写进去的）
    cached = cached_probe(driver_id)
    return ProbeOut(
        logged_in=status.logged_in,
        status=status.status,
        message=status.message,
        account_id=status.account_id,
        total_bytes=status.total_bytes,
        used_bytes=status.used_bytes,
        remain_bytes=status.remain_bytes,
        can_get_link=status.can_get_link,
        checked_at=cached.checked_at if cached else None,
    )


@router.get("/{driver_id}/diag/{remote_id}", response_model=DirectLinkDiagOut)
async def diag_direct_link(
    driver_id: int,
    remote_id: str,
    user_agent: str = "",
) -> DirectLinkDiagOut:
    """诊断直链参数（``f`` / ``t`` / ``c``）。

    **用途**：115 直链的 ``f`` 参数实际取值未实测确认（``docs/design.md`` §11）。
    ``f=1`` 决定缓存必须按 UA 分桶；``f=3`` 还要回吐 Cookie 头。

    直链 URL **脱敏后**返回（避免截图泄露可分享的链接）。
    """
    try:
        diag = await diagnose_driver_direct_link(driver_id, remote_id, user_agent)
    except DriverServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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
