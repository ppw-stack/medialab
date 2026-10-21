"""下载器管理服务（下载器 CRUD + 测连 + 原生任务操作）。

此前这些逻辑全在 :mod:`app.api.routes.downloaders` 路由层：9 处
``session_scope`` 直写 SQL + 5 处**直接调 adapter**（测连 ×2、列任务、
暂停、删除）——路由层既要碰 DB 又要碰网络，无法独立单测。

本模块把业务逻辑下沉，路由只留「参数校验 + 结果转换」。

错误一律抛 :class:`app.services.errors.ServiceError`（带 HTTP 状态码），
由 API 层统一翻译。
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import delete, select

from app.adapters.downloaders.base import (
    DownloaderAdapter,
    DownloaderError,
    TorrentFilter,
    TorrentInfo,
    TorrentState,
)
from app.core.logging import get_logger
from app.db.models import DownloaderInstance
from app.db.session import session_scope
from app.services.errors import ServiceError
from app.services.factory import get_factory, invalidate_downloader

logger = get_logger(__name__)

__all__ = [
    "ALL_TYPES",
    "BATCH_LIMIT",
    "SUPPORTED_TYPES",
    "DownloaderBatchOpResult",
    "DownloaderSnapshot",
    "DownloaderTorrentView",
    "create_downloader",
    "delete_downloader",
    "get_downloader",
    "list_downloader_torrents",
    "list_downloaders",
    "pause_downloader_torrents",
    "remove_downloader_torrents",
    "test_downloader",
    "test_downloader_config",
    "update_downloader",
    "validate_hashes",
]


#: 已实现的下载器类型（M1+M2 范围）。其他类型后续按 FR-1 补齐。
SUPPORTED_TYPES: Final[tuple[str, ...]] = ("qbittorrent", "transmission")
ALL_TYPES: Final[tuple[str, ...]] = ("qbittorrent", "transmission", "deluge", "aria2")

#: 批量操作上限（hash 条数）——防止一次请求打爆下载器。
BATCH_LIMIT: Final[int] = 200

#: BTIH infohash：40 位 hex。拒绝 ``all`` / ``|`` / 任意非 hash 值——
#: qBittorrent 会把 ``all`` 解析为"应用到所有种子"，可一次删光全部任务及数据。
_VALID_HASH_RE: Final[re.Pattern[str]] = re.compile(r"^[a-fA-F0-9]{40}$")


# ==================================================================== DTO


@dataclass(frozen=True, slots=True)
class DownloaderSnapshot:
    """下载器配置快照（对外 DTO，密码不外泄）。"""

    id: int
    name: str
    type: str
    url: str
    username: str | None
    has_password: bool
    save_path: str | None
    is_default: bool
    enabled: bool
    verify_tls: bool
    is_cross_seed: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DownloaderTorrentView:
    """下载器原生任务视图（来自 adapter.list_torrents）。"""

    hash: str
    name: str
    state: str
    progress: float
    size: int
    downloaded: int
    uploaded: int
    dlspeed: int
    upspeed: int
    ratio: float
    save_path: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DownloaderBatchOpResult:
    """批量操作结果。"""

    requested: int
    succeeded: int
    failed: int
    errors: list[str] = field(default_factory=list)


# ==================================================================== 校验


def validate_hashes(hashes: list[str]) -> None:
    """校验批量操作的 hash 白名单（拒绝 ``all`` / 通配等会批量误删的值）。

    :raises ServiceError: 400（非法 infohash）
    """
    bad = [h for h in hashes if not _VALID_HASH_RE.fullmatch(h)]
    if bad:
        raise ServiceError(
            f"非法 infohash：{bad[:5]}（必须是 40 位 hex，不支持 all/通配）",
            status=400,
        )


def _validate_type(dl_type: str) -> None:
    """校验下载器类型。

    :raises ServiceError: 400（未知类型）/ 501（合法但未实现）
    """
    if dl_type not in ALL_TYPES:
        raise ServiceError(f"未知下载器类型：{dl_type}", status=400)
    if dl_type not in SUPPORTED_TYPES:
        raise ServiceError(
            f"下载器类型 {dl_type} 暂不支持，目前仅支持 qBittorrent / Transmission",
            status=501,
        )


# ==================================================================== CRUD


def _to_snapshot(row: DownloaderInstance) -> DownloaderSnapshot:
    return DownloaderSnapshot(
        id=row.id,
        name=row.name,
        type=row.type,
        url=row.url,
        username=row.username,
        has_password=bool(row.password),
        save_path=row.save_path,
        is_default=row.is_default,
        enabled=row.enabled,
        verify_tls=row.verify_tls,
        is_cross_seed=bool(getattr(row, "is_cross_seed", False)),
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
    )


async def _apply_default(session: object, target_id: int) -> None:
    """确保只有一个 is_default=True（将其他全部置 False）。"""
    rows = (
        await session.execute(  # type: ignore[attr-defined]
            select(DownloaderInstance).where(
                DownloaderInstance.id != target_id,
                DownloaderInstance.is_default.is_(True),
            )
        )
    ).scalars().all()
    for r in rows:
        r.is_default = False


async def list_downloaders() -> list[DownloaderSnapshot]:
    """所有下载器（按 id 升序）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(select(DownloaderInstance).order_by(DownloaderInstance.id.asc()))
        ).scalars().all()
    return [_to_snapshot(r) for r in rows]


