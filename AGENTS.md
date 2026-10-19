# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `.orca/drops/PT辅种与自动监测系统-项目需求文档.md` v1.2 — 需求基线。
2. `README.md` — 现状与命令。
3. `app/adapters/sites/user_data.py` + `app/adapters/sites/parsers/nexus_php.py` — FR-1.6 站点用户数据契约。

## 工作准则
- 改动窗口锚定（FR-3.4）或中止链路（FR-3.5）前，先看需求文档 §3 / FR-5.2。
- 任何对数据删除的路径都需经 FR-5.2 六重保护（白名单/回收站/路径三重校验/二次确认/保护期/审计）。
- 站点适配层改动后用真实站点（测试环境 cookie）拉取验证，不使用 mock 数据测试。
- 不引入未文档化的反爬对抗 / 验证码绕过（R2 / 合规性）。
- **FR-1.6 站点用户数据契约**（2026-10-18 加入）：
  - 不抽 9 站 user_data 4 方法基类；NexusPHP 7 站共用 `app/adapters/sites/parsers/nexus_php.py` module-level helper（parse_user_info / parse_user_seeding_stats / parse_message_unread / parse_hr_status）
  - `SiteUserData` 表是**当前快照**（UNIQUE site_id），不做历史；如需时间序列需先建 `site_user_data_history` 表再迁
  - 适配器新增 4 方法默认实现（`get_user_info→None` / `get_seeding_stats→None` / `get_message_count→MessageSummary(0)` / `get_hr_status→HrStatus(False, 0)`），单站失败由 service 隔离
  - `refresh_user_data` cron `*/30 * * * *`（与 `push_free_torrents` 同分钟位错峰不冲突）
  - 暂不接告警推送；只落库 + UI 展示

## 必备命令
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/
uv run mypy app/
cd web && npx vue-tsc -b    # 前端类型检查
cd web && npx vite build    # 前端构建
```

静态检查不通过禁止合并。

## 提交信息格式
Conventional Commits，作用域示例：`feat(crypto)` / `fix(free-seed)` / `docs(req)` / `refactor(sites)` / `feat(sites)` (FR-1.6 站点 userdata) / `feat(db)` (SiteUserData 表) / `feat(api)` (user_data 端点) / `feat(web)` (UserData.vue) / `feat(scheduler)` (调度作业/配置/历史) / `feat(cross-seed)` (辅种下载器巡查)。

## 前端测试
```bash
cd web && npx playwright test    # 需先起 dev 容器 (localhost:8080)
```
新增页面/交互改动必须补 `web/tests/*.spec.ts` 用例。

