#!/bin/sh
# restore_push_status.sh 的本地自测（合成数据，不碰生产库）。
# 用法：sh scripts/selftest_restore_push_status.sh
set -eu

SCRIPT="$(cd "$(dirname "$0")" && pwd)/restore_push_status.sh"
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/rps-test.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT INT TERM

FAIL=0
ok()   { echo "  ✔ $1"; }
bad()  { echo "  ✘ $1"; FAIL=$((FAIL + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 [$3]，实际 [$2]）"; fi; }

mk_db() {
  # $1 = db 路径；$2 = 行数
  sqlite3 "$1" <<SQL
CREATE TABLE torrent_index(
  id INTEGER PRIMARY KEY,
  site_id INT NOT NULL,
  item_id TEXT,
  info_hash TEXT,
  name TEXT NOT NULL,
  size BIGINT DEFAULT 0,
  push_status TEXT,
  pushed_at TEXT);
CREATE UNIQUE INDEX uq_torrent_index_site_item ON torrent_index(site_id, item_id);
SQL
  i=1
  while [ "$i" -le "$2" ]; do
    sqlite3 "$1" "INSERT INTO torrent_index(site_id,item_id,name,size,push_status,pushed_at)
                 VALUES (1,'item-$i','name-$i',$((i * 100)),NULL,NULL);"
    i=$((i + 1))
  done
}

echo "== A. 正常恢复 =="
D="$ROOT/a"; mkdir -p "$D"
mk_db "$D/medialab.db" 10
{
  echo "site_id,item_id,name,size,pushed_at"
  i=1
  while [ "$i" -le 10 ]; do
    echo "1,item-$i,name-$i,$((i * 100)),2026-11-21 10:00:00"
    i=$((i + 1))
  done
} > "$D/cred.csv"
DB="$D/medialab.db" CSV="$D/cred.csv" APP_PORT=59999 sh "$SCRIPT" > "$D/out.log" 2>&1
check "退出码 0" "$?" "0"
check "10 行全部标记 pushed" \
  "$(sqlite3 "$D/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "10"
check "pushed_at 回填自凭据" \
  "$(sqlite3 "$D/medialab.db" "SELECT count(*) FROM torrent_index WHERE pushed_at='2026-11-21 10:00:00';")" "10"
check "写前快照存在" \
  "$(ls "$D"/medialab.db.bak.* 2>/dev/null | wc -l | tr -d ' ')" "1"
check "回滚 CSV 存在（10 行原值）" \
  "$(cat "$D"/pushed_restore_rollback_*.csv | wc -l | tr -d ' ')" "10"
check "回滚 SQL 存在（10 条还原语句）" \
  "$(grep -c '^UPDATE torrent_index' "$D"/pushed_restore_rollback_*.sql)" "10"

echo "== B. 幂等重跑（已恢复的行不再重复写）=="
DB="$D/medialab.db" CSV="$D/cred.csv" APP_PORT=59999 sh "$SCRIPT" > "$D/out2.log" 2>&1
check "退出码 0" "$?" "0"
check "提示「没有需要恢复的行」" \
  "$(grep -c '没有需要恢复的行' "$D/out2.log")" "1"

echo "== C. 回滚 SQL 可执行（逐行还原）=="
DB="$D/medialab.db" sqlite3 "$D/medialab.db" < "$(ls "$D"/pushed_restore_rollback_*.sql | head -1)"
check "还原后 pushed 行数归零" \
  "$(sqlite3 "$D/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "0"
check "还原后 pushed_at 归 NULL" \
  "$(sqlite3 "$D/medialab.db" "SELECT count(*) FROM torrent_index WHERE pushed_at IS NOT NULL;")" "0"

echo "== D. name/size 不一致 → 默认不写库 =="
D2="$ROOT/d"; mkdir -p "$D2"
mk_db "$D2/medialab.db" 3
{
  echo "site_id,item_id,name,size,pushed_at"
  echo "1,item-1,name-1,100,2026-11-21 10:00:00"
  echo "1,item-2,WRONG-NAME,200,2026-11-21 10:00:00"
  echo "1,item-3,name-3,300,2026-11-21 10:00:00"
} > "$D2/cred.csv"
DB="$D2/medialab.db" CSV="$D2/cred.csv" APP_PORT=59999 sh "$SCRIPT" > "$D2/out.log" 2>&1
check "退出码 0" "$?" "0"
check "只恢复匹配的 2 行（不一致行被跳过）" \
  "$(sqlite3 "$D2/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "2"
check "不一致行仍是 NULL" \
  "$(sqlite3 "$D2/medialab.db" "SELECT coalesce(push_status,'NULL') FROM torrent_index WHERE item_id='item-2';")" "NULL"

echo "== E. 凭据 CSV 含重复 (site_id,item_id) → 按 id 去重 =="
D3="$ROOT/e"; mkdir -p "$D3"
mk_db "$D3/medialab.db" 2
{
  echo "site_id,item_id,name,size,pushed_at"
  echo "1,item-1,name-1,100,2026-11-21 10:00:00"
  echo "1,item-1,name-1,100,2026-11-21 10:00:00"
  echo "1,item-2,name-2,200,2026-11-21 10:00:00"
} > "$D3/cred.csv"
DB="$D3/medialab.db" CSV="$D3/cred.csv" APP_PORT=59999 sh "$SCRIPT" > "$D3/out.log" 2>&1
check "退出码 0" "$?" "0"
check "恢复 2 行（重复行不重复计）" \
  "$(sqlite3 "$D3/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "2"

echo "== F. 分批（BATCH=2）后统计正确 =="
D4="$ROOT/f"; mkdir -p "$D4"
mk_db "$D4/medialab.db" 5
{
  echo "site_id,item_id,name,size,pushed_at"
  i=1
  while [ "$i" -le 5 ]; do
    echo "1,item-$i,name-$i,$((i * 100)),2026-11-21 10:00:00"
    i=$((i + 1))
  done
} > "$D4/cred.csv"
DB="$D4/medialab.db" CSV="$D4/cred.csv" BATCH=2 APP_PORT=59999 sh "$SCRIPT" > "$D4/out.log" 2>&1
check "退出码 0" "$?" "0"
check "5 行全部恢复" \
  "$(sqlite3 "$D4/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "5"
check "分批数 = 3" "$(grep -c '批次 [0-9]' "$D4/out.log")" "3"
check "累计恢复行数 = 5" \
  "$(grep -o '累计 [0-9]*' "$D4/out.log" | tail -n 1 | tr -d '累计 ')" "5"

echo "== G. UPDATE 失败 → 整批回滚、明确失败（不误报成功）=="
D5="$ROOT/g"; mkdir -p "$D5"
mk_db "$D5/medialab.db" 3
{
  echo "site_id,item_id,name,size,pushed_at"
  echo "1,item-1,name-1,100,2026-11-21 10:00:00"
  echo "1,item-2,name-2,200,2026-11-21 10:00:00"
  echo "1,item-3,name-3,300,2026-11-21 10:00:00"
} > "$D5/cred.csv"
# 让第 2 行的 UPDATE 必然失败（触发器 RAISE）
sqlite3 "$D5/medialab.db" "CREATE TRIGGER boom BEFORE UPDATE ON torrent_index
  WHEN OLD.id = 2 BEGIN SELECT RAISE(ABORT, 'boom'); END;"
set +e
DB="$D5/medialab.db" CSV="$D5/cred.csv" APP_PORT=59999 sh "$SCRIPT" > "$D5/out.log" 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ]; then ok "退出码非 0（${rc}）——未把失败当成功"; else bad "退出码 0——失败被当成成功"; fi
check "重试消息 3 次" "$(grep -c '次写入失败' "$D5/out.log")" "3"
check "批次中止消息 1 条" "$(grep -c '批写入失败' "$D5/out.log")" "1"
check "整批回滚（0 行被标记，触发器中止了本批事务）" \
  "$(sqlite3 "$D5/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "0"

echo "== H. 写锁被占用 → 重试后成功，最终行数正确 =="
D6="$ROOT/h"; mkdir -p "$D6"
mk_db "$D6/medialab.db" 2
{
  echo "site_id,item_id,name,size,pushed_at"
  echo "1,item-1,name-1,100,2026-11-21 10:00:00"
  echo "1,item-2,name-2,200,2026-11-21 10:00:00"
} > "$D6/cred.csv"
sqlite3 "$D6/medialab.db" "PRAGMA journal_mode=WAL;" >/dev/null
# 持住写锁 14s：第 1 次写入（busy_timeout 10s）必然失败，第 2 次重试时锁已释放
sqlite3 "$D6/medialab.db" >/dev/null 2>&1 <<'SQL' &
BEGIN EXCLUSIVE;
.shell sleep 14;
SQL
LOCKER=$!
sleep 0.5
set +e
DB="$D6/medialab.db" CSV="$D6/cred.csv" SKIP_BACKUP=1 APP_PORT=59999 \
  sh "$SCRIPT" > "$D6/out.log" 2>&1
rc=$?
set -e
kill "$LOCKER" 2>/dev/null || true
wait "$LOCKER" 2>/dev/null || true
check "退出码 0（重试后成功）" "$rc" "0"
check "有「写入失败」重试提示" \
  "$(grep -c '次写入失败' "$D6/out.log")" "1"
check "最终恢复 2 行" \
  "$(sqlite3 "$D6/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "2"
check "统计里的恢复行数正确（不是陈旧值）" \
  "$(grep -o '累计 [0-9]*' "$D6/out.log" | tail -n 1 | tr -d '累计 ')" "2"

echo "== I. checkpoint 未完成 → 中止，快照不可靠时不继续写 =="
D7="$ROOT/i"; mkdir -p "$D7"
mk_db "$D7/medialab.db" 2
{
  echo "site_id,item_id,name,size,pushed_at"
  echo "1,item-1,name-1,100,2026-11-21 10:00:00"
} > "$D7/cred.csv"
sqlite3 "$D7/medialab.db" "PRAGMA journal_mode=WAL;" >/dev/null
# 持住一个**写**事务（有未 checkpoint 的 WAL 帧）→ TRUNCATE checkpoint 会 busy；
# BACKUP 指到不可写路径让 .backup 先失败，逼脚本走 checkpoint 兜底分支
sqlite3 "$D7/medialab.db" >/dev/null 2>&1 <<'SQL' &
BEGIN IMMEDIATE;
INSERT INTO torrent_index(site_id,item_id,name,size) VALUES (9,'held','held',1);
.shell sleep 25;
SQL
HOLDER=$!
sleep 0.5
set +e
DB="$D7/medialab.db" CSV="$D7/cred.csv" BACKUP="$D7/nodir/x.db" APP_PORT=59999 \
  sh "$SCRIPT" > "$D7/out.log" 2>&1
rc=$?
set -e
kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true
if [ "$rc" -ne 0 ]; then ok "退出码非 0（${rc}）——快照不可靠时中止"; else bad "退出码 0——带着不可靠快照继续写了"; fi
check "输出里有「checkpoint 未完成」提示" \
  "$(grep -c 'checkpoint 未完成' "$D7/out.log")" "1"
check "库未被写入（push_status 仍为空）" \
  "$(sqlite3 "$D7/medialab.db" "SELECT count(*) FROM torrent_index WHERE push_status='pushed';")" "0"

echo
if [ "$FAIL" -eq 0 ]; then
  echo "全部通过"
else
  echo "失败 $FAIL 项"
  exit 1
fi
