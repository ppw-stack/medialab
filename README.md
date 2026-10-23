# PT SeedHelper

> 辅种自动化、免费种窗口看护与种子健康度监测。Python 3.12 + FastAPI + SQLite，Docker 一键部署。

## 现状

**M1 基础设施 + 核心功能**已落地：

- **M1**：项目骨架、配置中心、SQLite WAL + 短事务约定、下载器/站点双适配层、日志与审计、FastAPI 应用 + `/healthz` + `/metrics` + 完整 REST 骨架
- **F1 站点拉取**：9 站适配器（mteam / hdsky / hhanclub / audiences / hdhome / hddolby / ourbits / soulvoice / springsunday）拉取免费种 + RSS 清理
- **F2 免费种窗口**：推送 + 监测 + 中止（push_free_torrents / monitor_free_window / FR-3.5 链路）
- **FR-1.6 站点用户数据契约**（2026-10-18，2026-10-22 加时魔）：单方法 `get_user_snapshot` 拉 4 业务字段（上传/下载/做种数/下载中数）+ **时魔**（每小时获得的站点积分·合计口径，含是否封顶），刷新作业 `*/30 * * * *`（并发抓取，失败计入 failed 不写 0），Vue UI（`/user-data` 列表 + Sites drawer），API 端点 `/api/v1/user-data` 与 `/api/v1/sites/{id}/user-data`
- **FR-3.6 站点每日签到**（2026-10-22）：`daily_checkin` 作业（cron `37 8 * * *`）对 **4 站**自动签到 —— hhanclub / soulvoice / hdhome（NexusPHP `/attendance.php`）+ hdsky（自建 `showup.php`，**图形验证码走 AI 读码**，`app/core/ai.py`）。其余 5 站不支持（无此功能或需 Cloudflare Turnstile，后者实测服务端无法完成）

`F2 辅种引擎` / `F4 巡检` / `F5 清理` 自 **M2** 起实施。

## 文档索引

| 文档 | 内容 |
|---|---|
| `README.md` | 项目功能与快速开始（本文） |
| `DEPLOY.md` | Docker 双环境部署流程 |
| `LOCAL_ENV.md` | **本机 / prod 环境实况**：主机、工具链、端口、API 路径、凭据获取、部署链路 |
| `AGENTS.md` | AI / 协作者行为规范 |
| `docs/architecture-debt.md` | **架构债清单**：2026-10-21 全代码审查的发现（Phase 1~4 的输入） |

## 快速开始

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，填入面板凭据

# 2. 启动
docker compose up -d

# 3. 验证
curl http://localhost:8080/healthz
curl http://localhost:8080/metrics | head
```

## 开发

```bash
# 安装依赖（Python 3.12，由 uv 管理）
uv sync --extra dev

# 静态检查
uv run ruff check app/ tests/
uv run mypy app/

# 后端测试
uv run pytest
uv run pytest --cov=app          # 带覆盖率（当前基线 58%）
```

### 测试

```
tests/
├── conftest.py           # 基础设施：独立 DB / respx 拦截真实网络
├── unit/                 # 纯函数、无 IO
│   ├── test_timeutils.py       # 北京时间基准
│   ├── test_auth.py            # PBKDF2 + session 签名
│   ├── test_bencode.py         # .torrent → infohash
│   ├── test_window_parser.py   # 免费窗口判定（业务规则核心）
│   ├── test_scheduler.py       # 调度中心
│   ├── test_task_queue.py      # 崩溃恢复 / 幂等 / 退避
│   └── test_site_adapters.py   # 9 站契约（含「拉取失败必须抛错」）
└── integration/          # DB / 作业 / API
    ├── test_db_models.py
    ├── test_jobs.py            # 站点拉取（失败站统计）
    ├── test_push_free.py       # 免费种推送（选种白名单）
    └── test_api.py
```

测试用**独立临时 DB**，不会碰 `/app/data` 或 `data-dev/`；所有 HTTP 请求
必须用 respx mock，未 mock 的请求会直接失败（不会误拉真实 PT 站）。

### CI 闸门

`.github/workflows/ci.yml` 在每次 push / PR 上跑：

| job | 检查 |
|---|---|
| backend | `ruff` + `mypy` + `pytest` |
| frontend | `vue-tsc` + `oxlint` |

**全绿才允许合并。** Playwright e2e 需要真实下载器与真实站点，
CI 上跑不了也不跑（需在本机起 dev 容器后执行）。

## 架构

```
UI (Web 管理面板 / M5+)
  ↓ REST + WebSocket
核心服务层 (FastAPI)
  ├─ SeedEngine (M2)        ├─ FreeSeedHunter (M4)
  ├─ Inspector (M3)         ├─ Cleaner (M5)
  └─ Alerter (M3)
  ↓                          ↓
DownloaderAdapter          SiteAdapter (FR-1.5)
(qBittorrent/Trans/Deluge/  (GenericSiteAdapter,
 aria2 — 同接口降级)         永久免费优先 R10)
  ↓                          ↓
SQLite (WAL + 短事务约定, §8)
```

下载器与站点的差异全部收拢在适配层，引擎只面向统一接口编程。详见 `app/adapters/`。

## 关键约束（自 v1.2 评审）

- **限时窗口到点必停**：限时免费种临近 `torrent_index.free_end_at` 即自动暂停（`monitor_free_window` 作业，FR-3.4 巡检链路），不存在豁免。
- **数据安全第一**：删除 hash 必须是 40 位 hex（拒绝 `all` / 通配），前端二次确认，删磁盘数据独立开关并记审计日志。
- **monitor 命中后可删可停**：`monitor_delete_expired` 运行参数（0=仅暂停，默认；1=删任务+删磁盘数据）。两种动作判定范围**完全一致**；删除模式仍跳过已完成与已暂停的任务，并保留 `torrent_index` 行以防重推循环。
- **枚举参数用单选按钮组**：0/1 这类「在几个具名行为间二选一」的运行参数，前端渲染单选按钮组而非数字输入框（`ParamSpec.options` 声明取值）。
- **删站点 = 级联删关联数据**：拉取索引（`torrent_index`，可重建）+ 用户数据（`site_user_data`）+ 该站推送队列行（`task_queue`）在同一事务里删完再删站点行。不用"引用计数 > 0 就拦住"——拉取快照每 10 分钟就重新灌满，拦住等于永久删不掉。
- **永久免费绝不挂看护**：识别为 `freeleech_permanent` 的任务直接转辅种池（R10）。

## 目录

```
app/
├── core/          # 配置、时间、日志、指标、窗口核心
├── adapters/      # 下载器 + 站点双适配层（FR-1 / FR-1.5）
├── db/            # SQLAlchemy 2.0 async + WAL（session / migrations / bootstrap）
├── services/      # 调度中心、业务引擎（M2+）
├── api/           # FastAPI 路由
└── main.py        # 应用入口 + lifespan
```

## 安全

请**不要**把 `.env` / 数据库备份加入 git。