# 辅种域作业：按数据流串起来（方案）

**一句话结论**：作业**已经是节点**，缺的是**触发方式**。把 `cron` 从「唯一触发源」
降为「兜底下限」，加一条声明式依赖边（`depends_on` + 最小间隔），让上游**成功后**
主动唤醒下游——辅种域就从「各自按点醒」变成「一条数据流」，且**不动 DB 契约**。

本文是方案（未实施）。落地步骤见文末「实施顺序」。

> **📌 实施后变更（2026-11-22，同一天）**：`免费种推送` 与 `免费窗口监测`
> **已合并为一个作业** `push_and_monitor_free`（用户要求）。
>
> 合并后本文的**依赖边已无用户**：
>
> - 合并作业的 cron 是**监测所需的 10 分钟心跳**，比推送门槛（30 分钟）
>   还细 → 上游触发不会带来更早的推送，挂上就是「永不生效的死边」，故**不挂**；
> - 推送的 30 分钟节奏改由作业**内部节流**表达
>   （`app/services/push_and_monitor.py:PUSH_MIN_INTERVAL_SEC`）——同一个值，
>   行为连续。
>
> `depends_on` / `min_trigger_interval_sec` 这套机制**保留**（实现与测试都在，
> 将来有「真依赖 + 更粗 cron」的作业可以直接用），只是当前没有作业声明它。
> `GET /scheduler` 仍照常下发这两个字段（全部为 `null` / `0`）。

---

## 1. 为什么不是「重写成 pipeline 引擎」

| 约束 | 说明 |
|---|---|
| **日历语义** | 媒体库域 5 个作业是日历位（`47 3 * * *` / `7 * * * *`），改成 `interval` 会失真 |
| **重节点绑架** | `cross_seed_maintenance` 3 小时一轮、单轮 10+ 分钟；串进同一条链会把上游拖慢（2026-11-01 合并作业踩过：① 的新鲜度 30 分钟被拖到 3 小时） |
| **速率是时间函数** | `drain_task_queue` 是「退避到期」的时间轮询，不是数据函数 |
| **已固化的契约** | `scheduler_job_settings.cron` + 页面 `CronBuilder` 是 2026-10-24 定的「配置全落库、跨重启保持」 |

所以只做**触发层**的增强：依赖仍然是声明式的（一行字段），执行仍然各跑各的。

---

## 2. 辅种域的数据流（落地后的样子）

```
                    ┌──────────────────────────────┐
站点信息获取 ──时魔──▶│                              │
                    │  免费种推送与监测             │──▶ 下载器
站点拉取 ──种子─────▶│  （cron 10 分钟心跳：         │
                    │    监测每轮跑；                │
                    │    推送内部节流 ≥30 分钟）     │
                    └──────────────┬───────────────┘
                                   │ 入队 task_queue
                                   ▼
                        任务队列排空（兜底重试，5 分钟轮询）

辅种维护（旁支：巡查 → 磁盘流转 → 孤儿扫描，3 小时）

> 合并前这两件事是两个作业（推送 / 监测），由一条 `depends_on` 边串起来；
> 合并后它们共用一条时间轴，边不再需要（见文首「实施后变更」）。
```

- **依赖边**（`depends_on`）：**当前无**（合并后不再需要，见文首）。
- 无依赖：`refresh_user_data`（推送每轮自读时魔）、`drain_task_queue`（时间轮询）、
  `push_and_monitor_free`（cron 心跳 + 内部节流）、`cross_seed_maintenance`（旁支）。

### 依赖边的语义是「触发」，不是「正确性」

push 不依赖本轮 fetch **成功**：站点拉取失败时它照样能推库里已有的候选。
所以边只表达「有新数据了就醒一下」，不是「没新数据就不许跑」。

---

## 3. 关键决策：cron 必须改成兜底，否则这条边是空操作

**踩坑点**：若保留 push 的 `*/30 * * * *`、只把 `min_trigger_interval_sec` 设成 1800，
上游触发**完全不生效**——fetch 在 12:10 成功，push 上次 12:00 跑过，要等到 12:30
才够 30 分钟，而 12:30 正是 cron 自己会醒的时刻。收益为零。

**正确配法**：把 push 的 cron 拉长成兜底，节奏交给最小间隔。

