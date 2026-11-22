# CHANGELOG

本文件只记录**对外契约**的变更（HTTP API 的状态码 / 响应体语义、推送
语义、持久化格式）。实现细节与内部重构不在此列——那些看 git 历史。

版本号：git tag `v<major>.<minor>.<patch>` → 镜像 tag `<major>.<minor>.<patch>`
与 `<major>.<minor>`（**无 `v` 前缀**，见 `DEPLOY.md`）。

---

## [Unreleased]

### Added

- **`GET /api/v1/scheduler` 新增业务域分栏字段**（纯新增，向后兼容）：
  - `jobs[]` 增 `module`（`seed` / `media`，作业所属业务模块）与
    `module_title`（页面展示名，如「辅种域」）；
  - 顶层增 `modules: [{key, title, job_count}]`（顺序 = 作业注册顺序，
    即页面上的分组顺序）。

  用途：面板「定时任务」页按业务域分成「辅种」「媒体库」两栏展示，
  不再混排成一张清单。`module_title` 与侧栏一级菜单同名（辅种 / 媒体库）。
  旧客户端忽略未知字段即可。

- **`GET /api/v1/scheduler` 新增数据流依赖字段**（纯新增，向后兼容）：
  `jobs[]` 增 `depends_on`（上游作业名，无依赖为 `null`）与
  `min_trigger_interval_sec`（依赖触发门槛）。用途：面板画「上游 → 下游」
  链路并标注触发条件。旧客户端忽略未知字段即可。

### Removed

- **移除 `refresh_user_data` 独立周期作业**（原 `*/30 * * * *`），其职责**并入
  `fetch_all_sites`**（见下一条 Changed）：

  `POST /scheduler/refresh_user_data/…` 调用方需改用 `fetch_all_sites`
  （`toggle` / `cron` / `config` 返 404；`run` 返 200 + `ok=false`——端点对照表
  见下方「Versioning & Deprecation」）。作业数 10 → 9。

### Changed

- **站点信息获取并入「站点拉取」作业**（`fetch_all_sites`）：

  两者本来就遍历**同一批启用站点**，合并后共用一条 cron 心跳（`*/10`）：

  1. 站点拉取 → 种子列表写 `torrent_index`（推送的候选来源）
  2. 站点信息获取 → 上传/下载/做种/时魔写 `site_user_data`（选种排序的输入）

  **行为影响**：

  - 用户数据新鲜度从「30 分钟」提升到 **10 分钟**——`push_and_monitor_free`
    的时魔封顶档排序跟着变准
  - 站点请求量：每站每轮多 1 次 `/mybonus.php`（9 站 × 6 轮/小时 ≈ 54 次/小时，
    原独立作业是 18 次/小时）。若需压回旧量级，在作业内给后半段加个时间门槛
    即可（`job_compose` 已支持段级跳过）
  - 作业历史里该作业现在有两步：`站点拉取` / `站点信息获取`（各自带逐站明细）
  - **两段各自容错**：一段抛错不影响另一段；返回的顶层字段
    （`total_written` / `scanned_sites`…）保持不变，用户数据结果另挂 `user_data` 键
  - 面板「站点」页打开时仍会自动测连并顺手刷新（`POST /sites/test-all` +
    `/user-data/refresh`，5 分钟冷却）——现在它是「额外的一次即时刷新」，
    不再是唯一的刷新时机

- **面板菜单拆分「设置」**：`/settings` 原为「调度作业 + 环境变量」两个
  tab，现拆成两个平级菜单——`/scheduler`（定时任务，辅种 / 媒体库分栏）与
  `/settings`（设置 · 环境变量），并移入新的「系统」一级菜单。
  两者都是 SPA 深链接，刷新仍回退到 `index.html`（无 HTTP 契约变化）。

