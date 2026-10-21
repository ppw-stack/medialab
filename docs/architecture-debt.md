# 架构债清单

2026-10-21 全代码审查的发现。Phase 0（测试网 + CI 闸门）与
Phase 1（修真缺陷）已完成，本清单是 Phase 2~4 的输入。

**架构本身是对的**：依赖矩阵是干净的单向链，无循环 import：

```
core → db → adapters → services → api
```

`ruff` + `mypy --strict` 全绿。问题在**局部实现漂移**，不在分层选型。

---

## 进度总览

| 项 | 状态 | commit |
|---|---|---|
| 1.1 拉取路径无退避/重试 | ✅ 已修 | `4e48619` |
| 1.2 熔断没人接 | ✅ 已修 | `fddac72` `e8eab3c` `49f81e3` |
| 1.3 手推/自动推分叉 | ✅ 已修 | `a2b3cd0` |
| 1.4 NEUTRAL 语义矛盾 | ✅ 已修 | `4aeea82` |
| 1.5 站点名单 4 处 | ✅ 已修 | `69dcc58` |
| 1.6 单写者队列名实不符 | ✅ 已修 | `87fd5bd` |
| 1.7 db/session.py 混职责 | ✅ 已修 | `87fd5bd` |
| 2.1 抽站点公共基类 | ❌ **已决定不做** | — |
| 2.2 default_downloader 零调用 | ⏳ 待做（Phase 2） | — |
| 2.3 core→db 逆向依赖 | ⏳ 待做（Phase 2） | — |
| 2.4 API 层漏业务逻辑 | ⏳ 待做（Phase 2） | — |
| 3. 死代码 | ⏳ 待做（Phase 3） | — |
| 4. 前端类型分散 | ⏳ 待做（Phase 4） | — |

---

## Phase 1：真缺陷（7 项）

### 1.1 生产拉取路径没有退避 / 重试 / 熔断 ✅

`app/services/feed_fetcher.py` 整套指数退避 + 抖动 + `BreakerTripped`
熔断机制齐全，但**只被 `/sites/{id}/feed-test` 诊断端点用**。

生产路径 `app/services/jobs.py:_pull_site_items` 直接裸调
`adapter.search_torrents`：9 站连拉、无间隔、无重试。
而 `push_free` 反倒有 `push_download_interval_ms` 限频保护——
**限频层没接进主链路**（R2 账号风险）。

**已修**：`feed_fetcher` 新增 `fetch_pages()`（分页 + 退避重试），
`jobs.py` 改用它。`fetch()` 保持单页语义不变（诊断端点零影响）。

> **注意**：不复活 `min_interval_minutes`——`2a08bfd` 明确否决过
> （「用户决定：删掉字段而非接回」）。本改动只在**失败后**重试，
> 不改变拉取频率。

### 1.2 `BreakerTripped` 抛了没人接 ✅

`app/adapters/sites/mteam.py:294`、`hdhome.py:344` 在 429/403 时抛熔断异常，
但 `jobs.py` 只 `except Exception` 当普通失败——`base.py` 承诺的
「熔断 1 小时」从未发生。

**已修**（三层）：
1. 8 个 HTML 站的 `search_torrents` 拆出 403/429 → 抛 `BreakerTripped`
   （此前一律 `>= 400` 抛 `SiteFetchError`，限流与页面变更混为一谈）
2. `sites` 表加 `breaker_until` 列（熔断是站点级状态，`feeds` 表已下线）
3. `jobs.py` 捕获熔断 → 写状态、下一轮跳过且不发请求
   （计入 `skipped_breaker`，**不进 `failed_sites`**——保护性跳过不是故障）

熔断时长：429 → 1 小时，403 → 30 分钟（403 可能是 cookie 失效非限流）。

### 1.3 手推 / 自动推两条链路分叉 ✅

`app/api/routes/torrent_index.py:290-405` 手工重写了一遍
`task_executor.execute_add_torrent` 的完整流程（下载 → 解 hash →
add → 标 pushed），连 TOCTOU 防御
（`or_(IS NULL, != PUSHED)`）都复制了一份；手推还不走 `task_queue`
（无崩溃恢复 / 幂等键 / 退避）。

**已修**：
- 新增 `app/services/push_service.py`——手推改为「入队 + 同步等待」，
  与自动推**同一条队列 + 同一个执行器**（用户点了仍拿到同步结果；
  任务已落库，进程崩了 `drain_task_queue` 会接手）
- `task_executor.execute_add_torrent` 补上 TOCTOU 防御（两条链路都受保护）
- 路由只留参数校验 + 结果转换（405 → 294 行）

**顺带修一个并发 bug**（新写的并发测试暴露的）：
`task_queue.enqueue` 的 `IntegrityError` 分支直接查询同一 session，
但 flush 失败后 session 处于「事务已回滚」状态 → 抛
`PendingRollbackError`。并发入队同键时必现。已加 `rollback()`。

### 1.4 `FreeType.NEUTRAL` 语义自相矛盾 ✅

- `app/db/models.py:62` 把 NEUTRAL 放进 `NON_ABORTABLE_FREE_TYPES`
  → `app/core/windows.py:is_permanent` 返回 True
- `app/services/monitor.py:219` 把 NEUTRAL 当「非免费 → 立即暂停」

同一枚举两套语义。当前没炸是因为 `is_actionable` 无人调用，属潜伏雷。

处置：明确 NEUTRAL 到底属不属于「永久免费」，二选一后统一。

### 1.5 站点名单维护在 4 处

- `app/adapters/sites/_registry.py` 的 `_BUILTIN`（有启动期一致性校验）
- 同文件 `_META`（有校验）
- `app/api/routes/sites.py` 的两个 `Literal[...]`（**无校验**）