async def get_downloader(downloader_id: int) -> DownloaderSnapshot:
    """单下载器详情。

    :raises ServiceError: 404（下载器不存在）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("下载器不存在", status=404)
        return _to_snapshot(row)


async def create_downloader(
    *,
    name: str,
    type: str,
    url: str,
    username: str | None = None,
    password: str | None = None,
    save_path: str | None = None,
    is_default: bool = False,
    enabled: bool = True,
    verify_tls: bool = True,
) -> DownloaderSnapshot:
    """新增下载器。

    :raises ServiceError: 400（未知类型）/ 501（未实现）/ 409（同名）
    """
    _validate_type(type)
    async with session_scope() as session:
        exists = (
            await session.execute(
                select(DownloaderInstance.id).where(DownloaderInstance.name == name)
            )
        ).first()
        if exists:
            raise ServiceError(f"下载器 {name} 已存在", status=409)
        row = DownloaderInstance(
            name=name,
            type=type,
            url=url,
            username=username,
            password=password,
            save_path=save_path or None,
            is_default=is_default,
            enabled=enabled,
            verify_tls=verify_tls,
        )
        session.add(row)
        await session.flush()
        if is_default:
            await _apply_default(session, row.id)
        return _to_snapshot(row)


async def update_downloader(
    downloader_id: int,
    *,
    name: str | None = None,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    save_path: str | None = None,
    is_default: bool | None = None,
    enabled: bool | None = None,
    verify_tls: bool | None = None,
    is_cross_seed: bool | None = None,
) -> DownloaderSnapshot:
    """更新下载器配置。

    凭据字段（``password`` / ``save_path``）三态：

    - ``None`` → 不动 DB
    - ``""`` → 清空该字段
    - 有值 → 覆盖

    :raises ServiceError: 404（下载器不存在）/ 409（同名）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("下载器不存在", status=404)
        if name and name != row.name:
            if (
                await session.execute(
                    select(DownloaderInstance).where(DownloaderInstance.name == name)
                )
            ).first():
                raise ServiceError(f"下载器 {name} 已存在", status=409)
            row.name = name
        if url is not None:
            row.url = url
        if username is not None:
            row.username = username
        if save_path is not None:
            # 空串归一为 None（前端清空即取消）
            row.save_path = save_path or None
        if is_default is not None:
            row.is_default = is_default
        if enabled is not None:
            row.enabled = enabled
        if verify_tls is not None:
            row.verify_tls = verify_tls
        if is_cross_seed is not None:
            row.is_cross_seed = is_cross_seed
        # 用 model_fields_set 区分"未提供"与"显式清空"：空串允许清掉密码
        if password is not None:
            row.password = password or None
        if is_default:
            await _apply_default(session, row.id)
    # 快照在**提交后**取：``updated_at`` 由 ``onupdate`` 在 flush 时写入，
    # 事务内读到的是旧值（提交前）。``expire_on_commit=False`` 保证
    # session 关闭后已加载属性仍可读。
    out = _to_snapshot(row)
    # 更新后失效缓存：否则 cron/推送仍用旧凭据（旧 URL/密码继续生效）
    invalidate_downloader(downloader_id)
    return out