- **免费种推送与免费窗口监测合并为一个作业**（`push_and_monitor_free`）：

  - 原两个作业 `push_free_torrents`（推送）与 `monitor_free_window`
    （监测）合并；cron 统一为 **10 分钟心跳**（监测要求），推送由作业
    **内部节流到 ≥30 分钟一次**（站点限频决定，与合并前频率一致）。
  - **面板变化**：「定时任务 → 辅种」少一个作业卡片，配置弹框里两个作业的
    运行参数（推送 + 监测共 5 项）合并到同一个弹框。
  - **配置迁移**（启动期自动执行）：`scheduler_job_settings` 里两条旧行合并为
    一条，**启停取 AND**——任一边被用户停用过，新作业也停用（不静默复活用户
    关掉的行为）；**cron 不搬**（旧节奏对新作业是错的），走代码默认值。
  - **历史**：`job_runs` 里旧作业名的记录保留（仍可查），新作业从合并后开始记账。
  - **依赖边变化**：`push_and_monitor_free` **不声明** `depends_on`——10 分钟心跳
    已比 30 分钟推送门槛更细，上游触发不会带来更早推送。`depends_on` /
    `min_trigger_interval_sec` 机制保留但当前无作业使用（`GET /scheduler` 仍下发，
    值为 `null` / `0`）。
  - **BREAKING（对调用 `POST /scheduler/{job}/...` 的脚本）**：`push_free_torrents`
    / `monitor_free_window` 这两个 job 名不再存在——`toggle` / `cron` / `config`
    **404**，`run` 返 **200 + `ok=false`**（脚本要按 `ok` 判成败，不能只看状态码）；
    改用 `push_and_monitor_free`。`GET /scheduler` 的 `jobs[]` 里也不会再出现旧名。

    **回滚 / 恢复**（启动期迁移是**不可逆**的 `DELETE`）：

    - **删除前先留快照**（三条删除路径都留：合并 / 孤儿清理 / 改名迁移）：
      旧行内容写进**新表 `scheduler_job_settings_backup`**（`job_name` /
      `enabled` / `cron` / `reason` / `deleted_at`），**与 `DELETE` 在同一
      事务**里——要么都成、要么都回滚，绝不出现「删了却没记录」；同时打一条
      启动日志（`作业配置删除快照（…）`）作为人读线索。**快照表才是恢复依据**
      （容器日志会轮转、随重建而丢，不能当唯一依据）；
    - **合并失败时旧行不会被删**（`prune_orphaned_settings` 收到 `keep` 保护，
      下次启动自动重试）——此时**没有快照**（因为没删），旧行本身还在库里，
      直接查 `scheduler_job_settings` 即可；且失败会登记到 `startup_status`
      ——`/readyz` 的 `degradations` 里能看到 `job_settings_merge`；
    - **风险评估**（本次迁移涉及的两处 schema / 数据变更）：
      ① 新增 `scheduler_job_settings_backup` 表 = SQLite `CREATE TABLE`，
      **只建空表**，不改任何既有表结构、不重建索引（写锁毫秒级，**不停机**）；
      ② 删的行数 ≤ 3（两条旧作业 + `refresh_user_data` 一行），单条小事务；
      ③ 回滚本迁移本身 = 停容器 → 跑下面的还原 SQL → 启动（当前镜像用 ②；
      ① 需同时把镜像回退到 v2.0.x，否则启动期迁移会再次删除旧行）；回滚
      新表 = 留表无害（旧代码读不到）或 `DROP TABLE scheduler_job_settings_backup`；
    - **还原步骤**（停容器后用 sqlite3 执行）——**两条路径不等价**，按目标选：

      **② 还原用户设置到新作业（当前镜像上唯一生效的路径，推荐）**：
      保留合并后的单一作业，只把启停 / cron 改回去。启停按 **AND** 语义
      还原（与合并逻辑一致：任一旧作业被停用过，新作业也停用），**不能**
      只取单边值——否则用户关掉的推送会被静默复活。

      ```sql
      -- ② 还原到新作业（保留 push_and_monitor_free，只还原启停 / cron）
      --    启停取 AND：任一边 enabled = 0 则新作业也 0
      --    cron 取 monitor_free_window 的快照；若两条旧行的 cron 不同，
      --    没有「正确的那条」可自动还原 → 需要人工二选一后手改
      UPDATE scheduler_job_settings
         SET enabled = (SELECT MIN(enabled) FROM scheduler_job_settings_backup b
                         WHERE b.job_name IN ('push_free_torrents', 'monitor_free_window')),
             cron    = (SELECT cron FROM scheduler_job_settings_backup b
                         WHERE b.job_name = 'monitor_free_window')
       WHERE job_name = 'push_and_monitor_free';
      ```

      **① 彻底回退到旧版本（仅在同时把镜像回退到 v2.0.x 时有效）**：
      当前镜像**已不再注册** `push_free_torrents` / `monitor_free_window`，
      插回的旧行不会被任何作业读取，而且**下次启动的合并迁移会把它们再次
      合并 / 删除**（新作业行已存在时不覆盖，旧行的 `enabled` / `cron` 会被
      丢弃）——只跑 SQL 不换镜像等于没回滚。正确顺序是：
      **停容器 → 跑下面的 SQL → 启动 v2.0.x 旧镜像**（顺序不可反）。

      ```sql
      -- ① 还原成两条旧作业（必须先回退镜像到 v2.0.x，否则启动即被再删）
      INSERT OR REPLACE INTO scheduler_job_settings (job_name, enabled, cron, updated_at)
      SELECT job_name, enabled, cron, datetime('now', 'localtime')
      FROM scheduler_job_settings_backup
      WHERE job_name IN ('push_free_torrents', 'monitor_free_window');
      ```

      **还原后校验**：`SELECT * FROM scheduler_job_settings_backup;` 与
      `SELECT * FROM scheduler_job_settings;` 对照，确认 `enabled` 与
      `cron` 符合预期（AND 语义下两边都启用才会是 `enabled = 1`）。