加一个站点要改 4 处，漏改 API 不会报错。

处置：API 层改为从 `_registry` 动态生成（`Literal` 改 `str` + 运行时校验）。

### 1.6 「单写者队列」是名义上的 ✅

`app/db/session.py` 的 `SingleWriterQueue.write()` **全仓零调用**。
所有写路径走 `session_scope()`。README / §8 承诺的串行化没落地。

**已修**（改文档，不加队列）：SQLite 的单写者限制只在**多进程**下
才成问题——本项目是单进程单 worker（Dockerfile `--workers 1`），
真正管写安全的是「短事务 + 不在事务内做网络 IO」的约定。
`SingleWriterQueue` → `DbBootstrap`（职责收窄为建表 + 迁移编排），
`write()` 删除，README / DEPLOY.md 的「单写者队列」措辞一并修正。

### 1.7 `app/db/session.py` 579 行混 4 种职责 ✅

engine 管理 + 22 条裸 `ALTER` + 200 行 datetime→北京时间迁移 +
discount 回填 + 队列类。

- 22 条 ALTER 全被 `suppress(Exception)` 包着——真失败（如列不存在）
  会被静默吞掉
- `pyproject.toml` 的 ruff 配置引用了不存在的 `app/db/migrations/**`
  目录，无 Alembic

**已修**：
- 拆成 `app/db/session.py`（131 行，只管 engine 生命周期）+
  `app/db/migrations.py`（383 行，幂等迁移）+
  `app/db/bootstrap.py`（197 行，建表 + 编排）
- 22 条 ALTER 按类型分级处理失败（add_column / drop_column /
  rename_column / create_index），真失败不再静默
- **不引入 Alembic**（async 适配成本高，现有迁移已跑通；
  表结构改动超过 10 次再评估）

---

## Phase 2：重复（~437 行逐字重复 + 若干处散落）

### 2.1 站点适配器 9 站 5411 行里的逐字重复

```
_parse_cookies      × 8    _base_url / _detail_url / _download_url  × 7
__init__            × 5    check_reachability                      × 7
check_status        × 4(+2) close                                  × 9
get_user_snapshot   × 7    (7 站 15 行几乎一模一样)
```

`hdsky` ↔ `ourbits` 的 jaccard 相似度 **0.75**，`hddolby` ↔ `soulvoice` **0.71**。
8 个 NexusPHP 站里只有 `mteam` 是 API 系。

> **2026-10-21 决定：不抽公共基类**，9 站保持独立文件，重复代码接受现状。
> 本项**已关闭**。

### 2.2 「查默认下载器」散在 5 处

`app/services/factory.py:default_downloader()` 存在但**零调用**。
同样的查询散在 `monitor.py` / `push_free.py` / `torrent_index.py` /
`downloaders.py` / `factory.py`。

处置：统一走 `factory.default_downloader()`。

### 2.3 唯一的层级逆向依赖

`app/core/windows.py` → `app/db/models.py`（core 不该依赖 db）。

`FreeType` / `FreeSource` / `PushStatus` 是**领域枚举**却住在 `db/models.py`。

处置：抽 `app/domain/enums.py`，`db/models.py` 与 `core/` 都从那里导入。

### 2.4 API 层漏业务逻辑

40 处 `session_scope` 直写 SQL；
`app/api/routes/torrent_index.py` / `sites.py` 把业务逻辑漏进了路由层
（`torrent_index.py` 的手推流程就是 1.3 的根因）。

---

## Phase 3：死代码

| 对象 | 状态 |
|---|---|
| `TaskKind` 8 个成员 | 只有 `ADD_TORRENT` 在用，其余 7 个死 |
| `PushStatus.PENDING` | 从未写入（NULL 已表示「未推过」） |
| `FeedItem.link` | 全仓零读取 |
| `SiteAdapter.download_torrent_url()` | 零调用 |
| `SingleWriterQueue.write()` | 零调用（见 1.6） |
| `factory.default_downloader()` | 零调用（见 2.2） |

---

## Phase 4：前端

- 20+ 个 `interface` 散落在各 `.vue` 里，无共享 `types.ts`
  （`SchedulerInfo` 等已重复定义）
- `web/src/api.ts` 有 28 处 `api.get<T>` / `api.post<T>`，类型全靠手写

处置：抽 `web/src/types.ts`，页面从那里 import。

---

## 顺带记录：Phase 0/1 中修掉的问题

| 问题 | commit |
|---|---|
| `create_app(settings=...)` 注入失效（`cache_clear()` 不是注入） | `b3c0e7c` |
| 运行时参数初始化被 `scheduler_enabled` 绑死 → API 500 | `e5c83e6` |
| `EnqueueResult.existing_status` 注释与实现不符 | `74f1d06` |
| `_BACKOFF_CAP_MINUTES` 注释与实现不符（且 30 上限不可达） | `74f1d06` |
| `task_queue.enqueue` 的 IntegrityError 分支未 rollback → 并发入队抛 PendingRollbackError | `a2b3cd0` |
| 22 条 ALTER 被 `suppress(Exception)` 静默吞掉真失败 | `87fd5bd` |
| ruff 配置指向不存在的 `app/db/migrations/**` 目录 | `21951bd` |

---

## 测试与覆盖率

| 阶段 | 用例数 | 覆盖率 |
|---|---|---|
| 起点（Phase 0 前） | 0 | 0% |
| Phase 0 完成 | 276 | 44% |
| Phase 1 完成 | 382 | 48% |

覆盖率门槛在 `pyproject.toml` 的 `[tool.coverage.report] fail_under`，
当前 0（不阻断），**只许升不许降**。补测试后逐步抬高。
