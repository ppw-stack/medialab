# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `README.md` — 现状与命令。
2. `docs/merge-strmmanger.md` — **合并 strmmanger 的方案与实施记录**（含冲突清单与返工范围）。
3. `app/adapters/sites/user_data.py` + `app/adapters/sites/parsers/nexus_php.py` — FR-1.6 站点用户数据契约。
4. `app/adapters/sites/parsers/attendance.py` — FR-3.6 站点签到解析 helper。
5. `docs/upload-bandwidth.md` — **数据同步上传的带宽瓶颈实测结论**（并发为何无效）。

## 项目定位

**一个应用装下两个业务域**，共用一套面板、一个 SQLite、一个 SPA：

- **辅种域**（原 ptHelper）—— PT 站点拉取 / 免费种窗口 / 辅种巡检 / 孤儿清理
- **媒体库域**（原 strmmanger）—— 网盘 STRM 生成 / 302 中转 / WebDAV / 数据同步（本地 → 网盘）

**两个域的 API 前缀天然不冲突**：辅种域 `/api/v1/*`，媒体库域 `/api/*`。

## 工作准则
- **分层约束是硬性的**（`tests/unit/test_layering.py` 固化）：
  ```
  domain → core → db → adapters → services → api
  ```
  - `api` 层**不碰 DB、不碰适配层**——业务逻辑全在 service 层
  - `services` 层**不 import fastapi**，抛 `ServiceError` 由 API 层翻译成 HTTP 状态码
  - `adapters` 层可独立单测（不依赖 db / services）
  - `domain` 层是业务概念与常量（如 `DeviceType` / `FreeType`），不依赖任何层
  - 改完跑 `uv run pytest tests/unit/test_layering.py` 验证
- **时间口径已统一为北京时间字符串**（2026-10-30 修订，原「两个域的时间列口径不同，勿顺手统一」段废止）：
  - 两域所有「DB 记录时刻」类列（`created_at` / `updated_at` / `last_*_at` / `probed_at` / `mtime` 等）一律 `String(32)` 北京时间字符串
  - **协议侧时刻**保留 unix 秒（`Integer`）——它们与 `time.time()` 直接比较，语义是「绝对时刻/时间差」而非「DB 记录时刻」：
    - `link_cache.expires_at` —— L2 缓存过期时刻
    - `cross_seed_snapshot.stats[].added_at` —— 下载器入队时刻（JSON 内）
    - 进程内 token 有效期（非 DB 列）
  - 统一判据是**语义**，不是「属于哪个域」：辅种域的 `added_at` 是 unix 秒（协议侧），媒体库域的 `created_at` 是字符串（DB 记录时刻）
  - `0 = 从未` 语义统一为 `NULL`（`last_*_at` 列 nullable）；代码里判断「有过」用 `is not None`，不用 `> 0`
  - 网盘 adapter 返回的 `FileEntry.mtime` 仍是 unix 秒（协议契约），service 层消费时用 `format_bj(datetime.fromtimestamp(ts, UTC))` 转换写入
- 改动窗口锚定（FR-3.4）或中止链路（FR-3.5）前，先确认需求基线与现状口径一致。
- 数据删除以"不误删"为底线：hash 必须是 40 位 hex（拒绝 `all` / 通配），前端删除需确认，删磁盘数据单独开关并记审计日志。不引入回收站 / 保护期等未实现的机制。
- **删除站点 = 级联删关联数据**（2026-10-23 决定）：`torrent_index`（拉取快照，可重建）+ `site_user_data`（用户数据快照）+ `task_queue`（该站推送队列行）在**同一事务**里删完再删父行。
  - **不要**用"引用计数 > 0 就 409 拦住"——`torrent_index` 不是任务表，全仓没有删除它的入口，且 `fetch_all_sites` 每 10 分钟就重新灌满 → 那个 409 对任何被拉取过的站点都是**永久死锁**
  - `task_queue` 也要删：SQLite 删行后**会复用 id**，`idem_key` 格式是 `add_torrent|{site_id}|{item_id}|dl={dl}`，留着会让新站误命中老幂等键 → 推送被永久判为"已推过"
  - 前缀必须带结尾的 `|`（`add_torrent|1|` 不匹配 `add_torrent|11|...`）
  - 契约由 `tests/integration/test_services.py` + `tests/integration/test_api.py` 固化
- 站点适配层改动后用真实站点（测试环境 cookie）拉取验证，不使用 mock 数据测试。
- **数据同步（本地 → 网盘）按驱动声明支持哪些网盘**（2026-10-31 加入）：
  - 上传能力走 **`UploadDriver` 协议**（`app/adapters/drivers/upload_base.py`），与读能力 `CloudDriver`（`base.py`）分开——不是所有网盘都能写
  - **哪些网盘支持上传由 `UPLOADABLE_DRIVER_TYPES` 单一真源声明**；后端校验用 `is_uploadable()`，前端靠 `GET /api/upload-tasks/capabilities` 过滤下拉
  - **加新网盘只需三步**：实现 `UploadDriver` → 在 `UPLOADABLE_DRIVER_TYPES` 登记 → 在 `app/services/upload_driver_factory.py` 加构造分支。**前端零改动**
  - 业务代码**不写 `if driver.type == "guangya"`**——一律走工厂 + 协议
  - 上传流程的幂等判据（**同名 + 同大小 = 已同步**）、覆盖流程（删旧 → 确认消失 → 传新）、清本地的三重保护（仅 `uploaded`/`overwritten` + 大小一致 + `delete_after_upload` 显式开启）**与网盘无关**，别改
  - 契约由 `tests/unit/test_upload_base.py` + `tests/integration/test_upload_tasks_api.py` 固化
- **清本地时，空目录一并删**（2026-11-03 加入，`app/services/upload_cleanup.py`）：
  - 勾了「同步成功后删除本地文件」= 「传走的东西本地不留痕」→ 文件删完后**向上递归删连续的空父目录**，直到监听根（**监听根本身永不删**）
  - **只用 `os.rmdir`，绝不用 `rmtree`**——`rmdir` 只能删空目录（非空必然 `ENOTEMPTY`），这是**内核级保证**，没有「先 list 再判断空」的 TOCTOU 窗口。`rmtree` 会递归删非空目录，是灾难性后果
  - **四道安全闸**（`is_safe_to_prune_dir`）：① 不是监听根 ② 不是软链 ③ realpath 在监听根内 ④ 相对路径任一段不以 `.` 开头（`.strmmanger-trash` 等隐藏目录不动）
  - **errno 分级**（`_try_rmdir`）：`ENOENT` → 已删（幂等）→ **继续向上**；`ENOTEMPTY`/`EEXIST` → 非空 → **停止向上**（父目录必然也非空），重试 2 次（CIFS 上 `unlink` 后目录项可能还在服务端缓存）；其它（EACCES/CIFS 错误）→ 记 warning，停止向上
  - 删目录与删文件在**同一个 `to_thread`** 里完成——避免「文件删了目录还没删」的中间态暴露给监听器
  - **云端目录不动**（`UploadDriver` 只有 `delete_file`，无删目录能力；要删云端空目录得扩协议，是独立工程）
  - **不加独立开关**——绑定在已有的 `delete_after_upload` 上（用户原话：「如果选择删除，文件夹一并删除」）
  - 契约由 `tests/unit/test_upload_cleanup.py` 固化
- **大文件上传只能串行；小文件通道并发**（2026-11-02 实测，2026-11-07 修订为双通道；见 `docs/upload-bandwidth.md`）：
  - **瓶颈是本机上行带宽**（实测 ~3.2 MB/s，到阿里云 OSS 张家口 RTT 31ms）→ 分片大小几乎不影响速率（1/5/20 片都是 2.25-2.75 MB/s），是**带宽受限非 RTT 受限**
  - **大文件别再尝试并发**：同一 uploadId 分片并发 → 零收益或卡死；不同 uploadId 并发 → `Server disconnected` / 卡死，且总吞吐**低于**串行。`CHUNK_CONCURRENCY = 1`（`guangya_upload.py`）+ 大文件通道单 worker 是**正确实现，不是待优化的遗留**
  - **唯一支持并发的是小文件单 PUT**（<1MB，不分片）——耗时大头是协议开销（取凭证 + 轮询落盘确认 ~2-3s/个，传字节占比 <15%），并发摊掉轮询等待
  - **双通道**（`watch_manager.py`，2026-11-07）：size < `upload_small_file_threshold`（默认 1MB）→ 小文件队列 K 个 worker；≥ 阈值 → 大文件队列**恒 1 个 worker**。`concurrency` 列语义落地 = **小文件通道并发**（默认 4，单一真源 `app/domain/upload_types.py:DEFAULT_UPLOAD_CONCURRENCY`；面板可配 1-8）——旧「保留但不生效」废止；存量任务的占位值 1 由一次性迁移回填为 4（`backfill_upload_concurrency`，标记防重跑）
  - **动态并发闸 `_SmallGate`**：大文件上传中自动降为 1 让路；连续失败 3 次熔断（冷却 60s + 串行观察，连续成功 10 次才恢复并发）——「并发卡死」的保险丝
  - **全局「处理中」去重 `_INFLIGHT`**：同文件绝不被两个协程同时传——光鸭同名上传自动改名 `xxx(1).bin`，双传 = 云端垃圾副本。事件流双通道之间、事件流与全量同步之间全靠它互斥（小通道**不持** `_SYNC_LOCKS`）
  - **队列满不丢**：溢出暂存（按路径去重，上限 5000）→ worker 空闲回灌；只有暂存也满才真丢并计数。水位（大/小/溢出/丢弃）暴露到面板——过去只能靠刷屏的丢弃日志感知积压
  - **同名冲突策略 `upload_tasks.conflict_policy`**（2026-11-09 加入，2026-11-12 修订清理语义）：同名但**大小不同**时——`1`=**保留大的**（默认；本地更小则跳过上传、保住云端那份）、`0`=本地优先（删旧→传新，历史行为）。**为什么默认保留大的**：本地重新压制出的版本往往更小（去音轨/换压制组），直接覆盖等于用小版本顶掉大版本——真实踩过的坑。「更大」≠「更新」（同大小仍按「已同步」跳过，不比哈希）
  - **「保留大的」时本地小的也删**（2026-11-12 用户要求「保留大的，移除小的」）：策略 1 + 云端更大 + `delete_local` 开 → **本地原件一并删除**。实现是 `SyncOutcome.cloud_kept=True` 让清理闸从「云端 == 本地」放宽为「云端 ≥ 本地」——云端那份是按策略保留的完整版本，本地小的没有保留价值。注意：**只有策略 1 的「云端更大」分支走放宽**；上传后路径（==）与「云端 < 本地」的防御分支仍按原语义拦下。生产实例：`伟大的长征 S01E43`（云端 1550731096 / 本地 1532422131 字节）
  - ⚠️ **方法论**：测上传吞吐必须用**生产规模**文件（≥100MB）且做**串行/并发交替对照**。曾用 50MB/10 片测出「并发快 2 倍」的**假象**，据此写了两版并发实现再逐一证伪
