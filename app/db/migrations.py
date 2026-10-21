"""数据库迁移（幂等，每次启动跑一遍）。

从 :mod:`app.db.session` 拆出——原先 576 行的文件混了 4 种职责
（engine 管理 / 幂等 ALTER / datetime 迁移 / 回填），职责边界不清。

本模块只管**迁移**，不碰 engine 生命周期。

三类迁移：

1. **幂等 ALTER**：建表后的列增删改名（``create_all`` 不给已有表加列）
2. **datetime → 北京时间字符串**：历史数据的时间语义统一
   （见 :data:`_DATETIME_MIGRATION_BATCHES`）
3. **数据回填**：从已有列推导新列（如 ``discount_pct``）

注：**不引入 Alembic**。SQLAlchemy 2.0 async 需要额外适配
（``run_sync``），且现有迁移已跑通；表结构改动超过 10 次再评估。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, text

from app.core.logging import get_logger
from app.db.models import TorrentIndex
from app.db.session import get_session_factory
from app.domain.enums import FreeType

logger = get_logger(__name__)

__all__ = [
    "ADAPTER_VALUE_MIGRATIONS",
    "backfill_discount_pct",
    "migrate_adapter_values",
    "migrate_datetime_to_bj_str",
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


# ------------------------------------------- datetime → 北京时间字符串迁移

#: 时间存储切换点（北京时间，naive）。
#:
#: e861ccf（2026-10-16 18:03:33 +08:00）把时间存储统一为**北京时间字符串**，
#: 之前的时间列写的是 naive UTC，之后写的是北京时间字符串。
#:
#: 迁移时用它区分同一列里混存的两种语义：
#: - 值 < 切换点 → 当时写的是 UTC → +8h 转北京时间
#: - 值 >= 切换点 → 当时写的是北京时间字符串 → 原样保留
#:
#: 不加这个区分会让"列声明仍是 DATETIME、但值已是北京时间"的行
#: （如 225f3d6 新增的 pushed_at）被再 +8h，时间凭空 +8 小时。
_BJ_STRING_CUTOVER = datetime(2026, 10, 16, 18, 3, 33)

#: 需要从 UTC 迁移为北京时间字符串的表 → 时间列名列表。
#: 迁移逻辑：对每张表用 PRAGMA table_info 检查列类型，若含 DATETIME/TIMESTAMP
#: 列则重建表（SQLite 不支持 ALTER COLUMN TYPE），把 UTC 值 +8h 转为北京时间字符串。
#:
#: 按**批次**组织：批次号 = 该批迁移完成后的 ``PRAGMA user_version`` 值。
#: 批次 N 的表只在 ``user_version < N`` 时迁移——已迁过的库（user_version=1）
#: 不会重复 +8h，而后续新增的表（如批次 2 的 site_user_data）仍会被迁移。
#: 新增表时**追加新批次**，不要改旧批次（否则已迁库会重复迁移）。
_DATETIME_MIGRATION_BATCHES: dict[int, dict[str, list[str]]] = {
    1: {
        "downloader_instances": ["created_at", "updated_at"],
        "sites": ["created_at", "updated_at"],
        "torrent_index": [
            "free_end_at", "published_at", "first_seen_at",
            "last_seen_at", "pushed_at",
        ],
        "task_queue": ["run_at", "created_at", "updated_at"],
        "app_runtime_params": ["updated_at"],
        "job_runs": ["started_at", "finished_at"],
    },
    2: {
        # site_user_data 表在批次 1 之后才加入（FR-1.6 站点用户数据契约），
        # 老库的 user_version 已是 1 —— 必须独立批次才能补迁。
        "site_user_data": ["last_seen_at", "created_at", "updated_at"],
    },
}


async def migrate_datetime_to_bj_str() -> None:
    """把历史 DateTime 列的 UTC 值 +8h 转为北京时间字符串（幂等）。

    SQLite 不支持 ALTER COLUMN TYPE，需用"新建临时表 → 复制（+8h）→ 删旧 →
    重命名"策略。列类型已是 TEXT/VARCHAR 则跳过。

    幂等检测：PRAGMA user_version 标记。注意：UPDATE 只改值不改列声明类型，
    table_info 永远报 DATETIME，故列类型检查不能作为幂等依据，必须用
    数据库级标记。

    **按批次执行**（见 :data:`_DATETIME_MIGRATION_BATCHES`）：批次 N 只在
    ``user_version < N`` 时跑，批次完成即把 user_version 置为 N。这样新增
    表（如 site_user_data）可以独立成批补迁，而已迁过的老表不会被重复
    +8h。中途崩溃则重跑该批次（已迁表会重复 +8h，属可接受的小概率，
    远好于每次启动都重复迁移）。
    """
    factory = get_session_factory()

    async with factory() as session:
        ver = (await session.execute(text("PRAGMA user_version"))).scalar_one()
        current = int(ver or 0)
        for batch_no in sorted(_DATETIME_MIGRATION_BATCHES):
            if current >= batch_no:
                continue  # 该批次已完成
            specs = _DATETIME_MIGRATION_BATCHES[batch_no]
            for table_name, columns in specs.items():
                # 检查表是否存在
                result = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
                    {"n": table_name},
                )
                if result.first() is None:
                    continue

                # 检查列类型
                pragma_result = await session.execute(
                    text(f"PRAGMA table_info({table_name})")
                )
                cols_info = pragma_result.all()
                needs_migration = False
                # 实际存在且仍是 DATETIME 声明的列（**逐列**判定）
                #
                # 不能只按"表里有没有 DATETIME 列"决定整表是否迁移：
                # 1) 逐列判定可避免对"已是 VARCHAR 的北京时间字符串"列
                #    再 +8h（列声明残留 DATETIME 但值已是北京时间时，
                #    整表迁移会把值再偏移 8 小时）；
                # 2) 逐列判定天然跳过"本表不存在该列"的情况
                #    （老库缺列时整表迁移会 SELECT 到不存在的列 → 启动崩溃）。
                datetime_cols: list[str] = []
                present_cols: set[str] = set()
                # 推断主键列：PRAGMA 第 5 字段 pk > 0 即主键；多列主键取第一列
                pk_col: str = "id"
                for col in cols_info:
                    col_name = col[1]
                    col_type = (col[2] or "").upper()
                    present_cols.add(col_name)
                    if col_name in columns and col_type in (
                        "DATETIME", "TIMESTAMP", "DATE",
                    ):
                        datetime_cols.append(col_name)
                        needs_migration = True
                    # col 索引: (cid, name, type, notnull, dflt_value, pk)
                    if len(col) >= 6 and col[5] and isinstance(col[5], int) and col[5] > 0:
                        pk_col = col_name
                if not needs_migration:
                    continue
                # 只处理"存在且仍是 DATETIME 声明"的列
                target_cols = [
                    c for c in columns
                    if c in datetime_cols
                ]
                missing = [c for c in columns if c not in present_cols]
                if missing:
                    logger.info(
                        "表 %s 缺少列 %s（跳过这些列的迁移）", table_name, missing
                    )

                logger.info(
                    "开始迁移表 %s 的 DateTime 列 → 北京时间字符串（PK=%s）",
                    table_name, pk_col,
                )

                # SQLite datetime 存储格式：
                # 1) "YYYY-MM-DD HH:MM:SS" (naive UTC)
                # 2) "YYYY-MM-DD HH:MM:SS.ffffff" (naive UTC, 微秒)
                # 迁移：把每列的 UTC 值 +8h，去掉微秒后缀，格式统一为 "yyyy-MM-dd HH:mm:ss"
                for col_name in target_cols:
                    # 逐行更新：解析旧 UTC 值 → +8h → 写回字符串
                    # 用 SQLite 的 datetime() 函数做批量更新（如果可用）
                    # SQLite 没有 datetime() 函数，用字符串操作：
                    # "YYYY-MM-DD HH:MM:SS" + 8h 不容易用纯 SQL 做。
                    # 所以用 Python 逐行处理（数据量通常不大）。
                    rows = (
                        await session.execute(
                            text(
                                f"SELECT {pk_col}, {col_name} FROM {table_name} "
                                f"WHERE {col_name} IS NOT NULL"
                            )
                        )
                    ).all()
                    if not rows:
                        continue

                    from collections import defaultdict

                    by_new_val: dict[str, list[object]] = defaultdict(list)
                    # 已经是北京时间的值（列声明残留 DATETIME，但值早就是
                    # 北京字符串）不能再 +8h，否则时间会凭空 +8 小时。
                    #
                    # 判据用**切换点**而不是"值是否在未来"：切换点是
                    # e861ccf（2026-10-16 18:03 +08:00）——该提交把时间存储
                    # 统一为北京时间字符串，之前写的是 naive UTC，之后写的
                    # 是北京时间字符串。
                    # 切换点之后写入的值 → 已是北京时间，原样保留；
                    # 切换点之前的值 → 是 UTC，+8h 转换。
                    # （用"值 +8h > now"判会误伤 16 小时前的北京时间值。）
                    skipped_bj = 0
                    for row_pk, old_val in rows:
                        if old_val is None:
                            continue
                        try:
                            old_str = str(old_val).strip()
                            # 处理可能的微秒后缀
                            if "." in old_str:
                                old_str = old_str.split(".")[0]
                            # 尝试解析
                            try:
                                old_dt = datetime.strptime(old_str, "%Y-%m-%d %H:%M:%S")
                            except ValueError:
                                try:
                                    old_dt = datetime.fromisoformat(old_str)
                                except ValueError:
                                    continue
                            # 判定该行是 UTC 还是北京时间：
                            # - 值早于切换点（_BJ_STRING_CUTOVER）→ 当时写的是
                            #   naive UTC → +8h 转北京时间
                            # - 值晚于切换点 → 当时写的是北京时间字符串 →
                            #   原样保留（再 +8h 会凭空多 8 小时）
                            #
                            # 切换点取 e861ccf（2026-10-16 18:03 +08:00）——
                            # 该提交把时间存储统一为北京时间字符串。
                            if old_dt >= _BJ_STRING_CUTOVER:
                                skipped_bj += 1
                                continue
                            # UTC → +8h → 北京时间字符串

                            aware_utc = old_dt.replace(tzinfo=UTC)
                            bj_str = (aware_utc + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
                            by_new_val[bj_str].append(row_pk)
                        except Exception:
                            continue

                    if skipped_bj:
                        logger.info(
                            "表 %s 列 %s：%d 行已是北京时间，跳过 +8h 转换",
                            table_name, col_name, skipped_bj,
                        )

                    for new_val, pks in by_new_val.items():
                        placeholders = ",".join(f":pk{i}" for i in range(len(pks)))
                        params: dict[str, object] = {"new_val": new_val}
                        for i, rpk in enumerate(pks):
                            params[f"pk{i}"] = rpk
                        await session.execute(
                            text(
                                f"UPDATE {table_name} SET {col_name} = :new_val "
                                f"WHERE {pk_col} IN ({placeholders})"
                            ),
                            params,
                        )
                    if by_new_val:
                        logger.info(
                            "表 %s 列 %s：迁移 %d 行 UTC → 北京时间",
                            table_name, col_name, sum(len(v) for v in by_new_val.values()),
                        )

                await session.commit()
                logger.info("表 %s DateTime 迁移完成", table_name)
            # 批次完成 → 写入幂等标记（PRAGMA 赋值立即持久化，非事务性）
            await session.execute(text(f"PRAGMA user_version = {batch_no}"))
            logger.info("DateTime 迁移批次 %d 完成（user_version=%d）", batch_no, batch_no)