> ⚠️ **本节是「合并前」的方案**：`push_free_torrents` 这个作业名**已不存在**
> （2026-11-22 与 `monitor_free_window` 合并为 `push_and_monitor_free`），
> 下表记录的是**当时的设想**，当前实现见文首「实施后变更」与
> `app/services/job_registry.py:DEFAULT_CRON`。

| 参数 | 值 | 作用 |
|---|---|---|
| `DEFAULT_CRON["push_free_torrents"]`（**旧作业名，已移除**） | `0 */2 * * *` | **兜底**：fetch 连续失败时，push 最多停 2 小时 |
| `JobSpec.min_trigger_interval_sec` | `1800` | **节奏**：两次推送之间至少 30 分钟（≈ 现状频率） |
| `JobSpec.depends_on` | `"fetch_all_sites"` | 上游成功后触发判定 |

⚠️ **门槛基准用「上次*开始*时间」，不是结束时间**。用结束时间会让实际周期漂移成
「门槛 + 上一轮耗时」（30 → 33 分钟/轮）；用开始时间周期稳定在 30 分钟，而长作业
撞上触发时会被 `_running` 自然挡掉（下一次拉取再触发）——是**背压**，不是重复。

效果：**推送频率不变（≈30 分钟）**，但触发时刻对齐到「拉取刚完成」——
「新种子入库 → 推送」的最大延迟从 ~30 分钟降到 ~10 分钟（一个拉取周期）。
免费窗口有时效（`monitor` 的 pre_pause 只有 10 分钟），这段延迟是实打实的收益。

---

## 4. 触发规则（判定顺序）

```
cron 到点            → 跑（trigger="cron"）
上游 X 本轮成功结束   → 对每个 depends_on==X 的作业 Y：
                         Y.enabled            ？ 否 → 跳过
                         Y 正在运行            ？ 是 → 跳过（不排队，不堆积）
                         now - Y.上次开始 ≥ Y.min_trigger_interval_sec ？ 否 → 跳过
                         调度器 accepting_new_work ？ 否 → 跳过
                         全部通过 → asyncio.create_task(Y, trigger="upstream")
上游 X 本轮失败       → **不触发**下游（不级联失败，下游等自己的 cron）
```

护栏：

- **只用现成的 `_running` 集合挡重入**（cron 与手动触发已共享它）。
- **只在成功时触发**——失败级联会把一次站点 502 放大成整条链的红。
- **不递归**：上游触发下游时，下游的成功不再回头触发上游（边单向，注册期做环检测）。
- **重启后的首个上游成功会补跑一次 push**：`_last_finished_at` 是进程内状态，重启后
  为空 → 视为「从未跑过」，允许立即触发。这是**刻意**的（新进程尽快恢复推送），
  且幂等（`task_queue` 的 `idem_key` 保证已推过的种不会重推）。
- `job_runs.trigger` 增 `"upstream"`，历史里能区分「被上游喂的」与「自己到点的」。

---

## 5. 会不会重复执行 / 密集执行

结论：**作业级不会重复，频率不变**；数据级三层幂等兜底。逐条拆开：

### 5.1 作业级不重复（三道闸）

| 闸 | 机制 | 为什么够 |
|---|---|---|
| 同作业不重入 | `SchedulerCenter._running` 集合 | `if name in _running: return` 与 `add` 之间**没有 await**，单事件循环下是原子的。cron / 上游触发 / 手动触发**共享同一集合** |
| 最小间隔 | `_last_started_at[name]` + `min_trigger_interval_sec` | 从「上次开始」算，30 分钟内上游触发一律跳过 |
| 只在成功时触发 | `_ok` 为真才走 `_trigger_downstream()` | 一次站点 502 不会级联成整条链的红 |

补充：上游触发**不写库、不发请求**——只是读一个内存时间戳做比较，所以 fetch 每 10 分钟
成功一次也不会带来任何额外开销。

### 5.2 频率上界：与现在相同

| 触发源 | 上界（次/小时） | 说明 |
|---|---|---|
| cron 兜底 | 0.5 | `0 */2` |
| 上游触发 | ≤2 | fetch 每小时 6 次成功，但 30 分钟门槛只放行 2 次 |

