# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `README.md` — 现状与命令。
2. `app/adapters/sites/user_data.py` + `app/adapters/sites/parsers/nexus_php.py` — FR-1.6 站点用户数据契约。
3. `app/adapters/sites/parsers/attendance.py` — FR-3.6 站点签到解析 helper。

## 工作准则
- 改动窗口锚定（FR-3.4）或中止链路（FR-3.5）前，先确认需求基线与现状口径一致。
- 数据删除以"不误删"为底线：hash 必须是 40 位 hex（拒绝 `all` / 通配），前端删除需确认，删磁盘数据单独开关并记审计日志。不引入回收站 / 保护期等未实现的机制。
- **删除站点 = 级联删关联数据**（2026-10-23 决定）：`torrent_index`（拉取快照，可重建）+ `site_user_data`（用户数据快照）+ `task_queue`（该站推送队列行）在**同一事务**里删完再删父行。
  - **不要**用"引用计数 > 0 就 409 拦住"——`torrent_index` 不是任务表，全仓没有删除它的入口，且 `fetch_all_sites` 每 10 分钟就重新灌满 → 那个 409 对任何被拉取过的站点都是**永久死锁**
  - `task_queue` 也要删：SQLite 删行后**会复用 id**，`idem_key` 格式是 `add_torrent|{site_id}|{item_id}|dl={dl}`，留着会让新站误命中老幂等键 → 推送被永久判为"已推过"
  - 前缀必须带结尾的 `|`（`add_torrent|1|` 不匹配 `add_torrent|11|...`）
  - 契约由 `tests/integration/test_services.py` + `tests/integration/test_api.py` 固化
- 站点适配层改动后用真实站点（测试环境 cookie）拉取验证，不使用 mock 数据测试。
- **自动化 / 合规边界**（2026-10-22 修订，原「不引入未文档化的反爬对抗 / 验证码绕过（R2）」）：
  - ✅ **允许**对**自有账号**做站点自动化，含 AI 读图形验证码（`app/core/ai.py`）、真实浏览器交互
  - ❌ **禁止**伪造 / 破解站点**鉴权凭据**（如 soulvoice 的 JWT/HS256 downhash）、伪造站点签名、越权访问他人资源
  - Cloudflare Turnstile 等**人机验证**本仓库不实现（实测 `TurnstileError 600010`，服务端环境无法完成），相关站返回 `UNSUPPORTED`（需人工）
- **站点适配层保持 9 站独立文件**（2026-10-21 决定）：不抽公共基类，各站独立实现。重复代码接受现状。
- **FR-1.6 站点用户数据契约**（2026-10-18 加入，2026-10-18 精简为单方法，2026-10-22 加时魔）：
  - 不抽 9 站 user_data 基类；NexusPHP 7 站共用 `app/adapters/sites/parsers/nexus_php.py` module-level helper（`parse_user_snapshot` / `parse_hourly_bonus`）
  - `SiteUserData` 表是**当前快照**（UNIQUE site_id），不做历史；如需时间序列需先建 `site_user_data_history` 表再迁
  - 适配器单方法 `get_user_snapshot`：**返回 `None` 只表示"该站不支持 / 无凭据"**（service 记 skipped）；**拉取/解析失败必须抛 `SiteFetchError`**（service 记 failed 并点名，**不写 0 兜底**——写 0 等于把故障伪装成"上传 0 / 做种 0"）
  - 抓取页用 `/mybonus.php`（navbar 带 4 业务字段 + 时魔，**一页拿全**）；navbar 解析不出 4 字段时回退 `/userdetails.php`
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

## 必备命令
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/ tests/
uv run mypy app/
uv run pytest                # 后端测试（642 例）
cd web && npx vue-tsc -b    # 前端类型检查
cd web && npx oxlint .      # 前端 lint
cd web && node scripts/check-types-placement.mjs  # 类型放置约束（API 契约类型集中在 src/types.ts）
cd web && npx vite build    # 前端构建
```

静态检查不通过禁止合并。**CI（`.github/workflows/ci.yml`）会跑上述全部闸门**：
ruff + mypy + pytest + vue-tsc + oxlint + types-placement（API 契约类型集中检查），全绿才允许合并。

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
Conventional Commits，作用域示例：`feat(crypto)` / `fix(free-seed)` / `docs(req)` / `refactor(sites)` / `feat(sites)` (FR-1.6 站点 userdata) / `feat(db)` (SiteUserData 表) / `feat(api)` (user_data 端点) / `feat(web)` (UserData.vue) / `feat(scheduler)` (调度作业/配置/历史) / `feat(cross-seed)` (辅种下载器巡查) / `feat(checkin)` (FR-3.6 站点每日签到) / `feat(bonus)` (站点时魔抓取)。

## 前端测试
```bash
cd web && npx playwright test    # 需先起 dev 容器 (localhost:8080)
```
新增页面/交互改动必须补 `web/tests/*.spec.ts` 用例。

**注意**：Playwright e2e 连的是**真实下载器与真实站点**，CI 上跑不了也不跑
（CI 跑 vue-tsc + oxlint + types-placement；不含 Playwright）。e2e 需在本机起 dev 容器后跑。

