#!/bin/sh
# 批次表有无主键的耗时对比（restore_push_status.sh 头部「锁影响窗口」那段
# 实测数字的复现脚本）：
#   无主键 → UPDATE 里的相关子查询无法走索引，每更新一行都线性扫一遍整批（O(BATCH²)）
#   有主键 → 子查询按 rowid 命中（O(BATCH)）
# 用法：sh scripts/bench_restore_batch_pk.sh   （BATCH / INDEX_ROWS 可覆盖）
set -eu
W=$(mktemp -d "${TMPDIR:-/tmp}/bench.XXXXXX")
trap 'rm -rf "$W"' EXIT

BATCH="${BATCH:-5000}"
INDEX_ROWS="${INDEX_ROWS:-20000}"

sqlite3 "$W/index.db" "CREATE TABLE torrent_index(id INTEGER PRIMARY KEY, push_status TEXT, pushed_at TEXT);"
i=1
while [ "$i" -le "$INDEX_ROWS" ]; do
  echo "INSERT INTO torrent_index VALUES($i,NULL,NULL);"
  i=$((i + 1))
done > "$W/seed.sql"
sqlite3 "$W/index.db" < "$W/seed.sql"

i=1
while [ "$i" -le "$BATCH" ]; do
  echo "$i,2026-11-21 10:00:00"
  i=$((i + 1))
done > "$W/batch.csv"

python3 - "$W" "$BATCH" "$INDEX_ROWS" <<'PY'
import shutil, subprocess, sys, time, pathlib
W, BATCH, INDEX_ROWS = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]

def build(decl):
    return "\n".join([
        ".output /dev/null",
        "PRAGMA busy_timeout=10000;",
        ".output stdout",
        f"CREATE TEMP TABLE _batch({decl});",
        ".mode csv",
        f".import {W}/batch.csv _batch",
        "BEGIN IMMEDIATE;",
        "UPDATE torrent_index",
        "SET push_status='pushed',",
        "    pushed_at=coalesce(",
        "      (SELECT nullif(b.pushed_at, '') FROM _batch b WHERE b.id = torrent_index.id),",
        "      pushed_at)",
        "WHERE id IN (SELECT id FROM _batch)",
        "  AND coalesce(push_status, '') <> 'pushed';",
        "COMMIT;",
        "SELECT changes();",
    ])

def run(decl, label):
    shutil.copy(W / "index.db", W / "work.db")
    (W / "run.sql").write_text(build(decl))
    t0 = time.perf_counter()
    out = subprocess.run(["sqlite3", str(W / "work.db")], stdin=open(W / "run.sql"),
                         capture_output=True, text=True)
    dt = time.perf_counter() - t0
    print(f"  {label:38s} {dt:6.2f}s（更新 {out.stdout.strip()} 行）")

print(f"索引 {INDEX_ROWS} 行 / 批次 {BATCH} 行：")
for _ in range(2):
    run("id INT, pushed_at TEXT", "无主键（_batch id INT）")
for _ in range(2):
    run("id INTEGER PRIMARY KEY, pushed_at TEXT", "有主键（_batch id INTEGER PRIMARY KEY）")
PY
