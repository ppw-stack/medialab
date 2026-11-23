# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `README.md` — 现状与命令。
2. `docs/merge-strmmanger.md` — **合并 strmmanger 的方案与实施记录**（含冲突清单与返工范围）。
3. `app/adapters/sites/user_data.py` + `app/adapters/sites/indexer/parser/nexus_php.py` — FR-1.6 站点用户数据契约（用户数据由规则包的 schema 解析器产出）。
4. `CHANGELOG.md` — **对外契约变更记录**（HTTP API 状态码 / 响应体语义）。改公开端点语义时必须在此登记（含 BREAKING CHANGE 与迁移步骤）。
5. `docs/upload-bandwidth.md` — **数据同步上传的带宽瓶颈实测结论**（并发为何无效）。
6. `docs/quality-gates.md` — **前端工程规范与质量门禁**（ESLint / Prettier / TS 严格模式 / Git Hooks / CI）。改前端代码前必读。

## 项目定位

**一个应用装下两个业务域**，共用一套面板、一个 SQLite、一个 SPA：

- **辅种域**（原 ptHelper）—— PT 站点拉取 / 免费种窗口 / 辅种巡检 / 孤儿清理
- **媒体库域**（原 strmmanger）—— 网盘 STRM 生成 / 302 中转 / WebDAV / 数据同步（本地 → 网盘）

**两个域的 API 前缀天然不冲突**：辅种域 `/api/v1/*`，媒体库域 `/api/*`。

## 工作准则
- **分层约束是硬性的**（`tests/unit/test_layering.py` 固化）：
  ```
  domain → core → db → adapters → services → webdav → api
  ```
  - `api` 层**不碰 DB、不碰适配层**——业务逻辑全在 service 层
  - `webdav` 是协议适配层（ASGI 守卫 + wsgidav 挂载）：可依赖 services / db / core，
    但 services 不得反向依赖它。DAV 根配置的实现在 `app/services/dav_root.py`，
    `app/webdav/paths.py` 只做兼容转发
  - 新增 `app/<pkg>` 必须显式登记到 `LAYER_ORDER`——未登记的层现在会被直接拒绝
    （不再静默放行）
  - `services` 层**不 import fastapi**，抛 `ServiceError` 由 API 层翻译成 HTTP 状态码
  - `adapters` 层可独立单测（不依赖 db / services）
  - `domain` 层是业务概念与常量（如 `FreeType` / `DriverType`），不依赖任何层
  - 改完跑 `uv run pytest tests/unit/test_layering.py` 验证
- **应用只能单实例运行**（2026-11-17 加固）：`app/services/instance_lease.py`
  在 lifespan 里抢 DB 租约（TTL 60s + 心跳），第二个 worker / 副本直接启动失败。
  调度中心、三套 `*_jobs` 内存表、`watch_manager` 的 10 组全局字典、QR session
  都是进程内状态，**没有跨进程方案**——不要改 Dockerfile 的 `--workers 1`，
  也不要用多副本共享同一个 DB 卷。测试环境（`env="test"`）跳过租约。
- **启动降级必须登记**：以前「只记日志继续启动」的失败路径（监听器加载 /
  单任务启动、驱动测连、密码升级、探活目录读取）现在要写
  `app/services/startup_status.py` 的台账，`/readyz` 会把它显示在
  `degradations` 里并把 `status` 标成 `degraded`（HTTP 仍 200，不触发容器重启）。
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
  - **它什么时候才划算**：源文件**本来就在大带宽 CDN 上**（当年是 115 直链）→ 那正是**迁移功能**的场景（当时是 115→光鸭，2026-11-22 起只剩光鸭→光鸭）。**「本地 → 光鸭」走离线下载反而更慢**
  - **随之删除**：6 个后端文件（`offline_download` / `offline_files` / `offline_config` / `offline_jobs` + 两个路由）+ 「离线下载」页 + 三项环境变量配置（`OFFLINE_HMAC_SECRET` / `OFFLINE_PUBLIC_BASE` / `OFFLINE_FILES_ROOT`）。存量配置行由幂等迁移 `delete_offline_env_settings` 清掉（连带那个死密钥）
  - **光鸭的「离线任务」机制仍在用**（迁移功能）：`guangya.py` 的 `create_offline_task` / `list_active_offline_tasks` / `list_failed_offline_tasks` **保留**，别跟着删
  - **仍然有效的坑（2026-11-11 实测补充）**：光鸭的 `progress` 字段**不是可靠的百分比**（2.3GB 文件下载到 88% 时它只有 4）→ **不用它判定完成，也不再展示**——迁移在途列表只报「路径 + 大小」（假百分比曾误导用户，已随展示一起移除）。判定走「活跃列表消失 + 目标目录同名同大小校验」。实测还确认：任务项初始结构**没有** `progress` 字段（只有 taskId/fileName/totalSize/status/createTime/res/parentDirType），它只在真实拉取时动态出现；对**非媒体 URL**（TUNA 的 .iso/.gz）云添加会挂 3 分钟后统一 `code=8 云添加失败`——历史成功案例全是媒体文件（.mkv / 115 直链），**疑似云添加只接受媒体类 URL（待证）**；`get_task_status()` 是**上传任务**的接口，查云下载任务返回 145；**中途删目标目录会静默终止或卡住任务**（探测时踩到：一个 15MB 任务卡在活跃列表不结束）
- **迁移功能只支持「光鸭 → 光鸭」**（2026-11-22 移除 115 源，`app/services/migration.py`）：
  - **只有一条传输路线**：直链 → **云下载**（`cloud_create_task`）→ **改名**。光鸭源的直链**不绑 UA**
  - **云下载按 URL 末段落名**——**光鸭 CDN 直链末段是 `download`** → 落盘后必须 `fs_rename` 改回原名（驱动层 `rename_file`）
  - **光鸭源恒串行**（并发 1）+ 提交间隔收紧到 5s：落盘名撞车会触发服务端自动改名 `download(1)`，改名完成前不能提交下一个；瓶颈是云添加额度
  - **能搬分享审核拒绝的文件**：`shareAuditStatus=3`（`canShare=None`）的影视文件**分享不出去但直链+云下载完全可用**（实测 336MB / 7s ≈ 48MB/s）
  - **同名不同大小 → 「保留大的」**（2026-11-13，与数据同步 `conflict_policy` 口径一致）：目标更大 → **保留目标那份**、跳过传输（清理源开启时把源的冗余小副本清掉）；源更大 → **删目标侧的小文件**（进回收站，可恢复）→ 重传，最终目标侧是大的那份。**同名同大小** → 跳过传输（清理源开启时清掉源的冗余副本）——否则「跳过」会留下源副本，腾空间的目的落空
  - ⚠️ **云添加额度是硬墙：实测 3 次/账号/天**（前端文案「VIP 1000 次/天」对实测账号不成立；**失败也扣**）→ 撞限额转 **`paused`** 状态（不硬撞不重试），幂等设计保证**次日重跑自动续传**
  - ✅ **直链流量不是迁移的约束**（实测）：**取直链不吃流量**（批量取 141 条，计数器 `used` 1→1 纹丝不动）、**云下载是服务端内部复制也不吃**（1.09GB 传进目标，目标计数器不动）→ 流量只影响 **302 播放**（那才是「用直链下载数据」）；主库放 VIP 账号正是为此。**真正的墙只有目标账号的云添加次数**（记在**目标**账号上；直链流量记在**源**账号上）
  - → 因此可把「取链」与「提交」拆两段：**批量预取直链存着**（免费、不限速），等额度窗口再集中提交。工具：`scripts/fetch_guangya_links.py`（批量取链 + **连目录结构一起备份**，光鸭接口不返回路径，只有 `fullParentIds` id 链 + 目录名映射能还原位置）、`scripts/submit_guangya_links.py`（按清单建目录→云下载→改名→校验，额度耗尽即停）；⚠️ 直链有效期 **6 小时**（`urlDuration=21600`），过期需重取（取链免费，约 1 条/秒）
  - **「搬完清理源」是显式开关（默认关）**（2026-11-13 加，`migration.py` 的 `_delete_source_file` + 驱动 `delete_file`）：**只删目标侧已确认落盘同名同大小**的文件；删除走 `fs_delete` **进回收站**（可恢复，非永久删除）；**记审计日志**（`logger.warning` + 作业日志「已清理源」）——删数据不可逆，必须能回答「删了什么」；**单个清理失败不炸整轮**（记失败明细继续）；**源驱动须支持删除**（当前唯一的光鸭驱动支持；没有删除能力的驱动会被忽略并在日志说明）。**顺序不可反**：先删源再传，一旦传失败就是数据没了。**中断恢复**多一条——认领上一轮「已落盘但没改名」的 `download` 残留（按大小匹配后改名），不重复提交
  - 光鸭源的目录浏览**从账号根开始**（根 id 是空串，`ROOT_DIR_IDS`）；`fullParentIds` 链首是**账号根的直接子目录**（不是媒体库根）→ `iter_tree_by_cid` 支持**任意目录**作根（扫描传 media_cid，迁移传选中的源目录）
  - 探针 `scripts/probe_guangya_migration.py`（`probe` = 单文件端到端；`engine` = 跑真实引擎 + 幂等复跑；未部署时可用 `MEDIALAB_APP_PATH` 指向注入的新代码，不动生产镜像）
- **Bearer 长期凭据只用 `WEB_TOKEN`**（2026-10-30 加入）：`verify_bearer` 同时接受 API Token（`WEB_TOKEN`）与 session cookie 值两种口径，但 session cookie 值 24h 过期（`SESSION_MAX_AGE`）。CLI / 脚本长期使用应存 `WEB_TOKEN`，不可把 session cookie 值当长期 token 存储——后者会突然 401。session 值口径仅为面板/CLI 同源便利，非长期凭据。
- **站点适配层只有一个通用实现（2026-11-20 全量替换）**：旧的 10 个站点专用 adapter
  与 `parsers/`（含 `attendance.py`）**已删除**；站点差异全部由规则包
  （`app/adapters/sites/indexer/rules/indexers.json`）表达，`_registry` 只有 `generic`。
  随之移除的功能：签到（FR-3.6）、时魔（hourly bonus，含
  `site_user_data.hourly_bonus*` 四列，已由 bootstrap `drop_column` 删除）。
  HDWinG 逐种优惠查询在 2026-11-22 以通用适配器 + service 增量判定的形式恢复，
  见下方「hdwing 站点的免费判定」。
  字幕由新增契约 `SiteAdapter.search_subtitles()` 提供（无配置返回 `[]`，
  失败抛 `SiteFetchError`）。旧 adapter 的 9 站细节段落保留在此仅供历史参考。

- **规则包移植的遗留问题（2026-11-20 记录，待处理）**：
  - ✅ **HDWinG 逐种优惠查询已接回（2026-11-22）**：列表页 AJAX 注入
    （`engine.HDWING_PROMOTION_INJECTION = False`）仍保持关闭——站点
    `ajax_promotion.php` 受 Cloudflare Turnstile 保护，**未过闸时对任意
    id（含已过期、甚至不存在的）都返回兜底 `pro_free` 且 `timeout` 恒空**。
    免费判定改由 service 层逐种查 `GET /promotion.php?id={tid}` 权威 SSR
    优惠历史表：有 `Free` 记录且截止时间 > 现在 = 限时免费；截止为空 =
    永久免费；404 / 「页面不存在」= 无优惠；429/403 = 熔断；其它错误 =
    `SiteFetchError`。实现见 `GenericAdapter.get_promotion()` +
    `app/services/jobs.py:_resolve_promotions()`；并发 5、单轮上限 200。
  - **API 站（m-team 等）下载走「换票配方」**：`get_torrent_url` 返回
    `[base64]url` 形式的一次性请求配方（`app/adapters/sites/indexer/spider/*.py`
    的 `get_download_url`），**不是**可直接 GET 的普通 URL。旧的逐站 token
    下载路径已随 adapter 删除，下游消费方需按配方格式处理
  - m-team 的种子 ID 只能从 `page_url` 的 `/detail/{id}` 提取（spider 不产出
    `id` 键）——`generic._row_item_id()` 已做回退，取不到时下载/幂等键全失效
  - ✅ **NexusPHP「下载提示页」已接上（2026-11-23）**：`downloadnotice.php`
    **不是故障**，是站点的正常下载协议——详见下方「NexusPHP 下载提示页」
- **NexusPHP 下载提示页（2026-11-23 prod 事故修，重要）**：
  - **现象**：HDWinG 手动推送种子全部失败，报「响应不是 bencoded torrent」，
    队列重试 5 次全败 → 用户看到「推送失败」
  - **根因**：站点在**分享率不足**（0.985 < 1）时，`download.php` 会 302 到
    `downloadnotice.php?torrentid={id}&type=ratio` —— 一个**确认页**（15614
    字节 HTML，标题「HDWinG :: 下载提示」），要勾选「我会提高分享率的」并
    提交「下载种子文件」才放行
  - ⚠️ **确认表单有两个反直觉形态**（缺一个服务端就只回 `error`）：
    ① 同意项 checkbox **没有 `checked` 属性** → lxml 的 `form_values()` 会
    跳过它；② 提交按钮在条件未满足时带 **`disabled`**（靠页面 JS 在勾选后
    移除）→ 按「只提交有效控件」的规则会被排除。两者都由
    `indexer/download.py:parse_download_confirmation()` 显式补齐
  - **修复位置**：`GenericAdapter.download_torrent()` 命中提示页 → 走
    `GenericAdapter._confirm_download_notice()`（解析表单 → POST → 自动跟随
    重定向 → 拿到真种子）。判据 = 最终地址含 `downloadnotice.php` **或**
    正文含「下载种子文件」（端点名被改时的兜底）
  - **实测**：POST 成功 → 302 回 `download.php?…&letdown=1` → 真种子
    358KB（bencode `d8:announce`）。**注意**：确认后仍拿不到种子时按**瞬时**
    失败处理（无法确证是封禁，重试仍有可能成功）
  - ⚠️ **旧代码里 `parse_download_confirmation` / `is_download_confirmation`
    一直存在却零生产调用点**——规则包移植时带了协议解析器，但通用适配器的
    下载路径没接上（只在测试里被引用）。这类「移植了但没接线」的代码要查
  - 契约由 `tests/unit/test_generic_adapter.py`（三类用例）+ 
    `tests/unit/test_site_indexer_compat_pure.py`（表单补齐）固化