合计 ≤2 次/小时 = **现在的 `*/30` 完全一样**，只是相位从「整点/半点」变成「拉取完成后」。
最坏情况（进程重启后首个 fetch 成功想补跑，且 cron 恰好同分钟）：`_running` 挡住其中一个，
仍然只跑一次；补跑之后门槛立刻生效。

### 5.3 数据级：即使两个 push 重叠也不会重复推

| 层 | 机制 |
|---|---|
| 入队 | `task_queue.enqueue` 的 `idem_key = add_torrent\|{site}\|{infohash}\|dl={dl}`：命中即 `created=False`，push 记「幂等跳过」 |
| 认领 | `claim_with_retry` 见 `pending` 才认领；失败说明被别的执行者抢走 → 记「被其他执行者认领」并跳过 |
| 执行 | `execute_add_torrent` 内部还做索引行 `push_status` 短路（已推过直接返回） |

### 5.4 一处**既有**的窄窗口（与本次改动无关，可选加固）

`TaskQueueService.claim_one` / `claim_due` 是「先读 status → 再改 → flush」，不是
`UPDATE ... WHERE status='pending'` 的条件更新。因此理论上存在丢更新：`drain_task_queue`
（每 5 分钟）先 SELECT 到某行 pending，随后 push 就地认领并提交，drain 再把它改成
running → 双方都执行同一 task。

- **窗口极小**：需要 drain 的 SELECT 与 push 的「入队→认领」落在同一毫秒级窗口，且 drain
  每 5 分钟才醒一次。
- **后果有限**：qB / TR 按 infohash 去重，重复 add 只会多一次 HTTP 调用，不会产生两个种子。
- **建议**（独立小改动，可并入 Step 1）：两处改成条件更新——
  `UPDATE task_queue SET status='running', attempts=attempts+1 WHERE id IN (...) AND status='pending'`，
  按 `rowcount` 判定认领成功。改完 `test_task_queue` 补一条「并发认领只有一个成功」。

### 5.5 用户改密 cron 时的语义

`min_trigger_interval_sec` **只约束上游触发，不约束 cron**。用户把 push 的 cron 改成
`*/5 * * * *`（DB 覆盖优先）就会真的 5 分钟推一次——那是用户显式表达「我要这个频率」，
不该被门槛悄悄压掉。反过来若想要「无论如何最少 30 分钟」，把 cron 留在兜底位即可。


---

## 6. 改动清单

### 后端

| 文件 | 改动 |
|---|---|
| `app/services/scheduler.py` | `JobSpec` 增 `depends_on: str \| None` / `min_trigger_interval_sec: int`；`SchedulerCenter` 建 `_downstream` 索引 + `validate_graph()`（**在 `register_jobs` 末尾统一校验**：上游存在、无环——逐条注册时校验会因注册顺序误报）；把 `_wrap()` 里的执行体抽成 `_execute(spec, trigger)`（cron 与 upstream 共用，`job_runs.trigger` 才能区分；手动触发 `run_now` 保持独立——它要抛异常）；成功后调 `_trigger_downstream()`；记 `_last_started_at[name]`；日志「上游 X 成功 → 触发 Y（距上次 Nmin ≥ 门槛 Mmin）」 |
| `app/services/job_registry.py` | `push_free_torrents` 增 `depends_on="fetch_all_sites"`、`min_trigger_interval_sec=1800`；`DEFAULT_CRON["push_free_torrents"]` 改 `0 */2 * * *`；模块文档串补「触发关系」一节 |
| `app/api/routes/scheduler.py` | `GET /scheduler` 的 `jobs[]` 增 `depends_on`（前端画链路用；无依赖为 `null`） |

**不动**：`scheduler_job_settings` 表结构、`apply_overrides`、`update_cron`、
`runtime_params`（限速参数 `push_download_interval_ms` / `push_max_per_run` 保持原样，
它们管的是「单轮内怎么推」，与「多久醒一次」正交）。

### 前端

| 文件 | 改动 |
|---|---|
| `web/src/types.ts` | `SchedulerJob` 增 `depends_on?: string \| null` |
| `web/src/pages/Scheduler.vue` | **辅种 tab** 渲染成链路：`站点拉取 → 免费种推送`，边上标「成功后触发 · ≥30 分钟」；其余节点按「旁挂」分组（站点信息获取 / 免费窗口监测 / 任务队列排空 / 辅种维护）。**媒体库 tab 不变**（仍是并列清单，无依赖） |

