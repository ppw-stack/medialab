#!/usr/bin/env python3
"""回滚「规则包驱动的通用适配器」这次站点迁移（PR #27）。

迁移做了两件**不可逆**的事：

1. ``sites.adapter`` 从 10 个站点专用 adapter 统一改写成 ``generic``；
2. ``site_user_data`` 删除「时魔」四列
   （``hourly_bonus`` / ``hourly_bonus_unit`` / ``hourly_bonus_capped`` /
   ``hourly_bonus_progress``）。

本脚本按**迁移时留下的回滚台账**（表 ``medialab_bak_v3_site_adapter``）
逐行还原 adapter 值，并把删掉的四列加回来——**必须先跑本脚本、再回退镜像**，
否则旧镜像的 ORM 读写这四列会直接 ``OperationalError: no such column``。

用法::

    uv run python scripts/rollback_site_adapter_migration.py --db /path/to/medialab.db

    # 只看会改什么，不落库
    uv run python scripts/rollback_site_adapter_migration.py --db ... --dry-run

回滚后**不要**直接再升级回来：正向迁移是幂等的，但台账表已被消费（见
``app/db/migrations.py`` 的 ``_record_adapter_rollback_rows``）——需要重新
记录台账时，把 ``medialab_bak_v3_site_adapter`` 表一并清空。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.db.migrations import (
    ADAPTER_ROLLBACK_TABLE,
    HOURLY_BONUS_COLUMNS,
    readd_site_user_data_hourly_bonus_columns,
    restore_adapter_values,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger("medialab.rollback_site_adapter")


async def _run(db_path: Path, dry_run: bool) -> int:
    """执行回滚。

    :param db_path: SQLite 数据库文件路径
    :param dry_run: 只读模式，不写库
    :return: 进程退出码
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": ADAPTER_ROLLBACK_TABLE},
            )
            if exists.first() is None:
                logger.error(
                    "回滚台账表 %s 不存在——该库可能没做过本次迁移，无需回滚",
                    ADAPTER_ROLLBACK_TABLE,
                )
                return 2
            rows = (
                await conn.execute(
                    text(
                        f"SELECT site_id, old_adapter FROM {ADAPTER_ROLLBACK_TABLE} "
                        "ORDER BY site_id"
                    )
                )
            ).all()
            logger.info("台账记录 %d 个站点的旧 adapter 值", len(rows))
            for site_id, old_adapter in rows:
                logger.info("  site_id=%s → adapter=%s", site_id, old_adapter)

            table_info = await conn.execute(text("PRAGMA table_info(site_user_data)"))
            columns = {row[1] for row in table_info.all()}
            missing = [name for name, _ in HOURLY_BONUS_COLUMNS if name not in columns]
            logger.info("site_user_data 缺失的时魔列：%s", missing or "无")
            if dry_run:
                logger.info("dry-run：未写库")
                return 0

        # 两步写操作必须同生共死：第一步提交、第二步失败的话，库会停在
        # 「adapter 已还原成旧值、但 site_user_data 仍缺四列」的中间态——
        # 旧镜像的 ORM 读写这四列会直接 OperationalError: no such column。
        async with engine.begin() as conn:
            restored = await restore_adapter_values(conn=conn)
            added = await readd_site_user_data_hourly_bonus_columns(conn=conn)
        # 成功日志必须在事务提交**之后**再打：engine.begin() 只有在块正常
        # 退出时才 commit。块内打「已还原 / 已加回」，一旦第二步失败被回滚，
        # 日志里仍留着成功记录——运维会误判回滚已落盘而手工补跑。
        logger.info("已还原 %d 个站点的 adapter 值", restored)
        logger.info("已加回 %d 个时魔列", added)
        return 0
    except Exception:
        # 事务未提交即失败 → 整笔回滚，库没有任何改动。记清这一点，
        # 免得运维把「事务回滚」误读成「已改了一半」。
        logger.exception("回滚失败——事务已整体回滚，数据库未发生任何改动")
        raise
    finally:
        await engine.dispose()


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="SQLite 数据库文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的改动")
    args = parser.parse_args()
    if not args.db.exists():
        logger.error("数据库文件不存在：%s", args.db)
        return 2
    return asyncio.run(_run(args.db, args.dry_run))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
