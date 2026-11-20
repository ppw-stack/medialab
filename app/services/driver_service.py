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

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
from app.core.timeutils import bj_now
from app.db.models import Driver, ProbeResult
from app.db.session import session_scope
from app.domain.device_types import (
    ALL_DEVICE_TYPES,
    DEFAULT_DEVICE_TYPE,
    DeviceTypeLiteral,
)
from app.domain.driver_types import (
    PROBEABLE_DRIVER_TYPES,
    is_probeable,
    root_dir_id,
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
    #: 扫描时是否自动清理「网盘上已消失」的条目（prune）
    auto_prune: bool
    #: 上次扫描时间（北京时间字符串，NULL = 从未扫描）
    last_scan_at: str | None
    #: 上次扫描新增 / 更新的条目数
    last_synced_count: int
    # -- 聚合（主从）配置 --
    #: 所属聚合组（None = 独立存储源）
    aggregate_group_id: int | None
    #: 组内角色（``master`` / ``secondary``；None = 不参与聚合）
    role: str | None


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
        auto_prune=bool(row.auto_prune),
        last_scan_at=row.last_scan_at,
        last_synced_count=row.last_synced_count or 0,
        aggregate_group_id=row.aggregate_group_id,
        role=row.role,
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


#: 「未传该参数」的哨兵——区分「不传」与「传空串」。
#:
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
    now = bj_now()
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


def _make_token_refresh_persist(driver_id: int) -> Callable[[Any], Any]:
    """造一个「驱动 Token 刷新后写回 DB」的回调（给 ``build_driver``）。

    **为什么必须有**：光鸭的 ``access_token`` 只有 1~2 小时有效期。
    驱动只在内存里更新它——不落库的话，进程重启后又用旧 token，
    服务端返回 117 / 401，刷新也救不回来（``refresh_token`` 可能已失效）。

    **探测路径同样需要**：``probe_driver`` 构造光鸭驱动时会触发
    ``_ensure_token()`` 刷新。若不传回调，新 token 只存在于驱动实例内存，
    探测结束实例即丢弃 → **DB 里仍是旧 token** → 刷新接口一旦轮换
    ``refresh_token``，账号就彻底掉线，必须重新扫码。

    写回失败**不影响本次探测**（内存里的新 token 仍然有效），只记日志。
    """
    from app.adapters.drivers.guangya_credential import GuangyaCredential

    async def _persist(cred: Any) -> None:
        if not isinstance(cred, GuangyaCredential):
            return
        await persist_guangya_credential(driver_id, cred)

    return _persist


async def persist_guangya_credential(driver_id: int, cred: Any) -> None:
    """光鸭 Token 刷新后写回 DB（**合并**两层）。

    **为什么必须合并，不能整串替换**：光鸭凭据 = 「Web 层 + 客户端覆盖层」。
    刷新写回时，驱动实例手里可能只有一层（老实例 / Web 身份的实例）——
    整串替换会把另一层**抹掉**（2026-11-20 生产事故：driver 5 的客户端
    token 被一个老实例的刷新写回抹掉 → 大文件直链能力丢失）。

    合并规则与扫码落盘
    （:func:`app.services.driver_auth._store_guangya_credential`）一致：
    - 实例带**客户端层** → 更新客户端层，主 token 跟着一起更新
    - 实例只有 **Web 层** → 只更新 Web 层，**保留 DB 里已有的客户端层**

    **异常口径**：写回失败（DB 错误等）在此被捕获并记日志，**不向上抛**
    ——本函数是 ``on_token_refresh`` 回调，调用方依赖「写回失败不影响本次
    任务」（内存里的新 token 仍然有效）。

    ⚠️ **别改成向上抛**（2026-11-20 实测定论）：写回发生在**服务端已经
    轮换 token 之后**（它是 :meth:`GuangyaDriver._refresh_web_token` 的
    最后一步）。一旦向上抛：

    - 本次业务操作作废，驱动实例被丢弃 → **内存里的新 token 一起丢**
    - DB 里只剩**已被服务端作废**的旧 token → 重启后账号彻底掉线，
      必须重新扫码（刷新接口轮换 ``refresh_token`` 后无法自救）
    - 且异常是裸 ``RuntimeError``，接不住 :meth:`GuangyaDriver._recover_unauthorized`
      的 ``except DriverError`` → 401 兜底路径丢失 ``kind="auth"`` 分类

    吞掉的代价只是「重启后掉线」，抛出的代价是「**现在就掉**」。

    Raises:
        无。写回失败只记日志（``写回刷新的光鸭凭据失败``），不中断调用链。
    """
    from app.adapters.drivers.guangya_credential import (
        SCHEME as GUANGYA_SCHEME,
    )
    from app.adapters.drivers.guangya_credential import (
        GuangyaCredential as _GuangyaCredential,
    )

    if not isinstance(cred, _GuangyaCredential):
        return
    try:
        async with session_scope() as db:
            row = await get_driver(db, driver_id)
            existing = _decode_stored_guangya_credential(row.credential)
            merged = _merge_guangya_credential(existing, cred)
            await store_credential(
                db,
                driver_id,
                merged.to_json(),
                scheme=GUANGYA_SCHEME,
                # Token 刷新是**同一个账号换 token**，登录态没变
                # → 不能作废测连缓存（否则面板退回「未测连」）。
                invalidate_cache=False,
            )
        logger.info("已写回刷新的光鸭凭据 driver_id=%s", driver_id)
    except Exception:
        logger.exception("写回刷新的光鸭凭据失败（本次任务不受影响）")


def _decode_stored_guangya_credential(raw: str | None) -> Any:
    """解密 DB 里的光鸭凭据；解不开就按「没有旧凭据」处理。

    降级必须留痕：否则解密层出问题没人知道，客户端 token 覆盖层会被
    无声清空（与 :func:`app.services.driver_auth._store_guangya_credential`
    同一口径）。
    """
    from app.adapters.drivers.credential_crypto import CredentialCryptoError
    from app.adapters.drivers.guangya_credential import decode_credential

    if not raw:
        return None
    try:
        return decode_credential(raw)
    except (CredentialCryptoError, ValueError, TypeError):
        logger.warning(
            "光鸭凭据解密失败，按新建凭据降级处理（driver 凭据）",
            exc_info=True,
        )
        return None


def _merge_guangya_credential(existing: Any, incoming: Any) -> Any:
    """合并「DB 现有凭据」与「驱动手里刷新后的凭据」。"""
    from app.adapters.drivers.guangya_credential import GuangyaCredential

    merged = existing or GuangyaCredential(access_token=incoming.access_token)

    if incoming.client_access_token:
        # 客户端身份的实例：客户端层 + 主 token 一起更新。
        # Web 的 refresh_token **跟着清空**（client 模式下主 token 就是
        # 客户端那把；留着会被 Web 刷新刷成 Web aud → 客户端身份丢失）。
        merged.access_token = incoming.access_token
        merged.expires_at = incoming.expires_at
        merged.device_id = incoming.device_id
        merged.refresh_token = incoming.refresh_token
        merged.client_access_token = incoming.client_access_token
        merged.client_refresh_token = incoming.client_refresh_token
        merged.client_expires_at = incoming.client_expires_at
        merged.client_device_id = incoming.client_device_id
        return merged

    # Web 身份的实例：**只更新 Web 层**，客户端覆盖层原样保留。
    merged.access_token = incoming.access_token
    merged.expires_at = incoming.expires_at
    merged.device_id = incoming.device_id
    if incoming.refresh_token:
        # 空的别覆盖——否则老实例会把 DB 里有效的 RT 抹掉
        merged.refresh_token = incoming.refresh_token
    return merged


async def store_credential(
    db: AsyncSession,
    driver_id: int,
    credential: str,
    device_type: str | None = None,
    *,
    scheme: str | None = None,
    invalidate_cache: bool = True,
) -> Driver:
    """加密存储凭据。

    Args:
        db: DB session。
        driver_id: 存储源 id。
        credential: 明文凭据（115 是 cookie 字符串；光鸭是 JSON）。
        device_type: 115 设备类型（可同时写入）。
        scheme: 加密方案前缀。``None`` = 用默认的 115 方案；光鸭传
            ``"gypcipher:v2:"``。
        invalidate_cache: 凭据变更后是否作废测连缓存。**默认 True**
            （重新扫码登录 → 旧状态确实过期了）。

            **光鸭 Token 刷新写回必须传 False**——那是同一个账号换了个
            token，登录态**没变**。清缓存会让面板从「正常」退回
            「未测连」，而 Token 1~2 小时就刷一次，等于把这个特性废掉。

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
    row.updated_at = bj_now()
    await db.commit()
    await db.refresh(row)
    # 凭据变了 → 缓存的 115 客户端实例失效
    reset_clients()
    # 凭据变了 → 旧的测连状态作废（调用方登录成功后会自动重探）
    #
    # 但**光鸭 Token 刷新写回要传 invalidate_cache=False**——见参数说明。
    if invalidate_cache:
        invalidate_probe(driver_id)
    logger.info("已存储凭据 driver_id=%s", driver_id)
    return row


async def delete_driver(db: AsyncSession, driver_id: int) -> None:
    """删除存储源（级联删关联数据）。"""
    row = await get_driver(db, driver_id)
    await db.delete(row)
    await db.commit()
    reset_clients()
    # 删除存储源 → 测连缓存必须清掉。
    #
    # **为什么必须清**：SQLite 的 ``id`` 是普通 ``INTEGER PRIMARY KEY``
    # （无 AUTOINCREMENT）→ **删掉最大的 id 后会被复用**。不清缓存的话，
    # 新建的存储源会**继承上一个存储源的测连状态**（账号 id / 容量 /
    # 「直链正常」）——面板显示的是一个根本没测过的账号，且错误账号 id
    # 会被写进 ``probe_results`` 历史。
    invalidate_probe(driver_id)


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
        probed_at=bj_now(),
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
        row.updated_at = bj_now()
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
    auto_prune: bool = False,
) -> DriverRow:
    """保存媒体库配置。

    ``media_cid`` 保存时会**实时解析出路径**填入 ``media_path``（只读展示）。
    cid 无效 → 400（**不做路径降级**——按设计，cid 失效就是配置错误）。

    ``auto_prune``：扫描时自动清理「网盘上已消失」的条目。清理会**永久
    删除**本地 strm（不进回收站），故默认关。
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
        row.auto_prune = auto_prune
        row.updated_at = bj_now()
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

    结果同时写入**进程内缓存**（供 ``GET /drivers`` 携带）并落库
    ``probe_results`` 表（探测历史）。
    """
    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_type = row.type
        credential = row.credential
        device_type = row.device_type

    if not is_probeable(driver_type):
        raise DriverServiceError("该存储源暂不支持探测", status_code=400)

    if not credential:
        status = AccountStatus(
            logged_in=False,
            status=STATUS_NO_CREDENTIAL,
            message="尚未配置凭据，请先扫码登录",
        )
        _cache_probe(driver_id, status)
        return status

    try:
        driver = build_driver(
            driver_type,
            credential,
            device_type,
            on_token_refresh=_make_token_refresh_persist(driver_id),
        )
    except DriverError as exc:
        raise DriverServiceError(
            f"驱动初始化失败：{exc}", status_code=500, kind="config"
        ) from exc

    status = await probe_account(driver)
    _cache_probe(driver_id, status)
    if status.logged_in:
        # 探测历史落库（no_credential 没有账号信息可记，跳过）
        try:
            async with session_scope() as db:
                await record_probe(
                    db,
                    driver_id,
                    account_id=status.account_id,
                    used_gb=status.used_bytes / 1024**3,
                    total_gb=status.total_bytes / 1024**3,
                    available=status.healthy,
                )
        except Exception as exc:  # 落库失败不影响测连结果
            logger.warning("探测结果落库失败 driver_id=%s: %s", driver_id, exc)
            from app.services.startup_status import record

            record(f"driver_probe_persist:{driver_id}", exc)
    return status


# ================================================================ 测连缓存
#
# 进程内缓存：启动自动测连 / 手动测连的结果放这里，``GET /drivers``
# 携带给前端——页面打开不用再逐个探测，也不再有「刷新即全变未测连」。
#
@dataclass(frozen=True)
class CachedProbe:
    """某存储源的一次测连结果（进程内缓存）。

    Attributes:
        status: 测连状态（登录态 / 容量 / 能否直链）。
        checked_at: 测连时间（**北京时间字符串**）。
    """

    status: AccountStatus
    checked_at: str


#: driver_id → 测连结果
_probe_cache: dict[int, CachedProbe] = {}


def _cache_probe(driver_id: int, status: AccountStatus) -> CachedProbe:
    cached = CachedProbe(status=status, checked_at=bj_now())
    _probe_cache[driver_id] = cached
    return cached


def invalidate_probe(driver_id: int) -> None:
    """清掉某存储源的测连缓存（凭据变更时调用，随后应重探）。"""
    _probe_cache.pop(driver_id, None)


def cached_probe(driver_id: int) -> CachedProbe | None:
    """读某存储源的缓存测连结果（无则 ``None``）。"""
    return _probe_cache.get(driver_id)


async def probe_all_enabled() -> dict[int, CachedProbe]:
    """并发探测**所有启用且有凭据**的存储源（启动时后台跑）。

    - 信号量 2 并发：探测会打云端 API（登录态 + 容量 + 直链三项），
      全并发容易触发限流
    - 单个探测 60s 超时：慢站点不能拖死整批
    - 任何失败都不抛——启动测连是尽力而为，结果供面板展示
    """
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.enabled == True,  # noqa: E712
                        Driver.type.in_(PROBEABLE_DRIVER_TYPES),
                        Driver.credential.is_not(None),
                        Driver.credential != "",
                    )
                )
            )
            .scalars()
            .all()
        )
    targets = [r.id for r in rows]
    if not targets:
        logger.info("启动测连：没有可探测的存储源")
        return {}
    sem = asyncio.Semaphore(2)

    async def _one(driver_id: int) -> None:
        async with sem:
            try:
                await asyncio.wait_for(probe_driver(driver_id), timeout=60)
                logger.info("启动测连完成 driver_id=%s", driver_id)
            except Exception as exc:
                logger.warning("启动测连失败 driver_id=%s: %s", driver_id, exc)
                from app.services.startup_status import record

                record(f"driver_probe:{driver_id}", exc)

    await asyncio.gather(*(_one(i) for i in targets))
    logger.info("启动测连完成：%d 个存储源", len(targets))
    return dict(_probe_cache)


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


async def start_scan_task(driver_id: int, *, prune: bool | None = None) -> JobRef:
    """启动扫描任务（**立即返回 job_id**，扫描在后台跑）。

    为什么要异步：限速 1s/请求下扫一棵大树要几分钟，同步 HTTP 会让用户
    只看到转圈，且反代 / 网关的 60s 超时会把请求掐断。

    进度用 ``GET /api/scan-jobs/{job_id}`` 轮询。

    Args:
        prune: ``None``（**默认**）= 用存储源配置的 ``auto_prune``；
            ``True`` / ``False`` = 显式覆盖（手动单次强制清理 / 强制不清理）。
            区分「没传」与「传了 false」是这里的关键——配置为关时，
            手动 ``?prune=true`` 仍应能单次清理。
    """
    from app.services.scan_jobs import find_running
    from app.services.scan_runner import start_scan_job

    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_name = row.name
        effective_prune = bool(row.auto_prune) if prune is None else prune
        # 副数据源是**缓冲**：内容不参与媒体库（不生成 strm、不进库）
        if row.role == "secondary":
            raise DriverServiceError(
                "副数据源不参与媒体库扫描（它的内容会归拢到主数据源）",
                status_code=400,
                kind="config",
            )

    # 同一存储源不允许并发扫描（限速下并发只会互相拖慢，且会打爆 115）
    running = await find_running(driver_id)
    if running is not None:
        raise DriverServiceError(
            f"该存储源已有扫描任务在跑（job={running.id}）",
            status_code=409,
            kind="conflict",
        )

    job = start_scan_job(driver_id, driver_name, prune=effective_prune)
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


async def browse_driver_dirs(driver_id: int, parent_id: str = "") -> list[tuple[str, str]]:
    """列出存储源某个目录下的**子目录**（按 id 逐层浏览）。

    媒体库配置页用它选「媒体库目录」——用户不该手抄 fileId / cid
    （手抄既容易错，光鸭那种 19 位数字更是无从核对）。

    与 :func:`app.services.upload_tasks.browse_target_dirs` 的**关键差异**：
    **不做「是否支持上传」的校验**——浏览是 CloudDriver 的**读能力**，
    而 115 不支持上传（``UPLOADABLE_DRIVER_TYPES`` 只有光鸭），但媒体库
    扫描用的恰恰就是 115。

    Args:
        driver_id: 存储源 id。
        parent_id: 父目录 id；**留空 = 账号根目录**（按驱动类型解析，
            见 :data:`app.domain.driver_types.ROOT_DIR_IDS`）。浏览起点
            只能是根——此时 ``media_cid`` 还没配，没有更深的入口。

    Returns:
        ``[(remote_id, name), ...]``——只含目录（文件不进目录浏览器）。

    Raises:
        DriverServiceError: 存储源不存在（404）/ 未知驱动类型（400）/
            驱动不支持按 id 浏览（400）/ 上游调用失败（400）。
    """
    async with session_scope() as db:
        row = await get_driver(db, driver_id)
        driver_type = row.type
        credential = row.credential
        device_type = row.device_type

    root = root_dir_id(driver_type)
    if root is None:
        raise DriverServiceError(
            f"未知的驱动类型：{driver_type}", status_code=400, kind="config"
        )
    resolved_parent = parent_id or root

    try:
        driver = build_driver(
            driver_type,
            credential,
            device_type,
            on_token_refresh=_make_token_refresh_persist(driver_id),
        )
    except DriverError as exc:
        raise DriverServiceError(
            f"驱动初始化失败：{exc}", status_code=500, kind="config"
        ) from exc

    # ``list_dir_by_cid`` 不在 CloudDriver 协议里（115 / 光鸭都有，但协议只
    # 要求 ``list_dir``），故用 getattr 取——与 migration.py 同一做法。
    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        raise DriverServiceError(
            "该驱动不支持按 id 浏览目录", status_code=400, kind="config"
        )
    try:
        entries = await lister(resolved_parent)
    except DriverError as exc:
        raise DriverServiceError(str(exc), status_code=400, kind="upstream") from exc
    return [(e.remote_id, e.name) for e in entries if e.is_dir]


__all__ = [
    "DEFAULT_DEVICE_TYPE",
    "CachedProbe",
    "DeviceTypeInfo",
    "DeviceTypeLiteral",
    "DriverRow",
    "DriverServiceError",
    "JobRef",
    "add_driver",
    "browse_driver_dirs",
    "cached_probe",
    "create_driver",
    "delete_driver",
    "diagnose_driver_direct_link",
    "get_driver",
    "invalidate_probe",
    "latest_probe",
    "list_device_types",
    "list_drivers",
    "mask_credential",
    "probe_all_enabled",
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