- **hdwing 站点的免费判定（2026-11-16 实测改版，重要）**：
  - **列表页优惠标记是空占位** `<td class="discount"><span class="sp_state_placeholder" id="{tid}"></span></td>` —— FREE 徽章与倒计时由页面 JS 异步填充，**列表 HTML 里没有**。别再指望从列表行解析 `pro_*`（其它 8 站可以）
  - ⚠️ **填充它的 `ajax_promotion.php` 被 Cloudflare Turnstile 闸保护**：页面 JS 必须先 `POST ajax_turnstile.php` 带 `cf-turnstile-response` token。**未过闸的请求对任意 id 都返回兜底的 `pro_free` + 空 timeout**——实测连**根本不存在的 id（999999999）**都回「免费」。照它解析会把「限免 6 小时」误判成「永久免费」（`window_parser` 收到 `free_classes=['free']` + 无剩余时间 → 判 `PERMANENT`，窗口永不到期）
  - ✅ **权威源是 `GET /promotion.php?id={tid}`**（SSR 优惠历史表：优惠类型 / 开始时间 / 截止时间 / 操作人）。判定 = 有 `Free` 记录 **且 截止时间 > 现在**；**截止时间为空 = 永久免费**。实测窗口形态：6h / 24h / 48h / 30 天
  - **种子抓取不带任何筛选参数**（`GET /torrents.php?page=N`）：① 站点筛选参数语义随版本漂移（各站 `spstate` 含义都不同）② `spstate=2` 按「免费剩余时长」倒序，会把 2022 年老种（重开 30 天促销）顶到前面，**按发布时间排序的全量列表反而不含它们** ③ 筛选后的结果**仍然没有**每个种子的真实窗口
  - **代价与增量**：`promotion.php` 是**逐种子**请求（无批量入口，`promotion.php` 不带 id → 404；单条实测约 0.4–1.0s）→ 由 service 层做**增量判定**：`PERMANENT` 不复查；`TIMED` 只在截止后复查；已确认非免费的条目在 `free_payload` 写 `promotion_checked_at`，默认 **60 分钟 TTL** 后才复查；新增条目立即查。其余沿用库里旧判定（回填成等效 `free_payload`，upsert 后值不变）。适配器用 `supports_promotion_lookup = True` 声明该能力（`app/services/jobs.py:_resolve_promotions`），并发 5、单轮上限 200。2026-11-22 生产实测：3 页 300 条全量约 61s，300/300 HTTP 200，当前生效免费 34 条；因此**不能**把 `unknown` 当「每轮都查」——否则约 4.3 万请求/天
  - ⚠️ **404 = 种子不存在**（NexusPHP 标准 404 页）→ 当「无优惠记录」处理，不算故障；另有部分不存在的 id 返回 **HTTP 200 的「页面不存在」页**，同样按无优惠处理。429/403 → 熔断；其它 4xx/5xx / 缺少优惠表头 → `SiteFetchError`（不写判定，下轮重试）
  - ⚠️ **登录页 = 站点级故障（2026-11-22 审查修复）**：`promotion.php` 返回登录页（cookie 失效）→ adapter 抛 `SitePermanentError`（**永久**失败，与 `SiteFetchError` 分开）→ service 层**中止本轮优惠复查**并把该站记为**站点失败**（`failed_sites` + 明细点名）。**不能当单条失败吞掉**：那会把故障静默写成「非免费」，且下一轮还会对同一批失效 cookie 重打上百个请求（单轮上限 200）。⚠️ **service 层不 import httpx**——传输异常由 adapter 统一翻译成 `SiteFetchError`（分层约束：adapters 翻译，services 只认本层异常）
  - fixture 抓取：`scripts/capture_hdwing_promotion_fixtures.py`（裁剪 + 自校验；4 种真实形态：生效中 / 生效+过期混合 / 有表头无 Free / 已过期）。抓取脚本读生产库 cookie 时使用 `mode=ro`，只 GET 站点页面
- **FR-1.6 站点用户数据契约**（2026-10-18 加入，2026-10-18 精简为单方法，2026-10-22 加时魔）：
  - 不抽 9 站 user_data 基类；NexusPHP 7 站共用 `app/adapters/sites/parsers/nexus_php.py` module-level helper（`parse_user_snapshot` / `parse_hourly_bonus`）
  - `SiteUserData` 表是**当前快照**（UNIQUE site_id），不做历史；如需时间序列需先建 `site_user_data_history` 表再迁
  - 适配器单方法 `get_user_snapshot`：**返回 `None` 只表示"该站不支持 / 无凭据"**（service 记 skipped）；**拉取/解析失败必须抛 `SiteFetchError`**（service 记 failed 并点名，**不写 0 兜底**——写 0 等于把故障伪装成"上传 0 / 做种 0"）
  - 抓取页用 `/mybonus.php`（navbar 带 3 业务字段 + 时魔，**一页拿全**）；navbar 解析不出 3 字段时回退 `/userdetails.php`
  - **刷新时机：并进 `fetch_all_sites` 作业**（2026-11-22 起）：两者遍历同一批启用站点，共用 `*/10` 心跳——先拉种子；再刷上传/下载/做种/时魔。独立作业 `refresh_user_data` 已移除。面板「站点」页打开时**另外**自动测连+刷新一次（5 分钟冷却），属即时补充而非唯一时机
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
- **可重试的下载失败 = 202「已入队重试」，不是 502**（2026-11-21 加入，`push_service` / `generic` / `task_executor`）：
  - **现象（prod）**：手动推送 HDWinG 种子首轮拿到非 `.torrent` 响应（2xx HTML）→ 报 502「响应不是 bencoded」；5 分钟后 `drain_task_queue` 用同一任务重试成功，种子确实进了下载器。用户看到的「失败」与最终结果不一致。
  - **适配器层**：`generic.download_torrent` 首次非 `.torrent`（不以 `b"d"` 开头）→ 短退避（`_NON_TORRENT_RETRY_DELAY`，3s）后**重取一次**；两次都失败才抛 `SiteFetchError`，异常带 `HTTP status / content-type / 长度 / 前 120 字节前缀`（脱敏、折叠换行）。此前只有一句「响应不是 bencoded」，无法区分登录页 / 限频页 / CF 挑战。
  - **永久失败必须与瞬时失败分开**（2026-11-21 Kody 审查）：新增 `SitePermanentError`（继承 `SiteFetchError`，`app/adapters/sites/base.py`）——**凭据失效 / 详情页里没有下载地址**属于「重试 5 次也不会成功」，若混进 `download` 类就等于骗用户「已入队自动重试，无需重复点击」，真故障（凭据过期）被掩盖。判据（`generic.py`）：详情页跳登录 / 下载响应是登录页（`_looks_like_login_response`：URL 含 `login.php`、标题是登录页、正文前段有密码输入框或 `login.php`）→ 永久；详情页正常但**没有下载地址** → 永久；网络抖动 / 临时错误页 → 瞬时。**「请登录后下载」这类中文文案不算判据**（正常错误页的页脚也有「登录」链接，误判会把瞬时故障错报成永久）。
  - **服务层**：`PushItemError(category="download")` 不再抛 502，返回 `PushOutcome(deferred=True)`；任务留在队列按指数退避重试（最多 `MAX_ATTEMPTS=5` 次）。永久失败由执行层判为 **`fatal`**（死信 + 502，不退避重试）。**重试预算用尽**（`mark_failed` 返回 `failed`）时**不再报 202**（队列里已是死信，报「已入队自动重试」是说谎）→ 走 **502**，文案提示「再次点击『推送』可重试」。
  - **死信任务可重置**（手动重推入口）：`task_queue.enqueue` 对已存在的 `idem_key` 一律返回 `created=False` → 死信会让用户「再点一次也推不上去」（只拿到 200「已在推送队列中（状态：failed）」）。故 `TaskQueueService.requeue_failed()` 把死信**重置**为 pending（`attempts` 归零 + 清错误），`enqueue_index_entry` 在幂等命中且状态为 `failed` 时调用它 → 状态回到 `enqueued`。**只重置终态 failed**（pending / running / done 不动）。自动推送（`push_free`）**不走**这条路径（死信不自动复活，避免无限重试烧额度）。**重置时必须同步清掉索引行的 `push_status`**（`_clear_push_failure`）——否则重推会命中 `execute_add_torrent` 的幂等短路而「假成功」（种子压根没进下载器，死信却被成功掩埋）；清空后索引行状态也与任务一致（重试在途不该还显示「失败」）。
  - **API 层**：`POST /torrent-index/{id}/push` 在 `deferred` 时返回 **202**（`deferred=true`），前端提示「已入队自动重试」。**解析失败（parse）/ 致命错误（fatal）/ 永久失败 / 重试已用尽仍走 502**——这几类重试也不会成功。
  - **BREAKING CHANGE（对外 HTTP 契约）**：`POST /torrent-index/{id}/push` 的**可重试下载失败**由 **502** 改为 **202 + `deferred=true`**。破坏点：按「5xx = 推送失败」分支处理的调用方，会把「已入队、几分钟内会推成功」误判成失败（正是 2026-11-21 prod 事故的根因）。迁移步骤：① 判定「是否失败」改看响应体 `deferred` 字段（`true` = **已受理，不是失败**）；② 要最终结果就轮询任务队列 / 看拉取索引行的 `push_status`（`pushed` = 已推成功）；③ **502 仍保留**给解析失败 / 致命错误 / 永久失败 / 重试已用尽——按 5xx 分支处理「真失败」的调用方无需改动。受影响消费者：面板「拉取索引」页推送按钮（`web/src/pages/TorrentIndex.vue`，已适配 `deferred`）、批量推送端点（只入队、不受影响）、外部调用该端点的脚本（按 5xx 判失败的需改）。
  - **Versioning & Deprecation**：按语义化版本，本次属 **major（v2.0.0）**——「可重试下载失败」的**状态码语义**变了（502 → 202），外部脚本的「5xx = 失败」判定会**静默失效**（脚本不报错，只是把「已入队」当成失败），不能按 minor 让运维以为可无痛升级。弃用时间线：旧 502 语义自 v2.0.0 起**不再返回**，无兼容期（面板自托管、前后端同版本发布，面板前端已在本 PR 内适配）；**外部脚本须在升级镜像前改完**。镜像 tag：`v2.0.0` → `2.0.0` / `2.0`（`DEPLOY.md`）。迁移说明与变更记录见 [`CHANGELOG.md`](CHANGELOG.md) 的「Unreleased」小节。
  - 契约由 `tests/unit/test_generic_adapter.py` + `tests/integration/test_push_service.py` + `tests/integration/test_torrent_index_push.py` 固化
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
  - `JobStep.children: list[JobStep] | None` = **子步骤**（2026-11-21 加）：编排型作业（`cross_seed_maintenance`）把子作业自带的步骤挂上来——否则合并后页面上只看得到三个大段，看不到「写入快照 / 检测异常 / 自动暂停」。前端 `JobStepList` **总是展开** children（≤5 条，不撑面板），`items` 仍默认折叠。默认 None → 既有调用零改动；`asdict` 递归序列化，落库不用额外处理（`test_子步骤能落库` 固化）
  - **全部字段带默认值 → 向后兼容**：现有 60+ 处 `JobStep(...)` 调用零改动
  - **必须限流**：`build_items()` 上限 50 条/步（`monitor` 可能命中几百个种子），超限丢**最早**的（明细按执行顺序产生）
  - 前端**默认折叠**（点击「明细（N）」展开）——默认展开会把详情面板撑爆
  - 各作业全填：`fetch_all_sites`（逐站拉取 + 逐站用户数据，两段各自挂 children）/ `push_and_monitor_free`（逐种 + 逐档站名 + 逐 hash 动作）/ `drain_task_queue`（逐 task）/ `cross_seed_maintenance`（逐下载器）
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
  - 表头不截断 / 单元格不换行 / 行高 ≤ 55px（原 e2e 契约已随 e2e 移除，靠人工核对）
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
- **「定时任务」页按业务域分「辅种」「媒体库」两栏**（2026-11-22 加入）：
  - 菜单拆成同级的 **`/scheduler`（定时任务）** 与 **`/settings`（设置 · 环境变量）**，同属新的「系统」一级菜单——此前两者是 `/settings` 页里的两个 tab，找调度得先进「设置」，与它作为日常高频页面的地位不符
  - **域真源是后端**：`JobSpec.module`（`app/services/job_registry.py:JobMeta.module` **必填**）+ `JOB_MODULES`（`app/services/scheduler.py`，key →「辅种」/「媒体库」，**与侧栏一级菜单同名**）。`GET /scheduler` 增下发 `jobs[].module` / `jobs[].module_title` 与顶层 `modules[{key,title,job_count}]`
  - **前端不硬编码 job → 模块映射，也不自己排模块顺序**——两处顺序约定必然漂移（同 title/description 的教训）；`list_modules()` 按注册顺序返回，页面照抄
  - 新增作业忘了声明 module → 落进「其它」块（仍可展示，但分组意义丢了）→ `test_job_meta_covers_all_jobs` 用「必须是 `JOB_MODULES` 的键」把它挡在测试里
  - 归属口径：**看作业读写的表/协议属于哪个域**——`drain_task_queue`（辅种推送队列）属辅种域；`cleanup_link_cache`（302 中转缓存）/ `purge_dav_auth_cache` 属媒体库域
  - 契约由 `tests/unit/test_scheduler.py` + `tests/integration/test_api.py` + `tests/integration/test_spa.py`（`/scheduler` 深链回退）固化
- **作业触发 = 数据流（`depends_on`）+ cron 兜底**（2026-11-22 加入，`docs/scheduler-dataflow.md`）：
  - ⚠️ **当前没有作业声明 `depends_on`**：原唯一用户 `push_free_torrents` 当天就与 `monitor_free_window` 合并为 **`push_and_monitor_free`**（见下一条）。合并后 cron 心跳（10 分钟）比推送门槛（30 分钟）更细，挂边是「永不生效的死边」，故不挂。机制与测试**保留**，供将来「真依赖 + 更粗 cron」的作业使用
  - ⚠️ **cron 必须显著宽于门槛，否则边是空操作**：若 cron 仍是 `*/30` 而门槛也是 30 分钟，上游成功的时刻永远差一点够不到门槛，而 30 分钟正是 cron 自己醒的时刻——收益为 0。这条坑写进了 `JobSpec.depends_on` 的 docstring
  - **语义是「触发时机」不是「数据正确性前提」**：上游失败时下游照旧按自己的 cron 跑（不级联失败）；只有**成功**才触发下游
  - **三道防重复闸**：`_running` 集合（check→add 之间无 await，单事件循环原子；cron/upstream/手动共享）、门槛（基准是**上次开始**时间，用结束时间会让周期漂移成「门槛+耗时」）、`task_queue` 三层幂等（`idem_key` / 认领 / `push_status`）
  - **频率不变密**：fetch 每小时 6 次成功，门槛只放行 2 次 → 推送仍 ≈2 次/小时，只是相位对齐拉取完成（「入库 → 推送」延迟从 ≤30 分钟降到 ≤10 分钟）
  - **刻意不挂边的**：`drain_task_queue`（退避到期是时间的函数）、`cross_seed_maintenance`（旁支，内部已是三段流水线；串进链会重演 2026-11-01「最重节点绑架全链」）
  - **图校验在 `register_jobs` 末尾**（`scheduler_center.validate_graph()`）：逐条注册时校验会因注册顺序误报「上游未注册」；拼错/成环在启动即炸
  - **任务认领已改为条件更新**（2026-11-22 加固，`task_queue.claim_one` / `claim_due`）：`UPDATE ... WHERE status='pending'` + `RETURNING`，不再「先读状态再改」——否则 push 的就地认领与 drain 的批量认领可能双双执行同一 task。依赖 SQLite ≥3.35 的 `RETURNING`
  - 契约由 `tests/unit/test_scheduler.py`（成功才触发 / 失败不触发 / 门槛 / 停用或运行中不触发 / 环检测 / 下下发字段）+ `tests/unit/test_task_queue.py`（两 session 认领只有一个成功）+ `tests/integration/test_api.py` 固化
- **站点用户数据并进「站点拉取」作业**（2026-11-22 加入）：
  - `refresh_user_data` **独立作业已删除**；它的职责作为 `fetch_all_sites` 的**第二段**执行——两者遍历同一批启用站点，共用一个 `*/10` 心跳
  - 顺序：**先拉种子**（`torrent_index`，推送的候选来源）**再取信息**（`site_user_data`，选种排序的输入）。两段**互不依赖**，顺序只为日志可读
  - **两段各自容错**（`job_compose.run_stage`）：一段抛错不影响另一段，失败进 `steps`；子服务自带的 steps 挂成 **children**（前端展开可见逐站明细）
  - **返回结构向后兼容**：`total_written` / `scanned_sites` 等仍在**顶层**，用户数据结果另挂 `user_data` 键——`tests/integration/test_jobs.py` 与服务级调用不需要改
  - **新鲜度/代价**：用户数据从 30 分钟提升到 **10 分钟**；代价是每站每轮多 1 次 `/mybonus.php`（9 站 ≈ 54 次/小时，原 18 次/小时）。要压回旧量级就在第二段加时间门槛（`job_compose` 已支持段级跳过）
  - **面板打开时的自动刷新仍保留**（`POST /sites/test-all` + `/user-data/refresh`，5 分钟冷却、成功静默）——它现在是**即时补充**，不再是唯一刷新时机
  - 公共编排助手在 `app/services/job_compose.py`（`run_stage` / `build_summary` / `to_payload` / `extract_count`），`push_and_monitor_free` 与 `fetch_all_sites` 共用——**`to_payload` 必须保留 JobStep 对象**（`asdict` 会把子步骤变 dict，children 就挂了）
  - 契约由 `tests/unit/test_fetch_job.py`（两段都跑 / 各自容错 / 顶层键兼容）+ `tests/unit/test_push_and_monitor.py` + `tests/unit/test_scheduler.py`（作业清单 9 个、无 `refresh_user_data`）固化
