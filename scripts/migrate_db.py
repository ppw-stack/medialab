#!/usr/bin/env python3
"""合并两个 SQLite 库（strmmanger + ptHelper）为一个 medialab 库。

**背景**：合并前是两个独立服务、两个独立数据库：

- ``strmmanger.db`` —— 媒体库域（8 张表：drivers / media_files /
  link_cache / settings / dav_users / dav_audit_log / upload_tasks /
  probe_results）
- ``seedhelper.db`` —— 辅种域（12 张表：sites / downloader_instances /
  torrent_index / task_queue / app_runtime_params / app_env_settings /
  job_runs / scheduler_job_settings / cross_seed_snapshot /
  cross_seed_orphan / site_user_data）

合并后**表名零冲突**（已核对），外键各自闭环，故可以直接搬表。

**为什么用 ATTACH 而不是 dump/restore**：dump/restore 会重建表结构，
丢掉索引与自增序列；ATTACH + ``INSERT INTO ... SELECT`` **只搬数据**，
目标库的表结构由应用自己的 ``create_all`` 建好（结构以代码为准）。

**用法**::

    # 1. 备份（脚本不会自动做——但你**应该**先做）
    cp strmmanger.db strmmanger.db.bak
    cp seedhelper.db  seedhelper.db.bak

    # 2. 建目标库（让应用自己建表结构）
    python -c "import asyncio; from app.core.config import AppSettings; ..."

    # 3. 搬数据
    python scripts/migrate_db.py \
        --strm /path/to/strmmanger.db \
        --seed /path/to/seedhelper.db \
        --out   /path/to/medialab.db

**幂等**：已存在的表会被跳过（用 ``INSERT OR IGNORE`` + 主键去重）。
重复跑**不会**产生重复行，但**也不会**覆盖已有数据。

**安全**：脚本**只读**两个源库，**只写**目标库。源库绝不会被改动。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

#: 媒体库域的表（来自 strmmanger.db）
MEDIA_TABLES: tuple[str, ...] = (
    "drivers",
    "media_files",
    "link_cache",
    "settings",
    "dav_users",
    "dav_audit_log",
    "upload_tasks",
    "probe_results",
)

#: 辅种域的表（来自 seedhelper.db）
SEED_TABLES: tuple[str, ...] = (
    "sites",
    "downloader_instances",
    "torrent_index",
    "task_queue",
    "app_runtime_params",
    "app_env_settings",
    "job_runs",
    "scheduler_job_settings",
    "cross_seed_snapshot",
    "cross_seed_orphan",
    "site_user_data",
)


def _existing_tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    """取某个附加库里的表名集合。"""
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """取某个附加库里某张表的列名（按定义顺序）。"""
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [r[1] for r in rows]


def _copy_tables(
    conn: sqlite3.Connection,
    src_schema: str,
    tables: tuple[str, ...],
    label: str,
) -> tuple[int, int]:
    """把 ``src_schema`` 里的表复制到 ``main``。

    **按列名映射，不按位置** —— 这是本脚本最容易出错的地方：

    源库与目标库的**列顺序可能不同**（例：``sites.breaker_until`` 在源库
    排在 ``created_at`` 之后，在目标库排在之前）。``INSERT ... SELECT *``
    按**位置**对应，数据会静默错位——``updated_at`` 收到
    ``breaker_until`` 的值，直接触发 NOT NULL 约束失败。

    故这里显式列出**两库共有的列**（按目标库的顺序），源库缺的列用
    ``NULL`` 占位（目标库的 NOT NULL 列会报约束错——那是真问题，应该
    让人看见）。

    Returns:
        (复制的表数, 复制的行数)
    """
    src_tables = _existing_tables(conn, src_schema)
    main_tables = _existing_tables(conn, "main")
    copied = 0
    rows = 0
    for table in tables:
        if table not in src_tables:
            print(f"  跳过 {label}.{table}（源库无此表）")
            continue
        if table not in main_tables:
            print(f"  跳过 {label}.{table}（目标库无此表——先建好表结构）")
            continue

        src_count = conn.execute(
            f"SELECT COUNT(*) FROM {src_schema}.{table}"
        ).fetchone()[0]
        if src_count == 0:
            print(f"  跳过 {label}.{table}（源库为空）")
            continue

        src_cols = set(_columns(conn, src_schema, table))
        main_cols = _columns(conn, "main", table)
        # 只搬两库共有的列（按目标库顺序）
        common = [c for c in main_cols if c in src_cols]
        missing = [c for c in main_cols if c not in src_cols]
        if missing:
            print(
                f"  ! {label}.{table}：目标库有源库没有的列 {missing}"
                "（这些列将取默认值）"
            )
        if not common:
            print(f"  跳过 {label}.{table}（两库无共有列）")
            continue

        col_list = ", ".join(f'"{c}"' for c in common)
        # INSERT OR IGNORE：主键冲突的行跳过——重复跑脚本不会炸。
        conn.execute(
            f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
            f"SELECT {col_list} FROM {src_schema}.{table}"
        )
        copied += 1
        rows += src_count
        print(f"  ✓ {label}.{table}（{src_count} 行，{len(common)} 列）")
    return copied, rows


def migrate(
    strm_path: Path,
    seed_path: Path,
    out_path: Path,
    *,
    dry_run: bool = False,
) -> int:
    """执行迁移。返回退出码（0 = 成功）。"""
    for label, path in (("strmmanger", strm_path), ("ptHelper", seed_path)):
        if not path.exists():
            print(f"✗ 源库不存在：{label} → {path}", file=sys.stderr)
            return 1

    print("源库：")
    print(f"  媒体库域 {strm_path}")
    print(f"  辅种域    {seed_path}")
    print(f"目标库：{out_path}")
    if dry_run:
        print("（dry-run：不写任何数据）")
    print()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out_path)
    try:
        conn.execute("ATTACH DATABASE ? AS strm", (str(strm_path),))
        conn.execute("ATTACH DATABASE ? AS seed", (str(seed_path),))

        print("搬媒体库域表：")
        media_copied, media_rows = _copy_tables(conn, "strm", MEDIA_TABLES, "strm")
        print("搬辅种域表：")
        seed_copied, seed_rows = _copy_tables(conn, "seed", SEED_TABLES, "seed")

        if not dry_run:
            conn.commit()

        print()
        print(f"完成：{media_copied + seed_copied} 张表，{media_rows + seed_rows} 行")
        if dry_run:
            print("（dry-run：未写入）")
        else:
            print("→ 现在可以启动应用指向这个库了")
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="合并 strmmanger.db 与 seedhelper.db 为 medialab.db"
    )
    parser.add_argument("--strm", required=True, type=Path, help="strmmanger.db 路径")
    parser.add_argument("--seed", required=True, type=Path, help="seedhelper.db 路径")
    parser.add_argument("--out", required=True, type=Path, help="目标 medialab.db 路径")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只看会搬什么，不写数据",
    )
    args = parser.parse_args()
    return migrate(args.strm, args.seed, args.out, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