- **作业触发新增「数据流」能力**（`depends_on` + `min_trigger_interval_sec`）：

  上游作业本轮**成功后**可主动唤醒下游（"数据流触发"），cron 退为兜底。语
  义是**触发时机**而非数据正确性前提：上游失败时下游照旧按自己的 cron 跑。

  **当前没有作业使用它**——原唯一用户 `push_free_torrents` 已与
  `monitor_free_window` 合并为 `push_and_monitor_free`（见上一条），合并后
  cron 心跳（10 分钟）比推送门槛（30 分钟）更细，挂边等于永不生效。机制与
  测试保留，供将来「真依赖 + 更粗 cron」的作业使用。

  **注意**：挂边时 `cron` 必须**显著宽于**门槛，否则边是空操作（上游成功
  的时刻永远差一点够不到门槛，而那一刻 cron 自己也会醒）。`GET /scheduler`
  会下发 `depends_on` / `min_trigger_interval_sec`，当前全为 `null` / `0`。

### Versioning & Deprecation

- **语义化版本影响：minor（v2.1.0）**——`GET /scheduler` **只新增**字段
  （`module` / `module_title` / `modules[]` / `depends_on` /
  `min_trigger_interval_sec`），旧客户端忽略未知字段即可；破坏面**仅限**
  被移除的三个旧作业名（`refresh_user_data` / `push_free_torrents` /
  `monitor_free_window`）。失效**要按端点区分**：`toggle` / `cron` / `config`
  返 **404**（响亮失败，脚本会立刻报错）；`run` 返 **200 + `ok=false`**——
  **只查状态码的脚本会静默通过**，必须按 `ok` 判成败（与 2.0.0 那次
  「502 → 202」同属「状态码不再报错、判据要改」的一类，不能当无害升级）。
  端点对照表见下方「旧作业名的端点行为」。
- **弃用时间线**：旧作业名自 **v2.1.0 起立即不可调用**，**无兼容期**——面板自托管、
  前后端同版本发布，面板前端已在本 PR 内适配；**外部脚本须在升级镜像前改完**。

  | 旧作业名（已移除） | 新作业名 | 备注 |
  |---|---|---|
  | `refresh_user_data` | `fetch_all_sites` | 职责并入第二段「站点信息获取」 |
  | `push_free_torrents` | `push_and_monitor_free` | 与监测合并；启停取 AND |
  | `monitor_free_window` | `push_and_monitor_free` | 同上（两个旧名 → 一个新名） |

- **旧作业名的端点行为**（**不是所有端点都返 404**，脚本要按端点区分）：

  | 端点 | 旧作业名的响应 |
  |---|---|
  | `POST /scheduler/{job}/toggle` | **404**，detail `作业不存在：{job}` |
  | `POST /scheduler/{job}/cron` | **404**，detail `作业不存在：{job}` |
  | `GET /scheduler/{job}/config` | **404**，detail `作业不存在：{job}` |
  | `POST /scheduler/{job}/run` | **200** + `ok=false` + `error="unknown job: {job}"`（手动触发走 `run_now`，对未知作业名不抛 HTTP 异常——**必须按 `ok` 判成败**） |

- **`scheduler_job_settings` 的旧行**：启动期迁移把 `push_free_torrents` +
  `monitor_free_window` 合并成 `push_and_monitor_free` 一行（**启停取 AND**、
  **cron 不搬**）；`refresh_user_data` 的旧行按「孤儿清理」删除。迁移是**不可逆
  DELETE**——删除前把旧行内容**持久化到快照表 `scheduler_job_settings_backup`**
  （与 `DELETE` 同一事务，三条删除路径都留），并打一条启动日志
  （`作业配置删除快照（…）`）；恢复步骤见上文「免费种推送与免费窗口监测合并为
  一个作业 → 回滚 / 恢复」。
- **受影响调用方**：任何调用 `POST /scheduler/{job}/…`（手动触发 / 改 cron /
  改启停 / 看配置）的脚本与外部工具；`GET /scheduler` 的消费方只会看到
  `jobs[]` 少了三个名字、多了 `module` 等字段。