- **免费种推送 + 窗口监测已合并为一个作业**（2026-11-22，`app/services/push_and_monitor.py`）：
  - 作业名 **`push_and_monitor_free`**，cron **10 分钟心跳**（`5,15,…,55`）：监测每轮都跑（临期暂停是安全动作，不能被节流）；推送由**作业内部节流**到 ≥30 分钟一次（`PUSH_MIN_INTERVAL_SEC`，站点有限频）
  - **顺序「先监测后推送」**：监测先暂停临期种子腾出下载器位置，再推新的
  - **空转不刷新节流时间戳**（`selected == 0` / 未配下载器）：否则用户刚配好下载器也要白等 30 分钟；节流状态是**进程内**的，重启后首轮会立刻推一次（刻意，链路幂等）
  - **两段各自容错**：一段抛错不影响另一段，失败进 `steps`；子服务的 `steps` 挂成 `children`——**必须保留 JobStep 对象**（`_to_payload`），直接 `asdict` 会把子步骤变成 dict，`isinstance` 失败 → 前端看不到逐站/逐种明细
  - **配置迁移**（`job_settings.merge_job_settings`，启动期在 `apply_overrides` 之前）：两条旧行合并为一条，**启停取 AND**（任一边被停用 → 新作业也停用，不静默复活用户关掉的行为），**cron 不搬**（旧节奏对新作业是错的）
  - **迁移是不可逆 DELETE → 三道保护**：① 删除前把旧行内容（`job_name`/`enabled`/`cron`）**持久化到快照表 `scheduler_job_settings_backup`**（与 `DELETE` **同一事务** → 写不进去就整体回滚、旧行不删；**三条删除路径**——合并 / 孤儿清理 / 改名迁移——都留）+ 打一条启动日志（快照表才是恢复依据：日志会轮转、随重建而丢；还原 SQL 见 `CHANGELOG.md` 的「回滚 / 恢复」）② 失败登记 `startup_status`（`job_settings_merge` / `job_settings_prune` / `job_settings_migrate`）→ `/readyz` 的 `degradations` 看得见 ③ 合并失败时**旧行不删**（`prune_orphaned_settings(keep=…)` 保护，留给下次启动重试）——删掉等于用户的启停状态永久丢了
  - **BREAKING**：`push_free_torrents` / `monitor_free_window` 两个 job 名不再存在——**按端点区分**：`toggle` / `cron` / `config` 返 **404**，`run` 返 **200 + `ok=false`**（手动触发走 `run_now`，对未知作业名不抛 HTTP 异常——**脚本必须按 `ok` 判成败**，只看状态码会把「作业不存在」当成功）
  - 契约由 `tests/unit/test_push_and_monitor.py`（编排/节流/容错/children）+ `tests/integration/test_job_settings.py`（合并迁移 AND 语义）+ `tests/unit/test_scheduler.py` + `tests/integration/test_api.py` 固化
- **环境变量配置全部落库**（2026-10-26 加入，`app/services/env_settings.py` + `app/db/models.py:AppEnvSetting`）：
  - 与「调度配置全部落库」同构，但服务**字符串型**配置（与 `app_runtime_params` 的整型运行参数互不混用——避免 `value_str` 那种「为未来扩展」但实际从没用过的列承担新职责破坏现有契约）
  - 当前注册 `cross_seed_scan_dirs`（辅种扫描目录，逗号分隔多目录）；未来其它 env→DB 的字符串设置可同样在此注册
  - **env 改不再生效**——env 来源（`AppSettings.cross_seed_scan_dirs`）只作**首次启动的默认值**（DB 空时回填）；之后改 env 残留旧行被 `extra="ignore"` 静默忽略
  - 业务读取点用 `get_env_setting("cross_seed_scan_dirs")`（**不再**读 `get_settings().cross_seed_scan_dirs`）——`app/services/cross_seed_orphan.py:219` + `app/api/routes/cross_seed.py` 删除端点
  - 启动期 `init_env_settings_async()` 必须在调度作业跑之前调用（孤儿扫描作业会读它）；**不放在 `scheduler_enabled` 分支里**——API 端点在调度器关闭时仍要可用（与 `init_runtime_params_async` 同一处理）
  - API 写入路径：DB 先 → 内存后（commit 失败时内存未变，作业不会拿到一个重启后就消失的值）
  - API 路由 `app/api/routes/env_settings.py`：`GET/PUT /api/v1/env-settings`
  - 前端「设置」页（`web/src/pages/settings/EnvTab.vue`，挂在 `/settings`）——直接表单（不弹框），字段少 + 改后立即生效，弹框徒增一次点击
  - 契约由 `tests/unit/test_env_settings.py` + `tests/integration/test_api.py` + `tests/integration/test_db_models.py` 固化
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
    - **为什么合并**：`disk_rotation` 读 `cross_seed_snapshot` 判磁盘使用率，而快照由巡查产出——分开跑可能读到**旧快照**。合并后依赖顺序天然正确
      - ⚠️ 提交 `2a1f073` 当时还写了「省一次辅种站全量抓取」，**这条不成立**（② 本来就只读 DB 快照、③ 拉的是另一份 `files`）——2026-11-21 复核，详见下方「辅种维护作业的合并边界」
    - 三段**各自容错**：某段失败不影响其余，失败进 `steps`（前端步骤卡片逐段展示）
  - `cross_seed_snapshot` 表是**当前快照**（`downloader_id` UNIQUE），不做历史；FK **不带** `ON DELETE CASCADE`（当守卫：漏删会 IntegrityError，宁可失败也不留孤儿行）——删下载器 / 取消辅种标记时必须显式删快照
  - **前端读快照，不打下载器接口**：辅种页的「在途任务」读快照；实时端点 `GET /cross-seed-monitor/downloaders/{id}/scan` 保留作调试用
  - **辅种页是两层 Tab**（2026-10-26）：外层 = **下载器**（数据维度），内层 = **内容分区**（概览 / 分组 / 异常 / 孤儿，功能维度）
    - 为什么是两层而不是二级菜单：用户的真实心智是「我要看 <tr-下载器> 这个下载器怎么样」——那是数据维度，不该被功能维度打散。若按功能拆成独立页面，每个页面都得重放一遍下载器 Tab
    - 为什么内层要拆：原本 4 块内容纵向堆叠，实测 **3727 px / 4.1 屏**，其中 2 屏是 2237 行的分组表。拆分后概览 **921 px（1 屏）**，分组 3069 px / 异常 1950 px / 孤儿 720 px ——各块互不干扰（异常与孤儿各有独立空间，不会互相误点删除按钮）
    - 内层 Tab 在**每个**下载器 pane 里各渲染一份（Ant Design 隐藏 inactive pane）——注意定位时过滤 `:visible`，否则会点到隐藏那份
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
- **磁盘流转按「数据组」删，不按「种子」删**（2026-11-21 修正，`app/services/disk_rotation.py`）：
  - **删除的原子单位 = 同 `(name, size_bytes)` 的一组种子**。辅种的本质是**同一条数据被多个种子共享**（实测 4646 个种子 → 2237 份数据），组内多个 infohash 指向**同一份磁盘文件**——只删其中一个成员，磁盘一个字节都不释放（其余副本仍引用）；`delete_data=True` 时删一个成员会把共用文件一起删掉，余下 N-1 个副本立刻 `missingFiles`，要等下一轮巡查才被暂停（留下一批死种子，还把高价值组打成半残）
  - 旧实现按**成员**拆候选、逐个累加 `size_gb` 当"已释放量" → **幽灵记账**：循环拿着假账往下走，目标永远达不成，越删越多。现在 `_select_groups()` 返回整组（全部 hash），一次 `remove` 下发，`freed_bytes` 按**组大小记一次**
  - **批次绝不切开一个组**（`_pack_batches`）：第一批成功、第二批失败会留下"半个组"。单组超过 `DELETE_BATCH_SIZE` 时独占一批
  - **释放量有上界**：`need_free` 由**整个分区**使用率算出，候选池只有辅种数据。分区被媒体库/备份占满时辅种全删光也补不上 → 取 `min(need_free, max_releasable)`，达不到就**停手**，结果标 `unreachable` 并在 summary 写「缺 X GB 需人工介入」，不把辅种删空
  - **快照新鲜度守卫**（两道）：① `cross_seed_maintenance` 把 ① 巡查失败的 `downloader_id` 作为 `skip_downloader_ids` 传给 ②（① 失败时**不写**快照，"同轮顺序"≠"数据有效"）；② ② 自查 `scanned_at` 年龄，超过 `SNAPSHOT_MAX_AGE_HOURS`（6h = 2×作业周期）跳过并点名。`scanned_at` 解析不出时按**过期**处理（fail-safe）
  - **预演模式** `disk_rotation_dry_run`（运行参数，默认 0）：只算「会删哪几组、能释放多少」并写日志，不发删除指令。首次调阈值 / 改动作前先跑一轮
  - **`stats` 增加 `size_bytes`**（精确字节；`size_gb` 只有两位小数，删除预算按它算会累积舍入误差）。旧快照行没有该 key → 回退 `size_gb`（优雅降级，与 `added_at` 同款）
  - **校验中 / 下载中的组整组跳过**（2026-11-21 第二段）：快照 `stats[].blocked_hashes` 记下组内 `CHECKING` / `DOWNLOADING` / `STALLED_DL` 的 hash（`_DELETE_BLOCKED_STATES`），② 见到非空就**整组本轮跳过**并计入 `blocked_groups`。理由与 monitor 的 CHECKING 保护同口径：校验期完成度判定不可靠，删数据还可能被下载器拒绝；下载中的删了等于丢半个任务
    - **刻意不含 `SEEDING` / `UPLOADING`**：做种是辅种的正常稳态，算成"不可删"会让流转永远没有候选
    - **不含 `PAUSED_*`**：已暂停的副本不参与传输，删它是安全的
    - 全部候选都被冻结时，结果里的 `error` 写明「N 个候选组全部在校验/下载」，而不是含糊的「无候选」
  - 契约由 `tests/integration/test_disk_rotation.py`（整组删 / 释放上界 / 预演 / 快照过期 / 失败名单）+ `tests/unit/test_cross_seed_maintenance.py`（失败名单下传）固化
- **辅种维护作业的合并边界**（2026-11-21 复核）：
  - **只有 ①② 之间有真实依赖**（② 的候选池只来自 ① 的快照），③ 孤儿扫描的输入是自己的（下载器 `files` + 挂载盘扫盘），与 ① 无关
  - 合并的代价是**频率被最重的段绑架**：① 从 `2,32 * * * *`（48 次/日）降到 8 次/日 → 快照新鲜度 30 分钟 → 3 小时（异常种子最长占 3 小时才被暂停，辅种页与仪表盘一起变旧）；③ 从 `7 4 * * *`（1 次/日）升到 8 次/日 → 每天多 7 次 TR 全量 `files` 拉取（27.5 MB）+ 7 次 CIFS 全盘 walk（39–60 s）
  - 提交 `2a1f073` 里「省一次全量抓取」这条理由**不成立**：② 从 `e2ff1dc` 起就只读 DB 快照（不打下载器），③ 拉的是另一份数据 → 合并省下的网络调用是 0，反而同轮对同一台 TR 打了两次接口
  - 要拆回独立频率时：① 独立 30 分钟、② 与 ① 同作业但**内部节流**到 2–3 小时、③ 独立日频——直接改 `job_registry` 即可，`cross_seed_maintenance` 删掉第三段
  - **失败口径**：① 用 `failures` / `failed_downloaders`、③ 用 `errors`（「下载器名: 原因」）——`_run_stage` 三种都认（只认前两个时，扫盘全失败会显示 `ok`，故障被伪装成正常，2026-11-21 修）
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
  - 契约由 `tests/integration/test_services.py` + `tests/integration/test_api.py` 固化
- **统计卡片的分组删除**（2026-10-24 加入）：
  - 统计表一行 = 一个 (name, size) 分组，可能代表**多个**种子（`count=13` 就是 13 个）——删该行 = 删该组**全部**种子
  - **数据前提**：快照的每组必须存**完整 40 位** hash 列表（`StatsRow.hashes`）。`sample_hash` 是 **12 位截断**的，只作展示用——**不能**用于删除（适配器会拒绝非 40 位 hash）
  - 旧快照行没有 `hashes` → 前端删除按钮**置灰** + tooltip「需重新抓取」（不静默失败）
  - 快照体积：辅种站 2257 组 / 4600+ 种子 → stats 从 356KB 涨到约 590KB（SQLite 无压力）
  - 弹框必须写清「将删除 N 个种子（同名同大小）」+ 警告「这些是**正常做种**的种子」+ 提示「仅删任务会留下不再做种的孤儿数据」
  - 契约由 `tests/integration/test_cross_seed_job.py` + `test_api.py` 固化
- **辅种孤儿文件统计**（2026-10-24 加入，`app/services/orphan_scanner.py`）：
  - 孤儿 = **磁盘有、任务没了**（典型来源：用「仅删任务」清理种子时磁盘文件留下，不再做种却一直占空间）
  - **必须挂载磁盘**：下载器**没有**浏览文件系统的接口（TR 的 RPC spec 不提供 filesystem 浏览，qB 也没有列目录 API）→ 只能把辅种站的下载目录**挂载**进容器，扫盘 + 顶层名匹配（权限 **rw**——删除孤儿文件需要写权限）
  - <生产机> 上 Unraid **已自动挂好** CIFS（`/mnt/remotes/<nasIP>_Public`）→ 只需一行 compose 挂载，无需 NAS 配置
  - **挂载路径必须与下载器的 `downloadDir` 完全一致**（辅种站 = `<辅种下载目录>/辅种`）——孤儿判定把任务按 downloadDir 分组，路径不一致就分不对组
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
    - 端点 `POST /cross-seed-monitor/orphans/scan`（**手动扫描**，不等 cron）——孤儿扫描是 `cross_seed_maintenance` 的第三段（`17 */3 * * *`，每 3 小时一轮；2026-11-01 合并后不再是独立的日频作业），当天发现孤儿想现在清理等不到下一轮。**同步 await**（40–60 s），响应带上有孤儿表的新鲜统计 → 前端直接用，省一次请求
    - 前端入口在「孤儿」分区的区块标题行：「立即扫描」按钮 + 扫描时间标记
    - 端点 `POST /cross-seed-monitor/orphans/delete`（body：`paths`）——删的是**磁盘文件**，不是下载器任务（孤儿本来就没任务）
    - **三道安全闸**（不只靠前端弹框）：
      1. **目录范围**：路径必须是配置的扫描目录的**直接子项**——不能是任意路径、不能是子目录内部的某个文件（防路径穿越）
      2. **二次扫描复核**：删除前**重新**拉所有下载器的任务，确认条目**此刻**仍无主。快照可能是几小时前的 → 期间被加回来的任务会被**跳过**（不删）
      3. **审计日志**：逐条记路径 + 大小（删磁盘数据不可逆 → 必须能回答「删了什么」）
    - 逐条删除，**单条失败不中断**其余条目；复核不通过的记 `skipped`、删除失败的记 `failed`
    - 挂载权限必须是 **rw**（`ro` 删不了）——compose 里已改
    - **删除后自动重扫**（2026-10-26）：孤儿统计表是定时作业的快照（每 3 小时一轮），不重扫页面会一直显示旧数字（已删的还在列表里、且看不到新产生的孤儿）。故删除后**立即重扫**并把最新统计带在响应里（`orphans` 字段）→ 前端直接用，省一次请求
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
  - 契约由 `tests/integration/test_driver_probe_cache.py`（17 例）固化