- **「离线下载」（本地 → 光鸭）功能已移除**（2026-11-11 整体删除，含页面与配置）：
  - **移除依据（实测结论，值得记住）**：功能可行但**速率更差**——文件完整落地光鸭、md5 一致，实际速率 **~0.75 MB/s**（比直传的 3.3 MB/s **慢 4.4 倍**）。链路不是瓶颈（公网入口单个 100MB 分片实测 **17.2 MB/s**），真正原因是**光鸭把文件切成变长的随机区间并并发拉取，其中 73% 是重复请求**（5MB 文件同一区间被拉 130 次）——光鸭下载器的固有行为，客户端不可控
  - **它什么时候才划算**：源文件**本来就在大带宽 CDN 上**（如 115 直链）→ 那正是**迁移功能**（115→光鸭）的场景。**「本地 → 光鸭」走离线下载反而更慢**
  - **随之删除**：6 个后端文件（`offline_download` / `offline_files` / `offline_config` / `offline_jobs` + 两个路由）+ 「离线下载」页 + 三项环境变量配置（`OFFLINE_HMAC_SECRET` / `OFFLINE_PUBLIC_BASE` / `OFFLINE_FILES_ROOT`）。存量配置行由幂等迁移 `delete_offline_env_settings` 清掉（连带那个死密钥）
  - **光鸭的「离线任务」机制仍在用**（迁移功能）：`guangya.py` 的 `create_offline_task` / `list_active_offline_tasks` / `list_failed_offline_tasks` **保留**，别跟着删
  - **仍然有效的坑（2026-11-11 实测补充）**：光鸭的 `progress` 字段**不是可靠的百分比**（2.3GB 文件下载到 88% 时它只有 4）→ **不用它判定完成，也不再展示**——迁移在途列表只报「路径 + 大小」（假百分比曾误导用户，已随展示一起移除）。判定走「活跃列表消失 + 目标目录同名同大小校验」。实测还确认：任务项初始结构**没有** `progress` 字段（只有 taskId/fileName/totalSize/status/createTime/res/parentDirType），它只在真实拉取时动态出现；对**非媒体 URL**（TUNA 的 .iso/.gz）云添加会挂 3 分钟后统一 `code=8 云添加失败`——历史成功案例全是媒体文件（.mkv / 115 直链），**疑似云添加只接受媒体类 URL（待证）**；`get_task_status()` 是**上传任务**的接口，查云下载任务返回 145；**中途删目标目录会静默终止或卡住任务**（探测时踩到：一个 15MB 任务卡在活跃列表不结束）
- **光鸭跨账号转存（A 分享 → B 保存）**（2026-11-12 加入，`app/services/cloud_relay.py` + `app/api/routes/relays.py`）：
  - **机制**：源账号建分享 → 目标账号 `restore_share` 保存。**服务端内部复制**——实测 100MB 文件 **7 秒落盘**（≈14 MB/s），不占本机带宽与磁盘，**也不吃云添加额度**
  - **别用云添加做批量搬运**：云添加每天只有 **3 次/账号**（实测三个账号全部撞到「云添加次数超限，下个自然日刷新」），且**失败的也扣额度**；光鸭直链的云添加还会**丢文件名**（按 URL 末段取名 → 光鸭 CDN 直链末段是 `/download/` → 文件全叫 `download`，只能事后 `fs_rename` 补救）
  - **分享链路必须匿名**（取令牌 / 列内容）：带 Bearer 一律返回 `201 分享已失效`——那正是分享页给未登录访客走的那条路。故驱动另开一条**不带 Authorization** 的 httpx 通道（`_call_public`），不能走 `guangyaclient` 的客户端（它默认头带 authorization）
  - **保存必须带 Bearer**（匿名 → `117 无效token`）——存进自己的网盘本来就要登录态
  - **`shareId` 必须拼账号 id 后缀**（`{shareId}_{uid}`，uid = access_token 的 JWT `sub`）：缺后缀是 `112 参数错误`；**成功响应没有 `code` 字段**——别用 `code == 0` 判断成功
  - **建分享有频率限制**：同账号连建会被判 `201 分享已失效`（等 90s 以上稳定）→ 引擎有节流闸门（`_ShareGate`，默认 30s）+ `201` 自动退避重试（`112` **不可重试**，那是调用方拼错参数）
  - **提交单元 = 同一源目录下的文件批次**（≤500/批，实测服务端上限），**冲突判定逐文件**：同名同大小 → 跳过；同名不同大小 → 记冲突，**不覆盖、不改名**
  - ⚠️ **预检是必须的**：服务端保存同名文件时**不报错也不覆盖**，而是自动改名 `xxx(1)`——不预检的话重跑一次就多一份垃圾副本
  - **「搬完清理源」是显式开关**（默认关）：只删**已校验成功**的文件，走 `fs_delete`（进回收站，非永久删）+ **记审计日志**
  - **真实验证脚本** `scripts/probe_guangya_relay.py`（生产容器跑，造临时目录 → 真转存 → 校验 → 自删）。⚠️ **光鸭适配层改动必须用它真跑一遍**——mock 单测只是回归网，不算验证（`tests/unit/test_guangya_share.py` + `tests/unit/test_cloud_relay.py` + `tests/integration/test_relay_api.py`）
  - 协议细节与实测数据留档在 `docs/guangya-relay.md`
- **Bearer 长期凭据只用 `WEB_TOKEN`**（2026-10-30 加入）：`verify_bearer` 同时接受 API Token（`WEB_TOKEN`）与 session cookie 值两种口径，但 session cookie 值 24h 过期（`SESSION_MAX_AGE`）。CLI / 脚本长期使用应存 `WEB_TOKEN`，不可把 session cookie 值当长期 token 存储——后者会突然 401。session 值口径仅为面板/CLI 同源便利，非长期凭据。
- **自动化 / 合规边界**（2026-10-22 修订，原「不引入未文档化的反爬对抗 / 验证码绕过（R2）」）：
  - ✅ **允许**对**自有账号**做站点自动化，含 AI 读图形验证码（`app/core/ai.py`）、真实浏览器交互
  - ❌ **禁止**伪造 / 破解站点**鉴权凭据**（如 soulvoice 的 JWT/HS256 downhash）、伪造站点签名、越权访问他人资源
  - Cloudflare Turnstile 等**人机验证**本仓库不实现（实测 `TurnstileError 600010`，服务端环境无法完成），相关站返回 `UNSUPPORTED`（需人工）
- **站点适配层保持 9 站独立文件**（2026-10-21 决定）：不抽公共基类，各站独立实现。重复代码接受现状。
- **FR-1.6 站点用户数据契约**（2026-10-18 加入，2026-10-18 精简为单方法，2026-10-22 加时魔）：
  - 不抽 9 站 user_data 基类；NexusPHP 7 站共用 `app/adapters/sites/parsers/nexus_php.py` module-level helper（`parse_user_snapshot` / `parse_hourly_bonus`）
  - `SiteUserData` 表是**当前快照**（UNIQUE site_id），不做历史；如需时间序列需先建 `site_user_data_history` 表再迁
  - 适配器单方法 `get_user_snapshot`：**返回 `None` 只表示"该站不支持 / 无凭据"**（service 记 skipped）；**拉取/解析失败必须抛 `SiteFetchError`**（service 记 failed 并点名，**不写 0 兜底**——写 0 等于把故障伪装成"上传 0 / 做种 0"）
  - 抓取页用 `/mybonus.php`（navbar 带 3 业务字段 + 时魔，**一页拿全**）；navbar 解析不出 3 字段时回退 `/userdetails.php`
  - `refresh_user_data` cron `*/30 * * * *`（与 `push_free_torrents` 同分钟位错峰不冲突）
  - **网络 IO 在事务外**：短事务读站点 → 并发抓取（上限 5）→ 短事务批量 upsert；写锁不能横跨网络往返
  - 暂不接告警推送；只落库 + UI 展示
- **时魔（每小时获得的站点积分，2026-10-22 加入）**：
  - 各站叫法不同（魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分），统一 `HourlyBonus{amount, unit, capped, progress}`
  - **取"合计"口径**（含官种/勋章/后宫等各项加成），不是"基础时魔"——hhanclub 两者差 3 倍
  - `capped=None` 表示"该站页面不提供上限信号"（**不是**"没封顶"）；`progress` 是站点自有口径（通常相对基础时魔算）
  - m-team 是 API 站，**不走** `/mybonus.php`（那个页是纯 SPA 壳）：走 `POST /api/tracker/mybonus` 的 `data.formulaParams.finalBs`（合计列）。⚠️ `data.memberCount.bonus` 是**累计余额**（百万级），不是每小时收益
  - 解析规则用**真实页面**固化成 fixture（`tests/fixtures/*_mybonus.html`，m-team 是 `mteam_mybonus.json`），由 `scripts/capture_mybonus_fixtures.py` 抓取（裁剪 + 脱敏 + 自校验）
  - 契约由 `tests/unit/test_bonus_parser.py` + `tests/integration/test_user_data_job.py` 固化
- **FR-1.6 推送选种优先级**（2026-10-22 加入，`app/services/push_free.py`）：
  - **按时魔封顶档排序**（主排序，始终生效）：未封顶（`capped = 0`）→ 无信号（`NULL`）→ 已封顶（`= 1`）
  - 依据：NexusPHP 魔力公式 `每小时 = B0 × (1 − e^(−A/B0))`，A 越大越逼近上限——**多推一个种子的边际收益越小**，已封顶的站再推也不涨时魔
  - 档内排序由运行时参数 `push_within_bucket_order` 决定：**0**（默认）= 各站时魔进度升序（同进度退化为 `last_seen_at DESC`）；**1** = 整个档内随机打乱（含站点顺序）
  - 随机在 **Python 侧**做（不用 SQL `RANDOM()`——既不可 seed 也不可测）；生产不传 seed = 真随机，测试传 `random.Random(seed)` = 确定性
  - 打乱在**过滤循环之前**做：循环仍在凑满 `push_max_per_run` 时 break，「已推过」检查的 DB 查询次数不变（**不引入 N+1**）
  - 站点没有 `site_user_data` 行（没抓过 / 抓失败）→ 落「无信号」档，行为与加此特性前**完全一致**（优雅降级）
  - **只推启用站点**（2026-11-12 修复 prod 故障「站点已停用却还在推对应站点的种子」）：站点停用（`sites.enabled=0`）只停止**拉取**、**不删** `torrent_index` 快照 → 旧快照仍落在 24h 新鲜度窗口内被选中。故两道闸：① 候选查询 **INNER JOIN `sites` + `enabled=1`**（`push_free._select_candidates`）② 执行层兜底 `execute_add_torrent` 查 `site.enabled`，停用 → `PushItemError("fatal")`（拦停用前已入队、被 `drain_task_queue` 重试的遗留任务；fatal = 不重试，站点停用时重试也不会成功）。手动推送走同一执行函数，故**停用站也不能手动推**——想推先重新启用站点
  - 契约由 `tests/integration/test_push_free.py`（候选过滤）+ `tests/integration/test_task_drain.py`（执行层死信）固化