链路视图不引入图形库：一个节点列 + `→` 与旁挂分组即可（移动端也不会崩）。

### 测试

| 文件 | 用例 |
|---|---|
| `tests/unit/test_scheduler.py` | ① 上游成功 → 下游被触发（`trigger="upstream"`）② 上游失败 → 不触发 ③ 距上次 < 门槛 → 不触发 ④ 下游 disabled / 正在运行 → 不触发 ⑤ `depends_on` 指向不存在的作业 / 成环 → 注册期抛错 |
| `tests/integration/test_api.py` | `GET /scheduler` 下发 `depends_on`（辅种域覆盖，媒体库为 `null`） |
| `tests/integration/test_job_settings.py` | DB 覆盖 cron 时，兜底 cron 生效、上游触发仍按 `min_trigger_interval_sec` 判定 |

### 文档

- `CHANGELOG.md`：**Changed** —— `push_free_torrents` 默认 cron 由 `*/30` 改为
  `0 */2 * * *`（改为兜底，节奏由上游触发 + 30 分钟门槛决定）。这是**用户可见的
  节奏语义变化**，需写明「实际推送频率不变，延迟变短」。
- `AGENTS.md`：新增一条「作业触发：cron 兜底 + 数据流触发」的设计契约（含
  「cron 必须显著宽于 min_trigger_interval，否则边是空操作」这个坑）。

---

## 7. 契约与兼容性

### 7.1 数据流触发能力本身（纯新增）

- **DB**：无迁移。`scheduler_job_settings.cron` 仍是「兜底 cron」；用户改过 push 的
  cron 时 **DB 覆盖优先**（现有逻辑不动），此时实际频率 = `max(cron 节奏, 上游触发门槛)`。
- **API**：`GET /scheduler` 只**新增** `depends_on` 字段（向后兼容）。
- **行为**：其他作业完全不受影响（没声明 `depends_on` 就不参与触发）。
- **回滚**：删掉注册表里 `depends_on=` 一行 + 恢复 `DEFAULT_CRON` 即可，无残留状态。

> 上面这四条**只针对 `depends_on` 这套触发能力**。同一版本里还有一处
> **破坏性变更**（三个旧作业键被移除）——见下一节，别被「无迁移 / 向后兼容」
> 这句带偏。

### 7.2 BREAKING CHANGE：三个旧作业键被移除（v2.1.0 起立即失效）

| 旧作业键（已移除） | 新作业键 | 备注 |
|---|---|---|
| `refresh_user_data` | `fetch_all_sites` | 职责并入第二段「站点信息获取」 |
| `push_free_torrents` | `push_and_monitor_free` | 与监测合并；启停取 AND |
| `monitor_free_window` | `push_and_monitor_free` | 同上（两个旧名 → 一个新名） |

**旧键的调用行为**（不是所有端点都返回 404，脚本要按端点区分）：

| 端点 | 旧作业键的响应 |
|---|---|
| `POST /scheduler/{job}/toggle` | **404**，detail `作业不存在：{job}` |
| `POST /scheduler/{job}/cron` | **404**，detail `作业不存在：{job}` |
| `GET /scheduler/{job}/config` | **404**，detail `作业不存在：{job}` |
| `POST /scheduler/{job}/run` | **HTTP 200** + `ok=false` + `error="unknown job: {job}"`（手动触发走 `run_now`，对未知作业名不抛 HTTP 异常——**脚本必须看 `ok` 字段**，只看状态码会把失败当成功） |

`GET /scheduler` 的 `jobs[]` 里不再出现这三个名字（同时多了 `module` 等新字段，
旧客户端忽略未知字段即可）。

**受影响调用方**：任何按作业名调用 `/scheduler/{job}/…` 的脚本与外部工具；
面板「定时任务」页（前端已在本 PR 内适配）。

**`scheduler_job_settings` 里的旧行**：

- `push_free_torrents` + `monitor_free_window` → 启动期合并成 `push_and_monitor_free`
  一行（**启停取 AND**：任一边被用户停用过，新作业也停用；**cron 不搬** → 走
  代码默认值）；
