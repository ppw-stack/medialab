# MediaLab

> 网盘媒体库治理（STRM 生成 / 整理 / 302 中转 / WebDAV / 数据同步）+ PT 辅种自动化
> （免费种窗口看护 / 辅种巡检 / 孤儿清理）。
> Python 3.13 + FastAPI + SQLite + Vue 3，Docker 一键部署。

## 定位

一个应用装下**两个业务域**，共用一套面板、一个 SQLite、一个 SPA。

```
                    ┌──────────────────────────────┐
 播放器 ──/302/…───▶│                              │
 Infuse ──/dav/**───▶│   统一 FastAPI 应用            │
                    │  ┌────────────────────────┐  │
 浏览器 ─────/──────▶│  │ 媒体库域               │  │
                    │  │ /api/*  /302  /dav    │  │
                    │  ├────────────────────────┤  │
                    │  │ 辅种域                 │  │
                    │  │ /api/v1/*  /metrics    │  │
                    │  └────────────────────────┘  │
                    │   统一 SPA（侧边栏两组菜单）    │
                    │   统一 SQLite（19 张表）       │
                    └──────────────────────────────┘
```

**媒体库域的核心主张不变**：媒体字节流**不经过本服务器**。

```
播放器 ──GET /302/<id>/<sig>──▶ 你的服务器（几 KB）
       ◀──302 Location: cdn.115.com/...──┘
       ──GET cdn.115.com/...──▶ 网盘 CDN   ← GB 级流量在此，完全不经服务器
```

## 现状

### 媒体库域（来自 strmmanger）

| Phase | 内容 | 状态 |
|---|---|---|
| P0 | 骨架：配置 / DB 迁移 / 驱动接口 / fake driver / 契约测试 | ✅ |
| P1 | 中转：HMAC 签名 + L1/L2 本地直链缓存 + 单飞 + `/302` | ✅ |
| P2 | 115 驱动：扫码登录 + cookie 加密 + 列目录 + 换直链 | ✅ **已实测** |
| P2b | 光鸭盘驱动：扫码 / 短信登录 + Token 自动刷新 + 列目录 + 换直链 | ✅ **已实测** |
| P3 | 生成：扫描 + STRM 写出 + 幂等 + 孤儿清理 + **附件同步** | ✅ **已实测** |
| P4 | Web 应用：5 个模块 | ✅ |
| P5 | 监控：健康检查 + 批量重签/重建 + 密钥轮换 | ✅ |
| P6 | Docker 部署：多阶段构建 + compose 测试环境 | ✅ |
| P7 | 迁移：115 → 光鸭盘（流式边扫边迁 + 幂等预检 + 失败即跳过） | ✅ **已实测** |
| P8 | WebDAV：wsgidav 协议层 + 多用户 + 限流 + 审计 | ✅ **已实测** |
| P9 | 数据同步：监听本地目录 → 网盘（幂等比对「同名+同大小」/ 覆盖进回收站 / 可选删本地，**空目录一并删**）。**支持哪些网盘按驱动声明**（`UploadDriver` 协议 + `UPLOADABLE_DRIVER_TYPES`），当前已实测：光鸭盘 | ✅ **已实测** |

### 辅种域（来自 ptHelper）

- **M1**：项目骨架、配置中心、SQLite WAL + 短事务约定、下载器/站点双适配层、日志与审计
- **F1 站点拉取**：9 站适配器（mteam / hdsky / hhanclub / audiences / hdhome / hddolby / ourbits / soulvoice / springsunday）拉取免费种 + RSS 清理
- **F2 免费种窗口**：推送 + 监测 + 中止（push_free_torrents / monitor_free_window）
- **FR-1.6 站点用户数据契约**：单方法 `get_user_snapshot` 拉 3 业务字段 + **时魔**，刷新作业 `*/30 * * * *`。数据在**站点列表页**展示（上传/下载、做种、时魔 + 跨站汇总）
- **辅种快照**：`cross_seed_maintenance` 作业（cron `17 */3 * * *`）**定时抓取 → 落库 `cross_seed_snapshot`**，前端读快照而不打下载器接口（该作业还合并了磁盘空间流转与孤儿文件扫描）
- **辅种孤儿文件统计与清理**：把辅种站的下载目录**挂载**进容器，扫出「磁盘有、下载器里已无对应任务」的孤儿文件
- **FR-3.6 站点每日签到**：`daily_checkin` 作业（cron `37 8 * * *`）对 **4 站**自动签到（图形验证码走 AI 读码）

## 架构约束

**依赖矩阵是干净的单向链**，由 `tests/unit/test_layering.py` 固化：

```
domain → core → db → adapters → services → api
```

- `domain` —— 业务概念与常量（`FreeType` / `PushStatus` / `DeviceType`），不依赖任何层
- `api` —— **不碰 DB、不碰适配层**。业务逻辑全在 service 层
- `services` —— **不 import fastapi**，抛 `ServiceError` 由 API 层翻译成 HTTP 状态码
- `adapters` —— 可独立单测（不依赖 db / services）

**两个业务域的 API 前缀天然不冲突**：辅种域 `/api/v1/*`，媒体库域 `/api/*`。

## 文档索引

| 文档 | 内容 |
|---|---|
| `README.md` | 项目功能与快速开始（本文） |
| `DEPLOY.md` | Docker 双环境部署流程 |
| `LOCAL_ENV.md` | **本机 / prod 环境实况**：主机、工具链、端口、API 路径、凭据获取、部署链路 |
| `AGENTS.md` | AI / 协作者行为规范 |
| `docs/architecture-debt.md` | **架构债清单**：2026-10-21 全代码审查的发现 |
| `docs/merge-strmmanger.md` | **合并 strmmanger 的方案与实施记录** |

## 快速开始

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，填入面板凭据与签名密钥

# 2. 启动
docker compose up -d

# 3. 验证
curl http://localhost:8080/healthz
curl http://localhost:8080/metrics | head
```

## 开发

```bash
# 安装依赖（Python 3.13，由 uv 管理）
uv sync --extra dev --extra p115 --extra guangya

# 静态检查
uv run ruff check app/ tests/
uv run mypy app/

# 后端测试
uv run pytest
uv run pytest --cov=app          # 带覆盖率

# 前端
cd web && npm install && npm run build
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
- **调度配置全部落库**：cron 与作业运行参数都不再走 env。默认值在代码里（`DEFAULT_CRON` / `ParamSpec.default`），用户在「设置 → 调度」页改的落进 DB，**跨重启保持**。改 env 无效。
- **辅种数据走定时快照**：辅种页与仪表盘的「在途任务」都读 `cross_seed_snapshot` 表（定时作业写入），**不实时打下载器接口**——辅种站 3000+ 种子实时拉要 20s+，会把连接长期占住。抓取失败**保留上一次的快照**（不写 0 兜底），且失败站要在作业摘要里点名。
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