- **站点拉取失败必须抛错**（2026-10-21 prod 事故后确立）：
  - `search_torrents` 返回 `[]` = 站点确实没种子；抛 `SiteFetchError` = 故障（DNS / 连接 / HTTP）
  - 两者混用会让作业统计报"全部成功"而实际漏掉故障站（prod 的 hddolby DNS 失败就是这样被掩盖的）
  - 契约由 `tests/unit/test_site_adapters.py` 固化，9 站全参数化覆盖
- **FR-3.6 站点每日签到契约**（2026-10-22 加入）：
  - 适配器单方法 `checkin() → CheckinResult`，带状态枚举（success / already / unsupported / auth_expired / failed）——**不用 `None`**：签到必须区分"该站不支持"与"签到失败"，否则作业统计分不清
  - **只实现实测可用的 5 站**：hhanclub / soulvoice / hdhome（走 NexusPHP `/attendance.php`，GET 即签到）+ **hdsky**（自建 `showup.php` 三段式，图形验证码走 AI 读码）+ **hdwing**（X2015 主题插件 `plugin_sign-in.php`，**AJAX 路径无需验证码**）。其余 5 站走基类默认 `UNSUPPORTED`（audiences / ourbits 需 Turnstile、springSunday / mteam 无此功能、hddolby 未验证）
  - **hdwing 的签到协议**（2026-11-11 实测，生产 cookie）：**不是** NexusPHP 标准的 `/attendance.php`（本站 404），而是 X2015 主题自带的插件端点（协议来自站点自己的 `/plugin/js/base.js` 的 `do_signin()`）
    - `GET /usercp.php` → 取 `<meta name="x-csrf">` 的 csrf + 看 navbar 是「签 到」还是「已签到」（**两个特征都不在 = 站点改版 → FAILED，不能当成「未签到」盲签**）
    - `POST /plugin_sign-in.php?cmd=signin {csrf}` → 执行签到，**不需要图形验证码**
    - 响应：`{"state":"success","signindays":N,"integral":"10"}` / `{"state":false}`（今日已签，**无 msg**）/ `{"state":false,"msg":...}`（csrf 错）
    - ⚠️ **`{"state":false}` 与「真失败」同形** → 解析层标成 `attendance.UNREASONED_FAILURE`，适配器**再查一次状态**定性（宁可多一次请求，也不把幂等的「已签」误报成失败，或把真失败静默吞掉）
  - 失败必须抛 `SiteFetchError` / `BreakerTripped`，与拉取路径同一契约
  - 签到熔断**不写** `sites.breaker_until` —— 不与 10 分钟一次的拉取作业共享熔断状态
  - **不建表**：结果只进 `job_runs.result_json`（`summary` + `steps`），前端「调度 → 历史」可见
  - 解析规则用**真实页面**固化成 fixture（`tests/fixtures/*_attendance.html`；hdwing 是 `hdwing_signin_state*.html` + `hdwing_signin_*.json`）——手写最小 HTML 会漏掉真实文案（hhanclub 成功页**不含**"签到成功"四字，只有统计文案）
  - fixture 抓取：`scripts/capture_mybonus_fixtures.py`（时魔）/ `scripts/capture_checkin_fixtures.py`（hdwing 签到）/ `scripts/trim_checkin_fixtures.py`（脱敏 + 裁剪，csrf 抹成 40 位 0 占位符——**必须是合法 hex**，否则解析器提取不到）
  - 契约由 `tests/unit/test_checkin_parser.py` + `tests/unit/test_site_adapters.py` + `tests/integration/test_checkin_job.py` 固化
- **作业步骤带逐项明细**（2026-11-03 加入，`JobStep.items`）：
  - 步骤的 `detail` 是**聚合**描述（如「20 成功，0 失败」）——光看它**回答不了「是哪些出了问题」**。而作业执行时这些数据本来就在内存里（逐站结果、逐种推送结果…），只是没被带出来
  - `JobStep.items: list[JobStepItem] | None` = 逐项明细（`name` / `status` / `count` / `detail`）；`items_total` = 完整条数（`> len(items)` = 被截断，前端显示「显示 N / 共 M」）
  - **全部字段带默认值 → 向后兼容**：现有 60+ 处 `JobStep(...)` 调用零改动
  - **必须限流**：`build_items()` 上限 50 条/步（`monitor` 可能命中几百个种子），超限丢**最早**的（明细按执行顺序产生）
  - 前端**默认折叠**（点击「明细（N）」展开）——默认展开会把详情面板撑爆
  - 7 个作业全填：`fetch_all_sites`（逐站）/ `push_free_torrents`（逐种 + 逐档站名）/ `monitor_free_window`（逐 hash 动作）/ `refresh_user_data`（逐站 + 时魔值）/ `drain_task_queue`（逐 task）/ `daily_checkin`（逐站状态）/ `cross_seed_maintenance`（逐下载器）
  - 顺手干掉硬截断：`user_data.py` / `checkin.py` 的 `[:200]`（完整原因进明细，summary 只给一句人话）
  - 契约由 `tests/unit/test_scheduler.py` + 各作业的 integration 测试固化
  - **站点进度是「百分数」，不是 0~1 的小数**（2026-11-03 修正）：
    `hourly_bonus_progress` 存的就是 `16.45` / `110.4` 这样的**百分数**
    （契约见 `HourlyBonus.progress`：`>=100` 表示封顶；解析侧
    `parse_progress_percent` 直接返回 `alt="NN%"` 的数值）。**展示时不得再乘 100**
    ——曾在 `push_free.py` 的逐站明细里多乘了一次，页面显示「进度 1645%」
    「进度 11040%」。前端 `format.ts` 的 `站点进度 ${...toFixed(1)}%` 是对的
- **作业历史弹框的表格布局**（2026-11-03 修正，`SchedulerHistoryModal.vue`）：
  - **必须 `table-layout: fixed`**——Ant 默认 `auto`，列宽只是「建议值」，
    浏览器按内容重新分配，**列宽设了也不生效**
  - **必须给够列宽**：td 的 padding 左右各 16px（可用宽 = 列宽 − 32），
    放不下就**单词中间断行**——「触发」列的 "manual" 曾被断成 "ma" / "nual"，
    行高从 49px 撑到 69px，看起来像半个词被撕开
  - **约束 td 本身而非 span**：span 是 inline 元素，父级可用宽不足时照样溢出
  - 「错误」列靠 `ellipsis` 单行截断（tooltip 看全文）
  - 契约由 `web/tests/scheduler-config.spec.ts` 固化（表头不截断 / 单元格不换行 /
    行高 ≤ 55px）
- **作业执行日志**（2026-11-03 加入，`app/core/job_log.py`）：
  - `job_runs.result_json.logs` = 作业执行期间的 Python 日志（结构化 `{ts, level, logger, message}`），**不加列**（免 DB 迁移）
  - 与逐项明细**互补**：明细回答「**是哪些**」，日志回答「**什么时候 / 多慢**」
  - **`ContextVar` 隔离并发作业**——`emit` 只接受「当前采集器」的记录。普通全局变量会让并发作业互相污染（用户看到 A 作业的日志里有 B 的输出）
  - `asyncio.gather` 派生的 task **继承 context** → `user_data` 那种「并发抓 9 站」的子任务日志也收得到
  - 上限 200 条/次，超限丢**最旧**的
  - **门槛是 root logger 级别**（`LOG_LEVEL`）——`LOG_LEVEL=INFO`（生产默认）收完整时间线；调成 WARNING 则只剩警告以上
  - 契约由 `tests/unit/test_job_log.py` 固化
- **作业历史接口必须分层**（2026-11-03 加入）：
  - `GET /scheduler/runs`（列表）→ **剥离** `logs` 与大字段（`cross_seed.results[].stats` 含完整 40 位 hash 列表，辅种站快照 **590KB**；列表一次 100 条 = **~59MB** 响应）
  - `GET /scheduler/runs/{run_id}`（详情）→ **完整**数据（用户点开某次运行就该看到全部）
  - 前端**点行时按需拉详情**（`SchedulerHistoryModal.vue`）
  - `job_runs` 保留期 **30 天**，清理挂在 `drain_task_queue` 的「清理作业历史」步骤（它已承担 `task_queue` 的定期清理语义，每 5 分钟一次）——为一个 DELETE 单开第 8 个作业属过度设计
  - 契约由 `tests/integration/test_services.py` + `tests/integration/test_task_drain.py` 固化
- **monitor 命中后的动作：暂停 or 删除**（2026-10-23 加入，`monitor_delete_expired` 运行参数）：
  - `0`（**默认**）= 仅暂停（`set_pause`）；`1` = 删任务 + 删磁盘数据（`remove(delete_data=True)`）
  - **判定范围与暂停完全一致**——「该暂停的」就是「该删的」，差别只在执行动作（REFUND / TIMED 无 end_at / TIMED 窗口到期 / 非免费种；PERMANENT 永不处理）
  - 删除模式**仍跳过**已完成与已暂停的任务——已下载完的数据删掉是净损失
  - **「已完成」用字节口径判定**（`TorrentInfo.completed_bytes >= size`），**不能用 `progress >= 1.0`**——qBittorrent 的 `progress` 在**校验中**返回的是「校验进度」而非下载完成度（源码 `TorrentImpl::progress()`：`if (isChecking()) return m_nativeStatus.progress;`），qB 重启 recheck 的已完成做种种子会被误判成「未完成」
  - **`completed_bytes` 两站口径不同**（2026-10-24 加入）：qB 取 `row["completed"]`（libtorrent `total_wanted_done`），TR 取 `row["haveValid"]`（TR RPC 字段）。**不能复用 `downloaded`**——TR 的 `downloaded` 是 `downloadedEver`（**累计**下载量，含重复下载）
  - **校验中（`CHECKING`）的种子两种模式都跳过**（2026-10-24 加入）：暂停模式等下一轮巡检（作业每 10 分钟一轮）；删除模式**绝不删除**校验中的种子——校验期完成度判定不可靠，而删磁盘数据不可逆
  - **保留 `torrent_index` 行**且 `push_status` 保持 `pushed`——`push_free` 的候选条件要求 `push_status IS NULL OR != pushed`，保留即天然不重推，否则「推送→删除→再推送」无限循环
  - 删磁盘数据**不可逆** → 走 `delete_data=True` 且**必须记审计日志**（AGENTS.md：删磁盘数据单独开关并记审计日志）
  - 默认 0 = 生产升级后行为不变，需用户显式开启
  - 契约由 `tests/integration/test_monitor.py` + `tests/unit/test_downloader_adapters.py` + `tests/integration/test_api.py` 固化
