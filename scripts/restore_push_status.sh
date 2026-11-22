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
#      .backup 不可用时退化为「checkpoint 后 cp」——checkpoint 必须**真的做完**
#      （busy≠0 说明主库文件还缺 WAL 里的最新页，cp 出来的快照会静默缺失
#      最近的写入，「整体回滚路径」就废了）→ 校验不通过即中止，不带着一个
#      不可靠的快照继续写库。
#   3) 按 id 分批（INTEGER PRIMARY KEY 即 rowid；BATCH 默认 5000 行），每批
#      独立 BEGIN IMMEDIATE / COMMIT，每批最多重试 3 次；任一批失败即**停手**
#      （已提交的批次保留，重跑幂等续做——UPDATE 带 `push_status <> 'pushed'`
#      守卫，重复执行不会改错；中途被 kill 时 SQLite 自动回滚那一批）。
#      批次表 _batch 用 `id INTEGER PRIMARY KEY`（rowid 索引）：UPDATE 里的
#      相关子查询按 rowid 命中，每批行访问是 O(BATCH)；不建主键时 SQLite
#      不会为相关子查询自动建索引，单批退化成 O(BATCH²) 的行访问。
#   4) 锁影响窗口：写锁**只在每批 BEGIN IMMEDIATE→COMMIT 之间**持有，批与
#      批之间释放（应用可插进来）。单批的行访问量级是万级（默认 5000 行），
#      实测耗时（本机，2 万行索引 / 每批 5000 行）：_batch 有主键 ~0.01s、
#      无主键 ~0.2s（批次 1 万行时 ~0.02s vs ~0.77s——无主键是 O(BATCH²)
#      的线性扫）。所以「每批毫秒级」只在有主键时成立，整轮锁窗口 =
#      批次数 × 单批耗时，**不是**固定的毫秒级。停应用的前提下对面板零
#      影响；万一忘了停应用，也只阻塞其它写者最多 busy_timeout（10s）后
#      本脚本报错退出，不留半提交（每批单事务）。
#   5) 每批用 `.bail on`：任一语句出错立即中止本批——否则失败语句之后的
#      COMMIT 仍会把后续语句提交掉，SELECT changes() 还会返回上一条语句的
#      陈旧值（实测「UPDATE 失败 → 后面语句照样提交 → 报出一个看似正常的
#      行数」）。批次成功还要求 changes() **等于**该批应恢复的行数，不等
#      即停手：UPDATE 的守卫会跳过已被标记 pushed 的行，行数对不上说明有
#      行被并发改动或已删除，不能当成「本批成功」。
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
    # wal_checkpoint(TRUNCATE) 返回三列：busy | WAL 页数 | 已 checkpoint 页数。
    # busy≠0（有活跃读者/写者）时主库文件仍缺 WAL 里的最新页，此时 cp 出来
    # 的快照会**静默缺失**最近的写入——「整体回滚路径」就废了。故必须校验
    # 它真的做完了（WAL 为空 → 0|-1|-1；有内容 → 0|N|N），否则中止。
    _ckpt=$(sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null) || {
      echo "checkpoint 失败，快照不可靠，已中止（未写入任何数据）" >&2; exit 1; }
    _busy=${_ckpt%%|*}
    _rest=${_ckpt#*|}
    _log=${_rest%%|*}
    _done=${_rest#*|}
    _ckpt_ok=0
    if [ "$_busy" = "0" ]; then
      if { [ "$_log" = "-1" ] && [ "$_done" = "-1" ]; } \
         || { [ "$_log" != "-1" ] && [ "$_log" = "$_done" ]; }; then
        _ckpt_ok=1
      fi
    fi
    [ "$_ckpt_ok" = "1" ] || {
      echo "checkpoint 未完成（${_ckpt}）：主库仍缺 WAL 里的最新页，" >&2
      echo "快照会静默缺失写入——已中止（未写入任何数据）。" >&2
      exit 1; }
    cp -p "$DB" "$BACKUP" || {
      echo "复制主库生成快照失败，已中止（未写入任何数据）" >&2; exit 1; }
  fi
  [ -s "$BACKUP" ] || { echo "快照失败，已中止（未写入任何数据）" >&2; exit 1; }
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
-- 按 id 去重：凭据 CSV 里若出现重复的 (site_id,item_id)，同一 id 会命中多行，
-- 导出的批次文件就会含重复 id（行数校验会误判、导入 INTEGER PRIMARY KEY 也会
-- 撞唯一约束）。取 pushed_at 的最大值（空串 < 任意非空值）。
CREATE TEMP VIEW _pending AS
SELECT id, max(coalesce(pushed_at, '')) AS pushed_at
FROM _match
WHERE identity_ok = 1 OR $INCLUDE_MISMATCH = 1
GROUP BY id;
.headers off
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
# write_batch <批次文件> <该批应恢复的行数>
# 成功时 stdout 输出实际恢复行数；失败返回 1（不输出行数）。
write_batch() {
  _part=$1
  _expected=$2
  _try=1
  while [ "$_try" -le 3 ]; do
    # .bail on：任一语句出错立即中止本批（否则失败语句之后的 COMMIT 仍会把
    # 后续语句提交掉，SELECT changes() 还会返回上一条语句的陈旧值）。
    # _batch 用 INTEGER PRIMARY KEY（rowid 索引），相关子查询按 rowid 命中，
    # 单批行访问 O(BATCH)——不建主键会退化成 O(BATCH²)。
    if _out=$(sqlite3 "$DB" <<SQL
.bail on
.output /dev/null
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
.output stdout
CREATE TEMP TABLE _batch(id INTEGER PRIMARY KEY, pushed_at TEXT);
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
    ); then
      # sqlite3 可能输出多余行，只取最后一行数字
      _changed=$(printf '%s\n' "$_out" | tail -n 1 | tr -d ' \r')
      case "$_changed" in
        ''|*[!0-9]*)
          echo "    无法解析本批 changes()（原始输出：${_out}）" >&2
          return 1 ;;
      esac
      if [ "$_changed" -ne "$_expected" ]; then
        echo "    本批实际更新 $_changed 行，与应恢复 $_expected 行不符" >&2
        echo "    （有行被并发改动/删除，或已被标记 pushed——不能当成成功）" >&2
        return 1
      fi
      printf '%s\n' "$_changed"
      return 0
    fi
    echo "    第 $_try 次写入失败（写锁争用？或 SQL 出错——详见上方 sqlite3 报错），2 秒后重试" >&2
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
  # 本批应恢复的行数 = 批次文件的数据行数（MATCHED 无表头、id 已去重）
  expected=$(wc -l < "$part" | tr -d ' \r\n')
  if ! changed=$(write_batch "$part" "$expected"); then
    echo "第 $npart 批写入失败（重试 3 次仍失败，或实际更新行数与应恢复行数不符），已中止。" >&2
    echo "已提交的批次保留（幂等，重跑会跳过已恢复行）；回滚见 $ROLLBACK_SQL" >&2
    exit 1
  fi
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
