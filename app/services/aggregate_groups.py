"""聚合组与存储源角色管理（services 层）。

## 职责

- 聚合组的增删改查
- **把存储源指派为组内的主 / 副数据源**（含「干净状态」校验）

## 为什么角色切换要校验

角色不是随手可改的标签，它决定了**数据往哪走**：

- 主 → 副：该盘不再是真源。若它还挂着媒体库条目（``media_files``），
  那些条目与本地 STRM 会变成指向副盘的**死引用**（副盘内容不参与媒体库）
  → 必须先清理，故拦住。
- 副 → 主：台账里指向该副盘的未消化行会与新角色矛盾 → 必须先消化。

**校验在后端**（不是前端提示）——绕过前端直接打 API 也拦得住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AggregateGroup,
    AggregateGroupRoot,
    Driver,
    MediaFile,
    SecondaryStaging,
)
from app.db.session import session_scope
from app.domain.aggregate_types import (
    ROLE_LABELS,
    ROLE_MASTER,
    ROLE_SECONDARY,
    STAGING_TERMINAL,
)
from app.services.errors import ServiceError

__all__ = [
    "AggregateGroupRow",
    "GroupMapping",
    "GroupRootSpec",
    "assign_driver_role",
    "clear_driver_role",
    "create_group",
    "delete_group",
    "get_group",
    "get_mapping",
    "list_groups",
    "set_mapping",
    "update_group",
]

#: 参与聚合的驱动类型——**当前只支持光鸭**。
#:
#: 副 → 主的唯一传输路线是光鸭「云下载」（服务端内部复制，不吃本机带宽）；
#: 跨网盘类型（如 115 → 光鸭）需要另建通道，且 115 源没有删除能力，
#: 「归拢完清副盘」那一步做不了——故第一版只做光鸭。
SUPPORTED_AGGREGATE_TYPES = frozenset({"guangya"})


@dataclass(slots=True)
class AggregateGroupRow:
    """一个聚合组（供 API 组装响应）。"""

    id: int
    name: str
    enabled: bool
    created_at: str
    updated_at: str
    #: 归拢目标目录 id（主盘上的目录；None = 未配置）
    target_dir_id: str | None = None
    #: 归拢目标目录路径（只读展示）
    target_dir_path: str | None = None


@dataclass(slots=True)
class GroupRootSpec:
    """一个副盘的缓冲根（组 ↔ 副盘 的映射项）。"""

    driver_id: int
    root_id: str
    root_path: str | None = None


@dataclass(slots=True)
class GroupMapping:
    """一个聚合组的**完整目录映射**。

    语义：**从哪些副盘的哪些目录，搬到主盘的哪个目录**。
    两端都是组级配置——不借存储源的媒体库配置。
    """

    group_id: int
    #: 主盘上的归拢目标目录（None = 未配置）
    target_dir_id: str | None = None
    target_dir_path: str | None = None
    #: 各副盘的缓冲根
    roots: list[GroupRootSpec] = field(default_factory=list)


def _to_group_row(row: AggregateGroup) -> AggregateGroupRow:
    return AggregateGroupRow(
        id=row.id,
        name=row.name,
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
        target_dir_id=row.target_dir_id,
        target_dir_path=row.target_dir_path,
    )


async def list_groups() -> list[AggregateGroupRow]:
    """列出全部聚合组（按 id 升序）。"""
    async with session_scope() as db:
        rows = (
            (await db.execute(select(AggregateGroup).order_by(AggregateGroup.id)))
            .scalars()
            .all()
        )
        return [_to_group_row(r) for r in rows]


async def get_group(group_id: int) -> AggregateGroupRow:
    """取一个聚合组（不存在 → 404）。"""
    async with session_scope() as db:
        row = await _get_group_or_404(db, group_id)
        return _to_group_row(row)


async def create_group(name: str) -> AggregateGroupRow:
    """新建聚合组（组内暂时没有盘——先建组再指派）。"""
    name = (name or "").strip()
    if not name:
        raise ServiceError("聚合组名称不能为空", status=400)
    async with session_scope() as db:
        row = AggregateGroup(name=name)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_group_row(row)


async def update_group(
    group_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
) -> AggregateGroupRow:
    """改聚合组（重命名 / 启停）。

    **停用不删数据**：组内的盘与台账都留着，只是归拢调度不再处理这个组。
    """
    async with session_scope() as db:
        row = await _get_group_or_404(db, group_id)
        if name is not None:
            clean = name.strip()
            if not clean:
                raise ServiceError("聚合组名称不能为空", status=400)
            row.name = clean
        if enabled is not None:
            row.enabled = enabled
        await db.commit()
        await db.refresh(row)
        return _to_group_row(row)


async def delete_group(group_id: int) -> None:
    """解散聚合组。

    **两道闸**（都是「不误删」底线）：

    1. 组内还有盘 → 拒绝（先把盘移出组，或直接删盘）
    2. 组内还有**未消化**的台账行 → 拒绝（台账指向具体盘，组没了就失去归属）
    """
    async with session_scope() as db:
        await _get_group_or_404(db, group_id)

        drivers = await _group_driver_ids(db, group_id)
        if drivers:
            names = await _driver_names(db, drivers)
            raise ServiceError(
                f"该组仍有 {len(drivers)} 个存储源（{'、'.join(names)}），"
                "请先把它们移出组或删除",
                status=409,
            )

        pending = await _pending_staging_count(db, group_id)
        if pending:
            raise ServiceError(
                f"该组还有 {pending} 个待归拢文件，请先消化完再解散",
                status=409,
            )

        await db.execute(delete(SecondaryStaging).where(SecondaryStaging.group_id == group_id))
        await db.execute(delete(AggregateGroup).where(AggregateGroup.id == group_id))
        await db.commit()


async def get_mapping(group_id: int) -> GroupMapping:
    """取一个组的**完整目录映射**（主盘落点 + 各副盘缓冲根）。"""
    async with session_scope() as db:
        group = await _get_group_or_404(db, group_id)
        roots = (
            (
                await db.execute(
                    select(AggregateGroupRoot)
                    .where(AggregateGroupRoot.group_id == group_id)
                    .order_by(AggregateGroupRoot.driver_id)
                )
            )
            .scalars()
            .all()
        )
        return GroupMapping(
            group_id=group.id,
            target_dir_id=group.target_dir_id,
            target_dir_path=group.target_dir_path,
            roots=[
                GroupRootSpec(
                    driver_id=int(r.driver_id),
                    root_id=str(r.root_id),
                    root_path=r.root_path,
                )
                for r in roots
            ],
        )


async def set_mapping(
    group_id: int,
    *,
    target_dir_id: str | None,
    target_dir_path: str | None = None,
    roots: list[GroupRootSpec] | None = None,
) -> GroupMapping:
    """设置一个组的**目录映射**（一次提交整组映射）。

    校验：

    - 主盘落点：留空 = 不配置（归拢作业会**明确跳过**并把原因写进明细）
    - 副盘缓冲根：只接受**组内副数据源**（组外的盘给了也不认）
    - 同一副盘只能有一条（表上有 UNIQUE 约束）

    Args:
        group_id: 聚合组 id。
        target_dir_id: 主盘上的归拢目标目录 id（None/空 = 清除）。
        target_dir_path: 目标目录路径（只读展示）。
        roots: 各副盘的缓冲根（None = 不改动副盘侧）。
    """
    async with session_scope() as db:
        group = await _get_group_or_404(db, group_id)

        # 组内成员（校验副盘身份用）
        members = (
            (
                await db.execute(
                    select(Driver).where(Driver.aggregate_group_id == group_id)
                )
            )
            .scalars()
            .all()
        )
        secondary_ids = {
            int(d.id) for d in members if d.role == ROLE_SECONDARY
        }

        group.target_dir_id = target_dir_id or None
        group.target_dir_path = target_dir_path or None

        if roots is not None:
            # 全量替换该组的缓冲根（未列出的副盘 = 清掉）
            wanted = {int(r.driver_id) for r in roots}
            outside = wanted - secondary_ids
            if outside:
                raise ServiceError(
                    f"存储源 {sorted(outside)} 不是该组的副数据源，无法设置缓冲根",
                    status=400,
                )
            for r in roots:
                if not r.root_id:
                    continue
                existing = (
                    await db.execute(
                        select(AggregateGroupRoot).where(
                            AggregateGroupRoot.group_id == group_id,
                            AggregateGroupRoot.driver_id == int(r.driver_id),
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(
                        AggregateGroupRoot(
                            group_id=group_id,
                            driver_id=int(r.driver_id),
                            root_id=r.root_id,
                            root_path=r.root_path,
                        )
                    )
                else:
                    existing.root_id = r.root_id
                    existing.root_path = r.root_path
            # 清掉没在本次提交里的旧映射
            stale = (
                (
                    await db.execute(
                        select(AggregateGroupRoot).where(
                            AggregateGroupRoot.group_id == group_id,
                            AggregateGroupRoot.driver_id.notin_(tuple(wanted) or (0,)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in stale:
                await db.delete(row)

        await db.commit()
        await db.refresh(group)

    return await get_mapping(group_id)


async def assign_driver_role(
    driver_id: int,
    *,
    group_id: int,
    role: str,
) -> None:
    """把存储源指派为组内的主 / 副数据源。

    Raises:
        ServiceError: 存储源 / 组不存在（404）、类型不支持（400）、
            组内已有主数据源（409）、盘不干净（409）。
    """
    if role not in (ROLE_MASTER, ROLE_SECONDARY):
        raise ServiceError(f"未知的角色：{role}", status=400)

    async with session_scope() as db:
        driver = await _get_driver_or_404(db, driver_id)
        await _get_group_or_404(db, group_id)

        if driver.type not in SUPPORTED_AGGREGATE_TYPES:
            raise ServiceError(
                f"当前只支持光鸭盘参与聚合（该存储源类型：{driver.type}）",
                status=400,
            )
        if not driver.enabled:
            raise ServiceError("该存储源已停用，请先启用", status=400)

        # ① 组内唯一主：已有主盘就拒绝（不静默改别人的角色）
        if role == ROLE_MASTER:
            others = (
                (
                    await db.execute(
                        select(Driver).where(
                            Driver.aggregate_group_id == group_id,
                            Driver.role == ROLE_MASTER,
                            Driver.id != driver_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if others:
                raise ServiceError(
                    f"该组已有主数据源（{'、'.join(o.name for o in others)}），"
                    "请先把它改为副数据源",
                    status=409,
                )

        # ② 干净状态：主 → 副 时不能带着媒体库条目走
        if role == ROLE_SECONDARY and driver.role == ROLE_MASTER:
            media_count = await _media_file_count(db, driver_id)
            if media_count:
                raise ServiceError(
                    f"该存储源有 {media_count} 条媒体库记录，"
                    "降级为副数据源前请先清理（否则会留下指向副盘的失效条目）",
                    status=409,
                )

        # ③ 干净状态：该盘还有未消化的台账行 → 角色变更会与台账矛盾
        pending = await _driver_pending_staging_count(db, driver_id)
        if pending:
            raise ServiceError(
                f"该存储源还有 {pending} 个待归拢文件，请先消化完再变更角色",
                status=409,
            )

        driver.aggregate_group_id = group_id
        driver.role = role
        await db.commit()


async def clear_driver_role(driver_id: int) -> None:
    """把存储源移出聚合组（恢复为独立存储源）。"""
    async with session_scope() as db:
        driver = await _get_driver_or_404(db, driver_id)
        if driver.role is None:
            return
        if driver.role == ROLE_MASTER:
            media_count = await _media_file_count(db, driver_id)
            if media_count:
                raise ServiceError(
                    f"该存储源有 {media_count} 条媒体库记录，"
                    "移出聚合组前请先清理",
                    status=409,
                )
        pending = await _driver_pending_staging_count(db, driver_id)
        if pending:
            raise ServiceError(
                f"该存储源还有 {pending} 个待归拢文件，请先消化完再移出聚合组",
                status=409,
            )
        driver.aggregate_group_id = None
        driver.role = None
        await db.commit()


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------


async def _get_group_or_404(db: AsyncSession, group_id: int) -> AggregateGroup:
    row = (
        await db.execute(select(AggregateGroup).where(AggregateGroup.id == group_id))
    ).scalar_one_or_none()
    if row is None:
        raise ServiceError(f"聚合组不存在：{group_id}", status=404)
    return row


async def _get_driver_or_404(db: AsyncSession, driver_id: int) -> Driver:
    row = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).scalar_one_or_none()
    if row is None:
        raise ServiceError(f"存储源不存在：{driver_id}", status=404)
    return row


async def _group_driver_ids(db: AsyncSession, group_id: int) -> list[int]:
    rows = (
        (
            await db.execute(
                select(Driver.id).where(Driver.aggregate_group_id == group_id)
            )
        )
        .scalars()
        .all()
    )
    return [int(r) for r in rows]


async def _driver_names(db: AsyncSession, driver_ids: list[int]) -> list[str]:
    if not driver_ids:
        return []
    rows = (
        (
            await db.execute(select(Driver.name).where(Driver.id.in_(driver_ids)))
        )
        .scalars()
        .all()
    )
    return [str(r) for r in rows]


async def _media_file_count(db: AsyncSession, driver_id: int) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(MediaFile)
                .where(MediaFile.driver_id == driver_id)
            )
        ).scalar_one()
    )


async def _pending_staging_count(db: AsyncSession, group_id: int) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(SecondaryStaging)
                .where(
                    SecondaryStaging.group_id == group_id,
                    SecondaryStaging.state.notin_(tuple(STAGING_TERMINAL)),
                )
            )
        ).scalar_one()
    )


async def _driver_pending_staging_count(db: AsyncSession, driver_id: int) -> int:
    """该盘（作为源**或**目标）还有多少未消化台账。"""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(SecondaryStaging)
                .where(
                    SecondaryStaging.state.notin_(tuple(STAGING_TERMINAL)),
                    (
                        (SecondaryStaging.source_driver_id == driver_id)
                        | (SecondaryStaging.target_driver_id == driver_id)
                    ),
                )
            )
        ).scalar_one()
    )


def role_label(role: str | None) -> str:
    """角色的展示名（``None`` = 独立存储源）。"""
    if not role:
        return "独立存储源"
    return ROLE_LABELS.get(role, role)


def is_secondary(driver: Any) -> bool:
    """该存储源是否为副数据源。"""
    return getattr(driver, "role", None) == ROLE_SECONDARY