- **镜像 tag**：git tag `v2.1.0` → 镜像 tag `2.1.0` / `2.1`（见 `DEPLOY.md`）。
- **关联文档**：[`CHANGELOG.md`](CHANGELOG.md)（本页）；数据流方案与
  「实施后变更」见 [`docs/scheduler-dataflow.md`](docs/scheduler-dataflow.md)。

## [2.0.0] - 2026-11-21

### Changed（BREAKING）

- **手动推送端点：可重试的下载失败由 502 改为 202 + `deferred=true`**
  （`POST /torrent-index/{id}/push`，辅种域 `/api/v1/torrent-index/...`）。

  **破坏点**：站点**瞬时**异常（网络抖动、返回非 `.torrent` 的临时页）
  导致的下载失败，此前返回 **502**；现在返回 **202** 且响应体带
  `deferred=true`——含义是「**已受理，任务留在队列里自动重试**」，
  **不是失败**。实测多数会在几分钟内补推成功（2026-11-21 prod：12:05
  报 502，12:10 队列补推成功，用户看到的「失败」与最终结果不一致）。

  **迁移步骤**：

  1. 判定「是否失败」改看响应体字段 `deferred`：`true` = 已受理，
     **不要**当失败处理，也不要立即重复提交。
  2. 需要最终结果时，轮询任务队列 / 查拉取索引行的 `push_status`
     （`pushed` = 已推送成功）。
  3. **502 语义保留**：解析失败、致命错误（站点停用 / 下载器不可用）、
     站点的**永久失败**（凭据失效 / 详情页里没有下载地址）、以及
     **重试预算已用尽**（队列里的任务已成死信，不会自动再跑，需重新
     点击推送重置）——这四类仍返回 502。

  **受影响消费者**：面板「拉取索引」页的推送按钮（已适配 `deferred`）；
  批量推送端点 `POST /torrent-index/push-batch` 只入队不执行，语义不变；
  外部脚本若按 5xx 判定「推送失败」，需按上面第 1 步改。

  **版本与弃用**：按语义化版本属 **major（v2.0.0）**——HTTP **状态码语义**
  变了，外部脚本的「5xx = 失败」判定会**静默失效**（脚本不报错，只是把
  「已入队」当成失败），不能按 minor 让运维以为可无痛升级。旧 502 语义自
  v2.0.0 起**不再返回**，**无兼容期**（面板自托管、前后端同版本发布，面板
  前端已在本 PR 内适配）；**外部脚本须在升级镜像前改完**。镜像 tag：
  `v2.0.0` → `2.0.0` / `2.0`（见 `DEPLOY.md`）。

### Added

- 响应体新增 `deferred: bool` 字段（`POST /torrent-index/{id}/push`）。
  旧客户端忽略未知字段即可。

### Removed

- **辅种域「总览」页下线**（`/`）：侧栏的「总览」菜单项一并移除，**根路径
  `/` 现在重定向到 `/sites`**（辅种域第一个列表页）。后端
  `GET /api/v1/dashboard` 接口与 `dashboard_service` 一并删除（前端已无
  消费者）；媒体库域遗留的 `media/Dashboard.vue`（本就无路由引用）同时删除。
  - **统计信息的新查看位置**：启用站点 → 「站点」页；启用下载器 / 在途任务
    → 「下载器」页；索引总条数与免费类型分布 → 「拉取索引」页顶部的统计
    卡片（`GET /api/v1/torrent-index/stats`）；调度作业状态 → 「设置 →
    调度」；媒体条目数与总大小 → 「媒体库」页。

### Fixed

- **手动推送「重试已用尽」不再谎报 202**：队列里的重试预算用尽后任务
  已是死信，此时返回 202「已入队自动重试」与实际状态相反；改为返回
  **502** 并提示重新点击推送。
- **死信任务可手动重推**：幂等键命中死信任务时，`enqueue` 一律返回
  「已在推送队列中」，用户再点也推不上去；现在手动推送会**重置**死信
  任务（重试次数归零）并重新执行。
- **重置死信时同步清掉索引行的旧推送状态**：否则重推可能命中
  `push_status` 的幂等短路而「假成功」（种子压根没进下载器，死信却被
  成功掩埋）；清空后重推会真正执行，索引行的状态也与任务一致。
- **站点的永久失败不再伪装成「可重试」**：凭据失效、详情页里没有下载
  地址属于「重试也不会成功」，此前与瞬时故障混在一起统一报 202，用户
  白等重试；现在直接报错（502）并说明真实原因。
