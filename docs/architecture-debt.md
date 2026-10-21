# 架构债清单

2026-10-21 全代码审查的发现。Phase 0（测试网 + CI 闸门）已完成，
本清单是 Phase 1~4 的输入。

**架构本身是对的**：依赖矩阵是干净的单向链，无循环 import：

```
core → db → adapters → services → api
```

`ruff` + `mypy --strict` 全绿（63 个文件）。问题在**局部实现漂移**，
不在分层选型。

---

## Phase 1：真缺陷（7 项）

### 1.1 生产拉取路径没有退避 / 重试 / 熔断

`app/services/feed_fetcher.py` 整套指数退避 + 抖动 + `BreakerTripped`
熔断机制齐全，但**只被 `/sites/{id}/feed-test` 诊断端点用**。

生产路径 `app/services/jobs.py:_pull_site_items` 直接裸调
`adapter.search_torrents`：9 站连拉、无间隔、无重试。
而 `push_free` 反倒有 `push_download_interval_ms` 限频保护——
**限频层没接进主链路**（R2 账号风险）。

处置：把 `feed_fetcher` 的退避/熔断/限频能力接回主拉取路径，
或按「单站最小间隔」抽成 runtime param。

### 1.2 `BreakerTripped` 抛了没人接

`app/adapters/sites/mteam.py:294`、`hdhome.py:344` 在 429/403 时抛熔断异常，
但 `jobs.py` 只 `except Exception` 当普通失败——`base.py` 承诺的
「熔断 1 小时」从未发生。

处置：`jobs.py` 显式捕获 `BreakerTripped`，该站本轮跳过并记入熔断状态。

### 1.3 手推 / 自动推两条链路分叉

`app/api/routes/torrent_index.py:290-405` 手工重写了一遍
`task_executor.execute_add_torrent` 的完整流程（下载 → 解 hash →
add → 标 pushed），连 TOCTOU 防御
（`or_(IS NULL, != PUSHED)`）都复制了一份；手推还不走 `task_queue`
（无崩溃恢复 / 幂等键 / 退避）。

处置：手推改为入 `task_queue`，复用 `execute_add_torrent`。

### 1.4 `FreeType.NEUTRAL` 语义自相矛盾

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

### 1.6 「单写者队列」是名义上的

`app/db/session.py` 的 `SingleWriterQueue.write()` **全仓零调用**。
所有写路径走 `session_scope()`。README / §8 承诺的串行化没落地。

处置：要么让写路径真的走队列，要么把「单写者队列」从文档里删掉
（当前实际靠「短事务 + 不在事务内做网络 IO」的约定保证安全）。

### 1.7 `app/db/session.py` 579 行混 4 种职责

engine 管理 + 22 条裸 `ALTER` + 200 行 datetime→北京时间迁移 +
discount 回填 + 队列类。

- 22 条 ALTER 全被 `suppress(Exception)` 包着——真失败（如列不存在）
  会被静默吞掉
- `pyproject.toml` 的 ruff 配置引用了不存在的 `app/db/migrations/**`
  目录，无 Alembic

处置：拆成 `app/db/engine.py` + `app/db/migrations.py` + `app/db/queue.py`；
或引入 Alembic。

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

## 顺带记录：Phase 0 中修掉的问题

| 问题 | commit |
|---|---|
| `create_app(settings=...)` 注入失效（`cache_clear()` 不是注入） | `b3c0e7c` |
| 运行时参数初始化被 `scheduler_enabled` 绑死 → API 500 | `e5c83e6` |
| `EnqueueResult.existing_status` 注释与实现不符 | `74f1d06` |
| `_BACKOFF_CAP_MINUTES` 注释与实现不符（且 30 上限不可达） | `74f1d06` |
