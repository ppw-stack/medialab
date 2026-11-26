#!/usr/bin/env python3
"""SQLite 在线热备份与安全恢复工具（兼容 WAL 模式）。

为什么不能直接 ``cp medialab.db``：
本项目 SQLite 运行在 WAL 模式，直接拷贝主库文件可能遗漏 ``-wal`` 中的
未 checkpoint 事务，甚至在写入并发时产出损坏页。本脚本使用 SQLite 官方
在线备份 API（``sqlite3.Connection.backup``）并在备份/恢复后执行
``PRAGMA integrity_check`` 校验完整性。

用法示例::

    # 1) 在线备份（默认保留最近 7 份）
    python3 scripts/backup_sqlite.py backup \
        --db /app/data/medialab.db --out-dir /app/data/backups --keep 7

    # 2) 安全恢复（自动留存 .pre-restore.*.bak，清理旧 WAL/SHM 后恢复并验库）
    python3 scripts/backup_sqlite.py restore \
        --backup /app/data/backups/medialab-20261126-190000.db \
        --target /app/data/medialab.db
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def check_integrity(db_path: Path) -> None:
    """对 SQLite 文件执行 ``PRAGMA integrity_check``。

    :param db_path: 待校验的 SQLite 文件。
    :raises RuntimeError: 校验结果不为 ``ok``（文件损坏 / 不是合法 SQLite）。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if not rows or any(row[0] != "ok" for row in rows):
        details = "; ".join(str(r[0]) for r in rows[:5])
        raise RuntimeError(f"SQLite 完整性校验失败（{db_path}）：{details}")


def backup_db(
    db_path: Path,
    out_dir: Path,
    *,
    keep: int = 7,
    stamp: str | None = None,
) -> Path:
    """使用 ``sqlite3.Connection.backup`` 在线热备份并做完整性校验。

    :param db_path: 源数据库路径（可处于 WAL 运行态）。
    :param out_dir: 备份输出目录（不存在则创建）。
    :param keep: 保留最近 N 份备份（``0`` = 不清理）。
    :param stamp: 备份文件名的时间戳（默认当前时间，测试可注入）。
    :returns: 备份文件路径。
    :raises FileNotFoundError: 源数据库不存在。
    :raises RuntimeError: 备份产物完整性校验失败（临时文件已清理）。
    """
    if not db_path.exists():
        raise FileNotFoundError(f"源数据库不存在：{db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = stamp or time.strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"{db_path.stem}-{ts}.db"
    tmp = out_dir / f".{dest.name}.tmp"

    src = sqlite3.connect(str(db_path), timeout=30.0)
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    try:
        check_integrity(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    tmp.replace(dest)

    if keep > 0:
        pattern = f"{db_path.stem}-*.db"
        backups = sorted(
            out_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for old in backups[keep:]:
            old.unlink(missing_ok=True)
    return dest


def restore_db(backup_path: Path, target_path: Path) -> Path | None:
    """将已校验的备份安全恢复到目标路径，返回恢复前留存的旧库快照路径。

    **顺序不可反**：先把备份复制到临时文件并完成完整性校验，全部通过后
    才删除目标库的 ``-wal`` / ``-shm`` 并原子换入。若先删 WAL 再复制，
    一旦复制或校验失败，目标库虽仍在原地却已丢掉只存在于 WAL 中的已提交
    事务——「恢复失败」会静默变成「数据回退」。

    Args:
        backup_path: 待恢复的备份文件路径。
        target_path: 目标 SQLite 数据库路径。

    Returns:
        恢复前留存的旧库快照路径；目标库原本不存在时返回 ``None``。

    Raises:
        FileNotFoundError: 备份文件不存在。
        RuntimeError: 备份或恢复后目标库完整性校验失败（此时目标库尚未
            被换入，``.pre-restore.*.bak`` 快照已留存可人工回滚）。
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在：{backup_path}")
    check_integrity(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    pre_backup: Path | None = None
    if target_path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        pre_backup = target_path.with_name(f"{target_path.name}.pre-restore.{ts}.bak")
        src_old = sqlite3.connect(str(target_path), timeout=30.0)
        dst_old = sqlite3.connect(str(pre_backup))
        try:
            src_old.backup(dst_old)
            dst_old.commit()
        finally:
            dst_old.close()
            src_old.close()

    tmp = target_path.with_name(f".{target_path.name}.restoring")
    src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    # 临时文件先校验通过，才动目标库（含 WAL/SHM）——见 docstring 的顺序约束
    try:
        check_integrity(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    # 清理旧 WAL/SHM，避免残留旧日志覆盖刚恢复的主库
    for suffix in ("-wal", "-shm"):
        Path(f"{target_path}{suffix}").unlink(missing_ok=True)

    tmp.replace(target_path)
    check_integrity(target_path)
    return pre_backup


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="SQLite WAL 安全在线备份与恢复工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bak = sub.add_parser("backup", help="在线热备份并校验完整性")
    p_bak.add_argument("--db", required=True, type=Path, help="源 SQLite 文件路径")
    p_bak.add_argument("--out-dir", required=True, type=Path, help="备份输出目录")
    p_bak.add_argument("--keep", type=int, default=7, help="保留最近 N 份（0=不清理）")

    p_res = sub.add_parser("restore", help="校验并安全恢复到目标库")
    p_res.add_argument("--backup", required=True, type=Path, help="备份文件路径")
    p_res.add_argument("--target", required=True, type=Path, help="目标 SQLite 文件路径")

    args = parser.parse_args()
    if args.cmd == "backup":
        dest = backup_db(args.db, args.out_dir, keep=args.keep)
        logger.info("backup_completed db=%s dest=%s", args.db, dest)
        return 0
    pre = restore_db(args.backup, args.target)
    logger.info(
        "restore_completed target=%s backup=%s pre_restore=%s",
        args.target,
        args.backup,
        pre,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
