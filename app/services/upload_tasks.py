"""上传任务的 CRUD 与运行态。

**运行态是内存态**（与扫描 / 迁移任务同款结构）：进度丢了重跑即可——
幂等靠云端目录（同名 + 同大小 = 已同步），不依赖本地状态。

**任务配置在 DB**（``upload_tasks`` 表），**运行态在内存**
（``_TASKS`` 字典）。DB 是真相源：重启后按 DB 重新拉起监听器。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import UploadTask as UploadTaskRow
from app.db.session import session_scope
from app.domain.upload_types import DEFAULT_UPLOAD_CONCURRENCY
from app.services.upload_paths import PathGuardError, resolve_watch_dir
from app.services.uploader import CONFLICT_KEEP_LARGER

logger = get_logger(__name__)

#: 每个任务保留的日志行数（环形缓冲）
MAX_LOGS = 300

#: 状态
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"


class UploadTaskError(Exception):
    """上传任务操作失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


@dataclass(slots=True)
class UploadTaskView:
    """上传任务的对外形状（**API 层不认识 ORM**）。

    附带运行态（监听中？正在传什么？）——运行态在内存里，与 DB 无关。
    """

    id: int
    name: str
    driver_id: int
    target_file_id: str
    target_path: str | None
    watch_root: str
    include_subdirs: bool
    enabled: bool
    delete_after_upload: bool
    concurrency: int
    created_at: str
    updated_at: str
    #: 同名且大小不同时的冲突策略（0=本地优先 / 1=保留大的）
    conflict_policy: int = CONFLICT_KEEP_LARGER
    #: 目标**聚合组**（None = 目标是某个具体存储源）
    aggregate_group_id: int | None = None
    #: 目标**子目录**（聚合模式专属）：组映射的子路径覆盖，None = 无覆盖
    aggregate_sub_path: str | None = None
    #: 运行态（监听中？正在传什么？）
    listening: bool = False
    current: str = ""
    #: 小文件通道正在上传的文件
    current_small: str = ""
    total_uploaded: int = 0
    total_failed: int = 0
    total_bytes: int = 0
    #: 真正被丢弃的文件数（队列与溢出暂存都满时）
    dropped: int = 0
    #: 运行态的最近一次错误（**监听启动失败等**）——面板据此提示
    runtime_error: str | None = None


def to_task_view(row: UploadTaskRow) -> UploadTaskView:
    """ORM 行 → :class:`UploadTaskView`（附带运行态）。"""
    runtime = get_runtime(row.id)
    return UploadTaskView(
        id=row.id,
        name=row.name,
        driver_id=row.driver_id,
        target_file_id=row.target_file_id,
        target_path=row.target_path,
        watch_root=row.watch_root,
        include_subdirs=row.include_subdirs,
        enabled=row.enabled,
        delete_after_upload=row.delete_after_upload,
        concurrency=row.concurrency,
        conflict_policy=row.conflict_policy,
        aggregate_group_id=row.aggregate_group_id,
        aggregate_sub_path=row.aggregate_sub_path,
        created_at=row.created_at,
        updated_at=row.updated_at,
        listening=runtime.listening,
        current=runtime.current,
        current_small=runtime.current_small,
        total_uploaded=runtime.total_uploaded,
        total_failed=runtime.total_failed,
        total_bytes=runtime.total_bytes,
        dropped=runtime.dropped,
        runtime_error=runtime.error,
    )


@dataclass
class UploadRuntime:
    """一个任务的运行时状态。"""

    task_id: int
    #: 监听器是否在跑
    listening: bool = False
    #: 监听器协程
    watcher: Any = None
    #: 大文件队列（≥ 分流阈值，恒串行消费）
    queue_large: asyncio.Queue[Any] | None = None
    #: 小文件队列（< 分流阈值，K 个 worker 并发消费）
    queue_small: asyncio.Queue[Any] | None = None
    #: 上传 worker 协程（大 1 个 + 小 K 个）
    workers: list[asyncio.Task[None]] = field(default_factory=list)
    #: 正在上传的文件（大文件通道——含全量同步）
    current: str = ""
    #: 正在上传的文件（小文件通道）
    current_small: str = ""
    #: 累计统计
    total_uploaded: int = 0
    total_failed: int = 0
    total_bytes: int = 0
    #: 溢出暂存量（watch_manager 维护：队列满时暂存、空闲回灌）
    overflow: int = 0
    #: 真正被丢弃的文件数（队列与溢出暂存都满时）
    dropped: int = 0
    #: 日志（环形）
    logs: list[str] = field(default_factory=list)
    #: 上次错误
    error: str | None = None
    #: 是否正在跑「立即同步」
    syncing: bool = False

    def log(self, message: str) -> None:
        """追加一行日志。"""
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > MAX_LOGS:
            del self.logs[: len(self.logs) - MAX_LOGS]

    def to_dict(self) -> dict[str, Any]:
        """序列化给 API。"""
        return {
            "task_id": self.task_id,
            "listening": self.listening,
            "current": self.current,
            "current_small": self.current_small,
            "total_uploaded": self.total_uploaded,
            "total_failed": self.total_failed,
            "total_bytes": self.total_bytes,
            # 双通道水位 + 溢出/丢弃（**可观测**——过去队列满只能靠
            # 刷屏的丢弃日志感知，看不到积压程度）
            "queue_large": self.queue_large.qsize() if self.queue_large else 0,
            "queue_small": self.queue_small.qsize() if self.queue_small else 0,
            "overflow": self.overflow,
            "dropped": self.dropped,
            "logs": self.logs[-MAX_LOGS:],
            "error": self.error,
            "syncing": self.syncing,
        }


