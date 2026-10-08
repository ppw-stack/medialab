"""冷却黑名单与阶梯（FR-2.3.1）。

**冷却不是幂等**——两者职责严格分离：

- :class:`~app.db.models.AddHistory`：本表之外。"此站添加过此 hash" 是永久
  事实，幂等性由它保证（AC-09 重启 3 次不重复添加）。
- 本模块：**临时回避**。到期自动失效，某些 reason 甚至需人工解除。

最易出错的一点：**按 reason 独立计数**。用户手动移除（``user_removed``）
不该因为系统中止（``window_expired``）过 3 次而被"连坐"升级。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import utcnow
from app.db.models import CooldownHash, CooldownReason

#: 阶梯冷却时长（秒），下标 = 已失败次数 - 1
LADDERS: dict[str, tuple[int, ...]] = {
    # 窗口不足导致的失败，与"种本身有问题"无关 → 阶梯较短
    CooldownReason.WINDOW_EXPIRED: (24 * 3600, 72 * 3600, 7 * 86400, 30 * 86400),
    # 很可能站点已删种，重试无意义 → 阶梯长
    CooldownReason.DEAD_TORRENT: (7 * 86400, 30 * 86400),
    # 人工决策：系统绝不自动解除
    CooldownReason.USER_REMOVED: (30 * 86400,),
    CooldownReason.USER_BLACKLISTED: (0,),  # 0 = 永久
}

#: 不入冷却的原因（FR-2.3.1）：条件恢复后必须能立即重试
NO_COOLDOWN: frozenset[str] = frozenset(
    {
        CooldownReason.DISK_FULL,
        CooldownReason.DUPLICATE,
        CooldownReason.MANUAL_SKIP,
    }
)

#: 永久冷却：到期也不自动失效
PERMANENT: frozenset[str] = frozenset({CooldownReason.USER_BLACKLISTED})

#: 需要人工解除的原因
MANUAL_ONLY: frozenset[str] = frozenset(
    {CooldownReason.USER_BLACKLISTED}
)


@dataclass(frozen=True, slots=True)
class CooldownVerdict:
    """冷却查询结果。"""

    blocked: bool
    reason: str | None = None
    fail_count: int = 0
    until: datetime | None = None

    @property
    def permanent(self) -> bool:
        return self.blocked and self.reason in MANUAL_ONLY


def ladder_for(reason: str, fail_count: int) -> timedelta:
    """按阶梯取冷却时长。

    ``fail_count`` 超过阶梯长度时取最长档。``PERMANENT`` 返回极长时长，
    实际由 :meth:`CooldownService.is_blocked` 按 ``MANUAL_ONLY`` 处理。
    """
    ladder = LADDERS.get(reason)
    if not ladder:
        return timedelta(0)
    if reason in PERMANENT:
        return timedelta(days=365 * 100)
    idx = min(max(fail_count - 1, 0), len(ladder) - 1)
    return timedelta(seconds=ladder[idx])


class CooldownService:
    """冷却判定与写入。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_blocked(self, site_id: int, info_hash: str) -> CooldownVerdict:
        """该 hash 在该站是否处于冷却中（FR-2.3）。

        同站独立冷却（FR-2.3.1）——不同站点的收益互不相干。
        """
        row = (
            await self._session.execute(
                select(CooldownHash).where(
                    CooldownHash.site_id == site_id,
                    CooldownHash.info_hash == info_hash.lower(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return CooldownVerdict(blocked=False)

        if row.reason in MANUAL_ONLY:
            return CooldownVerdict(
                blocked=True, reason=row.reason, fail_count=row.fail_count, until=None
            )

        if row.cooldown_until is None:
            return CooldownVerdict(blocked=False, reason=row.reason, fail_count=row.fail_count)

        if row.cooldown_until > utcnow():
            return CooldownVerdict(
                blocked=True,
                reason=row.reason,
                fail_count=row.fail_count,
                until=row.cooldown_until,
            )

        # 已过期：自动失效
        await self._session.delete(row)
        return CooldownVerdict(blocked=False, reason=row.reason, fail_count=row.fail_count)

    async def record(
        self, site_id: int, info_hash: str, reason: str, *, now: datetime | None = None
    ) -> CooldownVerdict:
        """记录一次失败并写入/延长冷却。

        ``disk_full`` / ``duplicate`` / ``manual_skip`` **不入冷却**
        （FR-2.3.1），仅清理可能存在的旧记录。
        """
        ref = now or utcnow()
        h = info_hash.lower()

        if reason in NO_COOLDOWN:
            await self._clear(site_id, h, reason)
            return CooldownVerdict(blocked=False, reason=reason)

        row = (
            await self._session.execute(
                select(CooldownHash).where(
                    CooldownHash.site_id == site_id, CooldownHash.info_hash == h
                )
            )
        ).scalar_one_or_none()

        if row is None:
            fail_count = 1
            row = CooldownHash(
                site_id=site_id, info_hash=h, reason=reason, fail_count=1
            )
            self._session.add(row)
        elif row.reason != reason:
            # reason 变了 → fail_count 独立重新计数（不共享阶梯）。
            # 注意必须**复用同一行**：(site_id, info_hash) 上有唯一约束，
            # 新插一行会直接违反它。
            row.reason = reason
            fail_count = 1
            row.fail_count = 1
        else:
            fail_count = row.fail_count + 1
            row.fail_count = fail_count

        duration = ladder_for(reason, fail_count)
        row.last_at = ref
        row.cooldown_until = None if reason in PERMANENT else ref + duration
        await self._session.flush()
        return CooldownVerdict(
            blocked=True, reason=reason, fail_count=fail_count, until=row.cooldown_until
        )

    async def _clear(self, site_id: int, h: str, reason: str) -> None:
        """清除该 reason 的既有记录（用于 NO_COOLDOWN 与人工解除）。"""
        row = (
            await self._session.execute(
                select(CooldownHash).where(
                    CooldownHash.site_id == site_id,
                    CooldownHash.info_hash == h,
                    CooldownHash.reason == reason,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await self._session.delete(row)

    async def remove(self, site_id: int, info_hash: str) -> int:
        """人工解除冷却，返回删除条数。"""
        rows = (
            await self._session.execute(
                select(CooldownHash).where(
                    CooldownHash.site_id == site_id,
                    CooldownHash.info_hash == info_hash.lower(),
                )
            )
        ).scalars().all()
        for row in rows:
            await self._session.delete(row)
        return len(rows)

    async def blacklist(self, site_id: int, info_hash: str) -> None:
        """用户手动拉黑 → 永久，不可被系统自动解除（FR-2.3.1）。"""
        await self.record(site_id, info_hash, CooldownReason.USER_BLACKLISTED)