- **枚举型运行参数用单选按钮组，不用数字输入框**（2026-10-23 加入，`ParamSpec.options`）：
  - 0/1 这类参数是「在几个**具名行为**间二选一」，不是「调一个数值」——数字输入框看不出 0 和 1 各是什么，用户得去查文档
  - `ParamSpec.options` 声明枚举取值（`((值, 显示名), ...)`）；非 None 时前端渲染 `a-radio-group`，None 时渲染数字输入框
  - **label 只说「是什么」，不说「怎么取值」**——取值语义移到 options 的显示名里（label 固定 200px 列宽，长文案会挤爆）
  - `min`/`max` **保留**——`validate()` 仍靠它做范围校验（后端安全边界不依赖 options）
  - 枚举参数额外校验：值必须在 options 内（`_assert_enum_options_consistent()` **导入时**执行——取值重复 / 越界 / 默认值不在 options 里都会立刻抛错）
  - 契约由 `tests/unit/test_runtime_params.py` + `tests/integration/test_api.py` 固化
- **调度配置全部落库**（2026-10-24 加入）：
  - **cron 与运行参数都不再走 env**——env 里已无对应配置，改 env 无效（残留的旧行被 `extra="ignore"` 静默忽略）
  - cron 的**默认值真源**是代码常量 `app/services/job_registry.py` 的 `DEFAULT_CRON`；运行参数的默认值在 `app/services/runtime_params.py` 的 `ParamSpec.default`
  - 用户在页面改的 → cron 落 `scheduler_job_settings.cron` 列，运行参数落 `app_runtime_params` 表——**都跨重启保持**
  - `scheduler_job_settings` 的 `enabled` 与 `cron` **两列互相独立**：改 cron 不碰启停（改启停也不清 cron）
  - 启动期回填：`SchedulerCenter.apply_overrides(states, crons)` 必须在 `register_jobs` **之前**调用（`register()` 按 enabled 决定是否排进 APScheduler、按 cron 决定触发时刻）
  - **注册日志必须打生效值**（`scheduler_center.list_job_cron(name)`），不是 `DEFAULT_CRON` 里的默认值——打默认值会在用户改过 cron 后与实际行为矛盾
  - DB 写失败时 API 返 500（配置已在内存生效但没存住）——与 toggle 端点同一处理
  - 契约由 `tests/unit/test_scheduler.py` + `tests/integration/test_job_settings.py` + `tests/integration/test_api.py` 固化
- **环境变量配置全部落库**（2026-10-26 加入，`app/services/env_settings.py` + `app/db/models.py:AppEnvSetting`）：
  - 与「调度配置全部落库」同构，但服务**字符串型**配置（与 `app_runtime_params` 的整型运行参数互不混用——避免 `value_str` 那种「为未来扩展」但实际从没用过的列承担新职责破坏现有契约）
  - 当前注册 `cross_seed_scan_dirs`（辅种扫描目录，逗号分隔多目录）；未来其它 env→DB 的字符串设置可同样在此注册
  - **env 改不再生效**——env 来源（`AppSettings.cross_seed_scan_dirs`）只作**首次启动的默认值**（DB 空时回填）；之后改 env 残留旧行被 `extra="ignore"` 静默忽略
  - 业务读取点用 `get_env_setting("cross_seed_scan_dirs")`（**不再**读 `get_settings().cross_seed_scan_dirs`）——`app/services/cross_seed_orphan.py:219` + `app/api/routes/cross_seed.py` 删除端点
  - 启动期 `init_env_settings_async()` 必须在调度作业跑之前调用（孤儿扫描作业会读它）；**不放在 `scheduler_enabled` 分支里**——API 端点在调度器关闭时仍要可用（与 `init_runtime_params_async` 同一处理）
  - API 写入路径：DB 先 → 内存后（commit 失败时内存未变，作业不会拿到一个重启后就消失的值）
  - API 路由 `app/api/routes/env_settings.py`：`GET/PUT /api/v1/env-settings`
  - 前端「设置 → 环境变量」tab（`web/src/pages/settings/EnvTab.vue`）——直接表单（不弹框），字段少 + 改后立即生效，弹框徒增一次点击
  - 契约由 `tests/unit/test_env_settings.py` + `tests/integration/test_api.py` + `tests/integration/test_db_models.py` + `web/tests/env-settings.spec.ts` 固化
- **FR-3.6 AI 读码**（2026-10-22 加入，`app/core/ai.py`）：
  - 用于 hdsky 的 6 位图形验证码。**必须先放大 6 倍**再送模型：站点原图仅 150×40，同一模型直接读 0/8、放大后 8/8
  - 放大是**无损**的（只 resize，不二值化 / 不裁剪 / 不去噪）——按面积阈值去噪会删掉细笔画（实测 0/5）
  - 取 `message.content` 而非 `reasoning_content`（后者是 thinking 过程）
  - `max_completion_tokens` 至少 512（thinking 会吃掉大半）；MiniMax 系列 `thinking.type=disabled` 会报 400，不能关
  - 三项配置（`AI_API_BASE` / `AI_API_KEY` / `AI_MODEL`）**全空 = 未配置** → hdsky 返回 `UNSUPPORTED`，作业不变红
  - 服务端对"答错"（`invalid_imagehash`）与"今日已签"（`date_unmatch`）返回**不同错误码**——靠这个区分"换验证码重试"与"已签到"，务必按码处理不要一律当失败
  - **注意网关配额**：`llmapi.banstack.top` 对 MiniMax-M3 系列有配额上限，超额返回
    429 `Quota preflight blocked`。**调试时别用真实 key 批量跑样本**（2026-10-22
    测准确率时把配额打到 99%，恢复需约 3 小时）。日常签到 1–2 次/天可忽略；
    **但该 key 与 OpenCode 自身共用配额**，若在同一个 key 上跑批量实验会影响编码会话
- **辅种快照契约**（2026-10-24 加入，`app/services/cross_seed_monitor.py`）：
  - 辅种下载器种子量大（实测 3000+ 个），**实时拉一次要 20s+** → 改为**定时抓取 + 落库快照**
  - `cross_seed_maintenance` 作业（cron `17 */3 * * *`，**2026-11-01 合并** `scan_cross_seed` + `disk_rotation` + `scan_orphans`）：短事务读下载器 → 并发抓取（网络 IO 在事务外，上限 3）→ 短事务 upsert `cross_seed_snapshot` → 批量暂停异常 → 磁盘超阈删种子 → 孤儿扫描
    - **为什么合并**：`disk_rotation` 读 `cross_seed_snapshot` 判磁盘使用率，而快照由巡查产出——分开跑可能读到**旧快照**。合并后依赖顺序天然正确，且省一次辅种站全量抓取（20s+）
    - 三段**各自容错**：某段失败不影响其余，失败进 `steps`（前端步骤卡片逐段展示）
  - `cross_seed_snapshot` 表是**当前快照**（`downloader_id` UNIQUE），不做历史；FK **不带** `ON DELETE CASCADE`（当守卫：漏删会 IntegrityError，宁可失败也不留孤儿行）——删下载器 / 取消辅种标记时必须显式删快照
  - **前端读快照，不打下载器接口**：辅种页与仪表盘「在途任务」都读快照；实时端点 `GET /cross-seed-monitor/downloaders/{id}/scan` 保留作调试用
  - **辅种页是两层 Tab**（2026-10-26）：外层 = **下载器**（数据维度），内层 = **内容分区**（概览 / 分组 / 异常 / 孤儿，功能维度）
    - 为什么是两层而不是二级菜单：用户的真实心智是「我要看 tr-172 这个下载器怎么样」——那是数据维度，不该被功能维度打散。若按功能拆成独立页面，每个页面都得重放一遍下载器 Tab
    - 为什么内层要拆：原本 4 块内容纵向堆叠，实测 **3727 px / 4.1 屏**，其中 2 屏是 2237 行的分组表。拆分后概览 **921 px（1 屏）**，分组 3069 px / 异常 1950 px / 孤儿 720 px ——各块互不干扰（异常与孤儿各有独立空间，不会互相误点删除按钮）
    - 内层 Tab 在**每个**下载器 pane 里各渲染一份（Ant Design 隐藏 inactive pane）——e2e 定位必须用 `:visible` 过滤，否则会点到隐藏那份
    - 异常 / 孤儿明细不再用折叠面板（独立 Tab 本身就是展开态）
  - **抓取失败保留上一次的快照**（不写 0 兜底）+ 计入 failed **并点名**——写 0 等于把故障伪装成"没有种子"
  - **异常种子批量暂停**：按 100 个一批调 `set_pause([...])`（不是逐个发——477 个异常 = 477 次 API 往返）；**已暂停的跳过**（上一轮已暂停过，不重复发指令）
  - 「是否已暂停」用 adapter 归一化的 `state` 判定（`TorrentState.is_paused`），**不能用 `status == 0`**——qB 把 `error` / `missingFiles` 也映射成 0，用它判会把「数据丢失」的种子误当成已暂停而跳过暂停（那正是最该暂停的一类）
  - **异常检测必须同时看 error 码与文案**（2026-10-26 修正）：`_detect_anomaly(error_code, error_string)`
    - 只匹配文案会漏掉大量异常：TR 的 `error=3` 文案是 `"Please Verify Local Data! Piece #335 is corrupt."`——既不含 "no data found" 也不含 "not registered" → **整个漏判**
    - 实测（prod 辅种站）：TR 有 **156 个**种子带 error 码（154 个 tracker HTTP 502/522 + 2 个本地数据损坏），只匹配文案时**全部漏判**（异常数恒为 0）
    - error 码语义：`0`=无错 `1`=tracker 警告 `2`=tracker 错误 `3`=**本地数据丢失**
    - 异常类型：`no_data_found` / `tracker_not_registered`（文案匹配）/ `local_data_missing`（码 3）/ `tracker_error`（码 2）
    - **`tracker_error` 豁免暂停**（`_PAUSE_EXEMPT_ANOMALIES`）：那是 tracker **服务端**的问题（HTTP 502/522），与本地数据无关——暂停只会白白损失上传，等 tracker 恢复即可。`local_data_missing` **要**暂停（数据没了，做种无意义）
    - 「跳过」要分两类说：此前已暂停 vs 豁免暂停（tracker 报错）——混在一起会让人以为漏暂停了
  - 契约由 `tests/unit/test_cross_seed_monitor.py` + `tests/integration/test_cross_seed_job.py` + `tests/integration/test_db_models.py` + `tests/integration/test_api.py` 固化
