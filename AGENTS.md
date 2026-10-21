# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `README.md` — 现状与命令。
2. `app/adapters/sites/user_data.py` + `app/adapters/sites/parsers/nexus_php.py` — FR-1.6 站点用户数据契约。

## 工作准则
- 改动窗口锚定（FR-3.4）或中止链路（FR-3.5）前，先确认需求基线与现状口径一致。
- 数据删除以"不误删"为底线：hash 必须是 40 位 hex（拒绝 `all` / 通配），前端删除需确认，删磁盘数据单独开关并记审计日志。不引入回收站 / 保护期等未实现的机制。
- 站点适配层改动后用真实站点（测试环境 cookie）拉取验证，不使用 mock 数据测试。
- 不引入未文档化的反爬对抗 / 验证码绕过（R2 / 合规性）。
- **站点适配层保持 9 站独立文件**（2026-10-21 决定）：不抽公共基类，各站独立实现。重复代码接受现状。
- **FR-1.6 站点用户数据契约**（2026-10-18 加入，2026-10-18 精简为单方法）：
  - 不抽 9 站 user_data 基类；NexusPHP 7 站共用 `app/adapters/sites/parsers/nexus_php.py` module-level helper（`parse_user_snapshot`）
  - `SiteUserData` 表是**当前快照**（UNIQUE site_id），不做历史；如需时间序列需先建 `site_user_data_history` 表再迁
  - 适配器单方法默认实现 `get_user_snapshot → None`（未实现 / 拉取失败都返回 None），单站失败由 service 隔离
  - `refresh_user_data` cron `*/30 * * * *`（与 `push_free_torrents` 同分钟位错峰不冲突）
  - 暂不接告警推送；只落库 + UI 展示
- **站点拉取失败必须抛错**（2026-10-21 prod 事故后确立）：
  - `search_torrents` 返回 `[]` = 站点确实没种子；抛 `SiteFetchError` = 故障（DNS / 连接 / HTTP）
  - 两者混用会让作业统计报"全部成功"而实际漏掉故障站（prod 的 hddolby DNS 失败就是这样被掩盖的）
  - 契约由 `tests/unit/test_site_adapters.py` 固化，9 站全参数化覆盖

## 必备命令
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/ tests/
uv run mypy app/
uv run pytest                # 后端测试（465 例）
cd web && npx vue-tsc -b    # 前端类型检查
cd web && npx oxlint .      # 前端 lint
cd web && npx vite build    # 前端构建
```

静态检查不通过禁止合并。**CI（`.github/workflows/ci.yml`）会跑上述全部闸门**：
ruff + mypy + pytest + vue-tsc + oxlint，全绿才允许合并。

## 后端测试
```bash
uv run pytest                        # 全部
uv run pytest tests/unit             # 只跑单元
uv run pytest -k "site_adapters"     # 按名字过滤
uv run pytest --cov=app              # 带覆盖率（当前基线 53%）
```

- 目录：`tests/unit/`（纯函数、无 IO）+ `tests/integration/`（DB / 作业 / API）
- 基础设施在 `tests/conftest.py`：
  - `settings` fixture 注入独立 DB（**不会碰 `/app/data` 或 `data-dev/`**）
  - `db` fixture 每个用例重建 engine + 建表（function 级 —— aiosqlite 连接池绑定创建它的 loop）
  - `no_network` autouse fixture 用 respx 接管 httpx 传输层，**未 mock 的请求直接失败**（不会误拉真实 PT 站）
- 写 HTTP 用例**必须**用 respx mock；m-team 是 API 站需要 JSON 响应，其余 8 站是 HTML
- 测 `updated_at` 这类带 `onupdate` 的列，要用原生 SQL 改值（ORM 会在 flush 时覆盖手动赋值）


## 提交信息格式
Conventional Commits，作用域示例：`feat(crypto)` / `fix(free-seed)` / `docs(req)` / `refactor(sites)` / `feat(sites)` (FR-1.6 站点 userdata) / `feat(db)` (SiteUserData 表) / `feat(api)` (user_data 端点) / `feat(web)` (UserData.vue) / `feat(scheduler)` (调度作业/配置/历史) / `feat(cross-seed)` (辅种下载器巡查)。

## 前端测试
```bash
cd web && npx playwright test    # 需先起 dev 容器 (localhost:8080)
```
新增页面/交互改动必须补 `web/tests/*.spec.ts` 用例。

**注意**：Playwright e2e 连的是**真实下载器与真实站点**，CI 上跑不了也不跑
（`.github/workflows/ci.yml` 只跑 vue-tsc + oxlint）。e2e 需在本机起 dev 容器后跑。

