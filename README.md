# PT SeedHelper

> 辅种自动化、免费种窗口看护与种子健康度监测。Python 3.12 + FastAPI + SQLite，Docker 一键部署。

## 现状

**M1 基础设施**已落地：项目骨架、配置中心、SQLite WAL 单写者队列、下载器/站点双适配层骨架（qBittorrent 完整实现 + 配置驱动的通用站点适配器）、日志与审计、FastAPI 应用 + `/healthz` + `/metrics` + 完整 REST 骨架。

`F2 辅种引擎` / `F3 免费种` / `F4 巡检` / `F5 清理` 自 **M2** 起按文档 v1.2 实施。

详细需求见 `.orca/drops/PT辅种与自动监测系统-项目需求文档.md`（v1.2，已评审通过）。

## 快速开始

```bash
# 1. 生成凭据加密密钥（FR-7.1，缺失则启动失败）
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 ENCRYPTION_KEY 与面板凭据

# 3. 启动
docker compose up -d

# 4. 验证
curl http://localhost:8080/healthz
curl http://localhost:8080/metrics | head
```

## 开发

```bash
# 安装依赖（Python 3.12，由 uv 管理）
uv sync --extra dev

# 运行测试（含附录 D 完整窗口解析用例集 + 凭据加密 + 窗口核心逻辑）
uv run pytest

# 静态检查
uv run ruff check app/ tests/
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
- **数据安全第一**：所有删除走 FR-5.2 六重保护 + 路径三重校验；缺失 `get_files` 能力的下载器自动禁用数据删除（R12）。
- **凭据加密强制**：AES-256-GCM，每条记录 AAD 绑定归属；密钥仅环境变量（AC-17）。
- **永久免费绝不挂看护**：识别为 `freeleech_permanent` 的任务直接转辅种池（R10）。

## 目录

```
app/
├── core/          # 配置、加密、时间、日志、指标、窗口核心
├── adapters/      # 下载器 + 站点双适配层（FR-1 / FR-1.5）
├── db/            # SQLAlchemy 2.0 async + WAL + 单写者
├── services/      # 调度中心、业务引擎（M2+）
├── api/           # FastAPI 路由
└── main.py        # 应用入口 + lifespan
tests/             # 106 个用例，覆盖附录 D 全部场景
```

## 安全

请**不要**把 `.env` / 数据库备份 / 加密密钥加入 git。密钥丢失 = 凭据不可恢复（FR-7.1）。