- `refresh_user_data` → 作业已下线，旧行按「孤儿清理」删掉；
- 迁移是**不可逆 DELETE**：删除前把旧行内容**持久化到快照表
  `scheduler_job_settings_backup`**（与 `DELETE` 同一事务 → 要么都成、要么都
  回滚，绝不出现「删了却没记录」；三条删除路径——合并 / 孤儿清理 / 改名迁移
  ——都留），并打一条启动日志（`作业配置删除快照（…）`，含 `job_name` /
  `enabled` / `cron`）。**快照表才是恢复依据**（日志会轮转、随重建而丢）；
  还原 SQL 见 [`CHANGELOG.md`](../CHANGELOG.md) 的「回滚 / 恢复」。

**迁移步骤**（外部脚本，升级镜像**前**改完）：

1. 调 `push_free_torrents` / `monitor_free_window` 的地方改成
   `push_and_monitor_free`（两者行为已合并：监测每轮都跑，推送由作业内部节流到
   ≥30 分钟一次）；
2. 调 `refresh_user_data` 的地方改成 `fetch_all_sites`（该作业现在多一段
   「站点信息获取」，单轮耗时变长）；
3. 手动触发按 `ok` 字段判成败（见上表）。

**版本与弃用**：自 **v2.1.0** 起立即生效、**无兼容期**（面板自托管、前后端同版本
发布）；完整映射表、受影响调用方与镜像 tag 见
[`CHANGELOG.md`](../CHANGELOG.md) 的「Versioning & Deprecation」小节。

---

## 8. 验收标准

1. 单测 5 条全绿；`pytest tests/unit/test_scheduler.py tests/integration/test_api.py -k scheduler`。
2. 真机（dev 8092）：手动跑一次 `fetch_all_sites` → 历史里立刻出现 `push_free_torrents`
   的 `trigger=upstream` 记录；30 分钟内再跑 fetch **不会**重复触发 push。
3. 前端：辅种 tab 出现「站点拉取 → 免费种推送」的链路与门槛标注；媒体库 tab 无变化。
4. 手机端（390px）链路视图不挤压。

---

## 9. 实施顺序（每步可独立验证）

| 步 | 内容 | 验证 |
|---|---|---|
| 1 | 调度器能力（字段 + 索引 + 环检测 + 触发 + 单测） | `pytest tests/unit/test_scheduler.py` |
| 2 | 注册表接线（push 的边 + 兜底 cron） | 单测 + 启动日志显示生效 cron `0 */2` |
| 3 | API 字段 + 前端链路视图 | `vue-tsc -b` + 截图 |
| 4 | 文档（CHANGELOG / AGENTS）+ 重建 dev 镜像验证 | 真机验收 1–4 |

---

## 10. 明确不做（留待以后）

- **媒体库域**不进数据流：它的作业是日历位（每天 03:47 / 每小时第 7 分），
  彼此无数据依赖，改成 interval 只会失真。
- **`cross_seed_maintenance`** 不进链：内部已是流水线（巡查 → 流转 → 孤儿），
  外部只有「3 小时」这一个节奏，串出去只会被拉取周期绑架。
- **`drain_task_queue`** 不挂边：它是时间重试通道（退避到期），不是数据函数。
- **`push_and_monitor_free`** 不挂边：合并后 cron 心跳（10 分钟）已比推送门槛
  （30 分钟）更细，挂边是永不生效的死边；推送节奏由作业内部节流表达。
- **不引入 pipeline 引擎 / 不改 `job_runs` 结构**：一条链路每个节点仍是独立运行记录，
  前端用 `trigger` 字段区分来源即可。

### 如果以后确实要「一条流水线一条历史」

那需要 `cron → interval` 的迁移（breaking）：

1. `scheduler_job_settings` 增 `interval_sec` 列（保留 `cron` 列不动）；
2. 一次性迁移：把已有 `cron` 换算成最小间隔（`*/10` → 600、`5,15,…` → 600、
   `47 3 * * *` → 86400），写入 `interval_sec`，标记防重跑；
3. 页面 `CronBuilder` 换成「间隔 + 最小间隔」双控件（或按域切换：辅种用间隔、
   媒体库留 cron）；
4. `job_runs` 增 `parent_run_id`，前端历史按 pipeline 折叠。

工作量约是本方案的 3–4 倍，收益主要是「一次运行的完整视图」——
而后者用 `trigger` + `JobStep.children` 已经能近似表达。