- **媒体库目录用目录浏览器选，不手填 id**（2026-11-11 加入，`app/services/driver_service.py:browse_driver_dirs`）：
  - **起因**：光鸭的 fileId 是 19 位数字，让用户照着网盘网页手抄——抄错一位扫描就扫不到东西，且无从核对。生产上就出现过「光鸭盘扫描按钮点不动」——**按钮禁用条件是 `!media_cid || !output_dir`，与驱动类型无关**，用户只看到一个灰按钮，不知道缺什么
  - **禁用原因必须写出来**：扫描按钮禁用时给 tooltip，**点名缺哪一项**（`scanDisabledTip`）。tooltip 内层包 `<span>`——antd 的 disabled 按钮**不派发鼠标事件**，tooltip 直接包按钮完全不触发
  - **后端新增 `GET /api/drivers/{id}/browse-dirs?parent_id=`**：列出某目录下的**子目录**（逐层懒加载），响应 `{"driver_id","parent_id","dirs":[{id,name}]}`，形状与 `GET /api/upload-tasks/targets` 一致
    - **不能复用 `/upload-tasks/targets`**——它有 `is_uploadable` 校验，而 `UPLOADABLE_DRIVER_TYPES` 只含**支持写入**的类型，不支持上传的会被 400 挡掉。浏览是 CloudDriver 的**读能力**，不是上传能力
    - `list_dir_by_cid` **不在 `CloudDriver` 协议里**（协议只要求 `list_dir`），故用 `getattr` 取——与 `migration.py` 同一做法
  - **浏览起点只能是账号根**（此时 `media_cid` 还没配，没有更深的入口）：根 id 收敛在 `app/domain/driver_types.py:ROOT_DIR_IDS`（domain 层单一真源；光鸭的根 fileId 就是空串）
  - **`media_cid` 的叫法也是单一真源**：`app/domain/driver_types.py:MEDIA_CID_LABELS`（光鸭叫 fileId）+ `DriverOut.media_cid_label` 随接口下发——**前端不写 `if type === "guangya"`**，后端报错文案也用它
  - **前端复用现成的 `DirBrowser`**（`web/src/components/DirBrowser.vue`，面包屑 + 逐层懒加载，状态由父组件持有）——与「数据同步 / 迁移 / 离线下载」三处同一交互模式，**项目不用 `a-tree`**
  - **语义：进入子目录即选中它**（正在看哪个目录 = 要用哪个目录），面包屑回退即改选；回到根 = 取消选择
  - 契约由 `tests/unit/test_driver_types.py` + `tests/integration/test_driver_browse.py`（11 例）固化

- **媒体后缀默认清单收敛到 domain 层单一真源**（2026-11-11 加入，`app/domain/media_exts.py`）：
  - **起因**：「媒体图片后缀 / 媒体数据后缀」此前默认**空串**（placeholder 是唯一提示）——用户不知道该填什么，附件（海报 / nfo / 字幕）静默不同步。用户要求补默认值
  - **三份清单曾散落三处**：`models.py` 的 `DEFAULT_VIDEO_EXTS`（本地定义）、`naming.py` 的 `DEFAULT_IMAGE_EXTENSIONS` / `DEFAULT_DATA_EXTENSIONS`（frozenset 形态、**零引用的摆设**）、前端 placeholder 手写字符串——互不一致，改一处漏两处。现全部收敛到 `app/domain/media_exts.py`（`DEFAULT_VIDEO_EXTS` / `DEFAULT_IMAGE_EXTS` / `DEFAULT_DATA_EXTS`），naming.py 的死常量已删
  - **默认值生效的两条路径**：① **新建存储源** → ORM 列默认值（`models.py` 引 domain）；② **存量行** → 前端打开配置抽屉时空值预填，保存后落库
  - ⚠️ **不在 `DriverOut` 层兜底**（`or 默认`）——清空保存的行（`image_exts=""` = 关闭附件同步，合法配置）会被改写回默认，用户清空后重开抽屉会以为没存上。预填只发生在**打开抽屉**这一刻，保存后以 DB 为准
  - ⚠️ **bootstrap 的老库加列默认仍用 `''`**（`bootstrap.py:("image_exts","TEXT","''")`）——改它等于对全部存量库静默变更数据；存量行由用户下一次保存配置时自然获得默认值（必然重配 `media_cid`，一定会走一次保存）
  - **「当前忽略」的注释是过时的**：图片 / 数据后缀**实际生效**——扫描时**附件同步**（`attachment_*`，下载到本地与 strm 同目录），`models.py` / `types.ts` 注释已修正
  - 契约由 `tests/unit/test_media_exts.py`（格式规范 + `parse_ext_list` 往返 + 单一真源断言）+ `test_db_models.py` 列默认值固化；抽屉打开预填 + 随保存落库靠人工核对

- **strm 文件名去掉视频扩展名**（2026-11-14 加入，`app/services/naming.py:strm_filename`）：
  - `x.mkv` → `x.strm`——**目录与标题沿用网盘原样**，只多这一个扩展名
  - **理由**：媒体服务器按「同目录同 stem」匹配 nfo 与媒体文件——`x.mkv.strm` 的 stem 是 `x.mkv`，网盘侧的 nfo 多数叫 `x.nfo`（stem `x`）→ 对不上，Emby / Jellyfin 认不到 nfo，元数据刮不出来（用户实测「媒体服务器识别有问题」）
  - **只剥离属于该存储源 `video_exts` 的后缀**（用户配什么剥什么），不硬编码后缀表；后缀 >5 字符按 `split_name` 的既有语义**不算扩展名**（`movie.2020.1080p` 不被剥成 `movie.2020`）
  - **改名必留旧文件清理**：`_upsert_media` 用 `stale_paths` 收集旧路径，扫描 / 增量**写完新文件再删旧**（写锁之外，`asyncio.to_thread`）——不删就变成无主文件（Emby 重复条目 + `health` 报 orphan）。存量库靠**重扫一次**完成改名
  - **同名不同扩展名冲突**（去后缀后新增的风险）：同目录 `x.mp4` 与 `x.mkv` 会映射到同一个 `x.strm` → 两份是**不同压制版本**（内容不同、体积常差几倍），不是冗余拷贝。规则是「**保留大的**」（对齐数据同步的 `conflict_policy=1`）——小的那个**不落库**（DB 里只留一条），大的落库并重写 strm。**确定性规则**，不靠遍历顺序碰运气（生产实测 30 集全是《伟大的长征》mkv+mp4 两版）
    - ⚠️ **败者的存量行与遗留 strm 必须一并清掉**（2026-11-14 审查修复）：去重只在**新命名**路径上查得到冲突方，败者在存量库里可能还挂在**旧命名**路径（`x.mkv.strm`）——不清理则磁盘留孤儿 strm（Emby 重复条目），且下一轮仍命中同样冲突 → **每轮都去重、永远不收敛**。`_drop_loser_entry` 按 `remote_id` 复查并删行 + 收集旧路径（**写锁之外**才 unlink）
    - ⚠️ **只有「本轮网盘上还活着」的冲突方才参与「保留大的」竞争**（判据 `seen_remote_ids`，与 prune 判「源端已消失」同一信号）：拿**源端已消失**的陈旧行比大小，会把**活着的那份**误淘汰。冲突方若稍后才被遍历到，竞赛推迟到那时——**最终恒留最大的那份，与遍历顺序无关**
    - ⚠️ **冲突方可能有多条**（存量库里两份共用同一 strm 路径）：`find_strm_conflict` 返回**列表**（用 `scalars().all()`）——用 `scalar_one_or_none()` 会抛 `MultipleResultsFound` **把整个扫描任务打挂**
  - ⚠️ **prune 必须按「strm 路径引用计数」删文件**（`media.py:_prune_orphans`）：共用同一路径的两条记录，先删的那行若顺手把文件删了，胜者的 strm 就会凭空消失（Emby 里整集不见）。只有**最后一个引用者**被清理时才删文件
  - 契约由 `tests/unit/test_strm_naming.py` + `tests/integration/test_scan.py`（保留大的 / 幂等 / prune 不误删共用路径 / 败者遗留清理 / 源端已消失不参与竞争 / 多行共用不崩）+ `tests/unit/test_incremental.py` 固化

- **扫描统计的各计数**互不混用（2026-11-14 修正，`app/services/media.py:ScanStats`）：
  - `skipped_unknown` = **后缀不认识**的文件（不在 video/image/data 三个配置里）→ 被忽略，不生成 strm 也不下附件
  - `skipped_small` = 体积低于 `min_size_mb` 阈值
  - `deduped` = 同名不同扩展名被合并的条数（**保留大的**）——**不是错误**，不进 `failed`
  - `pruned` = prune 清理的「网盘上已消失」的旧条目数
  - ⚠️ **prune 的清理数以前被写成 `stats.skipped_unknown += _prune_orphans(...)`** → 把「源端消失」混进「未知后缀」，生产实测虚高 52 条（179 → 231），用户会以为有文件没被识别。**两类计数必须分开**
  - ⚠️ **去重明细走独立通道 `dedup_details`，不塞进 `errors`**（2026-11-14 审查修复）：去重是**正常业务结果**，而 `errors` 上限 20 且**只放真正的失败**——混进去会把真正的失败挤掉。明细经 `ScanStats.to_dict()` → `ScanJob` → API 透出，前端「展开去重明细」默认折叠展示
  - 前端「扫描进度」弹框分三行展示：跳过（未知/太小）/ 去重 / 清理；去重明细**默认折叠**（展开会撑高详情面板）
  - 契约由 `tests/integration/test_scan.py::TestPrune::test_prune_count_not_polluting_skipped_unknown` 固化

- **prune 是存储源级配置**（2026-11-14 加入，`drivers.auto_prune`）：
  - **起因**：prune 原本只能靠手敲 `?prune=true` 触发——**面板上的「扫描」按钮从不带这个参数** → DB 与本地 strm 里一直攒着网盘已删文件的遗留数据（生产实测 52 条）
  - 现在是 `drivers.auto_prune`（**默认关**）：开启后扫描自动清理「网盘上已消失」的条目与本地 strm
  - **默认关的理由**：清理会**永久删除**本地 strm（`rm`，**不进回收站**），不可逆
  - **三态语义**：`POST /api/drivers/{id}/scan` 的 `prune` 参数是 `bool | None`——**不传** = 用存储源配置；`true` / `false` = 显式覆盖（配置为关时仍可单次强制清理）。区分「没传」与「传 false」是关键（`prune: bool = False` 会让配置永远失效）
  - ⚠️ **prune 只清视频条目**（`media_files` 表）——**附件不进 DB**，网盘删了附件，本地的 jpg/nfo 会成孤儿残留（目前无清理机制）
  - ⚠️ **`min_size_mb` 过滤掉的文件会被连带清掉**——被「跳过」的文件不进 `seen_remote_ids`，下次 prune 就当成「源端消失」。改小阈值后再跑 prune，之前入库的小文件会被清掉
  - 契约由 `tests/integration/test_driver_auto_prune.py` 固化（默认值 / 配置往返 / 三态解析 / API 透传）

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
  - **附件失败的三种模式与对策**（同日实测）：① 光鸭直链是**一次性**的——同一个 URL 第二次访问必然失败（故每次重试都换新链 ✅）② **网络抖动**：<生产机> 容器 DNS 偶发 `Temporary failure in name resolution`、CDN `ConnectTimeout` → 退避从 1s 提到 **5s**（5/10/15s，跨过抖动窗口）+ 重试次数 3→**4** ③ **附件超时是敏感参数**：共享 client 的 60s 会让卡死连接占住 worker 60s（长尾毒瘤），但**压到 15s 更糟**——实测失败率从 0.24%（60s）飙到 12~17%（光鸭部分 CDN 节点首次 GET 无缓存，15s 不够）。折中 **45s**：给慢节点留够时间，又短一截
  - **换直链要对齐官方限频**（同日实测）：官方是 5 次/秒（列目录）/ **2 次/秒（换直链）**，但限速器只有一个光鸭桶（0.2s = 5/s）→ 并发 4 时换直链超限 2.5 倍 → 光鸭软限频 → 扫描速率从 45 掉到 21 个/分钟、30 分钟 96 次重试。修法：`get_direct_link` 走独立桶 `guangya_link`（`guangya_link_rate_limit_interval_sec`，默认 0.5s）；播放换直链同样受益（账号级配额本就该共享）
  - **条目缺 `fullParentIds` / 目录映射缺失 >10 → 报错不静默**——静默跳过会把故障伪装成「媒体库为空」，且 prune 会把「源端还在」的文件当孤儿删掉；少量缺失用 fileId 占位（路径可复现，不产出错路径）
  - **目录 `utime` 不可靠，别想做剪枝**（实测）：建子目录会变，**改名/删除都不变**——用它跳过子树必漏文件
  - **翻页防御上限**（`MAX_LIST_PAGES=1000`）：`cloud_task_list` 实测会忽略 `page` 参数无限返回同一页（1.5s 打出 2 万+ 请求）；`fs_files` 一旦同款故障必须可见失败
  - 混合列表（`file_types=[]`）里 nfo/jpg/iso 全在——本地按后缀分类不变，**绕开**光鸭自有类型分类（视频=2/图片=1/文档=4/压缩包=5，与我们的后缀清单对不上）
  - 契约由 `tests/unit/test_guangya_driver.py::TestIterTree` 固化；探针脚本 `scripts/probe_guangya_scan.py` / `probe_guangya_verify.py`（生产容器 `docker exec -i medialab /opt/venv/bin/python -` 跑，探针目录自建自删、绝不碰 `fs_clear_recycle_bin`）