#: 运行态表（task_id → UploadRuntime）
_RUNTIMES: dict[int, UploadRuntime] = {}


def get_runtime(task_id: int) -> UploadRuntime:
    """取（或建）某个任务的运行态。"""
    runtime = _RUNTIMES.get(task_id)
    if runtime is None:
        runtime = UploadRuntime(task_id=task_id)
        _RUNTIMES[task_id] = runtime
    return runtime


def all_runtimes() -> dict[int, UploadRuntime]:
    """全部运行态。"""
    return _RUNTIMES


def clear_all() -> None:
    """清空运行态（测试用）。"""
    _RUNTIMES.clear()


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


async def list_tasks() -> list[UploadTaskRow]:
    """列出所有上传任务。"""
    async with session_scope() as db:
        rows = (
            await db.execute(select(UploadTaskRow).order_by(UploadTaskRow.id))
        ).scalars().all()
        return list(rows)


async def get_task(db: Any, task_id: int) -> UploadTaskRow:
    """取一个任务。"""
    row: UploadTaskRow | None = (
        await db.execute(select(UploadTaskRow).where(UploadTaskRow.id == task_id))
    ).scalar_one_or_none()
    if row is None:
        raise UploadTaskError(
            f"上传任务不存在：{task_id}", status_code=404, kind="not_found"
        )
    return row


async def create_task(
    db: Any,
    *,
    name: str,
    driver_id: int | None,
    target_file_id: str,
    watch_root: str,
    include_subdirs: bool = True,
    enabled: bool = True,
    concurrency: int = DEFAULT_UPLOAD_CONCURRENCY,
    target_path: str = "",
    delete_after_upload: bool = False,
    conflict_policy: int = CONFLICT_KEEP_LARGER,
    aggregate_group_id: int | None = None,
    aggregate_sub_path: str | None = None,
) -> UploadTaskRow:
    """创建上传任务。

    ``aggregate_group_id`` 非空 = **「本地同步到网盘聚合」**：目标不是某个
    具体存储源，而是**一个聚合组**——落点由分配器在组内副盘之间挑选
    （撞到月度上传限额就换下一个），上传成功后**即时登记台账**，归拢
    引擎接管搬运。此时 ``driver_id`` / ``target_file_id`` 由后端写入
    **落点快照**（粘性——重启后恢复）。

    ``aggregate_sub_path`` = 任务级的「目标子目录」（**组映射的子路径
    覆盖**，仅聚合模式有效）：文件最终落在主盘「归拢目标目录」下的这个
    子目录里。

    Raises:
        UploadTaskError: 存储源不支持上传 / 路径非法 / 目录不存在 /
            聚合组不可用 / 目标子目录非法。
    """
    from app.adapters.drivers.upload_base import is_uploadable
    from app.db.models import Driver

    if aggregate_group_id is not None:
        landing = await _resolve_aggregate_landing(aggregate_group_id)
        aggregate_sub_path = _normalize_sub_path(aggregate_sub_path, aggregate=True)
        driver_id = landing.driver_id
        target_file_id = landing.root_id
        target_path = landing.root_path or ""
        if aggregate_sub_path:
            # 展示值与运行时的落点快照保持一致（副盘缓冲根 + 子目录）
            target_path = f"{target_path}/{aggregate_sub_path}"
    elif driver_id is None:
        raise UploadTaskError("必须选择目标存储源", status_code=400, kind="config")
    else:
        # 「指定存储源」模式：没有组映射可覆盖 → 传了子目录就是配置错误
        aggregate_sub_path = _normalize_sub_path(aggregate_sub_path, aggregate=False)

    driver = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).scalar_one_or_none()
    if driver is None:
        raise UploadTaskError(f"存储源不存在：{driver_id}", status_code=404, kind="not_found")
    if not is_uploadable(driver.type):
        raise UploadTaskError(
            f"该存储源不支持上传（当前：{driver.type}）", status_code=400, kind="config"
        )

    watch_dir = _validate_watch_dir(watch_root)

    now = bj_now()
    row = UploadTaskRow(
        name=name,
        driver_id=driver_id,
        target_file_id=target_file_id,
        target_path=target_path or None,
        watch_root=watch_root,
        include_subdirs=include_subdirs,
        enabled=enabled,
        delete_after_upload=delete_after_upload,
        concurrency=max(1, int(concurrency)),
        conflict_policy=max(0, min(1, int(conflict_policy))),
        aggregate_group_id=aggregate_group_id,
        aggregate_sub_path=aggregate_sub_path,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "创建上传任务 id=%s 监听=%s → %s",
        row.id,
        watch_dir,
        (
            f"聚合组 {aggregate_group_id}（落点副盘 {driver_id}）"
            if aggregate_group_id is not None
            else f"网盘 {target_file_id}（driver={driver_id}）"
        ),
    )
    return row