- **辅种空间统计**（2026-10-24 加入，`app/services/cross_seed_space.py`）：
  - **复用同一次全量拉取**：辅种站 4600+ 种子拉一次要 20s+，占用数据直接从 `list_torrents_with_errors()` 的返回值算（**不额外打接口**）；只有磁盘空间需要**额外一次**轻量调用（TR `free-space` / qB `sync/maindata`，毫秒级）
  - adapter 的 `list_torrents_with_errors()` 返回值新增 `completed_bytes`（实际占用）+ `save_path`（按目录分布）；`get_disk_space()` 是新增的基类方法（不支持时抛 `NotImplementedError`，与 `list_torrents_with_errors` 同模式）
  - **口径诚实**：TR 的 `haveValid` 是**单个种子**的精确磁盘占用；qB 的 `completed` 是 `total_wanted_done`（**不含** unwanted 文件与冗余块），会**小于**真实占用。不夸大、不折算
  - ⚠️ **`used_bytes` / `total_size_bytes` 不是磁盘占用**（2026-10-26 澄清）：辅种的本质是**同一条数据被多个种子共享**（实测辅种站 **4646 个种子只对应 2237 份数据**，组内种子数分布：1 个的 1352 组 / 2 个的 168 组 / 3 个的 378 组 / 4 个的 181 组…），共享部分被重复计了 N 次 → 实测「声明占用 71.47 TB」**大于**「分区总量 28.33 TB」
    - 故前端标签用「**种子声明占用**」（不是「辅种实际占用」），并加口径说明 alert
    - **真实磁盘占用 = `disk_total_bytes - disk_free_bytes`**（分区视角，含辅种之外的 VM/备份/其它共享）→ 前端进度条用这个，标为「分区已用（含非辅种数据）」
    - 字段名**不改**（`used_bytes` 保持）——改名字会破坏 API 契约 + DB 列名，代价远大于收益；只改**标签与注释**
  - **qB 拿不到磁盘总量**（`sync/maindata` 只给 `free_space_on_disk`）→ `total_bytes=None`（**不是 0**——0 会被前端画成「磁盘已满」）→ 前端据此**不画进度条**
  - **磁盘信息拿不到不阻塞**：适配器不支持 / 查询失败 → 降级成 NULL，占用数据照样落库
  - 数据落在 `cross_seed_snapshot` 的空间列（`disk_free_bytes` / `disk_total_bytes` / `used_bytes` / `total_size_bytes` / `by_dir`）
  - 契约由 `tests/unit/test_cross_seed_space.py` + `tests/unit/test_downloader_adapters.py` + `tests/integration/test_cross_seed_job.py` + `tests/integration/test_api.py` 固化
- **辅种异常种子批量删除**（2026-10-24 加入）：
  - 端点 `POST /cross-seed-monitor/downloaders/{id}/remove-torrents`（body：`hashes` / `delete_data`）——**通用**删除（异常种子明细 + 统计卡片的分组共用）
  - **不限数量**（辅种站异常种子动辄数百个，超出普通批量上限 200）→ `downloader_service.remove_torrents_in_batches()` 按批切分（默认 200/批）
  - **单批失败不中断**后续批次（尽可能多删），失败数计入结果；但**下载器整体不可用**（404 不存在 / 400 已禁用）**直接抛出**——后续批次同样会失败，且用户需要知道「下载器都没了」
  - hash 必须是 40 位 hex（拒绝 `all` / 通配）——复用既有 `validate_hashes`
  - 删磁盘数据走既有审计路径（`delete_data=True` → 记 warning 日志，含 hash 列表）
  - 前端：异常明细表格加勾选 + 「全选（N）」快捷键 + 「删除选中（N）」，确认弹框与「任务」页一致（仅删任务 / 删任务+磁盘数据）
  - 契约由 `tests/integration/test_services.py` + `tests/integration/test_api.py` + `web/tests/cross-seed.spec.ts` 固化
- **统计卡片的分组删除**（2026-10-24 加入）：
  - 统计表一行 = 一个 (name, size) 分组，可能代表**多个**种子（`count=13` 就是 13 个）——删该行 = 删该组**全部**种子
  - **数据前提**：快照的每组必须存**完整 40 位** hash 列表（`StatsRow.hashes`）。`sample_hash` 是 **12 位截断**的，只作展示用——**不能**用于删除（适配器会拒绝非 40 位 hash）
  - 旧快照行没有 `hashes` → 前端删除按钮**置灰** + tooltip「需重新抓取」（不静默失败）
  - 快照体积：辅种站 2257 组 / 4600+ 种子 → stats 从 356KB 涨到约 590KB（SQLite 无压力）
  - 弹框必须写清「将删除 N 个种子（同名同大小）」+ 警告「这些是**正常做种**的种子」+ 提示「仅删任务会留下不再做种的孤儿数据」
  - 契约由 `tests/integration/test_cross_seed_job.py` + `test_api.py` + `web/tests/cross-seed.spec.ts` 固化
- **辅种孤儿文件统计**（2026-10-24 加入，`app/services/orphan_scanner.py`）：
  - 孤儿 = **磁盘有、任务没了**（典型来源：用「仅删任务」清理种子时磁盘文件留下，不再做种却一直占空间）
  - **必须挂载磁盘**：下载器**没有**浏览文件系统的接口（TR 的 RPC spec 不提供 filesystem 浏览，qB 也没有列目录 API）→ 只能把辅种站的下载目录**挂载**进容器，扫盘 + 顶层名匹配（权限 **rw**——删除孤儿文件需要写权限）
  - 128 上 Unraid **已自动挂好** CIFS（`/mnt/remotes/192.168.0.172_Public`）→ 只需一行 compose 挂载，无需 NAS 配置
  - **挂载路径必须与下载器的 `downloadDir` 完全一致**（辅种站 = `/share/Public/home/downloads/辅种`）——孤儿判定把任务按 downloadDir 分组，路径不一致就分不对组
  - **多路径配置**：`cross_seed_scan_dirs`（逗号分隔多目录），每个目录独立扫描 + 独立统计（`scanned_dirs` 列）。辅种站实测 3 种 downloadDir：`辅种`(4639) / 父目录(5) / `game`(1)
  - **按 downloadDir 分组**：扫描目录 D 时**只把 downloadDir == D 的任务**算作「有主」——顶层名（`files[].name` 相对 downloadDir）只在同一 downloadDir 内才有意义，混在一起判会误认有主
  - **路径用完全相等匹配**（忽略尾部斜杠），**不做前缀匹配**——前缀匹配会把 `/downloads` 与 `/downloads/辅种` 混为一谈
  - **空目录也算孤儿**（0 字节）——是「任务没了的残留」，但**不占空间** → 单独统计 `empty_dir_count`
  - 匹配：下载器任务的 `top_names`（TR 取 `files[].name` 的顶层段、qB 取 `content_path` 相对 `save_path` 的首段）与磁盘顶层条目名比对
  - **口径是保守下界**：同名但不同种子的目录会**漏判** → 数字只会偏少，不会偏多
  - **跨下载器汇总「有主」**（2026-10-26 修正）：辅种目录**被多个下载器共用**——实测 qB 有 6 个任务的 `save_path` 也在辅种目录里。只看辅种下载器（TR）的任务会把那 6 个 qB 任务的数据误判成孤儿（实测误判 480 GB，占「孤儿」总量 78%）。故「有主」集合必须汇总**所有已启用下载器**的任务
    - 拉取失败的下载器不贡献「有主」→ 其数据可能被误判 → **必须点名**（作业 summary + 步骤里写明哪个下载器失败）
    - 每个下载器**只拉一次**（汇总时拉的结果复用给扫描，别拉两遍）
  - 新表 `cross_seed_orphan`（每下载器一行）+ 由 `cross_seed_maintenance` 作业的第三段扫描（cron `17 */3 * * *`）+ 端点 `GET /cross-seed-monitor/orphans`
  - **顶层名单独拉**（2026-10-26）：`list_torrent_file_names()` 是**独立**的 adapter 方法——顶层名需要 TR 的 `files` 字段，而它在辅种站是 **27.5 MB**（15.8 万个文件条目，占响应体 98%）。混进 `list_torrents_with_errors()` 会让每次辅种巡查都白拉 27 MB。**只有孤儿扫描段付这个代价**（辅种维护作业的第三段）
    - 该方法**自带 `save_path`**（= downloadDir）→ 孤儿扫描**只打一次**下载器接口
    - qB 的 `/torrents/info` 本来就返回 `content_path` + `save_path` → **零额外请求**
    - `list_torrents_with_errors()` 与 `list_torrents()` **都不再拉 `files`/`fileStats`**（TR 的 `TorrentInfo` 没有这两个字段，拉了完全用不上）
  - **孤儿文件删除**（2026-10-26 加入，`app/services/orphan_deleter.py`）：
    - 端点 `POST /cross-seed-monitor/orphans/scan`（**手动扫描**，不等 cron）——孤儿扫描默认**每天一次**（`7 4 * * *`），当天发现孤儿想现在清理等不到明天。**同步 await**（40–60 s），响应带上有孤儿表的新鲜统计 → 前端直接用，省一次请求
    - 前端入口在「孤儿」分区的区块标题行：「立即扫描」按钮 + 扫描时间标记
    - 端点 `POST /cross-seed-monitor/orphans/delete`（body：`paths`）——删的是**磁盘文件**，不是下载器任务（孤儿本来就没任务）
    - **三道安全闸**（不只靠前端弹框）：
      1. **目录范围**：路径必须是配置的扫描目录的**直接子项**——不能是任意路径、不能是子目录内部的某个文件（防路径穿越）
      2. **二次扫描复核**：删除前**重新**拉所有下载器的任务，确认条目**此刻**仍无主。快照可能是几小时前的 → 期间被加回来的任务会被**跳过**（不删）
      3. **审计日志**：逐条记路径 + 大小（删磁盘数据不可逆 → 必须能回答「删了什么」）
    - 逐条删除，**单条失败不中断**其余条目；复核不通过的记 `skipped`、删除失败的记 `failed`
    - 挂载权限必须是 **rw**（`ro` 删不了）——compose 里已改
    - **删除后自动重扫**（2026-10-26）：孤儿统计表是**每天一次**的作业快照，不重扫页面会一直显示旧数字（已删的还在列表里、且看不到新产生的孤儿）。故删除后**立即重扫**并把最新统计带在响应里（`orphans` 字段）→ 前端直接用，省一次请求
      - 重扫**失败不影响删除结果**（数据已删，只是统计没更新）→ 记 `rescan_error`，前端提示可稍后手动重扫
    - 契约由 `tests/unit/test_orphan_deleter.py` + `tests/integration/test_api.py` 固化
  - 实测：辅种站扫 1696 个顶层条目（1670 有主 / 63 孤儿，其中 39 空目录 + 24 有内容 612.85 GB），CIFS 全量扫 **约 40 秒**
  - **降级不静默**：目录未挂载 / 不可读 / 下载器拉取失败 → 记 `error` 字段（前端据此显示原因），作业不炸
  - 契约由 `tests/unit/test_orphan_scanner.py` + `test_orphan_deleter.py` + `test_downloader_adapters.py` + `tests/integration/test_cross_seed_orphan.py` + `test_api.py` + `test_db_models.py` 固化
