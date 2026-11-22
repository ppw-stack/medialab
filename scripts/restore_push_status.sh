#!/bin/sh
# 恢复种子索引的 push_status='pushed' 标记（分批写 + 写前快照 + 回滚凭据）。
#
# 背景（2026-11-21）：生产种子索引被清空重建。索引行里的 push_status 是
# 「已推送过」的唯一凭据——push_free 的候选查询按它排除重复推送。
# 清空时凭据随行一起丢失，已备份到同目录的 pushed_restore_*.csv。
#
# 为什么需要反复跑：torrent_index 是**滚动窗口**（fetch_all_sites 每站 3 页
# × 200，最新的 300 条/站），旧的种子会随时间一轮轮滚进窗口。本脚本把
# 当前窗口里能匹配上的行补回 'pushed'，窗口滚得越多、恢复得越多。
#
# 匹配键：site_id + item_id + name + size（三重校验）。
#   · (site_id, item_id) 跨轮次稳定，且唯一约束 uq_torrent_index_site_item
#     提供索引 —— 匹配**不是**全表扫描，锁风险来自 UPDATE 本身（故分批次）。
#   · 但 item_id 可能被站点复用、或 CSV 条目错配 → 再加 name + size 身份
#     校验；只命中键而 name/size 不一致的行**默认不写库**，只计数上报。
#     确认 CSV 名称口径漂移（导致大面积「不一致」）时，可用
#     INCLUDE_MISMATCH=1 放宽为仅按 (site_id, item_id) 匹配。
#   · 不能用 info_hash：列表页解析不产出 hash，只有推送成功后由下载器回填。
#
# 写库安全（评审要求：锁表评估 + 分批 + 回滚方案）：
#   1) **先停应用**：应用持单实例租约、且是应用独占的 WAL 库的唯一写者；
#      裸 sqlite3 与应用并发写会撞写锁（busy_timeout 10s），也可能互相
#      打断留下半提交。脚本启动时探测 /healthz，应用还在跑就拒绝执行
#      （确认无误可用 FORCE=1 跳过）。
#   2) 写前 `sqlite3 .backup` 一份快照，并导出「将被修改的行」的原始
#      push_status / pushed_at：
#        · pushed_restore_rollback_<时间戳>.csv —— 原值明细（人工核对用）
#        · pushed_restore_rollback_<时间戳>.sql —— 可直接执行的逐行还原语句
#   3) 按 id 分批（INTEGER PRIMARY KEY 即 rowid；BATCH 默认 5000 行），每批
#      独立 BEGIN IMMEDIATE / COMMIT，每批最多重试 3 次；任一批失败即**停手**
#      （已提交的批次保留，重跑幂等续做——UPDATE 带 `push_status <> 'pushed'`
#      守卫，重复执行不会改错；中途被 kill 时 SQLite 自动回滚那一批）。
#   4) 锁影响窗口：每批只在 UPDATE→COMMIT 之间持写锁（毫秒级）。停应用的
#      前提下对面板零影响；万一忘了停应用，也只阻塞其它写者最多
#      busy_timeout（10s）后本脚本报错退出，不留半提交（每批单事务）。
#   PRAGMA 与应用一致（app/db/session.py）：journal_mode=WAL、
#   synchronous=NORMAL、foreign_keys=ON、busy_timeout=10000。
#
# 回滚（两种）：
#   ① 逐行还原：sqlite3 "$DB" < pushed_restore_rollback_<时间戳>.sql
#   ② 整体还原：停应用 → 用写前快照覆盖 DB（连 -wal / -shm 一起删）
#
# 用法（**先停应用**，再跑，最后启应用）：
#   docker compose -f /mnt/user/appdata/medialab/app/docker-compose.prod.yml \
#     --env-file /mnt/user/appdata/medialab/app/.env.prod stop medialab
#   ssh <生产机> 'sh /mnt/user/appdata/medialab/app/restore_push_status.sh'
#   docker compose -f /mnt/user/appdata/medialab/app/docker-compose.prod.yml \
#     --env-file /mnt/user/appdata/medialab/app/.env.prod start medialab
#
# 环境变量：
#   DB=...            数据库路径
#   CSV=...           凭据 CSV（列：site_id,item_id,name,size,pushed_at）
#   BATCH=5000        每批行数
#   APP_PORT=8092     探测「应用是否已停」的端口
#   FORCE=0           1 = 跳过「应用是否已停」探测
#   INCLUDE_MISMATCH=0  1 = name/size 不一致也写（仅按 (site_id,item_id) 匹配）
#   SKIP_BACKUP=0     1 = 不生成写前快照（不推荐：会失去整体回滚路径）
set -eu