def _normalize_sub_path(raw: str | None, *, aggregate: bool) -> str | None:
    """归一化任务级的「目标子目录」（**组映射的子路径覆盖**）。

    Args:
        raw: 用户填的子目录（空串 / None = 不覆盖）。
        aggregate: 任务是否处于**网盘聚合模式**。指定存储源模式没有组
            映射可覆盖 → 传了子目录就是配置错误。

    Returns:
        归一化后的相对路径；``None`` = 不覆盖。

    Raises:
        UploadTaskError: 路径非法 / 模式不匹配。
    """
    from app.services import aggregate_upload
    from app.services.errors import ServiceError

    if raw is None or not str(raw).strip():
        return None
    if not aggregate:
        raise UploadTaskError(
            "「目标子目录」只适用于网盘聚合模式——"
            "指定存储源模式没有组映射可覆盖",
            status_code=400,
            kind="config",
        )
    try:
        return aggregate_upload.normalize_sub_path(raw)
    except ServiceError as exc:
        raise UploadTaskError(str(exc), status_code=400, kind="config") from exc


async def _resolve_aggregate_landing(group_id: int) -> Any:
    """校验聚合组并挑一个初始落点。

    Raises:
        UploadTaskError: 组不可用（不存在 / 停用 / 无主盘 / 未配归拢目标 /
            没有可用副盘）。
    """
    from app.services import aggregate_upload
    from app.services.errors import ServiceError

    try:
        await aggregate_upload.validate_group_target(group_id)
    except ServiceError as exc:
        raise UploadTaskError(str(exc), status_code=exc.status, kind="config") from exc
    candidate = await aggregate_upload.pick_candidate(group_id, 0)
    if candidate is None:
        raise UploadTaskError(
            "该聚合组没有可用的副盘落点（副盘需启用、已配缓冲根、"
            "且本月上传额度未耗尽）",
            status_code=400,
            kind="config",
        )
    return candidate


async def update_task(db: Any, task_id: int, **fields: Any) -> UploadTaskRow:
    """修改任务。

    **聚合组**：切换组时重新挑落点；留在同一个组里时**忽略**外部传入的
    ``driver_id`` / ``target_file_id``（落点由分配器维护）。

    Raises:
        UploadTaskError: 任务不存在 / 路径非法 / 聚合组不可用。
    """
    row = await get_task(db, task_id)

    watch_root = fields.get("watch_root", row.watch_root)
    if "watch_root" in fields:
        _validate_watch_dir(watch_root)

    prev_group = row.aggregate_group_id
    new_group = prev_group
    if "aggregate_group_id" in fields:
        new_group = fields["aggregate_group_id"]

    if new_group is not None and new_group != prev_group:
        # 换组 → 重新挑落点
        landing = await _resolve_aggregate_landing(new_group)
        fields["driver_id"] = landing.driver_id
        fields["target_file_id"] = landing.root_id
        fields["target_path"] = landing.root_path
    elif new_group is not None:
        # 同一个组：落点由分配器维护，忽略外部指定的目标
        fields.pop("driver_id", None)
        fields.pop("target_file_id", None)
        fields.pop("target_path", None)

    # 目标子目录（组映射的子路径覆盖）：**只在聚合模式下有效**。
    # 切回「指定存储源」模式 → 没有组映射可覆盖 → **一律清空**
    #（哪怕这次没传该字段——旧值留着就是脏数据）。
    if new_group is None:
        fields["aggregate_sub_path"] = None
    elif "aggregate_sub_path" in fields:
        fields["aggregate_sub_path"] = _normalize_sub_path(
            fields["aggregate_sub_path"], aggregate=True
        )

    for key in (
        "name",
        "driver_id",
        "target_file_id",
        "target_path",
        "watch_root",
        "include_subdirs",
        "enabled",
        "delete_after_upload",
        "concurrency",
        "conflict_policy",
    ):
        if key in fields and fields[key] is not None:
            setattr(row, key, fields[key])

    # 聚合组：**显式**赋值——``None`` 表示「退回存储源模式」
    if "aggregate_group_id" in fields:
        row.aggregate_group_id = fields["aggregate_group_id"]

    # 目标子目录：**显式**赋值——``None`` 表示「不覆盖」（清空）
    if "aggregate_sub_path" in fields:
        row.aggregate_sub_path = fields["aggregate_sub_path"]

    row.updated_at = bj_now()
    await db.commit()
    await db.refresh(row)
    return row


