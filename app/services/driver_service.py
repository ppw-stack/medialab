"""存储源管理服务。

负责：列出 / 创建 / 修改 / 删除存储源、媒体库配置、登录态探测、直链诊断。

**扫码 / 短信登录**在 :mod:`app.services.driver_auth`——登录流程的共同点是
「拿凭据 → 加密落盘」，与本模块的 CRUD 职责不同。

**API 层不感知 ORM**：对外一律返回 dataclass（:class:`DriverRow` 等），
路由层只做参数校验与响应组装（分层约束见
``tests/unit/test_layering.py``）。

**事务边界在 service 层**：带 ``db`` 参数的是**内部**函数（供其它 service
复用，如 :mod:`app.services.redirect` 落盘刷新的凭据）；``list_drivers`` /
``add_driver`` 等**对外**函数自己开事务——API 层不允许出现
``session_scope``。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers.base import DriverError
from app.adapters.drivers.credential_crypto import (
    CredentialCryptoError,
    encrypt,
    encrypt_with_scheme,
)
from app.adapters.drivers.p115 import P115Driver, reset_clients
from app.adapters.drivers.p115_credential import SECRET_ENV, decode_credential
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Driver, ProbeResult
from app.db.session import session_scope
from app.domain.device_types import (
    ALL_DEVICE_TYPES,
    DEFAULT_DEVICE_TYPE,
    DeviceTypeLiteral,
)
from app.services.account_status import (
    STATUS_NO_CREDENTIAL,
    AccountStatus,
    probe_account,
)
from app.services.diag import DirectLinkDiag, diagnose_direct_link
from app.services.driver_factory import build_driver

logger = get_logger(__name__)


class DriverServiceError(Exception):
    """存储源操作失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


# ================================================================ 数据载体
#
# API 层不认识 ORM——只认下面这几个 dataclass。字段与 ``drivers`` 表一一
# 对应，凭据一律以**掩码**形式出去（不回传明文）。


@dataclass(slots=True)
class DriverRow:
    """一条存储源（供 API 组装响应）。"""

    id: int
    #: 驱动类型标识（``p115`` / ``guangya`` …）
    type: str
    name: str
    #: 115 设备类型（扫码登录绑定的 app）；非 115 存储源为 None
    device_type: str | None
    enabled: bool
    #: 凭据掩码（**永不回传明文**）
    credential_masked: str
    # -- 媒体库配置 --
    #: 115 媒体库 cid
    media_cid: str | None
    #: 媒体库路径（**只读展示**，保存时用 cid 实时解析）
    media_path: str | None
    #: 本地媒体库目录（STRM 输出根）
    output_dir: str | None
    #: 视频后缀（逗号分隔）——扫描依据
    video_exts: str
    #: 媒体图片后缀（逗号分隔）
    image_exts: str
    #: 媒体数据后缀（逗号分隔）
    data_exts: str
    #: 最小视频体积（MB）——0 = 不过滤
    min_size_mb: int
    #: 是否包含子目录
    include_subdirs: bool
    #: 上次扫描时间（unix 秒）
    last_scan_at: int | None
    #: 上次扫描新增 / 更新的条目数
    last_synced_count: int


@dataclass(slots=True)
class DeviceTypeInfo:
    """一个 115 设备类型（供 API 组装响应）。"""

    value: str
    label: str
    #: 能否用于扫码登录（实测：18 个里只有 10 个能取到二维码）
    qr_capable: bool
    note: str = ""


@dataclass(slots=True)
class JobRef:
    """后台任务的引用（立即返回，前端拿 ``job_id`` 轮询）。"""

    job_id: str
    state: str


def to_driver_row(row: Driver) -> DriverRow:
    """ORM 行 → :class:`DriverRow`（给 API 层用）。"""
    return DriverRow(
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


def _encryption_secret() -> str:
    """取凭据加密密钥。

    Raises:
        DriverServiceError: 未配置密钥（此时不能存密文）。
    """
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise DriverServiceError(
            f"未配置 {SECRET_ENV}，无法加密存储凭据",
            status_code=500,
            kind="config",
        )
    return secret


def mask_credential(credential: str | None) -> str:
    """凭据掩码（前端展示用，不回传明文）。"""
    if not credential:
        return ""
    if credential.startswith(("p115cipher:", "gypcipher:")):
        return "encrypted"
    return f"set({len(credential)} chars)"


# ================================================================ 内部函数
#
# 带 ``db`` 参数——供其它 service 复用（它们自己管事务）。


async def get_driver(db: AsyncSession, driver_id: int) -> Driver:
    """取存储源。"""
    row = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).scalar_one_or_none()
    if row is None:
        raise DriverServiceError(
            f"存储源不存在：{driver_id}", status_code=404, kind="not_found"
        )
    return row