- **存储源测连缓存**（2026-11-06 加入，`app/services/driver_service.py`）：
  - **进程内缓存**（`_probe_cache`：`driver_id → CachedProbe`）承载启动自动测连 + 手动测连的结果，`GET /drivers` 通过 `DriverOut.conn` 携带（`null` = 未测连）
  - **缓存是「快照」，不是实时状态**——只在进程启动时刷新一次。前端**必须显示 `checked_at`**（超 30 分钟标「状态已过期」），否则用户会把几小时前的「正常」当成实时的
  - **可测连的驱动类型是单一真源**：`app/domain/driver_types.py:PROBEABLE_DRIVER_TYPES`。加新驱动类型只改这一处——`probe_driver` 的校验与 `probe_all_enabled` 的查询都从它取（此前硬编码两处，漏改只会**静默不测连**）
  - **`store_credential` 的 `invalidate_cache` 参数区分两类语义**：
    - `True`（默认）= 凭据**真的换了**（重新扫码登录）→ 作废缓存
    - `False` = **同一个账号换了 token**（光鸭 Token 刷新写回，1~2 小时一次）→ **不能**作废缓存，否则面板退回「未测连」，正好抵消本特性
  - **光鸭 Token 刷新必须写回 DB**：所有构造光鸭驱动的路径都要传 `on_token_refresh`（`probe_driver` / `redirect` / `scan_runner` / `watch_manager` / `upload_tasks` / `migration`）。漏传 → 刷新出的新 token 只在驱动实例内存 → 实例丢弃后 DB 仍是旧 token → 刷新接口一旦轮换 `refresh_token`，**账号彻底掉线**
  - **删除存储源必须清缓存**：SQLite 的 `id` 无 AUTOINCREMENT → **删掉最大的 id 后会被复用**。不清缓存 → 新建的存储源继承上一个的测连状态（账号 id / 容量 / 「直链正常」）
  - **启动测连任务在停机时必须取消**（`app/main.py` 的 `lifespan` finally，**在 `dispose_db()` 之前**）——否则任务跑到落库那一步时 session factory 已置 None，而 `except Exception` 会把异常吞掉 → 表现为「探测历史偶尔少一条」的静默故障
  - `probe_results` 表有 **30 天保留期**，清理挂在 `drain_task_queue`（与 `job_runs` 同款语义，勿单开作业）
  - 契约由 `tests/integration/test_driver_probe_cache.py`（17 例）+ `web/tests/drivers-probe.spec.ts` 固化

- **媒体库目录用目录浏览器选，不手填 id**（2026-11-11 加入，`app/services/driver_service.py:browse_driver_dirs`）：
  - **起因**：光鸭的 fileId 是 19 位数字（115 的 cid 同理），让用户照着网盘网页手抄——抄错一位扫描就扫不到东西，且无从核对。生产上就出现过「光鸭盘扫描按钮点不动」——**按钮禁用条件是 `!media_cid || !output_dir`，与驱动类型无关**，用户只看到一个灰按钮，不知道缺什么
  - **禁用原因必须写出来**：扫描按钮禁用时给 tooltip，**点名缺哪一项**（`scanDisabledTip`）。tooltip 内层包 `<span>`——antd 的 disabled 按钮**不派发鼠标事件**，tooltip 直接包按钮完全不触发
  - **后端新增 `GET /api/drivers/{id}/browse-dirs?parent_id=`**：列出某目录下的**子目录**（逐层懒加载），响应 `{"driver_id","parent_id","dirs":[{id,name}]}`，形状与 `GET /api/upload-tasks/targets` 一致
    - **不能复用 `/upload-tasks/targets`**——它有 `is_uploadable` 校验，而 `UPLOADABLE_DRIVER_TYPES = {"guangya"}`，**115 会被 400 挡掉**。浏览是 CloudDriver 的**读能力**，不是上传能力
    - `list_dir_by_cid` **不在 `CloudDriver` 协议里**（115 / 光鸭都有，协议只要求 `list_dir`），故用 `getattr` 取——与 `migration.py` 同一做法
  - **浏览起点只能是账号根**（此时 `media_cid` 还没配，没有更深的入口）：根 id 按类型解析 —— **光鸭 `""` / 115 `"0"`**，收敛在 `app/domain/driver_types.py:ROOT_DIR_IDS`（domain 层单一真源）。115 的根会列出「手机相册 / 资料 / 密钥」等私人目录，故前端用 alert 明说
  - **`media_cid` 的叫法也是单一真源**：`app/domain/driver_types.py:MEDIA_CID_LABELS`（115 叫 cid、光鸭叫 fileId）+ `DriverOut.media_cid_label` 随接口下发——**前端不写 `if type === "guangya"`**，后端报错文案也用它
  - **前端复用现成的 `DirBrowser`**（`web/src/components/DirBrowser.vue`，面包屑 + 逐层懒加载，状态由父组件持有）——与「数据同步 / 迁移 / 离线下载」三处同一交互模式，**项目不用 `a-tree`**
  - **语义：进入子目录即选中它**（正在看哪个目录 = 要用哪个目录），面包屑回退即改选；回到根 = 取消选择
  - 契约由 `tests/unit/test_driver_types.py` + `tests/integration/test_driver_browse.py`（11 例）+ `web/tests/drivers-media-pick.spec.ts` 固化

- **媒体后缀默认清单收敛到 domain 层单一真源**（2026-11-11 加入，`app/domain/media_exts.py`）：
  - **起因**：「媒体图片后缀 / 媒体数据后缀」此前默认**空串**（placeholder 是唯一提示）——用户不知道该填什么，附件（海报 / nfo / 字幕）静默不同步。用户要求补默认值
  - **三份清单曾散落三处**：`models.py` 的 `DEFAULT_VIDEO_EXTS`（本地定义）、`naming.py` 的 `DEFAULT_IMAGE_EXTENSIONS` / `DEFAULT_DATA_EXTENSIONS`（frozenset 形态、**零引用的摆设**）、前端 placeholder 手写字符串——互不一致，改一处漏两处。现全部收敛到 `app/domain/media_exts.py`（`DEFAULT_VIDEO_EXTS` / `DEFAULT_IMAGE_EXTS` / `DEFAULT_DATA_EXTS`），naming.py 的死常量已删
  - **默认值生效的两条路径**：① **新建存储源** → ORM 列默认值（`models.py` 引 domain）；② **存量行** → 前端打开配置抽屉时空值预填，保存后落库
  - ⚠️ **不在 `DriverOut` 层兜底**（`or 默认`）——清空保存的行（`image_exts=""` = 关闭附件同步，合法配置）会被改写回默认，用户清空后重开抽屉会以为没存上。预填只发生在**打开抽屉**这一刻，保存后以 DB 为准
  - ⚠️ **bootstrap 的老库加列默认仍用 `''`**（`bootstrap.py:("image_exts","TEXT","''")`）——改它等于对全部存量库静默变更数据；存量行由用户下一次保存配置时自然获得默认值（必然重配 `media_cid`，一定会走一次保存）
  - **「当前忽略」的注释是过时的**：图片 / 数据后缀**实际生效**——扫描时**附件同步**（`attachment_*`，下载到本地与 strm 同目录），`models.py` / `types.ts` 注释已修正
  - 契约由 `tests/unit/test_media_exts.py`（格式规范 + `parse_ext_list` 往返 + 单一真源断言）+ `test_db_models.py` 列默认值 + `drivers-media-pick.spec.ts`（预填 + 随保存落库）固化

- **直链缓存读写失败必须降级，不能让播放 500**（2026-11-12 prod 事故，`app/services/link_cache.py`）：
  - **现象**：播放中转 `GET /302/...` 首次请求 **500**、重试才 302——播放器通常不重试，等于随机播放失败
  - **根因**：扫描/调度作业大批量写库时，中转请求写 `link_cache` 撞上 SQLite 写锁。`DB_JOURNAL_MODE=DELETE` 的写锁是**全库排他**的；且 `busy_timeout=10s` 对**死锁升级**无效——读事务持共享锁 + 写事务要升级时 SQLite **直接报** `database is locked` 而非等待，加超时兜不住
  - **修法（业务语义优先）**：直链缓存是**优化**，不是播放的前提——直链已经换到手，缓存读写失败一律降级：读失败按「未命中」继续换取；写失败退避 0.3s 重试一次后**静默放弃**（只留 L1 内存缓存）。只 catch `OperationalError`，其它异常（真 bug）照常抛
  - 契约由 `tests/unit/test_link_cache.py::TestDbLockDegradation` 固化（patch `session_scope` 让 DB 整体抛锁，验证两条路径都不抛）
  - **同款场景第三处：作业历史落库**（2026-11-12 生产撞锁后修，`scheduler.py` 的 `_record_run._persist`）：扫描批量写库期间调度作业的 `job_runs` 落库 commit 撞锁，`OperationalError` 逃逸成「Task exception was never retrieved」ERROR 且历史丢失——修法同款：退避 0.5s 重试一次，仍失败 warning 降级（作业历史是统计性副产品，保留期 30 天）；其它异常同样降级记 warning 不吞真 bug。契约由 `tests/unit/test_scheduler.py::TestRecordRun` 固化

