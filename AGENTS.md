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
- **上传只能串行——并发无效**（2026-11-02 实测，见 `docs/upload-bandwidth.md`）：
  - **瓶颈是本机上行带宽**（实测 ~3.2 MB/s，到阿里云 OSS 张家口 RTT 31ms）→ 分片大小几乎不影响速率（1/5/20 片都是 2.25-2.75 MB/s），是**带宽受限非 RTT 受限**
  - **别再尝试并发**：同一 uploadId 分片并发 → 零收益或卡死；不同 uploadId 并发 → `Server disconnected` / 卡死，且总吞吐**低于**串行
  - `CHUNK_CONCURRENCY = 1`（`guangya_upload.py`）+ 单 worker（`watch_manager.py`）是**正确实现，不是待优化的遗留**
  - `concurrency` 参数保留但不生效（面板可配，恒为 1）——留给将来支持并发的网盘
  - ⚠️ **方法论**：测上传吞吐必须用**生产规模**文件（≥100MB）且做**串行/并发交替对照**。曾用 50MB/10 片测出「并发快 2 倍」的**假象**，据此写了两版并发实现再逐一证伪
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
  - 契约由 `tests/integration/test_push_free.py` 固化
- **站点拉取失败必须抛错**（2026-10-21 prod 事故后确立）：
  - `search_torrents` 返回 `[]` = 站点确实没种子；抛 `SiteFetchError` = 故障（DNS / 连接 / HTTP）
  - 两者混用会让作业统计报"全部成功"而实际漏掉故障站（prod 的 hddolby DNS 失败就是这样被掩盖的）
  - 契约由 `tests/unit/test_site_adapters.py` 固化，9 站全参数化覆盖
- **FR-3.6 站点每日签到契约**（2026-10-22 加入）：
  - 适配器单方法 `checkin() → CheckinResult`，带状态枚举（success / already / unsupported / auth_expired / failed）——**不用 `None`**：签到必须区分"该站不支持"与"签到失败"，否则作业统计分不清
  - **只实现实测可用的 4 站**：hhanclub / soulvoice / hdhome（走 NexusPHP `/attendance.php`，GET 即签到）+ **hdsky**（自建 `showup.php` 三段式，图形验证码走 AI 读码）。其余 5 站走基类默认 `UNSUPPORTED`（audiences / ourbits 需 Turnstile、springSunday / mteam 无此功能、hddolby 未验证）
  - 失败必须抛 `SiteFetchError` / `BreakerTripped`，与拉取路径同一契约
  - 签到熔断**不写** `sites.breaker_until` —— 不与 10 分钟一次的拉取作业共享熔断状态
  - **不建表**：结果只进 `job_runs.result_json`（`summary` + `steps`），前端「调度 → 历史」可见
  - 解析规则用**真实页面**固化成 fixture（`tests/fixtures/*_attendance.html`）——手写最小 HTML 会漏掉真实文案（hhanclub 成功页**不含**"签到成功"四字，只有统计文案）
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

## 必备命令
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/ tests/
uv run mypy app/
uv run pytest                # 后端测试（642 例）
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