- **光鸭两种直链（直接 / 分享）是同一通道，差在签名与前置条件**（2026-11-19 主盘实测，驱动 id=2，探针 `scripts/probe_guangya_link_final.py`）：
  - **两个入口**：`get_res_download_url` → 响应 `signedURL` + `urlDuration=21600`；`get_share_download_url(fileId, accessToken)` → 响应**只有 `downloadUrl`**，**没有 duration 字段**
  - **落地产物同构**：都是 `https://<节点>-httpdown.guangyacdn.com/download/?<14 个参数>`（节点在 zjk01/zjk02/sh01/qd01 间调度），参数 `at f fid g mid ms p s srcid th threshold tid ts verno` 两边齐全、**无独有参数**
  - **逐参数实测（同一 jpg）**：相同 `f fid g mid ms srcid th threshold tid verno`（`fid` 两边相同——分享链仍是文件自己的 64 字符加密 id，不是另一套 id 空间；`tid` 是账号维度）；**只有 4 个不同** `at`（32 字符令牌）/ `p`（签名串，分享链更长：383→412）/ `s`（16 字符签名）/ `ts`（签发秒，两次差 4 秒）
  - **判等价口径**：比 `fid + s + ts`；`etag` 相同即同一对象。两边都**不绑 UA、不要 Cookie/Bearer**，链接即凭证（Range 实测 206，可直接丢给播放器/下载器）
  - **分享直链的前置条件是 `shareType=0`**：`shareType=1` 的 `shareUrl` 尾段没有 `_key`，公开接口 `get_share_access_token` 直接报「分享链接错误」；`download_type` 只接受 1（传 2/3 建分享即 `code=112`）
  - **但视频拿不到分享直链**：`shareType=0` 对 mp4 稳定返回 `code=207 分享者未开启免登录下载`，同账号/同参数换 jpg 立即成功 → **光鸭视频分享不开放免登录下载（平台策略，不是参数能绕的）**；分享直链只对图片/附件类有效。另有独立坑：分享**逐文件审核**，视频大多根本进不了分享（首批 8 个视频 7 个「分享内容为空」）
  - **删除分享后分享直链仍 HEAD 200 可下**（自包含签名，不随分享生命周期失效）；有效期同样按 `ts` 起算（分享链没有 `urlDuration` 字段，6 小时为推断，未等到实际失效）
  - → **302 播放 / 迁移 / 批量预取只用直接链**（`get_direct_link`）；分享链仅在「附件免登录分发」场景有额外价值，别把它当视频取链备胎
  - ⚠️ 与 2026-11-12「光鸭直链是一次性的」记录冲突：本次同一 URL 连续 HEAD + Range + 删分享后 HEAD 三次均 200。差异可能来自附件下载场景的 CDN 缓存/连接行为——**别据此改重试逻辑**（重试仍换新链，代价小、更稳）
  - 探针：`scripts/probe_guangya_link_final.py`（只读；临时分享 `finally` 删除；`MEDIALAB_PROBE_DRIVER/VIDEO/IMAGE` 可覆盖样本）

- **光鸭客户端 token 覆盖层：非会员大文件直链的唯一入口**（2026-11-19 逆向 + 实测，`GuangyaCredential.client_*`）：
  - **判据是 token 的 `aud`，不是请求头**：服务端按 access_token 的 `aud`（客户端 clientId）认「是不是客户端」。实测客户端 token 配 **Web client_id + Web 路径 + dt=1** 也照样放行；反过来 Web 扫码 token 伪造 `dt=6` / 客户端 UA / 全套 x-* 头，6 种组合全被 `code=254` 拒绝 → **别再用请求头伪装去试**
  - 客户端身份：`macClientId = al9eLEtLE7_pp8GR`（逆向自 `app.asar` 的 `macClientId`）；token 由 `account.guangyapan.com` 签发，`scope=user sso offline profile`，2 小时过期
  - **凭据加密可逆（固定 key + 固定 IV）**：`Credentials.db.credentials2.credentials` 是 AES-128-CBC/PKCS7，密钥来自 `@xbase/electron_auth_kit` 的 `EncryptionKeyUtility`（8 段字符串打散）。**key/iv 属凭据类敏感材料，禁止以字面量写入仓库**——由 `scripts/export_guangya_client_token.py` 在本机从已安装客户端的 `app.asar` 动态提取（或用 `GUANGYA_AES_KEY_HEX` / `GUANGYA_AES_IV_HEX` 环境变量覆盖）。**不绑设备、不绑 Keychain**——本机任何能读该库的进程都能解密
  - **覆盖层字段**（`GuangyaCredential` 新增，向后兼容）：`client_access_token` / `client_expires_at` / `client_device_id`。**只影响 `get_direct_link`**：扫描 / 列目录 / 上传仍走 Web token（Web token 失效才会拖死整个驱动）
  - **`get_direct_link` 优先级**：客户端 token 有效（留 5 分钟余量）→ 走 `POST https://api.guangyapan.com/userres/v1/get_res_download_url` + `dt=6` + `did/av/vc`；非鉴权错误（如 254）**直接抛**（回退也拿不到，只会掩盖原因）；鉴权错误（117）才回退 Web
  - ~~**不自动刷新客户端 token**~~（**已废止**，见下方「✅ 客户端 token 现在能自己续期」）：原结论是 `guangyaclient.refresh_token()` 把 `client_id` 硬编码成 Web 值 → 现在驱动自己用 `CLIENT_ID_MAC` 续期，不依赖本机同步脚本
  - 工具：`scripts/export_guangya_client_token.py`（本机只读解密，输出 JSON）+ `scripts/import_guangya_client_token.py`（容器内写覆盖层，`MEDIALAB_DRIVER_ID` / `GUANGYA_CLIENT_TOKEN_B64`）+ `scripts/sync_guangya_client_token.sh`（本机 cron/launchd 定时导出→ssh 导入）
  - ⚠️ **旧代码会丢字段（2026-11-20 生产事故，已修）**：旧镜像的凭据写回是**整串替换**——Web token 刷新写回（`migration.py` / `redirect.py` 的 `persist_guangya_credential`）会把 `client_*` 一起抹掉（约 2 小时一次）→ **客户端身份丢失** → 非会员副盘的**大文件直链全部 `code=254`**（文案「网页端非会员下载文件大小超过限制，请开通会员或使用客户端下载」）。表现：聚合归拢里**只有大视频**全部 `failed`（附件走分享转存照常成功），而**同一账号几小时前还搬得动几十 GB 大文件**——这个「时好时坏」是判据。
    - 修复 = `driver_service.persist_guangya_credential` + `_merge_guangya_credential`（**合并两层写回**，commit `f4cc32c`），**v1.0.7 起随镜像发布**；**≤ v1.0.6 的镜像仍会抹**——只重新导入客户端 token 而不升级，只能撑到下一次老实例写回
    - 排查顺序：看台账 `secondary_staging` 的 `last_error` 是否同一句 254 → 解密 `drivers.credential` 看 `client_access_token` 是否为空 → 空 = 客户端身份已被抹掉（不是账号掉了会员）
    - 恢复：升级到 ≥ v1.0.7 后 `scripts/export_guangya_client_token.py`（本机）→ `scripts/import_guangya_client_token.py`（容器内，`MEDIALAB_DRIVER_ID`）；**先核对导出 token 的 `sub` == 目标驱动的 `account_id`**（`get_assets` / JWT `sub`），张冠李戴会把凭据写到别的账号上
  - 实测（驱动 5 = 小号3，`vipStatus=3` / 无 `directLinkFeature`）：导入客户端 token 后 1.07 GB 与 **19.6 GB** 文件都 `success`，URL 参数 `threshold=200` / `ms=th=204800`（与客户端任务库一致）；清空客户端 token 后同文件回退 Web → `254`
  - ✅ **客户端 token 现在能自己续期**（2026-11-19 实测，替代「只能靠本机定时同步」）：
    - `GuangyaCredential.client_refresh_token`（与 `client_access_token` / `client_expires_at` / `client_device_id` 同层）+ `GuangyaDriver._ensure_client_token()`
    - 刷新 = `POST {GUANGYA_ACCOUNT_BASE}/v1/auth/token`，`grant_type=refresh_token` + **`client_id` 必须用 `CLIENT_ID_MAC`** + 头 `X-Client-Id: al9eLEtLE7_pp8GR` / `X-Device-Id` / `x-action: 401`
    - 实测：刷新后 `aud` 仍是 `al9eLEtLE7_pp8GR`、`scope=user sso offline profile`、`exp=iat+7200`；**`refresh_token` 不轮换** → 与客户端各持一份互不影响（不会把客户端踢下线）
    - `import_guangya_client_token.py` 已改为**保存 refresh_token**（此前只存 access_token，这就是「2 小时一到只能回客户端解密」的根因）
    - `get_direct_link` 顺序变为：有客户端层 → 先 `_ensure_client_token()`（过期即自续）→ 客户端路径；续期/取链的 `auth` 错误才回退 Web（`254` 等非鉴权错误照旧直接抛）
  - ✅ **同 aud 的 token 可以自己申领，不需要浏览器**（2026-11-19 实测）：
    - `device_code` 的 `verification_uri_complete`（`account.guangyapan.com/__/auth/device/...`）**访问 404**——`account` 域名是纯 API，不托管前端页面（`GET /` 也 404）。别拿它给用户点
    - 真正的授权入口是客户端 SDK 的 `POST /v1/user/device/authorize`（body `{client_id, user_code}`，带任一**有效账号 token** 的 Bearer）→ 返回 `{address, device_name, browser}`
    - 之后照常轮询 `POST /v1/auth/token`（`grant_type=urn:ietf:params:oauth:grant-type:device_code`），拿到的 token `aud=al9eLEtLE7_pp8GR`、`scope=user sso offline profile`、`sub` 与原 token 同账号、带 `refresh_token`
    - `grant_type=client_credentials` 被拒（`4122 unauthorized_client`）→ **token 必须由账号授权产生**，本地无法自签（JWT 是 `RS256`，私钥在服务端）
    - 结论：只要手上有一份**未过期**的账号 token（客户端库里那份即可），就能自助换出可续期的客户端会话；需要人参与的只剩「种子 token 也过期」这一种情况
    - 工具：`scripts/probe_guangya_client_aud_login.py`（`--token-file <export 输出>` 全自动；不带则打印授权链接人工确认）
  - ✅ **扫码登录新增 `mode=client`**（`POST /api/drivers/{id}/qr/start?mode=client`）：用客户端 clientId + `offline` scope 取码，签发出的就是客户端身份 token（`QRStartOut.mode` 回显）
  - 前端入口：登录方式弹框里 **单选组「扫码身份」**（Web / 客户端，仅光鸭显示）+ 单个「扫码登录」按钮——**不是两个按钮**（枚举型选项按项目规范用 `a-radio-group`）。选中的身份经 `onLoginByQr` 传给 `openQr(driver, mode)`；每次打开弹框复位为 `web`（上次的客户端选择不该静默延续）。扫码弹框内「重新获取二维码」走 `refreshQr`，**沿用同一身份**，不会偷偷退回 Web。可用性判定直接看「重新获取二维码」按钮（不再依赖 `configDriver`——登录流程里它是 `null`）
  - ⚠️ **光鸭凭据落库必须「合并两层」**（`driver_auth._store_guangya_credential`）：`store_credential` 是整串替换，直接新建对象会把另一层抹掉。client 模式登录还会把**主 `access_token` 也设成客户端那把**（实测客户端 aud 对扫描 / 列目录 / 上传全部生效），并清空 Web 的 `refresh_token`——否则主 token 过期时会被 Web 刷新（`guangyaclient` 硬编码 Web client_id）刷成 `aud=web`，客户端身份丢失
  - **迁移 / 归拢无需改业务代码**：两处取链都走 `source.get_direct_link`（`migration.py:550` / `aggregate_sync.py:921`）→ 覆盖层在驱动层自动生效；云添加（主盘 `create_offline_task`）对链接**不校验 UA/Referer**（实测 1.07 GB 直链 12 秒落地、大小精确一致、落盘名 `download` 需改名）
  - 实测（迁移单文件，源=驱动 5（非会员客户端 token）→ 目标=驱动 2 VIP）：`min/max=1024/1025 MB` 只匹配一集 → `success`，1.00 GB，落盘改名 `download → 走马上任 - S01E01 - 第 1 集.mkv`，耗 1 次云添加额度
  - ⚠️ **不要靠临时改聚合组 root 来测归拢**：`assign_driver_role` 是单组绑定（`Driver.aggregate_group_id` 单列），把副盘指到临时组等于**把它从生产组移走**；归拢与迁移共用同一条取链路径，验证迁移即可

- **光鸭批量写操作的铁律**（2026-11-13 生产实测，规则）：
  - **id 与 parentId 一律传字符串**：`fs_copy` / `fs_delete` 收到数字 id 返回 `code=112 参数错误`，而第三方客户端 `guangyaclient` 的签名却写着 `Optional[int]`——照签名传 int 必炸。**统一走驱动层的公开方法** `GuangyaDriver.copy_files(file_ids, parent_id)` / `delete_files(file_ids)`（内部强制 `str()`），**不要直接调客户端的方法**
  - **同名不覆盖**：目标已有同名文件时光鸭自动改名成 `xxx(1).xxx` → 要真正覆盖必须**先删掉旧的那份**再复制
  - **`fs_create_dir` 只接受一层名字**（带 `/` → `code=141`），且必须 `fail_if_name_exist=True`（否则重名建出 `xxx(1)` 副本；已存在时返回 `code=159` 但 `data.fileId` 仍是目标目录 id）
  - **`fs_delete` 是异步任务**（返回 taskId）：实测单文件 <0.5 s 生效，但**批量删 20+ 个要重列确认**——曾有 6 个文件到 60 s 后才从列表消失
  - **回收站保留期远短于 `leftTime` 字段**：2026-11-09 删进回收站的 12863 项（约 236 GB），到 11-13 只剩 12 项 → **回收站不是长期保险**，要留退路就另存清单
  - **平台会自删暂存目录 / 重建分享目录**：`来自：云添加` 在 2026-11-09 20:12:14 被整棵删除（连带里面 14 集《长安的荔枝》）；`分享` 被重建成 `来自：分享`（每次 id 都变）→ **分享进来的内容要尽快归置**
  - **同名不同版本的取舍按「保留大的」**（与数据同步 `conflict_policy=1`、迁移功能同口径）：源更大 → 先删目标那份小的再复制；目标更大 → 跳过复制、只删源副本
  - 契约由 `tests/unit/test_guangya_driver.py::TestBatchCopyDelete` 固化（id 转字符串 / 错误码映射 / 单删与批删同路径）

- **批量整理网盘目录的三条硬规则**（2026-11-13 自己踩过，规则）：
  - **「残留」判定只算文件，子目录一律不碰**；空目录只在最后自底向上删，且删前必须重列确认「无文件且无子目录」。曾把「目录里还有条目就重删」做成目录级判定 → **提前删掉 37 个未处理的剧目目录 + 160 多个 Season 目录**（内容进回收站，靠整体还原救回）
  - **写操作（copy/delete/create_dir）后立刻失效目录列表缓存**；轮询判据一律用**新鲜列表**。曾用缓存列表做「等复制落地」「等文件消失」的轮询 → 读到陈旧快照，**每目录空等 60 s、大目录空等 900 s**（40 分钟只跑 5 个目录）
  - **遍历期间绝不对子目录发 `fs_delete`**；删源只等**文件**消失（≤30 s），不等目录变空

- **光鸭分享目录归置流程**（2026-11-13 加入，`scripts/organize_guangya_share.py`）：
  - 把主号（driver 2）根下的 `来自：分享` 按**路径映射**并进 `影视`：剥掉前导的 `来自：分享`/`影视` 段即得目标相对路径（`来自：分享/影视/电影/外语电影/X/poster.jpg` → `影视/电影/外语电影/X/poster.jpg`）
  - 跑法：`ssh <别名> 'docker exec -i medialab /opt/venv/bin/python - --dry-run' < scripts/organize_guangya_share.py`（只出统计）/ `--conflict keep-larger`（实跑，默认）/ `--keep-share-root`（保留空分享根目录，平台会重建）
  - **盘点走全账号批量列表**（`fs_files(parent_id="*")` + `fullParentIds` 链）：788 目录 / 15107 文件 ≈ 16 个请求出全量索引，比逐目录爬快两个数量级
  - 逐目录：建目标目录 → 批量 copy（20/批）→ 等落地 → 校验（**全部比字节数，≥1 MB 再比 gcid**）→ 批量 delete（50/批）→ 重列确认文件消失 → 最后自底向上清空目录
  - **删除只走 `fs_delete`（进回收站），绝不碰 `fs_clear_recycle_bin`**
  - ⚠️ **<生产机> 容器 DNS 偶发 `Temporary failure in name resolution`**（已知问题）→ 驱动层 `_call` **不重试网络失败**，脚本必须自带重试（4 次、3/6/9/12 s 退避）
  - 实测战绩：2026-11-13 三轮共归置 **508 个文件 / 9.56 GB**，失败 0

