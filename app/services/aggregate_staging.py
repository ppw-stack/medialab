"""副盘在途台账（services 层）：登记 / 查询 / 统计。

## 台账是什么

副数据源是**缓冲**：主盘额度用尽时新文件先落副盘，随后由归拢引擎搬回主盘。
「哪些文件在路上」就是台账（``secondary_staging`` 表）。

**副盘内容永不对外展示**——台账是「在路上」的唯一可见形态。

## 登记方式

副盘的**缓冲根目录**（``drivers.staging_root_id``）里的文件会被登记进台账，
目标路径 = 主盘媒体库目录下的**同名相对路径**。

登记是**幂等**的（``(source_driver_id, remote_id)`` 唯一约束）：同一个文件
重复扫描不会产生第二行——重复登记会让归拢引擎搬两次、白扣两次云添加额度。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.timeutils import bj_now
from app.db.models import (
    AggregateGroup,
    AggregateGroupRoot,
    Driver,
    SecondaryStaging,
)
from app.db.session import session_scope
from app.domain.aggregate_types import (
    ROLE_MASTER,
    ROLE_SECONDARY,
    STAGING_DONE,
    STAGING_PENDING,
    STAGING_STATES,
    STAGING_TERMINAL,
)
from app.services.driver_factory import build_driver
from app.services.driver_service import guangya_refresh_callback
from app.services.errors import ServiceError

__all__ = [
    "StagedEntry",
    "StagingRow",
    "StagingStats",
    "delete_staging",
    "enqueue_from_staging_root",
    "group_stats",
    "list_staging",
    "load_staged_index",
    "register_uploaded_file",
    "retry_staging",
]

#: 组级幂等索引的**加载上限**（行）。
#:
#: 台账是「已入缓冲 / 已交付」的记录——上传路径靠它判断「这个文件是不是
#: 已经传过了」（落点会换盘，不能只看当前落点目录）。台账会随时间增长，
#: 故设上限；超限时**只加载最近的**（老文件的记录查不到 → 会被重传
#: 一次并重新登记，自愈）。
_STAGED_INDEX_LIMIT = 50000


@dataclass(slots=True)
class StagingRow:
    """一条在途台账。"""

    id: int
    group_id: int
    source_driver_id: int
    target_driver_id: int
    remote_id: str
    file_name: str
    source_dir: str
    target_dir: str
    size_bytes: int
    state: str
    #: 搬运通道（``cloud_download`` / ``share_transfer``；None = 未搬）
    transfer_mode: str | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    synced_at: str | None
    cleaned_at: str | None


@dataclass(slots=True)
class StagingStats:
    """一个聚合组的台账水位。"""

    group_id: int
    group_name: str
    #: 主数据源 id（None = 组还没配主盘）
    master_driver_id: int | None
    #: 副数据源 id 列表
    secondary_driver_ids: list[int]
    #: 各状态的条数
    counts: dict[str, int]
    #: 待归拢字节数（不含已完成）
    pending_bytes: int
    #: 各状态的字节数——与 ``counts`` **同口径**（同一批 group by 产出）。
    #: 前端「失败 / 暂停」栏靠它显示字节，与行数不再错位
    #: （2026-11-18：水位「个数」与「字节」曾口径不一致——字节含 failed、
    #: 个数不含，页面读起来像「142G 只登记 1 个文件」）
    bytes_by_state: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "master_driver_id": self.master_driver_id,
            "secondary_driver_ids": list(self.secondary_driver_ids),
            "counts": dict(self.counts),
            "pending_bytes": self.pending_bytes,
            "bytes_by_state": dict(self.bytes_by_state),
        }


def _to_row(row: SecondaryStaging) -> StagingRow:
    return StagingRow(
        id=row.id,
        group_id=row.group_id,
        source_driver_id=row.source_driver_id,
        target_driver_id=row.target_driver_id,
        remote_id=row.remote_id,
        file_name=row.file_name,
        source_dir=row.source_dir,
        target_dir=row.target_dir,
        size_bytes=int(row.size_bytes),
        state=row.state,
        transfer_mode=row.transfer_mode,
        attempts=int(row.attempts),
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        synced_at=row.synced_at,
        cleaned_at=row.cleaned_at,
    )


@dataclass(slots=True)
class StagedEntry:
    """组内「已入缓冲 / 已交付」的一条记录（幂等索引的条目）。"""

    #: 字节数（登记时的文件大小）
    size_bytes: int
    #: 云端 fileId（缓冲盘上的）
    file_id: str
    #: 文件当前所在（或曾所在）的副盘 id
    driver_id: int
    #: 台账状态
    state: str

    @property
    def note(self) -> str:
        """人类可读的说明（用于日志 / 提示）。"""
        return f"副盘 {self.driver_id}（{self.state}）"


async def register_uploaded_file(
    *,
    group_id: int,
    source_driver_id: int,
    remote_id: str,
    file_name: str,
    rel_dir: str,
    size_bytes: int,
) -> str | None:
    """「本地同步 → 网盘聚合」上传成功后**登记台账**（幂等）。

    这是数据同步与网盘聚合之间的**联动点**：文件落到副盘缓冲根后立刻
    进台账，归拢引擎下一轮就能把它搬回主盘——不必等每小时的兜底扫描。

    幂等：``(source_driver_id, remote_id)`` 唯一约束——同一个文件重复
    登记不会产生第二行（重复登记 = 归拢搬两次 = 白扣两次云添加额度）。

    Args:
        group_id: 聚合组 id。
        source_driver_id: 文件所在的副盘 id。
        remote_id: 云端 fileId。
        file_name: 文件名（含扩展名）。
        rel_dir: 相对缓冲根的目录（不含文件名）。
        size_bytes: 文件字节数。

    Returns:
        ``"added"``（新登记）/ ``"exists"``（已登记过）/ ``None``
        （组没了或没有主盘 → 不该登记）。

    Raises:
        sqlalchemy.exc.OperationalError: 撞 SQLite 写锁——**向上抛**，
            由调用方降级（登记是可丢的：兜底扫描会补上）。
    """
    if not remote_id:
        return None
    async with session_scope() as db:
        master_id = (
            (
                await db.execute(
                    select(Driver.id).where(
                        Driver.aggregate_group_id == group_id,
                        Driver.role == ROLE_MASTER,
                    )
                )
            )
            .scalars()
            .first()
        )
        if master_id is None:
            return None
        db.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=source_driver_id,
                target_driver_id=int(master_id),
                remote_id=remote_id,
                file_name=file_name,
                source_dir=rel_dir,
                target_dir=rel_dir,
                size_bytes=int(size_bytes),
                state=STAGING_PENDING,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # 唯一约束冲突 = 已登记过（幂等）
            await db.rollback()
            return "exists"
    return "added"


async def load_staged_index(group_id: int) -> dict[tuple[str, str], StagedEntry]:
    """组内「已入缓冲 / 已交付」的**幂等索引**。

    键 = ``(相对目录, 文件名)``，值 = :class:`StagedEntry`。

    **为什么需要**：落点会**自动换盘**（撞到月度上传限额就换下一个），
    换盘后「当前落点目录里没有这个文件」≠「文件没传过」——它可能在
    **另一个副盘**的缓冲里，或**已经归拢到主盘**。没有这层探测就会
    重传（白耗本机上行带宽与云添加额度）。

    包含**终态（done）**的行：done = 文件已在主盘、副盘副本已清——
    本地那份是冗余，不该重传。

    ⚠️ 副作用：文件若在主盘上被**手动删掉**，台账还在 → 不会自动重传
    （需手动删掉对应台账行）。
    """
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(SecondaryStaging)
                    .where(SecondaryStaging.group_id == group_id)
                    .order_by(SecondaryStaging.id.desc())
                    .limit(_STAGED_INDEX_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    index: dict[tuple[str, str], StagedEntry] = {}
    for row in rows:
        key = (str(row.source_dir or ""), str(row.file_name))
        # 同一路径可能有多行（同名不同版本的多次登记）——**保留最新的**
        # （上面按 id 倒序取，第一个写入的就是最新的）。
        index.setdefault(
            key,
            StagedEntry(
                size_bytes=int(row.size_bytes),
                file_id=str(row.remote_id),
                driver_id=int(row.source_driver_id),
                state=str(row.state),
            ),
        )
    return index


# --------------------------------------------------------------------------
# 登记
# --------------------------------------------------------------------------


async def enqueue_from_staging_root(group_id: int) -> dict[str, int]:
    """扫描组内各副盘的**缓冲根目录**，把里面的文件登记进台账。

    幂等：已登记过的文件（``source_driver_id`` + ``remote_id``）不会重复登记。

    Returns:
        ``{"scanned": 扫描到的文件数, "added": 新登记数, "skipped": 已存在数}``
    """
    async with session_scope() as db:
        group = (
            await db.execute(
                select(AggregateGroup).where(AggregateGroup.id == group_id)
            )
        ).scalar_one_or_none()
        if group is None:
            raise ServiceError(f"聚合组不存在：{group_id}", status=404)

        master = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.aggregate_group_id == group_id,
                        Driver.role == ROLE_MASTER,
                    )
                )
            )
            .scalars()
            .first()
        )
        if master is None:
            raise ServiceError("该组还没有主数据源，无法登记归拢任务", status=400)
        # 归拢落点是**组自己的配置**（不是主盘的媒体库目录）
        if not group.target_dir_id:
            raise ServiceError(
                "该组未配置「归拢目标目录」，无处可放——请在「网盘聚合」页配置",
                status=400,
            )

        secondaries = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.aggregate_group_id == group_id,
                        Driver.role == ROLE_SECONDARY,
                    )
                )
            )
            .scalars()
            .all()
        )
        # 缓冲根来自**组级映射表**（aggregate_group_roots）——
        # 聚合的目录映射两端都属组，不挂在存储源上
        roots = (
            (
                await db.execute(
                    select(AggregateGroupRoot).where(
                        AggregateGroupRoot.group_id == group_id
                    )
                )
            )
            .scalars()
            .all()
        )
        root_by_driver = {int(r.driver_id): str(r.root_id) for r in roots}

        master_id = int(master.id)
        secondary_specs = [
            (int(s.id), root_by_driver[int(s.id)], s.credential)
            for s in secondaries
            if s.enabled and int(s.id) in root_by_driver
        ]

    scanned = 0
    added = 0
    skipped = 0
    for driver_id, root_id, credential in secondary_specs:
        entries = await _list_staging_root_files(
            driver_id, root_id, credential
        )
        scanned += len(entries)
        if not entries:
            continue
        async with session_scope() as db:
            # 一次查出该盘已登记的 remote_id（**别逐文件查**——缓冲根目录
            # 几百个文件就是几百条 SELECT 的 N+1）
            existing_ids: set[str] = set(
                (
                    await db.execute(
                        select(SecondaryStaging.remote_id).where(
                            SecondaryStaging.source_driver_id == driver_id,
                            SecondaryStaging.remote_id.in_(
                                [e["remote_id"] for e in entries]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for entry in entries:
                if entry["remote_id"] in existing_ids:
                    skipped += 1
                    continue
                db.add(
                    SecondaryStaging(
                        group_id=group_id,
                        source_driver_id=driver_id,
                        target_driver_id=master_id,
                        remote_id=entry["remote_id"],
                        file_name=entry["name"],
                        source_dir=entry["rel_dir"],
                        target_dir=entry["rel_dir"],
                        size_bytes=entry["size"],
                        state=STAGING_PENDING,
                    )
                )
                added += 1
            await db.commit()
    return {"scanned": scanned, "added": added, "skipped": skipped}


async def _list_staging_root_files(
    driver_id: int,
    root_id: str,
    credential: str | None,
) -> list[dict[str, Any]]:
    """列副盘缓冲根目录下的文件（**递归**，保留相对目录结构）。

    目录结构必须保留——媒体库按「剧名 / Season N / 集」组织，归拢时压平
    会让 Emby 认不出剧集。

    光鸭的 ``iter_tree_by_cid`` 走全账号批量列表（约 16 个请求），比逐目录
    递归快两个数量级；驱动不支持时退回「只取当前层」（顶层文件）。
    """
    driver = build_driver(
        "guangya",
        credential,
        on_token_refresh=guangya_refresh_callback(driver_id),
    )
    entries: list[dict[str, Any]] = []
    iterator = getattr(driver, "iter_tree_by_cid", None)
    if iterator is not None:
        async for entry in iterator(root_id, parent_path="", include_subdirs=True):
            if entry.is_dir:
                continue
            rel = (entry.relative_dir or "").strip("/")
            entries.append(
                {
                    "remote_id": str(entry.remote_id),
                    "name": entry.name,
                    "size": int(entry.size),
                    # 相对缓冲根目录的路径（不含文件名）
                    "rel_dir": rel,
                }
            )
        return entries

    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        raise ServiceError("该驱动不支持列目录", status=400)
    for entry in await lister(root_id):
        if entry.is_dir:
            continue
        entries.append(
            {
                "remote_id": str(entry.remote_id),
                "name": entry.name,
                "size": int(entry.size),
                "rel_dir": "",
            }
        )
    return entries


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


async def list_staging(
    *,
    group_id: int | None = None,
    state: str | None = None,
    limit: int = 200,
) -> list[StagingRow]:
    """列台账（按创建时间倒序）。"""
    if state is not None and state not in STAGING_STATES:
        raise ServiceError(f"未知的台账状态：{state}", status=400)
    async with session_scope() as db:
        stmt = select(SecondaryStaging).order_by(
            SecondaryStaging.id.desc()
        ).limit(max(1, min(limit, 1000)))
        if group_id is not None:
            stmt = stmt.where(SecondaryStaging.group_id == group_id)
        if state is not None:
            stmt = stmt.where(SecondaryStaging.state == state)
        rows = (await db.execute(stmt)).scalars().all()
        return [_to_row(r) for r in rows]


async def group_stats() -> list[StagingStats]:
    """各聚合组的台账水位 + 主盘额度。"""
    async with session_scope() as db:
        groups = (
            (await db.execute(select(AggregateGroup).order_by(AggregateGroup.id)))
            .scalars()
            .all()
        )
        result: list[StagingStats] = []
        for group in groups:
            drivers = (
                (
                    await db.execute(
                        select(Driver).where(
                            Driver.aggregate_group_id == group.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            master = next((d for d in drivers if d.role == ROLE_MASTER), None)
            secondaries = [
                int(d.id) for d in drivers if d.role == ROLE_SECONDARY
            ]

            rows = (
                (
                    await db.execute(
                        select(
                            SecondaryStaging.state,
                            func.count(),
                            func.coalesce(func.sum(SecondaryStaging.size_bytes), 0),
                        )
                        .where(SecondaryStaging.group_id == group.id)
                        .group_by(SecondaryStaging.state)
                    )
                )
                .all()
            )
            # 条数与字节数来自**同一次 group by**——两份口径天然一致
            counts = {str(state): int(cnt) for state, cnt, _ in rows}
            bytes_by_state = {
                str(state): int(total) for state, _, total in rows
            }

            pending_bytes = int(
                (
                    await db.execute(
                        select(func.coalesce(func.sum(SecondaryStaging.size_bytes), 0)).where(
                            SecondaryStaging.group_id == group.id,
                            SecondaryStaging.state.notin_(tuple(STAGING_TERMINAL)),
                        )
                    )
                ).scalar_one()
            )

            result.append(
                StagingStats(
                    group_id=group.id,
                    group_name=group.name,
                    master_driver_id=int(master.id) if master is not None else None,
                    secondary_driver_ids=secondaries,
                    counts=counts,
                    pending_bytes=pending_bytes,
                    bytes_by_state=bytes_by_state,
                )
            )
    return result


# --------------------------------------------------------------------------
# 人工干预
# --------------------------------------------------------------------------


async def retry_staging(staging_id: int) -> None:
    """把失败 / 暂停的行重置为「待归拢」（人工重试）。"""
    async with session_scope() as db:
        row = (
            await db.execute(
                select(SecondaryStaging).where(SecondaryStaging.id == staging_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError(f"台账行不存在：{staging_id}", status=404)
        if row.state == STAGING_DONE:
            raise ServiceError("该行已完成，无需重试", status=400)
        row.state = STAGING_PENDING
        row.last_error = None
        row.cloud_task_id = None
        row.updated_at = bj_now()
        await db.commit()


async def delete_staging(staging_id: int) -> None:
    """删除一条台账（**不动网盘上的文件**——只删记录）。

    副盘文件本身要删请走网盘客户端；这里删的只是「待归拢」的登记。
    """
    async with session_scope() as db:
        row = (
            await db.execute(
                select(SecondaryStaging).where(SecondaryStaging.id == staging_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError(f"台账行不存在：{staging_id}", status=404)
        await db.delete(row)
        await db.commit()
