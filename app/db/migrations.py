"""数据库迁移（幂等，每次启动跑一遍）。

从 :mod:`app.db.session` 拆出——原先 576 行的文件混了 4 种职责
（engine 管理 / 幂等 ALTER / datetime 迁移 / 回填），职责边界不清。

本模块只管**迁移**，不碰 engine 生命周期。

两类迁移：

1. **幂等 ALTER**：建表后的列增删改名（``create_all`` 不给已有表加列）
2. **数据回填**：从已有列推导新列（如 ``discount_pct``）

历史：原 ``migrate_datetime_to_bj_str``（把历史 DateTime(UTC) 列 +8h
转为北京时间字符串）已在「时间口径统一」时删除——新模型 DB 时间列
直接声明为 ``String(32)`` 北京时间字符串（见 :mod:`app.db.models`），
``create_all`` 建表即为最终态，无需运行时迁移。

注：**不引入 Alembic**。SQLAlchemy 2.0 async 需要额外适配
（``run_sync``），且现有迁移已跑通；表结构改动超过 10 次再评估。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult

from app.core.logging import get_logger
from app.db.models import TorrentIndex
from app.db.session import get_session_factory
from app.domain.enums import FreeType

logger = get_logger(__name__)

__all__ = [
    "ADAPTER_VALUE_MIGRATIONS",
    "backfill_discount_pct",
    "migrate_adapter_values",
]


# ---------------------------------------------------------------- 值迁移

#: 站点 adapter 迁移：从旧值映射到新值。空 dict 表示无迁移。
ADAPTER_VALUE_MIGRATIONS: dict[str, str] = {
    "generic": "mteam",  # 旧通用 adapter → m-team 专用 adapter（重构后唯一支持的实现）
}


async def migrate_adapter_values() -> None:
    """把 :data:`ADAPTER_VALUE_MIGRATIONS` 指定的旧值批量改写为新值（幂等）。

    启动时跑一次：若旧值已不存在则无副作用。
    """
    if not ADAPTER_VALUE_MIGRATIONS:
        return
    factory = get_session_factory()
    from sqlalchemy import update

    from app.db.models import Site

    async with factory() as session:
        for old_value, new_value in ADAPTER_VALUE_MIGRATIONS.items():
            stmt = (
                update(Site)
                .where(Site.adapter == old_value)
                .values(adapter=new_value)
            )
            result = cast(CursorResult[object], await session.execute(stmt))
            if result.rowcount:
                logger.info(
                    "站点 adapter 迁移：%s → %s，影响 %d 行",
                    old_value, new_value, result.rowcount,
                )
        await session.commit()


# ---------------------------------------------------------------- 回填

def _extract_discount_pct_from_payload(payload: object) -> int | None:
    """从已落库的 free_payload 反推 discount_pct（用于历史回填）。

    支持 mteam status.discount == "PERCENT_N"，以及 NexusPHP
    free_classes 含 halfdown / thirtypercent / 50 / 30。
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, dict):
        disc = status.get("discount")
        if isinstance(disc, str):
            up = disc.upper()
            if up == "FREE":
                return 100
            if up.startswith("PERCENT_"):
                try:
                    return int(up.split("_", 1)[1])
                except (IndexError, ValueError):
                    return None
        if isinstance(disc, (int, float)):
            return abs(int(disc))
    free_classes = payload.get("free_classes")
    if isinstance(free_classes, list):
        for cls in free_classes:
            if not isinstance(cls, str):
                continue
            c = cls.lower()
            if c in ("halfdown", "twouphalfdown", "50"):
                return 50
            if c in ("thirtypercent", "30"):
                return 30
    return None


async def backfill_discount_pct() -> None:
    """回填 torrent_index.discount_pct。

    仅更新 free_type ∈ {neutral_leech, freeleech_permanent} 的行；
    幂等：仅在 discount_pct IS NULL 时填，已填的不动。
    """
    from sqlalchemy import select, update

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(
            TorrentIndex.id, TorrentIndex.free_type, TorrentIndex.free_payload
        ).where(
            TorrentIndex.free_type.in_(
                (FreeType.NEUTRAL, FreeType.PERMANENT)
            ),
            TorrentIndex.discount_pct.is_(None),
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return
        updates: list[tuple[int, int]] = []
        for row_id, free_type, free_payload in rows:
            if free_type == FreeType.PERMANENT:
                pct: int | None = 100
            else:
                pct = _extract_discount_pct_from_payload(free_payload)
            if pct is not None:
                updates.append((row_id, pct))
        if not updates:
            return
        # 批量更新（按值分组）
        from collections import defaultdict

        by_pct: dict[int, list[int]] = defaultdict(list)
        for row_id, pct in updates:
            by_pct[pct].append(row_id)
        for pct, ids in by_pct.items():
            await session.execute(
                update(TorrentIndex)
                .where(TorrentIndex.id.in_(ids))
                .values(discount_pct=pct)
            )
        await session.commit()
        logger.info("discount_pct 历史回填：%d 行", len(updates))