- **<nas> NAS 上三个目录的分工**（2026-11-13 摸清，别搞混）：
  - `<媒体库>` = 容器 `/app/library`（compose 的 `LIBRARY_DIR`）——**STRM 与附件的真实输出目录，应用直接写**
  - `<暂存区>` = compose 的 `WATCH_DIR`——「数据同步」上传任务的监听目录，文件放这里会被传到目标存储源（**跨账号合并只能走这条路**：光鸭「云下载」实测 3 次/账号/天，批量搬不动）
  - `<临时区>` = **不在容器挂载里**的独立目录（外部产物）；2026-11-14 按「只删与库同路径同字节的真重复」清到 1618 个，再由人工移入 `<暂存区>` 走上传任务完成合并
  - ⚠️ **NAS 没有回收站** → 删除必须**先留清单**（`<临时区>_rm_manifest.tsv`），恢复靠从库里拷同名同大小那份
  - ⚠️ **QNAP 是 BusyBox**：`find -printf` 不支持、无 `comm`/`join`；`stat -c`/`md5sum`/`sort`/`awk`/`find -exec … +` 可用 → NAS 侧脚本按这个约束写

- **媒体库扫描不落地音频**（2026-11-13 现状）：驱动的 `video_exts`/`image_exts`/`data_exts` 里**没有 flac** → 音乐文件只在光鸭盘上、**Emby 听不到**（扫描判成未知后缀跳过）。要让 Emby 支持音乐得另加一条「音频附件」通道（功能开发，非整理可解）

- **事件循环里绝不出现同步 CIFS IO**（2026-11-06 prod 事故，规则）
  - 挂载是 `hard` 模式 → IO 卡死时**无限等待**；一次同步 `stat` 就能把整个事件循环卡住 → uvicorn 无法处理请求 → `/healthz` 被 RST → 容器 unhealthy、**面板实际不可用**（实测假死 20 分钟，挂载恢复才自愈）
  - **规则**：任何对「监听根 / 源根目录 / 挂载点」的 `stat` / `is_dir` / `resolve` / `scandir` 都必须 `asyncio.to_thread`（`_check_stable_once` 的 `FileStamp.of`、`start_watcher` 的 `resolve_watch_dir` 已改）
  - **同步 import 也算**（2026-11-07 prod 事故补）：`watchfiles.awatch` 首次调用会在循环线程上同步导入 anyio 后端（`get_cancelled_exc_class` → `import_module`，真实读源码文件）——CIFS 挂死引发 FS 层拥塞时被卡 8 分钟，loop 冻结、uvicorn 停在 bind 之前、面板假死（守卫 py-spy 自动取证定位）。修复：`watcher._watch_events` 先 `asyncio.to_thread(_preflight_watchdeps)` 预热（⚠️ 不能用 `get_cancelled_exc_class()` 预热——它要求当前线程有运行中的 loop，会 `NoEventLoopError`，直接 `importlib.import_module("anyio._backends._asyncio")`）
  - **附件下载路径也算**（2026-11-12 补漏）：`attachments.py` 的判重 `stat` / `mkdir` / 每个 chunk 的 `fh.write` / `part.replace` / `.part` 清理曾全部裸跑在事件循环里——当年 `write_strm` / `delete_strm` 改了线程池，这条路径漏了。现在 `_drain_to_file` 逐块丢 `to_thread`（读响应流是异步的，留在事件循环）
  - **运行时守卫**（`app/services/runtime_guard.py`）：① 心跳看门狗——loop 滞后 >10s 自动 py-spy dump 落日志（py-spy 已进镜像）；② 挂载探活线程——`/healthz` 的 `mounts[]` 显示 `ok`/`hang`/`error`，**挂载卡死可见而面板不假死**
  - `snapshot()` 只读缓存（`/healthz` 同步调用，不能被 IO 拖住）
- **发布到生产：镜像 tag = git tag 的完整版本号去掉 `v`**（2026-11-23 踩过，
  **生产被静默降级**）：
  - git tag `v3.0.1` → 镜像 tag **`3.0.1`**（**不是** `1.0.1`！后者是 2026-11-18
    真实存在的旧 tag，`docker pull` **不会报 not found**，而是拉到旧镜像 →
    `up -d` 用旧代码重建容器 → 表现为「刚修好的功能又没了」「报错与代码对不上」）
  - **部署后必须核对镜像的 commit**：
    `docker image inspect ppw111/medialab:3.0.1 --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'`
    应等于 `git rev-list -n1 v3.0.1`
  - 顺带核对目标修复在不在容器里：
    `docker exec medialab grep -c "<新函数名>" /opt/venv/lib/python3.12/site-packages/app/...`
  - 详见 `DEPLOY.md` §4
