# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `README.md` — 现状与命令。
2. `app/adapters/sites/user_data.py` + `app/adapters/sites/parsers/nexus_php.py` — FR-1.6 站点用户数据契约。
3. `app/adapters/sites/parsers/attendance.py` — FR-3.6 站点签到解析 helper。

## 工作准则
- 改动窗口锚定（FR-3.4）或中止链路（FR-3.5）前，先确认需求基线与现状口径一致。
- 数据删除以"不误删"为底线：hash 必须是 40 位 hex（拒绝 `all` / 通配），前端删除需确认，删磁盘数据单独开关并记审计日志。不引入回收站 / 保护期等未实现的机制。
- 站点适配层改动后用真实站点（测试环境 cookie）拉取验证，不使用 mock 数据测试。
- **自动化 / 合规边界**（2026-10-22 修订，原「不引入未文档化的反爬对抗 / 验证码绕过（R2）」）：
  - ✅ **允许**对**自有账号**做站点自动化，含 AI 读图形验证码（`app/core/ai.py`）、真实浏览器交互
  - ❌ **禁止**伪造 / 破解站点**鉴权凭据**（如 soulvoice 的 JWT/HS256 downhash）、伪造站点签名、越权访问他人资源
  - Cloudflare Turnstile 等**人机验证**本仓库不实现（实测 `TurnstileError 600010`，服务端环境无法完成），相关站返回 `UNSUPPORTED`（需人工）
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
- **FR-3.6 站点每日签到契约**（2026-10-22 加入）：
  - 适配器单方法 `checkin() → CheckinResult`，带状态枚举（success / already / unsupported / auth_expired / failed）——**不用 `None`**：签到必须区分"该站不支持"与"签到失败"，否则作业统计分不清
  - **只实现实测可用的 4 站**：hhanclub / soulvoice / hdhome（走 NexusPHP `/attendance.php`，GET 即签到）+ **hdsky**（自建 `showup.php` 三段式，图形验证码走 AI 读码）。其余 5 站走基类默认 `UNSUPPORTED`（audiences / ourbits 需 Turnstile、springSunday / mteam 无此功能、hddolby 未验证）
  - 失败必须抛 `SiteFetchError` / `BreakerTripped`，与拉取路径同一契约
  - 签到熔断**不写** `sites.breaker_until` —— 不与 10 分钟一次的拉取作业共享熔断状态
  - **不建表**：结果只进 `job_runs.result_json`（`summary` + `steps`），前端「调度 → 历史」可见
  - 解析规则用**真实页面**固化成 fixture（`tests/fixtures/*_attendance.html`）——手写最小 HTML 会漏掉真实文案（hhanclub 成功页**不含**"签到成功"四字，只有统计文案）
  - 契约由 `tests/unit/test_checkin_parser.py` + `tests/unit/test_site_adapters.py` + `tests/integration/test_checkin_job.py` 固化
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
uv run pytest                # 后端测试（607 例）
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
Conventional Commits，作用域示例：`feat(crypto)` / `fix(free-seed)` / `docs(req)` / `refactor(sites)` / `feat(sites)` (FR-1.6 站点 userdata) / `feat(db)` (SiteUserData 表) / `feat(api)` (user_data 端点) / `feat(web)` (UserData.vue) / `feat(scheduler)` (调度作业/配置/历史) / `feat(cross-seed)` (辅种下载器巡查) / `feat(checkin)` (FR-3.6 站点每日签到)。

## 前端测试
```bash
cd web && npx playwright test    # 需先起 dev 容器 (localhost:8080)
```
新增页面/交互改动必须补 `web/tests/*.spec.ts` 用例。

**注意**：Playwright e2e 连的是**真实下载器与真实站点**，CI 上跑不了也不跑
（CI 跑 vue-tsc + oxlint + types-placement；不含 Playwright）。e2e 需在本机起 dev 容器后跑。

