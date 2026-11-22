#!/bin/sh
# 恢复种子索引的 push_status='pushed' 标记。
#
# 背景（2026-11-21）：生产种子索引被清空重建。索引行里的 push_status 是
# 「已推送过」的唯一凭据——push_free 的候选查询按它排除重复推送。
# 清空时凭据随行一起丢失，已备份到同目录的 pushed_restore_*.csv。
#
# 为什么需要反复跑：torrent_index 是**滚动窗口**（fetch_all_sites 每站 3 页
# × 200，最新的 300 条/站），旧的种子会随时间一轮轮滚进窗口。本脚本把
# 当前窗口里能匹配上的行补回 'pushed'，窗口滚得越多、恢复得越多。
#
# 匹配键：(site_id, item_id)——item_id 是站点侧条目标识，跨轮次稳定。
# （不能用 info_hash：列表页解析不产出 hash，只有推送成功后由下载器回填。）
#
# 用法：ssh <生产机> 'sh /mnt/user/appdata/medialab/app/restore_push_status.sh'
set -e

DB="${DB:-/mnt/user/appdata/medialab/data/medialab.db}"
CSV="${CSV:-/mnt/user/appdata/medialab/data/pushed_restore_20261121.csv}"

[ -f "$DB" ] || { echo "找不到数据库：$DB" >&2; exit 1; }
[ -f "$CSV" ] || { echo "找不到凭据 CSV：$CSV" >&2; exit 1; }

sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;
CREATE TABLE IF NOT EXISTS _restore_tmp(
  site_id INT, item_id TEXT, name TEXT, size BIGINT, pushed_at TEXT);
DELETE FROM _restore_tmp;
.mode csv
.import --skip 1 '$CSV' _restore_tmp
BEGIN;
UPDATE torrent_index
SET push_status='pushed',
    pushed_at=coalesce(
      (SELECT r.pushed_at FROM _restore_tmp r
        WHERE r.site_id=torrent_index.site_id
          AND r.item_id=torrent_index.item_id),
      pushed_at)
WHERE (site_id, item_id) IN (SELECT site_id, item_id FROM _restore_tmp)
  AND coalesce(push_status,'') <> 'pushed';
COMMIT;
SELECT '本次恢复行数: ' || changes();
SELECT '当前索引总行数: ' || count(*) FROM torrent_index;
SELECT '凭据总数: ' || count(*) FROM _restore_tmp;
SELECT '仍未匹配（尚未滚进窗口）: ' || count(*) FROM _restore_tmp r
  WHERE NOT EXISTS (SELECT 1 FROM torrent_index t
                     WHERE t.site_id=r.site_id AND t.item_id=r.item_id);
DROP TABLE _restore_tmp;
SQL