DB="${DB:-/mnt/user/appdata/medialab/data/medialab.db}"
CSV="${CSV:-/mnt/user/appdata/medialab/data/pushed_restore_20261121.csv}"
BATCH="${BATCH:-5000}"
APP_PORT="${APP_PORT:-8092}"
FORCE="${FORCE:-0}"
INCLUDE_MISMATCH="${INCLUDE_MISMATCH:-0}"
SKIP_BACKUP="${SKIP_BACKUP:-0}"

[ -f "$DB" ] || { echo "找不到数据库：$DB" >&2; exit 1; }
[ -f "$CSV" ] || { echo "找不到凭据 CSV：$CSV" >&2; exit 1; }
case "$BATCH" in
  ''|*[!0-9]*|0) echo "BATCH 必须是正整数：$BATCH" >&2; exit 1 ;;
esac
case "$INCLUDE_MISMATCH" in
  0|1) ;;
  *) echo "INCLUDE_MISMATCH 只能是 0 或 1：$INCLUDE_MISMATCH" >&2; exit 1 ;;
esac

OUTDIR=$(dirname "$DB")
STAMP=$(date +%Y%m%d%H%M%S)
# 同一秒内重复执行时也不要覆盖上一轮的回滚凭据 / 快照
SUFFIX=""
_n=0
while [ -e "$OUTDIR/pushed_restore_rollback_$STAMP$SUFFIX.csv" ] \
   || [ -e "$OUTDIR/pushed_restore_rollback_$STAMP$SUFFIX.sql" ] \
   || [ -e "$DB.bak.$STAMP$SUFFIX" ]; do
  _n=$((_n + 1))
  SUFFIX="_$_n"
done
BACKUP="${BACKUP:-$DB.bak.$STAMP$SUFFIX}"
ROLLBACK_CSV="$OUTDIR/pushed_restore_rollback_$STAMP$SUFFIX.csv"
ROLLBACK_SQL="$OUTDIR/pushed_restore_rollback_$STAMP$SUFFIX.sql"
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/restore_push_status.XXXXXX")
MATCHED="$WORKDIR/matched.csv"
trap 'rm -rf "$WORKDIR"' EXIT INT TERM

# --- 0. 应用必须先停：它是这个 WAL 库的唯一写者 -------------------------
if [ "$FORCE" != "1" ]; then
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 2 "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then
      echo "应用仍在运行（127.0.0.1:$APP_PORT/healthz 可达）。" >&2
      echo "请先停应用再跑（见脚本头部）；确认无误可用 FORCE=1 跳过本检查。" >&2
      exit 1
    fi
  else
    echo "未找到 curl，跳过「应用是否已停」探测——请自行确认应用已停。" >&2
  fi
fi

# --- 1. 写前快照（整体回滚路径） ----------------------------------------
if [ "$SKIP_BACKUP" = "1" ]; then
  echo "跳过写前快照（SKIP_BACKUP=1）"
else
  echo "生成写前快照：$BACKUP"
  if ! sqlite3 "$DB" ".backup $BACKUP" 2>/dev/null; then
    echo "  .backup 不可用，改为 checkpoint 后复制"
    sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
    cp -p "$DB" "$BACKUP"
  fi
  [ -f "$BACKUP" ] || { echo "快照失败，已中止（未写入任何数据）" >&2; exit 1; }
fi

# --- 2. 导入凭据、算待恢复集合、导出回滚凭据（只读） ---------------------
echo "导入凭据并计算待恢复集合……"
sqlite3 "$DB" <<SQL
.output /dev/null
PRAGMA busy_timeout=10000;
.output stdout
CREATE TEMP TABLE _restore_tmp(
  site_id INT, item_id TEXT, name TEXT, size BIGINT, pushed_at TEXT);
.mode csv
.import --skip 1 $CSV _restore_tmp
CREATE TEMP VIEW _match AS
SELECT t.id AS id,
       r.pushed_at AS pushed_at,
       (t.name = r.name AND t.size = r.size) AS identity_ok
FROM torrent_index t
JOIN _restore_tmp r
  ON r.site_id = t.site_id
 AND r.item_id = t.item_id
WHERE coalesce(t.push_status, '') <> 'pushed';
CREATE TEMP VIEW _pending AS
SELECT id, pushed_at FROM _match
WHERE identity_ok = 1 OR $INCLUDE_MISMATCH = 1;
.output $MATCHED
SELECT id, coalesce(pushed_at, '') FROM _pending ORDER BY id;
.output $ROLLBACK_CSV
SELECT t.id,
       t.site_id,
       coalesce(t.item_id, ''),
       coalesce(t.push_status, ''),
       coalesce(t.pushed_at, '')