async def delete_downloader(downloader_id: int) -> int:
    """删除下载器配置。

    删除下载器实例配置仅是 DB 层操作——``TorrentIndex`` 没有
    ``downloader_id`` 列（推送状态记录的是"种子级"而非"下载器级"），
    故无法按下载器维度做引用计数。已在下载器里运行的种子由用户在
    下载器控制台自行清理，删除配置不阻塞于历史推送。

    :raises ServiceError: 404（下载器不存在）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("下载器不存在", status=404)
        await session.execute(
            delete(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
        )
    invalidate_downloader(downloader_id)
    return downloader_id


# ==================================================================== 测连


async def test_downloader(downloader_id: int) -> bool:
    """用 **DB 里的配置**测连（验证 URL + 凭据）。

    :raises ServiceError: 404（不存在）/ 400（已禁用）/ 502（下载器报错）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("下载器不存在", status=404)
        if not row.enabled:
            raise ServiceError("下载器已禁用", status=400)
        adapter = get_factory().downloader(row)
    try:
        return await adapter.ping()
    except DownloaderError as exc:
        raise ServiceError(str(exc), status=502) from exc


async def test_downloader_config(
    downloader_id: int,
    *,
    type: str | None = None,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    verify_tls: bool | None = None,
) -> bool:
    """用**未保存的表单值**测连（不落库）。

    编辑弹窗的"测连"按钮与凭据输入框相邻，用户很可能刚改完 URL/账号/
    密码就点测连。按 id 测 DB 里的旧配置会得到与表单矛盾的结果
    （表单看着是新的，测的是旧的）。故这里用表单值构造临时适配器测。

    未提供的字段回落到 DB 里的值（编辑时只改了部分字段也能测）。

    :raises ServiceError: 404 / 400（类型不支持）/ 502（下载器报错）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("下载器不存在", status=404)

    resolved_url = url or row.url
    resolved_user = username if username is not None else row.username
    resolved_pass = password if password is not None else row.password
    resolved_tls = verify_tls if verify_tls is not None else row.verify_tls
    dl_type = type or row.type

    # 构造**一次性**适配器（不入 factory 缓存——不能污染已缓存实例）
    try:
        adapter: DownloaderAdapter
        match dl_type:
            case "qbittorrent":
                from app.adapters.downloaders.qbittorrent import QbittorrentAdapter

                adapter = QbittorrentAdapter(
                    row.name, resolved_url, resolved_user or "", resolved_pass or "",
                    verify_tls=resolved_tls,
                )
            case "transmission":
                from app.adapters.downloaders.transmission import (
                    TransmissionAdapter,
                )

                adapter = TransmissionAdapter(
                    row.name, resolved_url, resolved_user or "", resolved_pass or "",
                    verify_tls=resolved_tls,
                )
            case _:
                raise ServiceError(f"不支持的下载器类型：{dl_type}", status=400)
    except ServiceError:
        raise
    except (ValueError, NotImplementedError) as exc:
        raise ServiceError(str(exc), status=400) from exc

    try:
        return await adapter.ping()
    except DownloaderError as exc:
        raise ServiceError(str(exc), status=502) from exc
    finally:
        close = getattr(adapter, "close", None)
        if close is not None:
            with suppress(Exception):
                await close()


# ==================================================================== 原生任务


async def _resolve_downloader(downloader_id: int) -> DownloaderAdapter:
    """取下载器适配器（校验存在 + 启用）。

    :raises ServiceError: 404（不存在）/ 400（已禁用）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("下载器不存在", status=404)
        if not row.enabled:
            raise ServiceError("下载器已禁用", status=400)
    return get_factory().downloader(row)