async def create_driver(
    db: AsyncSession,
    *,
    driver_type: str,
    name: str,
    device_type: str | None = None,
) -> Driver:
    """创建存储源（尚无凭据）。

    ``device_type`` 是 **115 专属**的概念（扫码登录绑定的 app）。
    其它驱动（光鸭）没有这个概念 → 存 None，否则界面上会出现
    「光鸭盘的设备类型是 115android」这种 nonsensical 的值。
    """
    now = int(time.time())
    resolved_device = device_type if driver_type == "p115" else None
    row = Driver(
        type=driver_type,
        name=name,
        credential=None,
        device_type=resolved_device or (DEFAULT_DEVICE_TYPE if driver_type == "p115" else None),
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info("创建存储源 id=%s type=%s", row.id, row.type)
    return row


async def store_credential(
    db: AsyncSession,
    driver_id: int,
    credential: str,
    device_type: str | None = None,
    *,
    scheme: str | None = None,
) -> Driver:
    """加密存储凭据。

    Args:
        db: DB session。
        driver_id: 存储源 id。
        credential: 明文凭据（115 是 cookie 字符串；光鸭是 JSON）。
        device_type: 115 设备类型（可同时写入）。
        scheme: 加密方案前缀。``None`` = 用默认的 115 方案；光鸭传
            ``"gypcipher:v2:"``。

    Raises:
        DriverServiceError: 加密失败 / 存储源不存在。
    """
    row = await get_driver(db, driver_id)
    secret = _encryption_secret()
    try:
        if scheme is None:
            encrypted = encrypt(credential, secret)
        else:
            encrypted = encrypt_with_scheme(credential, secret, scheme)
    except CredentialCryptoError as exc:
        raise DriverServiceError(
            f"凭据加密失败：{exc}", status_code=500, kind="config"
        ) from exc

    row.credential = encrypted
    if device_type:
        row.device_type = device_type
    row.updated_at = int(time.time())
    await db.commit()
    await db.refresh(row)
    # 凭据变了 → 缓存的 115 客户端实例失效
    reset_clients()
    logger.info("已存储凭据 driver_id=%s", driver_id)
    return row


async def delete_driver(db: AsyncSession, driver_id: int) -> None:
    """删除存储源（级联删关联数据）。"""
    row = await get_driver(db, driver_id)
    await db.delete(row)
    await db.commit()
    reset_clients()


async def record_probe(
    db: AsyncSession,
    driver_id: int,
    *,
    account_id: str,
    used_gb: float,
    total_gb: float,
    available: bool,
) -> ProbeResult:
    """记录一次可用性探测结果。"""
    row = ProbeResult(
        driver_id=driver_id,
        account_id=account_id,
        used_gb=used_gb,
        total_gb=total_gb,
        available=available,
        probed_at=int(time.time()),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def latest_probe(db: AsyncSession, driver_id: int) -> ProbeResult | None:
    """取最近一次探测结果。"""
    result = await db.execute(
        select(ProbeResult)
        .where(ProbeResult.driver_id == driver_id)
        .order_by(ProbeResult.probed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ================================================================ 对外函数
#
# 以下是 **API 层与 DB / 适配层之间的唯一通道**——每个都**自开事务**。


async def list_drivers() -> list[DriverRow]:
    """列出存储源（按 id 升序）。"""
    async with session_scope() as db:
        rows = (await db.execute(select(Driver).order_by(Driver.id))).scalars().all()
        return [to_driver_row(r) for r in rows]


async def add_driver(
    *,
    driver_type: str,
    name: str,
    device_type: str | None = None,
) -> DriverRow:
    """创建存储源（尚无凭据）。"""
    async with session_scope() as db:
        row = await create_driver(
            db,
            driver_type=driver_type,
            name=name,
            device_type=device_type,
        )
        return to_driver_row(row)


async def update_driver(
    driver_id: int,
    *,
    name: str | None = None,
    device_type: str | None = None,
) -> DriverRow:
    """修改存储源（名称 / 设备类型）。

    改 ``device_type`` 后，下次扫码登录会绑定新设备类型。
    """
    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        if name is not None:
            row.name = name
        if device_type is not None:
            # device_type 是 115 专属——光鸭没有这个概念
            if row.type != "p115":
                raise DriverServiceError(
                    "该存储源不支持设备类型设置", status_code=400
                )
            row.device_type = device_type
        row.updated_at = int(time.time())
        await db.commit()
        await db.refresh(row)
        return to_driver_row(row)


async def update_driver_config(
    driver_id: int,
    *,
    media_cid: str | None = None,
    output_dir: str | None = None,
    video_exts: str | None = None,
    image_exts: str | None = None,
    data_exts: str | None = None,
    min_size_mb: int = 0,
    include_subdirs: bool = True,
) -> DriverRow:
    """保存媒体库配置。

    ``media_cid`` 保存时会**实时解析出路径**填入 ``media_path``（只读展示）。
    cid 无效 → 400（**不做路径降级**——按设计，cid 失效就是配置错误）。
    """
    async with session_scope() as db:
        row = await get_driver(db, driver_id)

        media_path = row.media_path
        if media_cid:
            media_path = await resolve_media_path(row, media_cid)

        row.media_cid = media_cid
        row.media_path = media_path
        row.output_dir = output_dir
        row.video_exts = video_exts or ""
        row.image_exts = image_exts or ""
        row.data_exts = data_exts or ""
        row.min_size_mb = min_size_mb
        row.include_subdirs = include_subdirs
        row.updated_at = int(time.time())
        await db.commit()
        await db.refresh(row)
        return to_driver_row(row)


async def remove_driver(driver_id: int) -> None:
    """删除存储源（级联删关联数据）。"""
    async with session_scope() as db:
        await delete_driver(db, driver_id)


async def list_device_types() -> list[DeviceTypeInfo]:
    """115 设备类型清单（**单一真源**）。

    前端下拉**从这里取**，不手写——本项目踩过「前端手写 4 个、后端
    另一份、扫码时压根没传」的坑。

    ``qr_capable=false`` 的类型**不能用于扫码**（实测：18 个里只有 10 个
    能取到二维码，其余 8 个返回 ``state=1`` 却无 ``qrcode`` 字段）。
    """
    return [
        DeviceTypeInfo(
            value=d.value,
            label=d.label,
            qr_capable=d.qr_capable,
            note=d.note,
        )
        for d in ALL_DEVICE_TYPES
    ]


async def probe_driver(driver_id: int) -> AccountStatus:
    """探测存储源状态（测连）。

    检查三项：**凭据有效** → **账号信息 / 容量** → **能否换到直链**。

    只读——不修改任何状态。第三项最关键：前两项通过但换直链被 115 的
    账号安全验证挡住，是**常见情况**，故单独报 ``needs_verify``。

    未配置凭据时返回 ``status=no_credential``（而不是 400）——
    「还没登录」是正常状态，不是错误。
    """
    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_type = row.type
        credential = row.credential
        device_type = row.device_type

    if driver_type not in ("p115", "guangya"):
        raise DriverServiceError("该存储源暂不支持探测", status_code=400)

    if not credential:
        return AccountStatus(
            logged_in=False,
            status=STATUS_NO_CREDENTIAL,
            message="尚未配置凭据，请先扫码登录",
        )

    try:
        driver = build_driver(driver_type, credential, device_type)
    except DriverError as exc:
        raise DriverServiceError(
            f"驱动初始化失败：{exc}", status_code=500, kind="config"
        ) from exc

    return await probe_account(driver)


async def diagnose_driver_direct_link(
    driver_id: int,
    remote_id: str,
    user_agent: str = "",
) -> DirectLinkDiag:
    """诊断直链参数（``f`` / ``t`` / ``c``）。

    **用途**：115 直链的 ``f`` 参数实际取值未实测确认（``docs/design.md`` §11）。
    ``f=1`` 决定缓存必须按 UA 分桶；``f=3`` 还要回吐 Cookie 头。

    直链 URL **脱敏后**返回（避免截图泄露可分享的链接）。
    """
    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_type = row.type
        credential_raw = row.credential
        device_type = row.device_type

    if driver_type != "p115":
        raise DriverServiceError("该存储源暂不支持直链诊断", status_code=400)

    credential = _decode_p115_credential(credential_raw)
    driver = P115Driver(credential, device_type)
    ua = user_agent or get_settings().default_user_agent
    try:
        return await diagnose_direct_link(driver, remote_id, ua)
    except DriverError as exc:
        # 账号需安全验证 → 403（不是 502：那不是上游故障，是账号状态问题）
        if exc.kind == "auth_verify":
            raise DriverServiceError(str(exc), status_code=403, kind="auth") from exc
        raise DriverServiceError(str(exc), status_code=502, kind="upstream") from exc


async def start_scan_task(driver_id: int, *, prune: bool = False) -> JobRef:
    """启动扫描任务（**立即返回 job_id**，扫描在后台跑）。

    为什么要异步：限速 1s/请求下扫一棵大树要几分钟，同步 HTTP 会让用户
    只看到转圈，且反代 / 网关的 60s 超时会把请求掐断。

    进度用 ``GET /api/scan-jobs/{job_id}`` 轮询。
    """
    from app.services.scan_jobs import find_running
    from app.services.scan_runner import start_scan_job

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_name = row.name

    # 同一存储源不允许并发扫描（限速下并发只会互相拖慢，且会打爆 115）
    running = await find_running(driver_id)
    if running is not None:
        raise DriverServiceError(
            f"该存储源已有扫描任务在跑（job={running.id}）",
            status_code=409,
            kind="conflict",
        )

    job = start_scan_job(driver_id, driver_name, prune=prune)
    return JobRef(job_id=job.id, state=job.state)


async def start_incremental_task(driver_id: int) -> JobRef:
    """启动增量同步（**立即返回 job_id**，后台跑）。

    与全量扫描的区别：只处理「上次同步之后」的变化（基于 115 生活事件流），
    不遍历目录树。

    **必须先跑过一次全量扫描**建立基线——没有基线就无法判断哪些是新增。

    进度用 ``GET /api/incremental-jobs/{job_id}`` 轮询。
    """
    from app.services.incremental_jobs import find_running, start_incremental_job

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_type = row.type
        driver_name = row.name

    if driver_type != "p115":
        raise DriverServiceError("增量同步目前只支持 115 存储源", status_code=400)

    running = await find_running(driver_id)
    if running is not None:
        raise DriverServiceError(
            f"该存储源已有增量任务在跑（job={running.id}）",
            status_code=409,
            kind="conflict",
        )

    job = await start_incremental_job(driver_id, driver_name)
    return JobRef(job_id=job.id, state=job.state)


async def set_incremental_baseline(driver_id: int) -> str:
    """把**今天**记为增量基线（**不扫任何文件**）。

    用途：已经跑过全量扫描、但当时没记游标时，用它手动补一个基线。

    语义：**从此刻起**的事件才算变化——之前的事件一律不处理。
    """
    from app.adapters.drivers import p115_events
    from app.services import settings_store

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_type = row.type
        credential = row.credential
        device_type = row.device_type

        if driver_type != "p115":
            raise DriverServiceError("增量同步目前只支持 115 存储源", status_code=400)

        driver_impl = build_driver(driver_type, credential, device_type)
        client = getattr(driver_impl, "_client", None)
        if client is None:
            raise DriverServiceError("115 客户端不可用", status_code=503, kind="config")

        today = p115_events.today_str()
        await settings_store.set_value(
            db, settings_store.cursor_key(driver_id), today
        )
        await db.commit()
        return today


# ================================================================ 内部辅助


def _decode_p115_credential(credential: str | None) -> str:
    """解密 115 凭据。

    Raises:
        DriverServiceError: 解密失败（500）/ 尚未配置凭据（400）。
    """
    try:
        plain = decode_credential(credential)
    except Exception as exc:
        raise DriverServiceError(
            f"凭据解密失败：{exc}", status_code=500, kind="config"
        ) from exc
    if not plain:
        raise DriverServiceError(
            "尚未配置凭据，请先扫码登录", status_code=400, kind="config"
        )
    return plain


async def resolve_media_path(row: Driver, media_cid: str) -> str:
    """用 cid 解析出路径（只读展示用）。

    **光鸭没有这个能力**（接口只接受 fileId，没有反查）→ 直接返回空串，
    路径栏留空即可，**不算错误**。

    Raises:
        DriverServiceError: 115 的 cid 无效（400）/ 凭据不可用。
    """
    if row.type == "guangya":
        # 光鸭的 fileId 无法反查路径——接口只接受 ID，不接受路径
        return ""

    credential = _decode_p115_credential(row.credential)
    driver = P115Driver(credential, row.device_type)
    try:
        return await driver.resolve_path(media_cid)
    except DriverError as exc:
        raise DriverServiceError(f"cid 无效：{exc}", status_code=400) from exc


__all__ = [
    "DEFAULT_DEVICE_TYPE",
    "DeviceTypeInfo",
    "DeviceTypeLiteral",
    "DriverRow",
    "DriverServiceError",
    "JobRef",
    "add_driver",
    "create_driver",
    "delete_driver",
    "diagnose_driver_direct_link",
    "get_driver",
    "latest_probe",
    "list_device_types",
    "list_drivers",
    "mask_credential",
    "probe_driver",
    "record_probe",
    "remove_driver",
    "resolve_media_path",
    "set_incremental_baseline",
    "start_incremental_task",
    "start_scan_task",
    "store_credential",
    "to_driver_row",
    "update_driver",
    "update_driver_config",
]