FROM torrent_index t
JOIN _pending p ON p.id = t.id
ORDER BY t.id;
.mode list
.output $ROLLBACK_SQL
SELECT 'UPDATE torrent_index SET push_status='
       || coalesce(quote(nullif(t.push_status, '')), 'NULL')
       || ', pushed_at=' || coalesce(quote(nullif(t.pushed_at, '')), 'NULL')
       || ' WHERE id=' || t.id || ';'
FROM torrent_index t
JOIN _pending p ON p.id = t.id
ORDER BY t.id;
.output stdout
SELECT 'CSV 凭据行数: ' || count(*) FROM _restore_tmp;
SELECT '已匹配索引（(site_id,item_id) 命中）: ' || count(*) FROM _match;
SELECT '其中 name/size 不一致（默认跳过）: ' || count(*) FROM _match
 WHERE identity_ok = 0;
SELECT '本次待恢复行数: ' || count(*) FROM _pending;
SELECT '仍未匹配（尚未滚进窗口）: ' || count(*) FROM _restore_tmp r
 WHERE NOT EXISTS (SELECT 1 FROM torrent_index t
                    WHERE t.site_id = r.site_id AND t.item_id = r.item_id);
SELECT '索引里已标记 pushed 的命中行: ' || count(*) FROM _restore_tmp r
 WHERE EXISTS (SELECT 1 FROM torrent_index t
                WHERE t.site_id = r.site_id AND t.item_id = r.item_id
                  AND coalesce(t.push_status, '') = 'pushed');
SQL

if [ ! -s "$MATCHED" ]; then
  echo "没有需要恢复的行（窗口内已全部匹配完成）。"
  rm -f "$ROLLBACK_CSV" "$ROLLBACK_SQL"
  exit 0
fi

# --- 3. 分批写入：每批独立事务 + 重试，任一批失败即停手 -------------------
write_batch() {
  _part=$1
  _try=1
  while [ "$_try" -le 3 ]; do
    if sqlite3 "$DB" <<SQL
.output /dev/null
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
.output stdout
CREATE TEMP TABLE _batch(id INT, pushed_at TEXT);
.mode csv
.import $_part _batch
BEGIN IMMEDIATE;
UPDATE torrent_index
SET push_status='pushed',
    pushed_at=coalesce(
      (SELECT nullif(b.pushed_at, '') FROM _batch b WHERE b.id = torrent_index.id),
      pushed_at)
WHERE id IN (SELECT id FROM _batch)
  AND coalesce(push_status, '') <> 'pushed';
COMMIT;
SELECT changes();
SQL
    then
      return 0
    fi
    echo "    第 $_try 次写入失败（写锁争用？），2 秒后重试" >&2
    sleep 2
    _try=$((_try + 1))
  done
  return 1
}

echo "开始分批写入（每批 $BATCH 行）……"
split -l "$BATCH" "$MATCHED" "$MATCHED.part."
total=0
npart=0
for part in "$MATCHED".part.*; do
  [ -f "$part" ] || continue
  npart=$((npart + 1))
  if ! changed=$(write_batch "$part"); then
    echo "第 $npart 批写入失败（重试 3 次仍失败），已中止。" >&2
    echo "已提交的批次保留（幂等，重跑会跳过已恢复行）；回滚见 $ROLLBACK_SQL" >&2
    exit 1
  fi
  changed=${changed:-0}
  # sqlite3 可能输出多余行（PRAGMA 回显等），只取最后一行数字
  changed=$(printf '%s\n' "$changed" | tail -n 1 | tr -d ' \r')
  total=$((total + changed))
  echo "  批次 ${npart}：恢复 ${changed} 行（累计 ${total}）"
done

# --- 4. 收尾统计 ---------------------------------------------------------
sqlite3 "$DB" <<SQL
.output /dev/null
PRAGMA busy_timeout=10000;
.output stdout
CREATE TEMP TABLE _restore_tmp(
  site_id INT, item_id TEXT, name TEXT, size BIGINT, pushed_at TEXT);
.mode csv
.import --skip 1 $CSV _restore_tmp
SELECT '本次恢复行数: ' || $total;
SELECT '当前索引总行数: ' || count(*) FROM torrent_index;
SELECT '索引中 pushed 行总数: ' || count(*) FROM torrent_index
 WHERE push_status = 'pushed';
SELECT '仍未匹配（尚未滚进窗口）: ' || count(*) FROM _restore_tmp r
 WHERE NOT EXISTS (SELECT 1 FROM torrent_index t
                    WHERE t.site_id = r.site_id AND t.item_id = r.item_id);
SQL

echo "回滚凭据："
echo "  原值明细：$ROLLBACK_CSV"
echo "  逐行还原：$ROLLBACK_SQL"
echo "  整体快照：${BACKUP}（SKIP_BACKUP=1 时无）"