- **必备命令**
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/ tests/
uv run mypy app/
uv run pytest                # 后端测试（1673 例）
cd web && npx vue-tsc -b    # 前端类型检查
cd web && npm run lint      # 前端 lint（ESLint，--max-warnings 0）
cd web && npm run format:check  # 前端格式检查（Prettier）
cd web && node scripts/check-types-placement.mjs  # 类型放置约束（API 契约类型集中在 src/types.ts）
cd web && node scripts/check-css-classes.mjs   # CSS class 定义约束（template 用的 class 必须有样式定义）
cd web && npx vite build    # 前端构建
```

静态检查不通过禁止合并。**CI（`.github/workflows/ci.yml`）会跑上述全部闸门**：
ruff + mypy + pytest + vue-tsc + ESLint + Prettier + types-placement（API 契约类型集中检查）
+ css-classes（CSS class 定义检查），全绿才允许合并。前端规范详见
`docs/quality-gates.md`。

- **CSS class 定义检查**（2026-11-03 加入，`web/scripts/check-css-classes.mjs`）：
  - 扫每个 `.vue` 的 template 里用到的 class，校验它在「本文件 `<style>`」或
    「`web/src/styles/global.css`」里有定义
  - **防的是什么**：`544f8f9 feat(downloaders): 合并「下载器」页与「任务」页`
    把 `Torrents.vue` 并入 `Downloaders.vue` 时**只搬了 template + script，
    没搬 `<style scoped>`**——6 个 class 定义就此丢失，页面样式全坏
    （筛选栏控件挤成一团、进度条与百分比上下堆叠）。**vue-tsc 不管 CSS、
    ESLint 也不管 CSS** → 没有任何闸门能发现，直到用户截图反馈
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
  - 13 页 × 无横向溢出 / 侧栏隐藏 + 抽屉可开 / 桌面端不溢出 / 每页都有统一标题（原 e2e 契约已随 e2e 移除，靠人工核对）

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

- **网盘聚合（多账号主从，2026-11-15 加入）**：
  - **模型：聚合组 = 1 主 + N 副**（`aggregate_groups` 表）。主数据源是**唯一
    真源**（媒体库扫描 / STRM / 播放都只认它）；副数据源是**缓冲**——副盘内容
    **永不对外展示**，归拢到主盘后**自动清理副盘副本**（进回收站 + 审计日志）
  - **多个聚合组并存**，组间互不影响（G1 主盘额度耗尽不影响 G2 归拢）；
    组外独立盘（`aggregate_group_id IS NULL`）行为与引入聚合前**完全一致**
  - **一盘一组一角色**：`drivers.aggregate_group_id` + `drivers.role`
    （`master` / `secondary`）。**不允许跨组嵌套**（归拢链路会成环、额度归属
    混乱）；**不支持跨组搬迁**（跨组需求走既有迁移功能手动处理）
  - **角色切换有「干净状态」校验**（后端，绕过前端直接打 API 也拦得住）：
    ① 组内**唯一主**——已有主盘时指派第二个主 → 409（不静默改旧主的角色）
    ② 主 → 副时该盘还有 `media_files` → 409（否则留下指向副盘的**死引用**：
    副盘内容不进媒体库，那些条目与本地 STRM 会失效）
    ③ 该盘还有未消化台账 → 409（台账归属会与新角色矛盾）
  - **副数据源不参与媒体库扫描**——`start_scan_task` 对 `role=secondary`
    直接 400（副盘不生成 STRM、不进库）
  - **硬墙是云添加额度**：光鸭实测 **3 次/账号/天**、**失败也扣**，消耗在
    **目标账号**（主盘）头上
    - ⚠️ **额度靠撞墙探测，不记账**（2026-11-15 决策）：光鸭**不提供「剩余
      次数」查询**，只能撞墙。故**没有**额度账本（原 `quota_ledger` 表已删）：
      撞到文案特征（`次数超限` / `额度` / `配额` / `每日上限`）→ **整组**待
      归拢行转 `paused`（**不硬撞不重试**）+ **停手**（不逐个撞：撞墙 =
      当天的门关了，继续提交只是白费服务端请求）→ 次日额度窗口打开时由
      作业自动续传（`paused` 行会被捡起来）
    - ⚠️ **额度消耗在接收端，不在源端**：云添加是**目标盘去拉直链**的消耗。
      **源端取直链不吃额度**——源账号头上记的是**直链流量**，与云添加额度
      是两回事。故 光鸭B → 光鸭A 消耗 **光鸭A** 的额度；同账号 → 同账号消耗
      **光鸭A** 的额度，**副盘的额度分文不动**
    - ⚠️ **泛化失败不自动重试**（2026-11-15 决策，2026-11-18 修订）：光鸭撞额度时**可能只回
      泛化的「云添加失败」**（code=8，无「次数超限」字样）——认不出是不是额度
      问题，而**失败也扣额度** → **定时作业不自动重试** failed 行（无人值守
      不白扣）。**手动「立即归拢」（`sync_group_now`）包含 failed 行**
      （`run_group` 的 `include_failed=True`）——人工意志的重试不是自动重试，
      与「手动不限单轮次数」同一语义；逐行「重试」按钮（
      `aggregate_staging.retry_staging`）仍可用。契约：
      `test_generic_failure_is_failed_and_not_auto_retried`
    - ⚠️ **非会员副盘取不到大文件直链——「转存优先」是唯一出路**（2026-11-19 实测）：
      - **现象**：副盘账号非会员时 `get_res_download_url` 返回 code=254
        「网页端非会员下载文件大小超过限制，请开通会员或使用客户端下载」。
        生产实测：同一账号 11-17 还成功传过 10.2 GB 的文件，11-18 起连
        0.56 GB 都被拒 → **会员到期**，不是代码问题
      - **换客户端身份绕不开**（探针实测 6 种组合**全部同样被拒**）：dt=1/2/3、
        客户端 UA、去 origin/referer、全套 `x-*` 客户端头 → **端别不由这些头
        决定**，这条路对非会员是死的
      - **VIP 状态探测会误导**：`get_assets` 的 `vipStatus` 与下载接口的会员
        判定**不是同一口径**（实测三个副盘 `vipStatus=3`、剩余 0.9~2.9 天，
        仍被判「非会员」）→ **别用 `vipStatus` 判断能不能搬**
      - ✅ **分享转存不受影响**（服务端内部复制，**不取直链**）：实测
        **7.74 GB 单文件 11 秒落地**（同名同大小）、**不消耗云添加额度**
      - ⚠️ **但能分享是「逐文件」审核决定的**（实测 58 个失败行：**14 个
        / 59.6 GB 能进分享，44 个 / 93.7 GB 的分享内容为空**）→ 表现为转存报
        **「未选择转存文件」**。这不是竞态（等 2.5 s 重试、重建分享再试，
        同样失败）——是**服务端把文件排除在分享之外**
      - ⚠️ **判据要确定性**：转存前用 `share_files_list`（驱动方法
        `share_files`）确认文件**确实在分享里**，别白撞转存接口（分享里
        的 fileId 与台账 `remote_id` **是同一个**）
      - ⚠️ **一个被拒的文件不能连坐整批**：分享审核逐文件 → 批次里**只转存
        分享里有的文件**，被拒的直接交给回退通道（`_restore_share_with_retry`
        返回 `dropped_ids`）——整批失败时再**逐个重试**
        （`_transfer_rows_one_by_one`）兜底
      - ⚠️ **列分享内容失败 ≠ 审核拒绝**：接口抖动时按「内容齐全」继续
        （`_share_content_ids` 返回 `None` = **未知**，与「空集 = 确实被拒」
        分开）——宁可让转存接口自己报错（那个错有信息量），也不要一次列表
        失败把整批当成「审核拒绝」
      - ⚠️ **分享内容只列到一部分 → 先等再判**：内容索引有延迟（真实竞态）
        → 等 `SHARE_SETTLE_SEC` 重列一次才判「审核拒绝」——非会员副盘取不到
        直链，被误判就等于这个文件彻底搬不动
      - 故新增运行时参数 **`aggregate_transfer_mode`**（0=按体积分流 /
        1=**转存优先**，默认 1）：转存优先 = 所有文件走分享转存，转存失败才
        回退云下载（保底）。`aggregate_share_threshold_bytes` 的体积上限
        **只在按体积分流模式下生效**（存量配置语义零变化）
      - ⚠️ **replace 场景（主盘已有更小的同名文件）仍走云下载**——转存落地
        同名文件的覆盖语义未验证，保守不碰
      - ⚠️ **分享批次要同时管数量与体积**：光鸭限制 20 个/分享（实测），
        再加**单批 ≤10 GB**（`SHARE_BATCH_MAX_BYTES`）——数量管不住体积，
        20×5 GB 的分享服务端很可能拒收；大文件自动单独成批
      - 契约：`test_share_first_mode_sends_big_file_to_share_transfer`、
        `test_share_batches_split_by_total_bytes`、
        `test_share_transfer_retries_when_content_not_ready`、
        `test_share_content_rejected_file_falls_back_alone`、
        `test_share_content_list_failure_is_not_audit_rejection`、
        `test_share_content_partial_list_settles_before_rejecting`
    - 契约：`test_sync_pauses_group_when_hitting_quota_wall`（撞墙 → 整组
      paused + 停手）、`test_paused_rows_resume_after_quota_resets`（次日续传）
  - **归拢链路 = 手动迁移同一条**（`app/services/migration_pipeline.py`
    抽出 `TargetIndex` / `TaskWatcher` / `SubmitGate` 供两边共用，**别复制
    第二份**）：取直链 → 云下载 → 等任务结束 → **改名**（光鸭直链末段是
    `download`，落盘名要改回原名）→ 校验同名同大小 → 清理副盘副本
    - **「云下载」= 光鸭的离线任务机制**（`cloud_create_task` /
      `list_active_offline_tasks` / `list_failed_offline_tasks`，
      `guangya.py` 保留着）——**目标盘从 URL 自己拉**（URL 是光鸭 CDN 直链
      → 服务端内部复制，**不吃本机带宽**）。别与 2026-11-11 下线的
      「离线下载」功能混淆：那套是**本地文件 → 光鸭**（光鸭去拉本机，
      实测 0.75 MB/s），已整体删除
    - ⚠️ **「`download` 是谁的」在两个步骤里判断相反**：
      - **认领残留**（步骤②）：按 `row.attempts`（本次处理**前**的快照值）
        - **从未提交过**（attempts == 0）：目录里的 `download` 只可能是用户
          的 → **不认领**（认了 = 把用户的文件改名，不可逆）
        - **提交过**（attempts > 0，重试 / 中断恢复）：`download` 大概率是
          我们的残留 → 认领它（排除 = 永远认领不到 → 重试重新提交白扣额度
          + 落成 `download(1)` 死循环 + 垃圾累积）
      - **提交后改名**（步骤⑦）：候选**不排除**任何名字，改名时用
        `exclude_names` **精确跳过**目录里原有的同名条目——用户有
        `download` 时服务端把我们的落成 `download(1)`，跳过 `download` 后
        `download(1)` 仍能匹配 → **只改我们那份**
        - ⚠️ **别把候选整个删掉**（曾经的错法）：删了 `download` 候选，
          `download(1)` 也认不出来 → **这个目录的归拢必然失败**
      - 认领命中多个匹配时，**服务端自动改名形态（`download(N)`）优先**于
        原名——前者必是服务端生成的，后者可能是用户文件
      - 契约：`test_user_download_file_is_protected`（用户文件分毫未动 +
        一次就成功）、`test_leftover_is_claimed_on_retry`（残留被认领、
        不重复提交）
    - **归拢与手动迁移共用同一条管线**（`migration_pipeline`），顺序、额度
      判定、提交间隔、「保留大的」口径、删除源的安全顺序**完全一致**；
      刻意差异 4 处：① **清理源**：迁移可选（默认关）、归拢**必须**（副盘是
      缓冲的定义）② **额度撞墙的载体**：迁移暂停内存作业、归拢台账转
      `paused`（跨重启记得住）③ **认领残留**：迁移无条件认领、归拢按
      `attempts` 分流 ④ **失败**：迁移继续下一个（重跑即可）、归拢不自动
      重试（额度稀缺）
    - **迁移的「提交后改名」也补了 `exclude_names`**（2026-11-15 对齐）：
      用户原有的 ``download`` 不动，服务端改名成的 ``download(1)`` 照改。
      契约：`test_existing_download_file_is_not_renamed`
      - ⚠️ **残留差异（刻意保留）**：迁移的**认领残留**仍是无条件的 →
        用户有个**字节数恰好相同**的 ``download`` 文件时，迁移会认领它
        （改名可逆，但媒体文件本身没传过去）。归拢靠 ``attempts`` 规避；
        迁移是单次作业、没有持久台账，`attempts` 概念不适用，而它的
        **幂等重跑**（认领上一轮残留）是明确承诺的能力——故保留现状。
  - **迁移与归拢的云下载通道恒串行**（2026-11-15 决策）：
    - 落盘名撞车（`download(1)`）+ 云添加额度稀缺，并发只会更快撞墙
    - ⚠️ **但归拢的两条通道之间是并行的**（2026-11-17）：分享转存通道
      （小文件，0 额度、不撞名）与云下载通道（大文件，串行 + 额度）
      **同时跑**——两者瓶颈不同（转存=服务端落地延迟、云下载=额度与提交
      间隔），并行时附件批次等落地的时间正好让视频跑起来
    - ⚠️ **额度墙只暂停云下载通道的行**（`_pause_rows`，不再整组连坐）——
      分享转存通道不受云添加额度影响，附件不该被拖到次日
    - 转存未落地的回退行：额度已耗尽 → **直接 paused**（不再白撞一次）；
      额度还在 → 再走一轮云下载
    - 归拢：组内并发 1、组与组之间也串行；**定时作业单轮上限 = 运行时参数
      `aggregate_max_submissions`**（默认 20，**0 = 不限**；纯运行时保护，
      真正的限制是服务端的额度墙）；**手动触发始终不限**（用户盯着，要搬
      到台账清空）——参数在「调度 → 聚合归拢 → 配置」里改，改完下一轮生效
      （作业每次执行前读最新值，无缓存）
    - 迁移：`MigrationParams` **没有并发参数**（API 也拒绝传——`extra="forbid"`
      → 422 **显式拒绝**而非静默忽略）；`MIGRATION_CONCURRENCY = 1` 写死
    - 提交间隔统一收紧到 ≤5s（额度是瓶颈，不是下载频控）
    - 契约：`test_migration_is_strictly_serial`（任何时刻只有一个在途任务）
  - **顺序不可反**：副盘副本**只在主盘确认落盘同名同大小之后**才删。先删源
    再传，一旦传失败就是数据没了
  - **同名不同大小 → 保留大的**（与数据同步 `conflict_policy` / 迁移功能同口径）：
    主盘更大 → 跳过传输直接清副盘副本；副盘更大 → 删主盘侧的小文件后重传
  - **中断恢复**：`syncing` 的行先查光鸭活跃任务——**还在跑就跳过**（别重复
    提交白扣额度），已消失则重置为待归拢；上一轮「已落盘但没改名」的
    `download` 残留按字节数**认领**并改名（不重复下载）
  - **目录映射整组归到聚合域**（2026-11-15 用户决策）：「从副盘的哪个目录，
    搬到主盘的哪个目录」是一条**组与组之间的映射关系**，两端都是**组级配置**
    ——**不借存储源的媒体库配置**（那是存储源管媒体库的事：媒体库目录 /
    本地输出目录 / 后缀 / prune）
    - 主盘落点 = `aggregate_groups.target_dir_id`（+ `target_dir_path` 展示）
    - 副盘缓冲根 = **新表 `aggregate_group_roots`**（group_id + driver_id +
      root_id，UNIQUE(group_id, driver_id)）
    - `drivers.staging_root_id` **已删**（存量全 NULL，删列安全）
    - **归属**（`drivers.aggregate_group_id` / `drivers.role`）仍挂在盘上——
      那是 1:1 的归属关系（一个盘一个组、组内一个角色），无处可挂
    - 一个端点管整组映射：`PUT /api/aggregate/groups/{id}/mapping`
      （`target_dir_id` + `roots[]`，**全量替换**副盘侧）；组外的盘给了
      缓冲根 → 400
    - 未配置目标目录 → 归拢作业**明确跳过并把原因写进作业明细**（不是
      静默跳过）；「登记在途」则报 400
  - **副盘缓冲根下的相对目录结构原样保留**到主盘目标目录下（媒体库按
    「剧名 / Season N / 集」组织，压平会认不出剧集）
  - 「登记在途」扫描副盘缓冲根 → 进台账（**幂等**，`(source_driver_id,
    remote_id)` 唯一约束——重复登记 = 白扣两次额度）
    - ⚠️ **两条入口都要「先登记再归拢」**（`aggregate_sync.run_group`——
      定时作业与手动触发**共用**这一步）：副盘是
      **流动的缓冲区**——「数据同步」上传任务会持续往副盘缓冲根写新文件，
      而台账里没有它们的行 → **永远不会被归拢**。故每轮先扫描登记（幂等）
      再归拢。契约：`test_scheduled_sync_registers_new_files_first`、
      `test_manual_sync_also_registers_new_files`
    - ⚠️ **测试隔离**（autouse fixture）：`_share_threshold_bytes` 默认
      patch 成 0（关分享通道）——不关的话多数用例走分享通道，假驱动的
      转存落地名与校验期望名不匹配 → **每文件轮询满超时**（600s），
      测试文件跑到天荒地老；`SHARE_TRANSFER_WAIT_SEC` 也要压短
      （回退类用例 1s）
  - **调度作业 `aggregate_sync`**（cron `29 * * * *`）：**单轮限 20 次提交**
    （无人值守的运行时保护），其余留到下一轮；**手动触发不限**（用户盯着，
    要搬到台账清空，只有服务端的额度墙才会停）
    - 定时作业遇到「手动正在跑」→ **跳过该组**（不排队干等，下一轮自然补）
  - ⚠️ **轮询协程不能在「空闲」时退出**（`migration_pipeline.TaskWatcher`）：
    那会留一个竞态窗口——协程判定「没有在途任务」→ 决定退出 → 但任务对象
    还没被标记 done；此刻下一个 ``wait()`` 进来，``_ensure_running`` 看到
    「协程还活着」→ 不新建 → **那个任务永远等不到事件**（wait 永久阻塞，
    最长 2 小时才被超时兜住）。生产里靠「文件间 5 秒提交间隔」侥幸避开。
    故协程**活着等到 close()**：空闲时只 sleep、**不发请求**。
    契约：`test_task_watcher_survives_sequential_waits`
  - **并发防护与手动触发**（2026-11-15 审查修复）：
    - **组级锁**（`aggregate_sync._group_lock`）：手动 `POST /sync` 与 cron
      作业可重叠（APScheduler `max_instances=1` 只防作业自身重叠），两个
      run 各拿同一批 pending 行 → 同一文件提交两次白扣额度。单进程应用，
      进程内 per-group `asyncio.Lock` 足够
    - **手动触发走后台 job**（`aggregate_jobs.py`，照 scan_jobs 模式）：
      `POST /groups/{id}/sync` **立即返回 job_id**，前端轮询
      `GET /sync-jobs/{id}`——单文件传输几分钟，同步 HTTP 会被网关 60s
      超时掐断；同组已有任务在跑 → 409
    - **job 的计数就地更新**（`job.result` 是同一个 `AggregateSyncResult`
      对象）→ 面板每次轮询拿到的都是**实时进度**（原来跑完才一次性更新，
      面板干等几分钟看不到动静）
    - **云下载等待有上限**（`TASK_MAX_WAIT_SEC` = 2h）：光鸭会把任务卡在
      活跃列表不结束（实测），无超时 = run 永不结束 = **其它组也饿死**。
      超时记 `failed`（`cloud_task_id` 保留，人工重试时认领残留能接住
      已落盘的情况）。契约：`test_stuck_task_times_out_and_marks_failed`
    - **在途任务查询失败 → 本轮跳过 syncing 行**：查询失败返回 `None`
      （绝不能返回空集冒充「都不在跑」——那会把在途任务重置为待归拢并
      重新提交，白扣额度）。<生产机> 容器 DNS 偶发故障是已知触发源。
      契约：`test_active_task_query_failure_skips_syncing_rows`
  - **前端（`web/src/pages/media/Aggregate.vue`）**：组卡片（成员 / 目录映射 /
    水位）+ 在途台账表格 + 「登记在途」「立即归拢」（都是后台 job，2s 轮询）
    + 「目录映射」弹框（主盘目标目录 + 各副盘缓冲根，各一个 `DirBrowser`）
    - **归拢实时进度**：组卡片内嵌进度卡（状态 + 成功/清理/暂停/失败/跳过
      计数 + 日志尾部 + 取消按钮），2s 轮询 `GET /sync-jobs/{id}`；页面加载
      时恢复「正在跑」的 job（`GET /sync-jobs?group_id=` 找 running）——
      手动与定时（在跑的那轮）都能看到。job 表是**内存态**，容器重启即清空
    - ⚠️ **禁用条件与 tooltip 文案必须分成两个函数**（2026-11-16 踩过）：
      曾把 tooltip 文案（非空串）直接当禁用条件 `:disabled="!!tip(group)"`，
      而**配置齐全时文案也是非空的** → 按钮**永远点不动**。现为
      `enqueueDisabled()` / `enqueueTip()` 分离（配置齐全时可点 / 缺映射时禁用，原 e2e 契约已随 e2e 移除）
  - **小文件走「分享转存」通道**（2026-11-16 加入，实测可行）：
    - **为什么**：附件（nfo/jpg/png）占**文件数的 76%** 却只占 ~0% 体积，
      每个都要花一次**云添加额度** → 纯浪费。转存是**服务端内部复制**，
      **不消耗云添加额度**
    - **链路**：源账号 `share_create` → 取 `access_token` → 目标账号
      `share_restore(… , parent_id=目标目录)` → **轮询目标目录**直到出现
      同名同大小 → 清副盘副本 → 删分享
    - ⚠️ **实测坑**（本地真机联调踩到）：
      - 公开接口只认 **URL 形态**的分享 id（`{shareId}_{账号分享key}`）——
        裸 shareId → code=112「参数错误」
      - **必须剥掉查询串**：`shareUrl` 形如 `…/s/{id}_{key}?code=xxxx`，
        不剥 → id 变成「…_key?code=xxxx」→ 取 token 报「分享链接错误」
      - **提取码必需**（`needCode: true`），否则 209「提取码错误」
      - 转存是**异步任务**且没有可靠的完成查询接口 → 只能**列目录轮询**
      - 转存落地**保持原名**（不像云下载要落成 `download` 再改名）
      - 分享用完即删（`share_delete`），别在账号里堆分享
    - **分流规则**：体积阈值（运行时参数 `aggregate_share_threshold_bytes`，
      默认 10MB，**0 = 关闭该通道**）；失败 → **回退离线下载**（保底）
    - **批量打包**（2026-11-16 加入，吞吐的最大杠杆）：同 (源账号, 目标目录)
      的小文件**打包成一个分享**（`share_create(file_ids)` 原生支持列表）、
      一次转存整批（`SHARE_BATCH_SIZE=20`）→ N 个附件从「N×(4 请求+轮询)」
      变成「1 次分享+1 次转存」。转存只能落**一个目录** → 跨目录/跨账号
      的行各自成批。整批超时（未全部落地）→ 已落地的标 done、没落地的
      **回退云下载**
      - ⚠️ **批量路径必须逐行预检**（2026-11-16 生产实测教训）：曾漏掉
        「主盘已有同名文件」的处理 → 转存撞上目标已有文件 → 失败/自动改名
        → 轮询条件「原名+副盘大小」永远不命中 → **每批烧满 600s 超时**
        （27 批 ≈ 4.6 小时啥也搬不动）。现预检抽成 `_precheck_target`
        （两条通道共用）：skip（同大小/更大→清副盘标 done）/ replace
        （更大→删旧走云下载）/ transfer
      - ⚠️ **整批失败的异常路径也要把行交给回退**——曾漏掉（`not_landed`
        只在 try 块赋值）→ 回退列表为空 → 云下载循环空转
      - ⚠️ **本批完成即入账**——曾攒到整轮结束 → 面板 15 分钟「成功=0」
        实际早就在搬
      - ⚠️ **假驱动的 dir id 必须含父目录**（不同剧的同名 Season 目录要
        拿到不同 id）——聚合的目录分组靠它
      - ⚠️ **轮询传相对路径，不传目录 id**（2026-11-16 生产踩到）：曾把
        `row.target_dir_id`（fileId）传给 `index.refresh`（它要的是
        **相对路径**）→ TargetIndex 把 fileId 当目录名在归拢根下**建了一堆
        空垃圾目录**并列空的 → 轮询永不命中 → 每批烧满超时。文件**其实都
        转存成功了**，回退的预检正确识别「主盘已有」→ 标 done，无数据
        损失——但垃圾目录要清理。
      - ⚠️ **光鸭限制：一个分享最多 20 个文件**（实测二分：10/20 成功、
        25/30/35/40/50 全部「分享目录文件数量超过限制」）——超限批次**建
        分享直接失败** → 触发逐个回退云下载（白扣额度），**别拍脑袋调大**
    - **批次并行**（2026-11-17 加入）：分享通道**没有**云下载的两条串行
      约束（落盘名撞车 / 按次额度）→ 批次可以并行（`SHARE_MAX_IN_FLIGHT=3`，
      有界并发）。服务端转存任务的**落地延迟实测 ~10 分钟/批**——串行时
      吞吐被延迟卡死（20 个/10 分钟），并行后 ×3–5
    - **批量清理**：副盘副本用 `delete_files`（一批一次请求）；失败逐个兜底
      （`_cleanup_source`）
      - 首次部署曾用 50 → 全部批次失败 → 回退烧额度（当日额度本就耗尽，
        未造成实际损失）；已修为 20
    - **完成判定已从「等任务结束」改为「列目录等文件出现」**（两条通道
      统一）：云下载的「2 次缺席确认」+ 提交间隔曾是每文件 ~15s 的纯开销
      （提交间隔 5s→1s：额度按天计，间隔不影响额度；轮询 5s→2s）
    - 台账记 `transfer_mode`（`cloud_download` / `share_transfer`）
    - 探针：`scripts/probe_guangya_share.py`（生产容器内）、
      `scripts/probe_guangya_share_local.py`（**本地真机**，用
      `.env.probe` 里的生产凭据——gitignored，测完即删）
  - **待办（用户明确暂缓）**：写入入口形态（上传任务目标是「选组」还是
    「选具体盘」+ planner 自动溢出）——阶段 1、2 落地后结合实际使用再定
  - 契约由 `tests/unit/test_aggregate.py` + `tests/integration/test_aggregate.py`
    + `tests/integration/test_aggregate_sync.py` 固化
- **「本地同步 → 网盘聚合」（2026-11-19 加入，`app/services/aggregate_upload.py`）**：
  数据同步的**目标类型**多了一种：不是某个具体存储源，而是**一个聚合组**
  - **只选组，副盘自动分配**（用户明确要求「不选具体副盘」）：落点由分配器
    在组内副盘之间挑——**固定顺序**（按 `driver id` 升序，**不排序**、
    **不做月度记账**、**不手填限额**）
  - **粘性落点**：当前落点持久化在 `upload_tasks.driver_id` /
    `target_file_id`（**复用为分配器状态**，列不动）——重启后恢复，避免
    「换盘后已传文件被重传」
  - **闸门**（只过滤，不排序）：① 本月上传额度未耗尽 ② 测连缓存健康
    （**已知时**才判，没探过不拦）③ 剩余空间 ≥ 文件大小（已知时）
  - ⚠️ **月度上传限额只能撞墙探测**（与「云添加额度」同一套哲学）：光鸭
    **没有查询接口**，上传失败按**文案特征**判定（`is_upload_quota_wall`：
    限额 / 额度 / 配额 / 超限 / 上传流量）——**限流 / 网络 / 凭据类错误不
    触发换盘**（抖动换盘只会把问题放大）
    - 命中 → 给该账号打「本月耗尽」标记（`drivers.upload_quota_exhausted_month`
      存月份键）→ **换下一个候选并当场重试该文件**；全部撞墙 → 失败并点名
    - 标记**跨月自动失效**（键 ≠ 当前月即正常），无清理任务
    - ⚠️ **真实撞墙文案未知**：匹配器收口在 `aggregate_upload` 一处、可扩展；
      上线初期靠上传失败日志观察真实文案，命中特征后固化关键词
    - ⚠️ **实测结论（2026-11-19 生产探针 `scripts/probe_guangya_upload_quota.py`）**：
      **API 上传通道根本没有这堵墙**——三个副盘、小文件单 PUT（64B）与大文件
      分片（1.2MB / 5MB）、真实落点（组内缓冲根）+ 媒体后缀（.mkv）**全部
      上传成功**；生产历史更是小号2 单日 API 上传 **115GB**、小号3 两天
      **392GB**（台账 1863 + 613 行），**零上传失败**，生产日志里
      「限额/额度/超限/配额」**零命中**
      - 光鸭客户端**没有额度查询接口**（`user_info` 早已 501；接口面里没有
        任何 quota / traffic 类方法）——`get_assets` 原始返回只有
        `highSpeedTraffic`（500MiB，**不随上传消耗** → 是下载加速包，
        **不是**上传额度）、`vipStatus`（副盘 = 3 = 非会员）、`vipLeftTime`
      - 故「撞墙换盘」是**永不触发的保险丝**（无害，但不会按预期分流）；
        真要按限额主动分流，必须有额度来源（记账 / 手工填 / 网页端读数）
      - ⚠️ **真正的墙在归拢通道**（不是上传）：生产台账 58 个 failed /
        142.74GB 全是同一句
        `光鸭换直链失败：网页端非会员下载文件大小超过限制，请开通会员或使用客户端下载`
        ——非会员副盘**取不到大文件直链**（2026-11-19 那条实测记录）。
        **推论**：「本地同步 → 网盘聚合」会把**大文件**先落副盘缓冲，而大文件
        归拢正卡在这堵墙上 → 大文件会堆在副盘（生产小号3 缓冲根已积压
        163GB）。**先转存优先 + 重试旧 failed 行**，别急着放量
  - **联动点 = 登记台账**（`aggregate_staging.register_uploaded_file`）：上传
    成功 / 覆盖 → **幂等**登记台账（UNIQUE `(source_driver_id, remote_id)`）
    → 归拢引擎（**零改动**）下一轮就把它搬回主盘。撞写锁 → **降级记日志**
    （兜底扫描 ≤1h 补登记），**绝不影响上传结果**
  - **组级幂等域**（`SyncContext.staged_lookup`）：落点会换盘，「当前落点
    目录里没有这个文件」≠「文件没传过」——文件可能在**另一个副盘**的缓冲
    里，或**已归拢到主盘**。台账（含**终态 done** 行）是判据：命中同名
    同大小 → 跳过上传（不白耗上行带宽与云添加额度）
    - ⚠️ 副作用：文件若在主盘上被**手动删掉**，台账还在 → 不会自动重传
      （需手动删掉对应台账行）
  - **删本地口径不变**：仍是「缓冲盘收到完整副本即删」（接力链：本地→缓冲
    确认即删 → 主盘确认后清缓冲副本），每一跳都有下一跳确认，全程不丢数据
  - **校验矩阵**（创建 / 切组时）：组存在且**启用**、有主盘、已配归拢目标
    目录、至少一个**启用且已配缓冲根且支持上传**的副盘——否则 400 文案点名
    缺哪一项；组不存在 → 404
  - **删组守卫**：组下还挂着数据同步任务 → **409**（不级联删用户配置；
    且组没了任务会往一个没人认领的缓冲根里写文件）
  - 前端「数据同步」页：目标二选一（指定存储源 / 网盘聚合），聚合模式只列
    组 + 各副盘状态（缓冲根 / 剩余空间 / 本月额度 / 测连）；**聚合页零改动**
    （台账行自然出现）
  - 契约由 `tests/unit/test_aggregate_upload.py`（匹配器 + 闸门）+
    `tests/integration/test_aggregate_upload.py`（校验矩阵 / 登记幂等 /
    撞墙换盘 / 组级幂等 / 删组守卫）固化
  - **加列迁移的锁表评估与回滚**（2026-11-19，Kody 评审要求）：本次新增
    `upload_tasks.aggregate_group_id` 与 `drivers.upload_quota_exhausted_month`
    —— SQLite 的 `ADD COLUMN` 是**纯元数据操作**（不改数据、不重建表，
    写锁毫秒级，**不停机**），两列**可空 + 无默认值** → **不回填、不改写
    现有行**；**回滚首选「留列无害 + 回退镜像」**（旧代码读不到这两列），
    彻底删列走 `_drop_column`（SQLite ≥3.35），兜底恢复 appdata 备份快照
  - **落点快照写回的降级必须登记台账**（2026-11-19，Kody 评审要求）：
    `_persist_landing` 写库失败 → `startup_status.record(
    "upload_landing_persist:{task_id}")`（`/readyz` 的 `degradations`
    会显示「粘性落点没写成」），写成功后 `clear` —— 不登记的话运维只能
    事后从「重启后重传」倒查
    - ⚠️ 但**运行期**的偶发降级（台账登记撞锁）**不登记**：它可丢（兜底
      扫描会补），登记会让面板长期 degraded，反而淹没真正的降级
- **任务级「目标子目录」= 组映射的子路径覆盖**（2026-11-20 加入）：
  - **为什么有它**：目录映射（主盘落点 + 各副盘缓冲根）是**组级**配置（配
    一次）；但「这个任务传到 `电影/外语`、那个任务传到 `电视剧/国产`」是
    **任务级**需求 → `upload_tasks.aggregate_sub_path`（**仅聚合模式**有效）
  - **实现口径：改上传、不改归拢**——子目录落在**副盘缓冲根**下的同名子
    目录（`CloudDirIndex._resolve_dir` 沿途逐层建，不需预建）；台账的相对
    路径（相对缓冲根）带上前缀 → 归拢引擎「相对结构原样保留到主盘目标
    目录」自动把它带过去（**归拢零改动**）
  - ⚠️ **三个口径必须分清**（本项目最易踩的坑）：
    ① **监听口径**（相对本地监听根）→ `SyncOutcome.rel_path` / 本地清理 /
    进度展示，**永远不要改**；② **缓冲根口径**（相对副盘缓冲根）→ 台账
    `rel_dir` / 组级幂等索引键；③ **云端口径**（相对落点目录）→ `index` /
    `staged_lookup`。聚合模式下 ②③ = ① + 子目录前缀
  - **粘性落点语义不变**：`target_file_id` 仍存**缓冲根** id（子目录是上传
    上下文的前缀，不是落点目录）→ 换盘 / 重启恢复逻辑不用改
  - **非法路径显式拒绝**（400）：`..` / `.` / 超长段 / 超深层 → 归一化在
    `aggregate_upload.normalize_sub_path`（单一真源）；「指定存储源」模式
    传了子目录也 400（**没有组映射可覆盖**）→ 切回该模式时**一律清空**
  - 前端：聚合模式下多一个「目标子目录（可选）」= **手填 + 从主盘归拢目标
    目录起浏览**（`/drivers/{master_id}/browse-dirs`，相对路径取目录**名**
    而非 id——云端路径由目录名拼出）
  - 契约由 `tests/unit/test_aggregate_upload.py`（归一化）+
    `tests/integration/test_aggregate_subpath.py`（落点 / 台账 / 幂等 / 换盘 /
    重启）+ `tests/integration/test_aggregate_sync.py::test_sync_lands_in_master_subdir`
    （归拢零改动）+ `tests/integration/test_upload_tasks_api.py`（API）固化
- **启动期密钥闸门（2026-11-17，P0）**：`WEB_TOKEN` 与 `SIGNING_SECRET` 都是
  必填，lifespan 缺任一即 `RuntimeError` 拒绝启动；配置层拒绝 `change-me*`
  占位符，prod 还要求二者 ≥32 字符。`/metrics` 不再公开，抓取端要带
  `Authorization: Bearer <WEB_TOKEN>`（或反代内网放行）。
  - 库健康检查：`SIGNING_SECRET` 为空时报告 `configuration_error`，**跳过**
    签名校验而不是把全库判成「签名无效」——绝不能据此诱导批量重签。
  - 健康检查的磁盘扫描（CIFS 网络挂载）必须先关 DB 事务、再走线程池，
    不允许在 async 请求路径里同步 `rglob` / `stat`。
- **启动期 DDL 与删表的硬规则（2026-11-17，P0）**：
  - `_run_alter` / `_EXTRA_INDEXES` 的真失败（非「列已存在」）一律抛错阻断
    启动，不再只记 error 让服务带半迁移状态对外提供。
  - 废弃表（`scan_rules` / `dav_acl` / `quota_ledger`）删除走
    `app/db/migrations.py:drop_obsolete_tables_once`，由 `user_version >= 3`
    守卫；删前在同事务备份为 `medialab_bak_v3_<表名>`。旧库 `user_version`
    可能是 2（合并前 seedhelper），所以版本号选 3 而不是 2。
  - 任何新的一次性、不天然幂等的迁移都必须用 `user_version` 维护版本号，
    且版本号只增不减；不要在「每次启动都跑」的路径上做不可逆操作。


## 后端测试
```bash
uv run pytest                        # 全部
uv run pytest tests/unit             # 只跑单元
uv run pytest -k "site_adapters"     # 按名字过滤
uv run pytest --cov=app              # 带覆盖率（门槛 65，阻断；实测约 69%）
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