async def delete_task(db: Any, task_id: int) -> None:
    """删除任务（先停监听，再删行）。"""
    await get_task(db, task_id)
    await stop_task(task_id)
    row = await get_task(db, task_id)
    await db.delete(row)
    await db.commit()
    logger.info("删除上传任务 id=%s", task_id)


# --------------------------------------------------------------------------
# 对外函数（自开事务）
#
# **API 层不碰 DB** —— 路由层只调这些。带 ``db`` 参数的 CRUD 是**内部**
# 函数（供其它 service 复用）。
# --------------------------------------------------------------------------


async def list_task_views() -> list[UploadTaskView]:
    """列出所有上传任务（附运行态）。"""
    async with session_scope() as db:
        rows = (
            await db.execute(select(UploadTaskRow).order_by(UploadTaskRow.id))
        ).scalars().all()
        return [to_task_view(r) for r in rows]


async def get_task_view(task_id: int) -> UploadTaskView:
    """取单个任务（附运行态）。"""
    async with session_scope() as db:
        row = await get_task(db, task_id)
        return to_task_view(row)


async def create_task_view(**kwargs: Any) -> UploadTaskView:
    """创建上传任务（自开事务）。"""
    async with session_scope() as db:
        row = await create_task(db, **kwargs)
        return to_task_view(row)


async def update_task_view(task_id: int, **fields: Any) -> UploadTaskView:
    """修改任务（自开事务）。"""
    async with session_scope() as db:
        row = await update_task(db, task_id, **fields)
        return to_task_view(row)


async def delete_task_view(task_id: int) -> None:
    """删除任务（自开事务）。"""
    async with session_scope() as db:
        await delete_task(db, task_id)


async def upload_capabilities() -> dict[str, Any]:
    """本环境**支持上传的存储源类型**。

    前端用它过滤「目标网盘」下拉——只列支持上传的。
    **加新网盘时前端零改动**：后端在
    :mod:`app.adapters.drivers.upload_base` 的 ``UPLOADABLE_DRIVER_TYPES``
    里登记即可。

    （API 层不直接 import 适配层——分层约束见 ``test_layering.py``，
    故此函数住在 service 层。）
    """
    from app.adapters.drivers.upload_base import UPLOADABLE_DRIVER_TYPES

    return {"uploadable_types": sorted(UPLOADABLE_DRIVER_TYPES)}