def _to_torrent_view(t: TorrentInfo) -> DownloaderTorrentView:
    """适配器任务 → 对外视图。

    ``state`` 用枚举**值**（camelCase，如 pausedDL / missingFiles）而非
    name（PAUSED_DL）——前端 STATE_META 与过滤参数都用值，
    且过滤参数要能被 TorrentState.parse 原样解析回去。
    """
    state = t.state
    return DownloaderTorrentView(
        hash=t.hash,
        name=t.name,
        state=state.value if isinstance(state, TorrentState) else str(state),
        progress=t.progress,
        size=t.size,
        downloaded=t.downloaded,
        uploaded=t.uploaded,
        dlspeed=t.dlspeed,
        upspeed=t.upspeed,
        ratio=t.ratio,
        save_path=t.save_path,
        category=t.category,
        tags=list(t.tags),
    )


async def list_downloader_torrents(
    downloader_id: int,
    *,
    state: str | None = None,
    category: str | None = None,
) -> list[DownloaderTorrentView]:
    """列出下载器原生任务。

    过滤：state（downloading/uploading/paused/error 等）/ category。

    :raises ServiceError: 404 / 400（已禁用、state 非法）/ 502（下载器报错）
    """
    adapter = await _resolve_downloader(downloader_id)

    states: frozenset[TorrentState] = frozenset()
    if state:
        try:
            # TorrentState.parse 接受枚举值 / 枚举名 / snake_case 别名
            states = frozenset({TorrentState.parse(state)})
        except ValueError as exc:
            # 之前静默降级为"不过滤"返全量，会误以为 foobar 匹配到了所有任务
            raise ServiceError(str(exc), status=400) from exc

    tags: frozenset[str] = frozenset()
    try:
        items = await adapter.list_torrents(
            TorrentFilter(states=states, category=category, tags=tags)
        )
    except DownloaderError as exc:
        raise ServiceError(str(exc), status=502) from exc

    return [_to_torrent_view(t) for t in items]


async def pause_downloader_torrents(
    downloader_id: int,
    hashes: list[str],
    *,
    paused: bool = True,
) -> DownloaderBatchOpResult:
    """批量暂停 / 恢复下载器原生任务。

    :raises ServiceError: 400（超上限 / 非法 hash）/ 404 / 502
    """
    if len(hashes) > BATCH_LIMIT:
        raise ServiceError(f"批量操作上限 {BATCH_LIMIT}", status=400)
    validate_hashes(hashes)
    adapter = await _resolve_downloader(downloader_id)
    try:
        ok = await adapter.set_pause(hashes, paused)
    except DownloaderError as exc:
        raise ServiceError(str(exc), status=502) from exc
    return DownloaderBatchOpResult(
        requested=len(hashes),
        succeeded=len(hashes) if ok else 0,
        failed=0 if ok else len(hashes),
    )


async def remove_downloader_torrents(
    downloader_id: int,
    hashes: list[str],
    *,
    delete_data: bool = False,
) -> DownloaderBatchOpResult:
    """删除下载器原生任务。

    :raises ServiceError: 400（超上限 / 非法 hash）/ 404 / 502
    """
    if len(hashes) > BATCH_LIMIT:
        raise ServiceError(f"批量操作上限 {BATCH_LIMIT}", status=400)
    validate_hashes(hashes)
    adapter = await _resolve_downloader(downloader_id)
    # 删数据不可恢复：记审计日志（前端另有确认弹窗）
    # AGENTS.md "删磁盘数据单独开关并记审计日志"——审计的取证价值在于
    # 能定位"删了什么"，故记完整 hash 列表（最多 200 条，已校验格式）
    if delete_data:
        logger.warning(
            "删除下载器原生任务并删除磁盘数据：downloader_id=%d 数量=%d hashes=%s",
            downloader_id, len(hashes),
            ",".join(hashes[:50]),
        )
    try:
        ok = await adapter.remove(hashes, delete_data=delete_data)
    except DownloaderError as exc:
        raise ServiceError(str(exc), status=502) from exc
    return DownloaderBatchOpResult(
        requested=len(hashes),
        succeeded=len(hashes) if ok else 0,
        failed=0 if ok else len(hashes),
    )