- **生产数据库已切 WAL 模式**（2026-11-12，`.env.prod` 的 `DB_JOURNAL_MODE=WAL`）：
  - **代码默认本来就是 WAL**（`config.py` 的 `db_journal_mode` 默认 `"WAL"`，`session.py` 每个连接建立时执行 `PRAGMA journal_mode=`）——生产当初是 `.env.prod` **显式覆盖成 DELETE**
  - **当初 DELETE 的理由不成立**：注释写「commit 后立即落盘，重启不丢 WAL 缓冲」，但 `.env.prod` 同时设了 `DB_SYNCHRONOUS=NORMAL`——DELETE+NORMAL 同样不保证 commit 落盘，两者持久性几乎相同，WAL 的并发收益却是数量级的
  - **收益实测**：DELETE 模式写锁全库排他，扫描批量写期间播放写 `link_cache` 必撞锁（播放 500 事故的根因）；WAL 下读写不互斥——锁冲突从「持续刷屏」收敛为「**仅容器启动瞬间一次性**」
  - **切法**：停容器 → 冷备份 `medialab.db.bak.<时间戳>` → 改 `.env.prod` 一行 → `docker compose up -d`（首个连接即完成切换，属性持久化在 DB 头）→ 验证 `PRAGMA journal_mode` 返回 `wal` + `-wal`/`-shm` 文件出现。回滚：改回 `DELETE` + 重启（SQLite 自动合并 WAL，无损失）
  - **降级逻辑保留**：撞锁降级仍要有（WAL 下多写者仍会冲突，只是窗口小得多；且 `drain_task_queue` 的历史清理是大事务，扫描启动瞬间仍可能撞上）
  - **文件系统的注意**：DB 在 `/mnt/disk1`（本地阵列盘）经 `/mnt/user`（shfs/FUSE）访问——SQLite 官方警告的是**网络文件系统**（NFS/SMB），shfs 底下是本地盘，WAL 的 `-shm`（mmap 共享内存）实践可靠；这台 Unraid 无 cache 池
  - **备份注意**：Unraid 备份 appdata 要连 `-wal` 文件一起（或接受最近事务未合并——库内数据全是可重建的派生数据）

- **历史清理必须分批**（2026-11-12，`app/services/task_drain.py` 的 `_purge_in_batches`）：
  - **为什么**：一次性 `DELETE WHERE started_at < cutoff` 删几千行会**长时间持有 SQLite 写锁**——`job_runs` 单条 `result_json` 有 10–30KB，几千行即几十 MB 的删除事务，与扫描/调度的写事务互相撞（`database is locked`）
  - **分批**：每批 500 行（`_PURGE_BATCH`），批间 `commit` + `await asyncio.sleep(0.05)` 让出事件循环——SQLite 只有一个写者，背靠背的写事务会让播放中转插不上队
  - **单次上限**：`_PURGE_MAX_BATCHES=10`（= 5,000 行），删不完**下轮继续**（作业每 5 分钟一轮）——任何时刻都不会长时间持锁；停机 N 天的补账也只需几轮
  - **实测数据**：`job_runs` 稳态 ~1,300 行/天（drain 自身 518 + fetch_all_sites 313 + monitor 306…），单条 ~19KB → 30 天保留期 ≈ 4 万行 / ~750MB（占库 89%，全是派生数据，SQLite 无压力但值得留意）
  - 契约由 `tests/integration/test_task_drain.py::TestPurgeBatching` 固化（超量分批 + 上限 + 不足批一次删完 + `probe_results` 清理——后者此前零覆盖）

- **扫描不得长时间持有 SQLite 写锁**（2026-11-12 prod 事故，`app/services/media.py` 的 `scan_driver`）：
  - **现象**：面板手动触发存储源扫描（光鸭，航海王 800+ 集附件同步）后，40 分钟内 61 次 `database is locked`——`drain_task_queue`（每 5 分钟必炸）、`refresh_user_data`、手动触发的 `push_free_torrents` 全中；`job_runs` 历史从 15:55 起**全部丢失**
  - **根因**：扫描的**一个事务**横跨「写 strm（CIFS）+ 等附件下载」——`_upsert_media` 的 `flush` 已拿到写锁，之后却在事务里做 CIFS 写文件与 `asyncio.wait` 等附件（单附件最坏 60s 超时 × 3 次重试）→ **写锁被持有几十秒**
  - **为什么 WAL 救不了**：WAL 只解决**读写**互斥，**写-写仍是单写者**；`busy_timeout=10s` 等不到 → 别人的写事务全被挡住。WAL 切换后事故照旧发生
  - **修法（铁律）**：**flush 之后、commit 之前，不做任何非 DB 的 await**。strm 写入推迟到 commit 之后（`pending_strms` 清单 + `_write_strm_batch`）；附件背压等待前先 `db.commit()`
  - **统计时点后移**：strm 的 created/updated/unchanged 改为批次提交后统计（数字不变，`on_progress` 读到的进度最多滞后一批）
  - **契约**：`tests/integration/test_scan.py::TestScanLockWindow`（第二个连接写库**不被饿死** + 写 strm 时无未提交写）

- **入队撞写锁要退避重试，不能炸整轮**（2026-11-12，`app/services/task_queue.py`）：
  - `enqueue_with_retry` / `claim_with_retry`：撞 `OperationalError` 退避重试 3 次（0.5 / 1 / 2s），每次**新开 session**——锁冲突后旧事务已回滚，session 的事务状态不可复用
  - 重试耗尽 → 抛 `TaskQueueBusyError`：自动推送**记失败明细并 continue 下一个候选**（整轮不炸；幂等键保证下轮 cron 自动补推、不重复）；手动推送返 503 `db_busy`
  - **入队是核心业务**，不能像直链缓存那样静默放弃——降级语义与 `link_cache` / `_record_run`（可丢的优化/统计）**刻意不同**
  - 契约由 `tests/integration/test_push_free.py::TestEnqueueLockContention` 固化（入队撞锁重试 / 认领撞锁重试 / 重试耗尽只挂一个候选且其它照常推）

- **光鸭扫描走全量列表，~16 请求替代逐目录递归**（2026-11-12 生产探针实测，`app/adapters/drivers/guangya.py`）：
  - `fs_files` 支持 `parent_id="*"`（全账号）+ `res_type` 过滤，条目带 **`fullParentIds`**（从账号根到父目录的完整 fileId 链，`/` 分隔）——一次目录翻页（`res_type=2`，生产 726 目录 = 1 请求）建 fileId→目录名映射 + 一次文件翻页（`res_type=1`）按链首过滤 `media_cid` 子树，`relative_dir` 由链上 fileId 换算目录名拼出
  - **收益**：生产 970 目录 ≈ 1100+ 请求（0.2s 限速下 5-9 分钟）→ ≈16 请求（≈10 秒）；`page_size` 实测支持 1000+（200/500/1000 全生效，无 code=112）
  - **附件下载并发 3 路**（同日生产实测，`media.py` 的 `_sync_attachment_counted`）：串行时首次全量附件 ~18s/个（CDN 掐连接 → 3 次重试 × 60s 超时的长尾），几千个附件 = 十几小时。换直链走全局限速器天然安全，并发只摊掉 CDN 等待；`ATTACHMENT_CONCURRENCY` 从 4 调到 **3**（并发 4 失败率 17%：`peer closed connection without sending complete message body`；并发 2 对比实测成功率 91%、41 个/分钟），`ATTACHMENT_WINDOW=64` 做背压；扫描取消时终止剩余附件任务，正常结束等全部收尾（统计才完整）
  - **附件失败的三种模式与对策**（同日实测）：① 光鸭直链在**附件下载场景**下不可复用——同一个 URL 第二次访问会失败（故每次重试都换新链 ✅）。⚠️ **但「直链一次性」不是普适结论**：2026-11-12 用 `Range: bytes=0-99` 连续 GET 同一个直链**两次都返回 206**（可重复访问）——附件那次更可能是别的因素（完整 GET / 特定 header / CDN 缓存策略），**别把「一次性」当公理** ② **网络抖动**：128 容器 DNS 偶发 `Temporary failure in name resolution`、CDN `ConnectTimeout` → 退避从 1s 提到 **5s**（5/10/15s，跨过抖动窗口）+ 重试次数 3→**4** ③ **附件超时是敏感参数**：共享 client 的 60s 会让卡死连接占住 worker 60s（长尾毒瘤），但**压到 15s 更糟**——实测失败率从 0.24%（60s）飙到 12~17%（光鸭部分 CDN 节点首次 GET 无缓存，15s 不够）。折中 **45s**：给慢节点留够时间，又短一截
  - **换直链要对齐官方限频**（同日实测）：官方是 5 次/秒（列目录）/ **2 次/秒（换直链）**，但限速器只有一个光鸭桶（0.2s = 5/s）→ 并发 4 时换直链超限 2.5 倍 → 光鸭软限频 → 扫描速率从 45 掉到 21 个/分钟、30 分钟 96 次重试。修法：`get_direct_link` 走独立桶 `guangya_link`（`guangya_link_rate_limit_interval_sec`，默认 0.5s）；播放换直链同样受益（账号级配额本就该共享）
  - **条目缺 `fullParentIds` / 目录映射缺失 >10 → 报错不静默**——静默跳过会把故障伪装成「媒体库为空」，且 prune 会把「源端还在」的文件当孤儿删掉；少量缺失用 fileId 占位（路径可复现，不产出错路径）
  - **目录 `utime` 不可靠，别想做剪枝**（实测）：建子目录会变，**改名/删除都不变**——用它跳过子树必漏文件
  - **翻页防御上限**（`MAX_LIST_PAGES=1000`）：`cloud_task_list` 实测会忽略 `page` 参数无限返回同一页（1.5s 打出 2 万+ 请求）；`fs_files` 一旦同款故障必须可见失败
  - 混合列表（`file_types=[]`）里 nfo/jpg/iso 全在——本地按后缀分类不变，**绕开**光鸭自有类型分类（视频=2/图片=1/文档=4/压缩包=5，与我们的后缀清单对不上）
  - 契约由 `tests/unit/test_guangya_driver.py::TestIterTree` 固化；探针脚本 `scripts/probe_guangya_scan.py` / `probe_guangya_verify.py`（生产容器 `docker exec -i medialab /opt/venv/bin/python -` 跑，探针目录自建自删、绝不碰 `fs_clear_recycle_bin`）

