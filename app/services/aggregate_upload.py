"""「本地同步 → 网盘聚合」的**落点分配器**与额度墙判定。

## 为什么需要这一层

「数据同步」原本的目标是**一个具体存储源**（``upload_tasks.driver_id`` +
``target_file_id``）。本模块让目标可以是**一个聚合组**——落点由系统
在组内副盘之间自动挑选，上传成功后**即时登记台账**，归拢引擎（现有，
零改动）接管后续搬运。

## 落点分配规则

- **粘性**：当前落点持久化在 ``upload_tasks.driver_id`` /
  ``target_file_id``——**重启后恢复**。这是防重传的关键：落点会换盘，
  若重启后换了盘，「当前落点目录里没有这个文件」≠「文件没传过」。
- **固定顺序**：按副盘 ``driver id`` 升序。**不排序**——不按用量 /
  空间动态排序（用户明确要求）。
- **闸门**（只过滤，不排序）：
  ① 本月上传额度未耗尽（撞墙标记）
  ② 测连缓存健康（**已知时**才判；没探过不拦）
  ③ 剩余空间 ≥ 文件大小（**已知时**才判；没探过不拦）

## 月度上传限额：撞墙探测

光鸭的「每月上传限额」**没有查询接口**——只能撞墙（与聚合的「云添加
额度」同一套哲学，见 :mod:`app.services.aggregate_sync`）。

上传失败时按**文案特征**判定是否额度墙（:func:`is_upload_quota_wall`）：
命中 → 给该账号打「本月耗尽」标记 → 换下一个候选**并当场重试**。

标记按月份键（``"2026-11"``）存进 ``drivers.upload_quota_exhausted_month``
→ **跨月自动失效**（键 ≠ 当前月即正常），不需要清理任务。

⚠️ **限流 / 网络 / 凭据类错误不触发换盘**——那是抖动，换盘只会把问题
放大（每个盘都试一遍）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import AggregateGroup, AggregateGroupRoot, Driver
from app.db.session import session_scope
from app.domain.aggregate_types import ROLE_MASTER, ROLE_SECONDARY
from app.services.errors import ServiceError

logger = get_logger(__name__)

__all__ = [
    "LandingCandidate",
    "candidate_ok",
    "current_month_key",
    "is_upload_quota_wall",
    "list_candidates",
    "mark_quota_exhausted",
    "pick_candidate",
    "reset_quota_exhausted",
    "validate_group_target",
]


def current_month_key() -> str:
    """当前北京时间的月份键（``"2026-11"``）。

    月度额度的标记与它同键——**跨月自动失效**。
    """
    return bj_now()[:7]


# --------------------------------------------------------------------------
# 限额墙判定
# --------------------------------------------------------------------------

#: **限额类**错误特征（命中即判定「本月上传额度耗尽」）。
#:
#: ⚠️ 真实撞墙文案目前**未知**——匹配器收口在这里，可扩展。上线初期靠
#: 上传失败日志观察真实文案，命中特征后固化关键词。
_QUOTA_WALL_KEYWORDS: tuple[str, ...] = (
    "限额",
    "额度",
    "配额",
    "超限",
    "上传流量",
    "流量超",
    "quota",
    "limit exceeded",
)

#: 明确**不是**额度墙的特征——**先排除**再匹配（限流是抖动，换盘只会
#: 把问题放大：每个盘都试一遍）。
_NOT_QUOTA_KEYWORDS: tuple[str, ...] = (
    "限流",
    "过于频繁",
    "too many requests",
    "rate limit",
)


def is_upload_quota_wall(reason: str) -> bool:
    """上传失败的原因文案是否指向「本月上传额度耗尽」。

    Args:
        reason: 失败原因（``SyncOutcome.reason``，形如
            ``"上传失败：光鸭上传失败：<服务端文案>"``）。

    Returns:
        True = 额度墙（可换盘重试）；False = 别的失败（不换盘）。
    """
    text = (reason or "").lower()
    if not text:
        return False
    if any(word in text for word in _NOT_QUOTA_KEYWORDS):
        return False
    return any(word in text for word in _QUOTA_WALL_KEYWORDS)


async def mark_quota_exhausted(driver_id: int) -> None:
    """给账号打「本月上传额度耗尽」标记（跨月自动失效）。

    写失败只记日志——**不能因此中断上传链路**（标记只是优化：少撞一次
    墙而已）。
    """
    month = current_month_key()
    try:
        async with session_scope() as db:
            row = (
                await db.execute(select(Driver).where(Driver.id == driver_id))
            ).scalar_one_or_none()
            if row is None:
                return
            row.upload_quota_exhausted_month = month
            await db.commit()
    except Exception as exc:
        logger.warning("标记存储源 %s 本月上传额度耗尽失败：%s", driver_id, exc)


async def reset_quota_exhausted(driver_id: int) -> None:
    """清掉「本月上传额度耗尽」标记（人工干预 / 测试用）。"""
    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == driver_id))
        ).scalar_one_or_none()
        if row is None:
            return
        row.upload_quota_exhausted_month = None
        await db.commit()


# --------------------------------------------------------------------------
# 候选（落点）
# --------------------------------------------------------------------------


@dataclass(slots=True)
class LandingCandidate:
    """一个可选落点：组内的一个副数据源 + 它的缓冲根。"""

    driver_id: int
    driver_name: str
    driver_type: str
    root_id: str
    root_path: str | None
    #: 本月上传额度已耗尽（撞墙标记）
    exhausted: bool
    #: 测连缓存：True=健康 / False=不健康 / **None=未知（没探过）**
    healthy: bool | None
    #: 剩余空间（字节）：**None = 未知**
    free_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        """对外形状（API 层用）。"""
        return {
            "driver_id": self.driver_id,
            "driver_name": self.driver_name,
            "driver_type": self.driver_type,
            "root_id": self.root_id,
            "root_path": self.root_path,
            "exhausted": self.exhausted,
            "healthy": self.healthy,
            "free_bytes": self.free_bytes,
        }


async def list_candidates(group_id: int) -> list[LandingCandidate]:
    """组内可作为落点的副盘（启用 + 已配缓冲根），按 ``driver id`` 升序。

    **固定顺序**——不按用量 / 空间排序（用户明确要求）。
    """
    month = current_month_key()
    async with session_scope() as db:
        secondaries = (
            (
                await db.execute(
                    select(Driver)
                    .where(
                        Driver.aggregate_group_id == group_id,
                        Driver.role == ROLE_SECONDARY,
                        Driver.enabled.is_(True),
                    )
                    .order_by(Driver.id)
                )
            )
            .scalars()
            .all()
        )
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
    root_by_driver = {int(r.driver_id): r for r in roots}

    # 只保留**支持上传**的副盘——落点要能写。
    from app.adapters.drivers.upload_base import is_uploadable

    candidates: list[LandingCandidate] = []
    for row in secondaries:
        if not is_uploadable(str(row.type)):
            continue
        root = root_by_driver.get(int(row.id))
        if root is None:
            continue  # 没配缓冲根 → 不能当落点
        healthy, free_bytes = _probe_snapshot(int(row.id))
        candidates.append(
            LandingCandidate(
                driver_id=int(row.id),
                driver_name=str(row.name),
                driver_type=str(row.type),
                root_id=str(root.root_id),
                root_path=root.root_path,
                exhausted=(row.upload_quota_exhausted_month == month),
                healthy=healthy,
                free_bytes=free_bytes,
            )
        )
    return candidates


def _probe_snapshot(driver_id: int) -> tuple[bool | None, int | None]:
    """读探测缓存：``(是否健康, 剩余空间)``。

    **拿不到就是 None**——未探测过（缓存没有）时**不拦**（闸门只在
    「已知」时生效）。缓存是**快照**，可能滞后；故只当启发式用。
    """
    try:
        from app.services.driver_service import cached_probe

        cached = cached_probe(driver_id)
    except Exception:  # pragma: no cover - 缓存读取不该影响主链路
        return None, None
    if cached is None:
        return None, None
    status = cached.status
    healthy = status.status == "ok"
    free = int(status.remain_bytes or 0)
    return healthy, (free if free > 0 else None)


def candidate_ok(candidate: LandingCandidate, size: int) -> bool:
    """候选是否通过闸门（**只过滤，不排序**）。"""
    if candidate.exhausted:
        return False
    if candidate.healthy is False:
        return False
    # 剩余空间未知（没探过）→ 不拦
    return candidate.free_bytes is None or size <= candidate.free_bytes


async def pick_candidate(
    group_id: int,
    size: int = 0,
    *,
    exclude: set[int] | None = None,
    prefer: int | None = None,
) -> LandingCandidate | None:
    """挑一个落点。

    Args:
        group_id: 聚合组 id。
        size: 待传文件大小（字节）——用于剩余空间闸门。
        exclude: 已试过并失败的副盘（不再选）。
        prefer: **优先**考虑的副盘（粘性落点）——它若通过闸门就选它。

    Returns:
        落点候选；没有可用候选时返回 ``None``。
    """
    candidates = await list_candidates(group_id)
    skipped = exclude or set()
    if prefer is not None:
        for cand in candidates:
            if (
                cand.driver_id == prefer
                and cand.driver_id not in skipped
                and candidate_ok(cand, size)
            ):
                return cand
    for cand in candidates:
        if cand.driver_id in skipped:
            continue
        if candidate_ok(cand, size):
            return cand
    return None


# --------------------------------------------------------------------------
# 组校验
# --------------------------------------------------------------------------


async def validate_group_target(group_id: int) -> dict[str, Any]:
    """校验「把上传任务指向这个聚合组」是否可行。

    Raises:
        ServiceError: 组不存在（404）/ 组停用 / 无主盘 / 未配归拢目标目录 /
            没有可用的副盘缓冲根（均为 400，文案点名缺哪一项）。
    """
    async with session_scope() as db:
        group = (
            await db.execute(
                select(AggregateGroup).where(AggregateGroup.id == group_id)
            )
        ).scalar_one_or_none()
        if group is None:
            raise ServiceError(f"聚合组不存在：{group_id}", status=404)
        if not group.enabled:
            raise ServiceError(
                f"聚合组「{group.name}」已停用——停用的组不参与归拢，"
                "上传进去的文件没人搬",
                status=400,
            )
        if not group.target_dir_id:
            raise ServiceError(
                f"聚合组「{group.name}」未配置「归拢目标目录」——"
                "请先在「网盘聚合」页配置",
                status=400,
            )
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
            raise ServiceError(
                f"聚合组「{group.name}」还没有主数据源——没有主盘就没有归拢落点",
                status=400,
            )
        name = str(group.name)

    candidates = await list_candidates(group_id)
    if not candidates:
        raise ServiceError(
            f"聚合组「{name}」没有可用的副数据源缓冲根——"
            "请先在「网盘聚合」页配置副盘的缓冲根目录",
            status=400,
        )
    return {
        "group_id": group_id,
        "group_name": name,
        "master_driver_id": int(master_id),
        "candidates": candidates,
    }