async def aggregate_targets() -> dict[str, Any]:
    """「本地同步到网盘聚合」的**可选目标**（前端下拉用）。

    返回**可用**的聚合组：启用 + 有主盘 + 已配归拢目标目录 + 至少一个
    可用副盘（启用、已配缓冲根、支持上传、本月额度未耗尽）。

    每个组带各副盘的状态（缓冲根、剩余空间、额度标记、测连状态）——
    前端据此展示「自动分配会落到哪」。
    """
    from app.db.models import AggregateGroup, Driver
    from app.domain.aggregate_types import ROLE_MASTER
    from app.services import aggregate_upload
    from app.services.errors import ServiceError

    async with session_scope() as db:
        groups = (
            (await db.execute(select(AggregateGroup).order_by(AggregateGroup.id)))
            .scalars()
            .all()
        )
        masters = (
            (
                await db.execute(
                    select(Driver).where(Driver.role == ROLE_MASTER)
                )
            )
            .scalars()
            .all()
        )
    master_names = {int(m.id): str(m.name) for m in masters}

    items: list[dict[str, Any]] = []
    for group in groups:
        try:
            info = await aggregate_upload.validate_group_target(int(group.id))
        except ServiceError as exc:
            # **只吞「组不可用」**——数据库故障 / KeyError 等**真实缺陷**
            # 必须冒泡，不能被静默 continue 掩盖成「这个组不可用」。
            logger.info("聚合组 %s 不可用作同步目标：%s", group.id, exc)
            continue
        items.append(
            {
                "group_id": info["group_id"],
                "group_name": info["group_name"],
                "master_driver_id": info["master_driver_id"],
                "master_driver_name": master_names.get(
                    info["master_driver_id"], ""
                ),
                "target_dir_path": group.target_dir_path,
                # 主盘归拢目标目录的 fileId——前端用它作为「目标子目录」
                # 浏览器的**起点**（子目录是它下面的相对路径）
                "target_dir_id": group.target_dir_id,
                "candidates": [
                    cand.to_dict() for cand in info["candidates"]
                ],
            }
        )
    return {"groups": items}


async def browse_target_dirs(driver_id: int, parent_id: str = "") -> list[tuple[str, str]]:
    """列出目标网盘目录下的子目录（按 id 逐层浏览）。

    返回 ``[(remote_id, name), ...]``——只含目录（文件不进目录浏览器）。

    部分网盘的目录**没有「路径 ↔ id」反查**（接口只接受 id），故只能
    逐层往下走。

    Raises:
        UploadTaskError: 存储源不存在（404）/ 不支持上传（400）/ 驱动不支持
            按 id 浏览（400）/ 上游调用失败（400）。
    """
    from app.adapters.drivers.base import DriverError
    from app.adapters.drivers.upload_base import is_uploadable
    from app.db.models import Driver
    from app.services.driver_factory import build_driver
    from app.services.driver_service import persist_guangya_credential

    async with session_scope() as db:
        driver_row = (
            await db.execute(select(Driver).where(Driver.id == driver_id))
        ).scalar_one_or_none()
        if driver_row is None:
            raise UploadTaskError(f"存储源不存在：{driver_id}", status_code=404, kind="not_found")
        if not is_uploadable(driver_row.type):
            raise UploadTaskError(
                f"该存储源不支持上传（当前：{driver_row.type}）",
                status_code=400,
                kind="config",
            )
        credential = driver_row.credential
        row_id = driver_row.id

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(row_id, cred)

    driver = build_driver(driver_row.type, credential, on_token_refresh=_on_refresh)
    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        raise UploadTaskError("该驱动不支持按 id 浏览目录", status_code=400, kind="config")
    try:
        entries = await lister(parent_id)
    except DriverError as exc:
        raise UploadTaskError(str(exc), status_code=400, kind="upstream") from exc
    return [(e.remote_id, e.name) for e in entries if e.is_dir]


async def sync_task_now(task_id: int) -> dict[str, Any]:
    """立即跑一次全量同步（补偿事件流漏掉的文件）。

    **异常转换**：service 层把适配层的 :class:`DriverError` 与路径守卫的
    :class:`PathGuardError` 统一转成 :class:`UploadTaskError`——API 层
    不认识适配层的异常类型（分层约束）。
    """
    from app.adapters.drivers.base import DriverError
    from app.services import watch_manager
    from app.services.upload_paths import PathGuardError

    try:
        result: dict[str, Any] = await watch_manager.sync_now(task_id)
        return result
    except DriverError as exc:
        raise UploadTaskError(str(exc), status_code=400, kind="upstream") from exc
    except PathGuardError as exc:
        raise UploadTaskError(str(exc), status_code=400, kind="invalid") from exc


def _validate_watch_dir(watch_root: str) -> Any:
    """校验监听目录（绝对路径 + 存在）。

    Raises:
        UploadTaskError: 路径非法。
    """
    try:
        return resolve_watch_dir(watch_root)
    except PathGuardError as exc:
        raise UploadTaskError(str(exc), status_code=400, kind="config") from exc


async def stop_task(task_id: int) -> None:
    """停止某个任务的监听（不断任务本身）。"""
    from app.services.watch_manager import stop_watcher

    await stop_watcher(task_id)
    runtime = get_runtime(task_id)
    runtime.listening = False


__all__ = [
    "MAX_LOGS",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "UploadRuntime",
    "UploadTaskError",
    "all_runtimes",
    "clear_all",
    "create_task",
    "delete_task",
    "get_runtime",
    "get_task",
    "list_tasks",
    "stop_task",
    "update_task",
]