## 前端检查
```bash
cd web && npm run check                          # lint + format:check + type-check + test（提交前跑这个）
cd web && npm run lint                           # ESLint（--max-warnings 0）
cd web && npm run format:check                   # Prettier 格式检查
cd web && npm run type-check                     # vue-tsc 类型检查
cd web && npm test                               # 前端单元测试（node:test，零新增依赖，需 Node ≥ 22.18）
cd web && npm run build                          # 生产构建（app+node，不含测试工程）
cd web && node scripts/check-types-placement.mjs # 类型放置约束
cd web && node scripts/check-css-classes.mjs      # CSS class 定义检查
```
项目**不保留 e2e 测试**（原 Playwright e2e 连真实下载器与真实站点，CI 上无法运行，已整体移除）。前端正确性靠上述静态检查 + 单元测试 + 构建 + 人工核对。

## 前端工程规范（2026-11-23 加入）

完整规范见 **`docs/quality-gates.md`**（含工具职责、规则集、Git Hooks 接线细节、CI 门禁）。要点：

- **工具链**：ESLint 10（flat config，`strictTypeChecked` + `stylisticTypeChecked`）+ Prettier 3 + vue-tsc + Husky/lint-staged。**oxlint 已移除**——它是纯语法检查器，没有类型信息，拿不到 `no-floating-promises` / `no-unnecessary-condition` 这类真 bug 探测器。
- **插件**：Vue 官方（`eslint-plugin-vue`）、TS（`@vue/eslint-config-typescript` / `typescript-eslint`）+ 三个「推荐」插件（`eslint-plugin-import-x` / `eslint-plugin-promise` / `eslint-plugin-vuejs-accessibility`）。
  ⚠️ **import-x 必须配 `eslint-import-resolver-vite`**：本项目靠 Vite 的 `resolve.extensions` 允许省略扩展名，import-x 默认 resolver 会把 **39 处正常 import** 误报成「无法解析」。配置里手写了一份等价于 `vite.config.ts` 的 resolve 段——**改 vite 的 resolve 配置时要同步改**（`web/eslint.config.mjs` 的 `import-x/resolver`）。
- **TypeScript 严格模式已开满**（`tsconfig.app.json` + `tsconfig.node.json`）：`strict` / `noUncheckedIndexedAccess` / `exactOptionalPropertyTypes` / `noImplicitReturns` 等。⚠️ 此前**根本没开 `strict`**——改前端时别再依赖「null 不可见」。
- **禁止**：显式 `any`、非空断言 `!`、`@ts-ignore` / `@ts-nocheck`、`@ts-expect-error`（必须带 ≥10 字说明）、未处理的 Promise、`console`、宽松相等。
- **例外必须带明确理由**（写在 `web/eslint.config.mjs` 注释里），不允许用 `eslint-disable` 或批量关规则消错。当前仅三处例外：`scripts/*.mjs` 放行 `no-console`（CI 闸门脚本，stdout 即产物）；`import-x/default` 对 `vite.config.ts` 关闭（resolver 把 `@vitejs/plugin-vue` 解析到 CJS 构建，认不出 default 导出 → 误报）。
- **`web/src/types.ts` 是后端 DTO 的手写镜像**：`no-unnecessary-condition` 报的「多余 null 判定」多数是类型在说谎或防御写多了——**先核对后端**（`app/db/models.py` / `app/api/routes/`）再决定是删防御还是改类型。后端保证非空 → 删 `?? ''`；前端运行时真会拿到 null（如 `a-input-number` 清空写 null）→ 改类型。
- **模板串里的数字要显式 `String()`**（`restrict-template-expressions`）：`${id}` → `${String(id)}`；运算表达式整体包（`String((page - 1) * 50)`）。
- **`prefer-nullish-coalescing` 不许批量 `--fix`**：自动修复会把 `count || 5` 改成 `count ?? 5`（0 变 5，静默改语义）。要保语义就写成 `(x ?? '') || fallback`。
- **Git Hooks 装在 `web/.husky/`**（hooksPath = `web/.husky/_`），由 `web/package.json` 的 `prepare` 脚本注册。⚠️ husky 必须在**仓库根**跑（找的是 cwd 下的 `.git`），故 prepare 脚本是 `cd .. && husky web/.husky`；hook 脚本内部 cwd 是仓库根，要自己 `cd web`。
- **前端有单元测试了**（2026-11-23 加入，`web/tests/` + `web/tsconfig.test.json`）：Node 内置 `node:test`（**零新增依赖**），`npm test` 跑。测试代码**单独一个 TS 工程**（`types: ["node"]`）——Node 全局**不进应用工程**（进了会把 `setInterval` 返回类型变成 `NodeJS.Timeout`）。**改 `web/src/api.ts` 这类共享请求层必须同步补用例**（请求头归一化 / 请求体语义）。需 Node ≥ 22.18（原生 TS 类型擦除）。
- **AI 不得自行修改工程质量配置来规避检查**；确需改规范先说明原因并获维护者认可。
