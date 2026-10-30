#!/usr/bin/env python3
"""一次性迁移脚本:把合并前的两个旧库搬到合并后的 medialab.db。

**背景**:128 上仍跑着合并前的两个独立服务:

- ``seedhelper.db`` —— 辅种域(12 表,216 MB)
- ``strmmanger.db`` —— 媒体库域(8 表,8 MB)

合并后的 medialab 把两个域放在一个库里。本脚本把两个旧库的数据搬到
一个由应用 ``create_all`` 建好结构的新库。

**时间列口径差异**(本脚本最容易出错的地方):

- **辅种域旧库**:`user_version=2`,老表(DATETIME 声明)的值**已是
  北京时间字符串**(历史上跑过 ``migrate_datetime_to_bj_str`` 批次 1+2)。
  **原样搬**,不做 +8h 转换。新表(``site_user_data`` / ``cross_seed_*``
  / ``app_env_settings`` / ``scheduler_job_settings``)本就是 VARCHAR(32)。
- **媒体库域旧库**:时间列是 ``INTEGER``(unix 秒,``0 = 从未``)。
  转换为北京时间字符串(``strftime('%Y-%m-%d %H:%M:%S', ts, 'unixepoch', '+8 hours')``),
  ``0 / NULL → NULL``。``link_cache.expires_at`` **保留 INTEGER**(协议侧)。

**废弃表**:`seedhelper.torrents`(834 行,新模型无此表)跳过。

**用法**(本机跑,scp 库来回)::

    # 1. 128 上停两个旧容器 + 备份
    ssh mp "docker stop strmmanger seedhelper-prod"
    ssh mp "cp /mnt/user/appdata/seedhelper/data/seedhelper.db /mnt/user/appdata/seedhelper/data/seedhelper.db.bak.migrate"
    ssh mp "cp /mnt/user/appdata/strmmanger/data/strmmanger.db /mnt/user/appdata/strmmanger/data/strmmanger.db.bak.migrate"

    # 2. 拉两个旧库到本机
    scp mp:/mnt/user/appdata/seedhelper/data/seedhelper.db /tmp/legacy/
    scp mp:/mnt/user/appdata/strmmanger/data/strmmanger.db /tmp/legacy/

    # 3. 建空新库(让应用 create_all 建结构)
    #    用 dev 容器或本机 uv run python -c "..."
    uv run python -c "import asyncio; from app.core.config import AppSettings, set_settings; from app.db.session import init_db, dispose_db; from app.db.bootstrap import db_bootstrap; import tempfile, pathlib; tmp=pathlib.Path(tempfile.mkdtemp()); s=AppSettings(data_dir=tmp, db_path=tmp/'medialab.db', log_dir=tmp, web_token='x'*32, signing_secret='y'*32, web_username='a', web_password='p'); set_settings(s); init_db(s); asyncio.run(db_bootstrap.create_all()); asyncio.run(dispose_db()); print(tmp/'medialab.db')"

    # 4. 搬数据
    uv run python scripts/migrate_legacy_db.py \
        --seed /tmp/legacy/seedhelper.db \
        --strm /tmp/legacy/strmmanger.db \
        --out  /tmp/legacy/medialab.db

    # 5. 拉到 128
    scp /tmp/legacy/medialab.db mp:/mnt/user/appdata/medialab/data/medialab.db

**幂等**:`INSERT OR IGNORE` + 主键去重,重复跑不报错(但也不覆盖已有数据)。

**安全**:脚本**只读**两个源库,**只写**目标库。源库绝不会被改动。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

#: 辅种域的表(来自 seedhelper.db)。不含 ``torrents``(废弃,新模型无此表)。
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

#: 媒体库域的表(来自 strmmanger.db)。
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

#: 媒体库域需要 unix 秒 → 北京时间字符串转换的列(按表分组)。
#:
#: - ``link_cache.expires_at`` **不在此列**(协议侧,保留 INTEGER)
#: - 这些列在新模型里都是 ``String(32)``(部分 nullable)
#: - 转换规则:``0 / NULL → NULL``;否则 ``strftime('%Y-%m-%d %H:%M:%S', ts, 'unixepoch', '+8 hours')``
MEDIA_TIME_COLS: dict[str, tuple[str, ...]] = {
    "drivers": ("created_at", "updated_at", "last_scan_at"),
    "media_files": ("mtime", "created_at", "updated_at"),
    "link_cache": ("created_at",),  # expires_at 保留 INTEGER
    "settings": ("updated_at",),
    "dav_users": ("last_login_at", "created_at", "updated_at"),
    "dav_audit_log": ("created_at",),
    "upload_tasks": ("created_at", "updated_at", "last_sync_at"),
    "probe_results": ("probed_at",),
}


def _existing_tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    """取某个附加库里的表名集合。"""
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """取某个附加库里某张表的列名(按定义顺序)。"""
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [r[1] for r in rows]


def _copy_seed_tables(
    conn: sqlite3.Connection,
    src_schema: str,
    tables: tuple[str, ...],
    label: str,
) -> tuple[int, int]:
    """搬辅种域表:原样搬(值已是北京时间字符串,无需转换)。

    按列名映射(只搬两库共有列,按目标库顺序)。源库缺的列用 NULL 占位。
    """
    src_tables = _existing_tables(conn, src_schema)
    main_tables = _existing_tables(conn, "main")
    copied = 0
    rows_total = 0
    for table in tables:
        if table not in src_tables:
            print(f"  跳过 {label}.{table}(源库无此表)")
            continue
        if table not in main_tables:
            print(f"  跳过 {label}.{table}(目标库无此表——先建好表结构)")
            continue

        src_count = conn.execute(
            f"SELECT COUNT(*) FROM {src_schema}.{table}"
        ).fetchone()[0]
        if src_count == 0:
            print(f"  跳过 {label}.{table}(源库为空)")
            continue

        src_cols = set(_columns(conn, src_schema, table))
        main_cols = _columns(conn, "main", table)
        common = [c for c in main_cols if c in src_cols]
        missing = [c for c in main_cols if c not in src_cols]
        if missing:
            print(
                f"  ! {label}.{table}:目标库有源库没有的列 {missing}"
                "(这些列将取默认值)"
            )
        if not common:
            print(f"  跳过 {label}.{table}(两库无共有列)")
            continue

        col_list = ", ".join(f'"{c}"' for c in common)
        conn.execute(
            f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
            f"SELECT {col_list} FROM {src_schema}.{table}"
        )
        copied += 1
        rows_total += src_count
        print(f"  ✓ {label}.{table}({src_count} 行,{len(common)} 列)")
    return copied, rows_total


def _copy_media_tables(
    conn: sqlite3.Connection,
    src_schema: str,
    tables: tuple[str, ...],
    label: str,
) -> tuple[int, int]:
    """搬媒体库域表:时间列做 unix 秒 → 北京时间字符串转换。

    - 时间列(在 :data:`MEDIA_TIME_COLS` 里):``0/NULL → NULL``;否则
      ``strftime('%Y-%m-%d %H:%M:%S', ts, 'unixepoch', '+8 hours')``
    - 非时间列:原样搬
    - ``link_cache.expires_at`` 保留 INTEGER(不在时间列清单里)
    """
    src_tables = _existing_tables(conn, src_schema)
    main_tables = _existing_tables(conn, "main")
    copied = 0
    rows_total = 0
    for table in tables:
        if table not in src_tables:
            print(f"  跳过 {label}.{table}(源库无此表)")
            continue
        if table not in main_tables:
            print(f"  跳过 {label}.{table}(目标库无此表——先建好表结构)")
            continue

        src_count = conn.execute(
            f"SELECT COUNT(*) FROM {src_schema}.{table}"
        ).fetchone()[0]
        if src_count == 0:
            print(f"  跳过 {label}.{table}(源库为空)")
            continue

        src_cols = set(_columns(conn, src_schema, table))
        main_cols = _columns(conn, "main", table)
        common = [c for c in main_cols if c in src_cols]
        missing = [c for c in main_cols if c not in src_cols]
        if missing:
            print(
                f"  ! {label}.{table}:目标库有源库没有的列 {missing}"
                "(这些列将取默认值)"
            )
        if not common:
            print(f"  跳过 {label}.{table}(两库无共有列)")
            continue

        # 构造 SELECT 表达式:时间列用 CASE WHEN 转换,其它列原样
        time_cols = set(MEDIA_TIME_COLS.get(table, ()))
        select_exprs: list[str] = []
        for col in common:
            if col in time_cols:
                select_exprs.append(
                    f"CASE WHEN \"{col}\" IS NULL OR \"{col}\" = 0 THEN NULL "
                    f"ELSE strftime('%Y-%m-%d %H:%M:%S', \"{col}\", 'unixepoch', '+8 hours') END "
                    f'AS "{col}"'
                )
            else:
                select_exprs.append(f'"{col}"')

        col_list = ", ".join(f'"{c}"' for c in common)
        select_list = ", ".join(select_exprs)
        conn.execute(
            f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
            f"SELECT {select_list} FROM {src_schema}.{table}"
        )
        copied += 1
        rows_total += src_count
        print(f"  ✓ {label}.{table}({src_count} 行,{len(common)} 列,{len(time_cols)} 个时间列转换)")
    return copied, rows_total


def migrate(
    seed_path: Path,
    strm_path: Path,
    out_path: Path,
) -> int:
    """执行迁移。返回退出码(0 = 成功)。"""
    for label, path in (("seedhelper", seed_path), ("strmmanger", strm_path)):
        if not path.exists():
            print(f"✗ 源库不存在:{label} → {path}", file=sys.stderr)
            return 1

    print("源库:")
    print(f"  辅种域    {seed_path}")
    print(f"  媒体库域 {strm_path}")
    print(f"目标库:{out_path}")
    print()
    print("⚠️  目标库必须已由应用 create_all 建好表结构(结构以代码为准)")
    print()

    if not out_path.exists():
        print(f"✗ 目标库不存在:{out_path}", file=sys.stderr)
        print("  先让应用 create_all 建库:", file=sys.stderr)
        print(
            "  uv run python -c \"import asyncio; ...\"", file=sys.stderr
        )
        return 1

    conn = sqlite3.connect(out_path)
    try:
        conn.execute("ATTACH DATABASE ? AS seed", (str(seed_path),))
        conn.execute("ATTACH DATABASE ? AS strm", (str(strm_path),))

        print("搬辅种域表(原样搬,值已是北京时间字符串):")
        seed_copied, seed_rows = _copy_seed_tables(
            conn, "seed", SEED_TABLES, "seed"
        )
        print()
        print("搬媒体库域表(unix 秒 → 北京时间字符串):")
        media_copied, media_rows = _copy_media_tables(
            conn, "strm", MEDIA_TABLES, "strm"
        )

        conn.commit()

        print()
        print(f"完成:{seed_copied + media_copied} 张表,{seed_rows + media_rows} 行")
        print(f"  辅种域    {seed_copied} 表 / {seed_rows} 行")
        print(f"  媒体库域 {media_copied} 表 / {media_rows} 行")
        print("→ 现在可以把 medialab.db 拉到 128 启动应用了")
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把合并前的 seedhelper.db + strmmanger.db 搬到合并后的 medialab.db"
    )
    parser.add_argument(
        "--seed", required=True, type=Path, help="seedhelper.db 路径"
    )
    parser.add_argument(
        "--strm", required=True, type=Path, help="strmmanger.db 路径"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="目标 medialab.db 路径(需已建好表结构)"
    )
    args = parser.parse_args()
    return migrate(args.seed, args.strm, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
