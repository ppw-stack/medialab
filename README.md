# PT SeedHelper

> 辅种自动化、免费种窗口看护与种子健康度监测。Python 3.12 + FastAPI + SQLite，Docker 一键部署。

## 现状

**M1 基础设施 + 核心功能**已落地：

- **M1**：项目骨架、配置中心、SQLite WAL 单写者队列、下载器/站点双适配层、日志与审计、FastAPI 应用 + `/healthz` + `/metrics` + 完整 REST 骨架
- **F1 站点拉取**：9 站适配器（mteam / hdsky / hhanclub / audiences / hdhome / hddolby / ourbits / soulvoice / springsunday）拉取免费种 + RSS 清理
- **F2 免费种窗口**：推送 + 监测 + 中止（push_free_torrents / monitor_free_window / FR-3.5 链路）
- **FR-1.6 站点用户数据契约**（2026-10-18）：9 站 user_info / seeding_stats / message_count / hr_status 4 方法，刷新作业 `*/30 * * * *`，Vue UI（`/user-data` 列表 + Sites drawer），API 端点 `/api/v1/user-data` 与 `/api/v1/sites/{id}/user-data`

`F2 辅种引擎` / `F4 巡检` / `F5 清理` 自 **M2** 起按文档 v1.2 实施。

详细需求见 `.orca/drops/PT辅种与自动监测系统-项目需求文档.md`（v1.2，已评审通过）。

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
uv run ruff check app/
uv run mypy app/
```

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
SQLite (WAL + 单写者队列, §8)
```

下载器与站点的差异全部收拢在适配层，引擎只面向统一接口编程。详见 `app/adapters/`。

## 关键约束（自 v1.2 评审）

- **`abort_at` 是无例外绝对红线**：所有到点未完成的任务立即中止，不存在豁免。
- **数据安全第一**：删除 hash 必须是 40 位 hex（拒绝 `all` / 通配），前端二次确认，删磁盘数据独立开关并记审计日志。
- **永久免费绝不挂看护**：识别为 `freeleech_permanent` 的任务直接转辅种池（R10）。

## 目录

```
app/
├── core/          # 配置、时间、日志、指标、窗口核心
├── adapters/      # 下载器 + 站点双适配层（FR-1 / FR-1.5）
├── db/            # SQLAlchemy 2.0 async + WAL + 单写者
├── services/      # 调度中心、业务引擎（M2+）
├── api/           # FastAPI 路由
└── main.py        # 应用入口 + lifespan
```

## 安全

请**不要**把 `.env` / 数据库备份加入 git。