- **事件循环里绝不出现同步 CIFS IO**（2026-11-06 prod 事故，规则）
  - 挂载是 `hard` 模式 → IO 卡死时**无限等待**；一次同步 `stat` 就能把整个事件循环卡住 → uvicorn 无法处理请求 → `/healthz` 被 RST → 容器 unhealthy、**面板实际不可用**（实测假死 20 分钟，挂载恢复才自愈）
  - **规则**：任何对「监听根 / 源根目录 / 挂载点」的 `stat` / `is_dir` / `resolve` / `scandir` 都必须 `asyncio.to_thread`（`_check_stable_once` 的 `FileStamp.of`、`start_watcher` 的 `resolve_watch_dir` 已改）
  - **同步 import 也算**（2026-11-07 prod 事故补）：`watchfiles.awatch` 首次调用会在循环线程上同步导入 anyio 后端（`get_cancelled_exc_class` → `import_module`，真实读源码文件）——CIFS 挂死引发 FS 层拥塞时被卡 8 分钟，loop 冻结、uvicorn 停在 bind 之前、面板假死（守卫 py-spy 自动取证定位）。修复：`watcher._watch_events` 先 `asyncio.to_thread(_preflight_watchdeps)` 预热（⚠️ 不能用 `get_cancelled_exc_class()` 预热——它要求当前线程有运行中的 loop，会 `NoEventLoopError`，直接 `importlib.import_module("anyio._backends._asyncio")`）
  - **附件下载路径也算**（2026-11-12 补漏）：`attachments.py` 的判重 `stat` / `mkdir` / 每个 chunk 的 `fh.write` / `part.replace` / `.part` 清理曾全部裸跑在事件循环里——当年 `write_strm` / `delete_strm` 改了线程池，这条路径漏了。现在 `_drain_to_file` 逐块丢 `to_thread`（读响应流是异步的，留在事件循环）
  - **运行时守卫**（`app/services/runtime_guard.py`）：① 心跳看门狗——loop 滞后 >10s 自动 py-spy dump 落日志（py-spy 已进镜像）；② 挂载探活线程——`/healthz` 的 `mounts[]` 显示 `ok`/`hang`/`error`，**挂载卡死可见而面板不假死**
  - `snapshot()` 只读缓存（`/healthz` 同步调用，不能被 IO 拖住）
- **必备命令**
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/ tests/
uv run mypy app/
uv run pytest                # 后端测试（1673 例）
cd web && npx vue-tsc -b    # 前端类型检查
cd web && npx oxlint .      # 前端 lint
cd web && node scripts/check-types-placement.mjs  # 类型放置约束（API 契约类型集中在 src/types.ts）
cd web && node scripts/check-css-classes.mjs   # CSS class 定义约束（template 用的 class 必须有样式定义）
cd web && npx vite build    # 前端构建
```

静态检查不通过禁止合并。**CI（`.github/workflows/ci.yml`）会跑上述全部闸门**：
ruff + mypy + pytest + vue-tsc + oxlint + types-placement（API 契约类型集中检查）
+ css-classes（CSS class 定义检查），全绿才允许合并。

- **CSS class 定义检查**（2026-11-03 加入，`web/scripts/check-css-classes.mjs`）：
  - 扫每个 `.vue` 的 template 里用到的 class，校验它在「本文件 `<style>`」或
    「`web/src/styles/global.css`」里有定义
  - **防的是什么**：`544f8f9 feat(downloaders): 合并「下载器」页与「任务」页`
    把 `Torrents.vue` 并入 `Downloaders.vue` 时**只搬了 template + script，
    没搬 `<style scoped>`**——6 个 class 定义就此丢失，页面样式全坏
    （筛选栏控件挤成一团、进度条与百分比上下堆叠）。**vue-tsc 不管 CSS、
    oxlint 也不管 CSS** → 没有任何闸门能发现，直到用户截图反馈
  - **豁免机制**：BEM 子元素（样式由父元素覆盖）可加进脚本里的
    `EXEMPTIONS`，**必须写豁免理由**——否则会变成垃圾桶
  - **合并 / 重构页面时必须连 `<style>` 一起搬**——这条闸门只防「完全丢失」，
    不防「搬漏了某个规则里的某条属性」
- **响应式（手机适配，2026-11-04 加入）**：
  - **此前全站零 `@media`、零断点**——侧栏恒占 280px，375px 视口下内容区
    只剩 95px（13 个页面实测全是 95px）；表格不配 `:scroll` 时 Ant 的
    `table-layout:auto` 按内容撑开，`/sites` 文档宽 1503px、`/downloaders`
    1551px（视口只有 375px）
  - **断点**：`<768px` 手机 / `<992px` 平板 / `≥992px` 桌面。与 Ant 的
    `a-col` 断点一致
  - **`<DataTable>` 是表格的唯一入口**（`web/src/components/DataTable.vue`）：
    自动按列宽总和注入 `:scroll="{ x }"` + `table-layout: fixed`。
    **新增表格一律用它**，别直接写 `<a-table>`——否则又是一个「忘了配
    scroll」的表格
    - `fixed` 是**全局约定**：`auto` 时列宽只是建议值，浏览器按内容重新
      分配，设了宽度也白设
  - **`<PageHeader>` 是页面标题的唯一入口**：统一两个业务域的页面骨架
    （此前辅种域用 `.page-header`、媒体库域把标题写在 `a-card` 的
    `#title` 里——两套契约，手机端没法统一折叠）。媒体库域用 `compact`
  - **`useResponsive()` 是断点判断的唯一入口**（`web/src/composables/`）：
    基于 Ant 的 `Grid.useBreakpoint()`。**别写 `window.innerWidth`**——
    不响应窗口尺寸变化，且会散落成灾
  - **窄屏的关键 CSS 陷阱**：
    - flex item 默认 `min-width: auto` → 内容多宽它就多宽。要显式
      `min-width: 0`
    - `max-width: 100%` 参照的是**父容器**宽度；父容器被内容撑开时
      max-width 形同虚设（循环）
    - `a-space` 是 flex 容器，**默认不换行** → 要 `flex-wrap: wrap`
    - 行内 `style="width: 380px"` 会把视口**撑宽**（`innerWidth` 变成
      506px）→ 用 `max-width: 100%` 兜底 + 组件级 `compact` 类
  - **侧栏在手机端必须 `display: none`**——不能用 Ant Sider 的
    `collapsedWidth: 0`：那只是把宽度收成 0，侧栏自身 `overflow: visible`，
    菜单内容仍会溢出到页面左上角（Ant 为此配的 `zeroWidthTrigger` 黑色
    浮标又会和 header 汉堡重复）。导航走 `<a-drawer>`
  - 契约由 `web/tests/responsive.spec.ts` 固化（13 页 × 无横向溢出 /
    侧栏隐藏 + 抽屉可开 / 桌面端不溢出 / 每页都有统一标题）

- **整理 + 刮削（2026-11-11 迁移自 MoviePilot）**：
  - **输入是「源目录」不是「下载器任务」**——整理与下载器**彻底解耦**：
    文件从哪来无所谓（BT 下载 / 手动拷贝 / 其它工具），放进源目录就会被
    扫到。配置 `organize_source_dirs`（逗号分隔多目录），判重键
    `organize_history.source_path`（UNIQUE）。**不要**改回「轮询下载器 +
    tag 过滤」——那把媒体库域和辅种域纠缠在一起，还依赖下载器文件系统可达
  - **产物目录名用 TMDB 权威标题与年份**（电影与剧集**必须一致**）：
    文件名解析出的标题常是英文原名（`Fight Club`），TMDB 的中文名才是媒体
    服务器界面里显示的那个（`搏击俱乐部`）。曾因电影路径漏改而把英文名
    直接写进媒体库目录
  - **方括号里的中文译名不能当噪声剥掉**（`[把黑夜点燃]`）：它是 TMDB 上
    的正式名，搜索它能**唯一命中**；只用英文名会搜到同名的**另一部剧**
    （实测 `Light the Dark` → 命中《黑夜告白》而非《把黑夜点燃》）。故
    `meta_parser` 提取为 `alt_titles` 候选（**纯 ASCII 的方括号是发布组标记，
    忽略**），TMDB 客户端 `search_any` **任一候选 exact 命中即采用**
  - **识别优先级：别名表 > 缓存 > 搜索**。别名（`scrape_alias`）是用户固化
    的「标题 → TMDB ID」映射，命中时**跳过搜索**；别名键按归一化标题去重，
    **key 必须是文件名解析出的标题**（不是 TMDB 返回的名字）——「记住此
    修正」自动写入时连**媒体类型**一起记，否则命中时会按 movie 取 TV 详情
  - **季目录判定用 `season_number_from_dir()`**，不能用 `parse_meta()`：
    `Season 01` 是纯季标记无标题，`parse_meta` 返回 None → 整剧产物
    （`tvshow.nfo` + 海报）会落进**季目录**而不是剧集根
  - **冲突绝不覆盖**：目标已存在且大小不同 → 记 failed 点名（不误删底线）；
    大小一致 → skip（幂等）。move 模式**复制→校验→才删源**，且不动下载器任务
  - 实测脚本 `scripts/selftest_scrape.py`（造假文件跑电影 / 多季 / 字幕 /
    move / 边界五条路径，自清理）：`docker exec -i medialab-dev
    /opt/venv/bin/python - < scripts/selftest_scrape.py`
  - 契约由 `tests/unit/test_meta_parser.py`、`tests/unit/test_tmdb.py`、
    `tests/integration/test_scrape_service.py`、`test_scrape_api.py`、
    `test_scrape_alias.py`、`test_organize_service.py` 固化


## 后端测试
```bash
uv run pytest                        # 全部
uv run pytest tests/unit             # 只跑单元
uv run pytest -k "site_adapters"     # 按名字过滤
uv run pytest --cov=app              # 带覆盖率（当前基线 58%）
```

- 目录：`tests/unit/`（纯函数、无 IO）+ `tests/integration/`（DB / 作业 / API）
- 基础设施在 `tests/conftest.py`：
  - `settings` fixture 注入独立 DB（**不会碰 `/app/data` 或 `data-dev/`**）
  - `db` fixture 每个用例重建 engine + 建表（function 级 —— aiosqlite 连接池绑定创建它的 loop）
  - `no_network` autouse fixture 用 respx 接管 httpx 传输层，**未 mock 的请求直接失败**（不会误拉真实 PT 站）
- 写 HTTP 用例**必须**用 respx mock；m-team 是 API 站需要 JSON 响应，其余 8 站是 HTML
- 测 `updated_at` 这类带 `onupdate` 的列，要用原生 SQL 改值（ORM 会在 flush 时覆盖手动赋值）


## 提交信息格式
Conventional Commits，作用域示例：`feat(crypto)` / `fix(free-seed)` / `docs(req)` / `refactor(sites)` / `feat(sites)` (FR-1.6 站点 userdata) / `feat(db)` (SiteUserData 表) / `feat(api)` (user_data 端点) / `feat(web)` (UserData.vue) / `feat(scheduler)` (调度作业/配置/历史) / `feat(cross-seed)` (辅种下载器巡查 + 快照落库) / `feat(checkin)` (FR-3.6 站点每日签到) / `feat(bonus)` (站点时魔抓取)。

## 前端测试
```bash
cd web && npx playwright test    # 需先起 dev 容器 (localhost:8080)
```
新增页面/交互改动必须补 `web/tests/*.spec.ts` 用例。

**注意**：Playwright e2e 连的是**真实下载器与真实站点**，CI 上跑不了也不跑
（CI 跑 vue-tsc + oxlint + types-placement；不含 Playwright）。e2e 需在本机起 dev 容器后跑。

