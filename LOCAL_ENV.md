# 本地环境信息（Local Environment）

> 本文件记录**当前开发机的真实环境状态**，供后续接手的 agent / 开发者快速上手。
> 与 `README.md`（项目功能）、`DEPLOY.md`（部署流程）、`AGENTS.md`（协作规范）配套使用。
>
> **维护约定**：环境变更（换机器、改端口、换部署方式）后请同步更新本文件。
> 本文件不含明文密钥，只记录**获取方式**。

---

## 1. 主机

| 项 | 值 |
|---|---|
| 操作系统 | macOS 27.0.1 (Darwin 26A434) |
| 架构 | `arm64` (Apple Silicon) |
| 主机名 | 本机开发机 |
| LAN IP | `192.168.0.92` (en0) |
| 仓库路径 | `/Users/pw/工作/ptHelper` |
| 分支 | `main` |
| 远端 | `https://github.com/ppw-stack/ptHelper.git` |

## 2. 工具链

| 工具 | 版本 | 备注 |
|---|---|---|
| Python | **3.12**（`.python-version` / `requires-python >=3.12`） | 系统自带 `python3` 是 3.9.6，**必须用 `uv`**，勿直接 `python3` |
| uv | 0.12.5 (Homebrew aarch64) | 依赖管理 + 运行入口 |
| Node.js | v26.7.0 | 仅前端用 |
| npm / npx | 12.0.2 | |
| Docker | 29.4.0 | |
| Docker Compose | v5.1.2 | |
| Git | 2.54.0 (Apple Git-157) | |

> ⚠️ `python3` 直接调用会拿到 3.9.6，语法与依赖都不兼容。**统一用 `uv run <cmd>`**。

## 3. 双环境架构

两套环境共享同一份代码，**数据 / 配置 / Token 完全隔离**。

| | dev | prod |
|---|---|---|
| 位置 | 本机（192.168.0.92） | 远端 `192.168.0.128`（Unraid Tower, x86_64） |
| 端口 | **8080** | **8091** |
| 容器名 | `seedhelper-dev` | `seedhelper-prod` |
| 镜像 | `seedhelper:dev-arm64`（本地构建） | `ppw111/seedhelper:latest`（Docker Hub） |
| 数据目录 | `./data-dev/` | `/mnt/user/appdata/seedhelper/data` |
| 日志目录 | `./logs-dev/` | `/mnt/user/appdata/seedhelper/logs` |
| 凭据文件 | `.env` | `.env.prod` |
| Compose | `docker-compose.yml` | `docker-compose.prod.128.yml` |
| 用途 | 开发调试 | 生产运行 |

**SSH 访问 prod**：`ssh mp`（`~/.ssh/config` 中的别名 → `root@192.168.0.128`）

> ⚠️ **两个 compose 文件别搞混**：
>
> | 文件 | 目标 | 端口 | 数据 | 启动方式 |
> |---|---|---|---|---|
> | `docker-compose.yml` | dev（本机） | 8080 | `./data-dev` | `build` 本地构建 |
> | `docker-compose.prod.yml` | **本机** prod | 8081 | `./data-prod` | `build` 本地构建 |
> | `docker-compose.prod.128.yml` | **远端** prod（128） | 8091 | `/mnt/user/appdata/seedhelper/data` | 拉 Docker Hub 镜像，无 `build` |
>
> `scripts/deploy.sh prod` 用的是 **`docker-compose.prod.yml`**（本机 8081），
> **不是** 128 那套。部署到 128 要手动用 `docker-compose.prod.128.yml`（见 §9）。
> 本机当前没有跑 8081 那套（只有 `seedhelper-dev` 在运行）。

## 4. 常用地址与端点

### 健康检查（重要）

真实健康端点是 **`/healthz`**，返回 JSON：

```json
{"status":"ok","version":"0.1.0","db":true}
```

> ⚠️ **`/health` 不是健康端点**。前端 SPA 有 fallback 路由，任何不存在的路径都返回 **200 + HTML**，
> 包括 `/health`。用它探活会得到**假阳性**。请用 `/healthz`（容器 healthcheck 也是用这个）。

| 端点 | 用途 |
|---|---|
| `/healthz` | 健康检查（`status` / `version` / `db`） |
| `/metrics` | Prometheus 指标 |
| `/api/v1/...` | REST API（需 Bearer Token） |

### 关键 API 路径

| 路径 | 说明 |
|---|---|
| `/api/v1/scheduler` | 调度作业列表（**注意：不是 `/scheduler/jobs`**） |
| `/api/v1/scheduler/runs` | 作业执行历史 |
| `/api/v1/scheduler/runs/{run_id}` | 单次执行详情 |
| `/api/v1/scheduler/runtime-params` | 作业运行参数（GET / PUT） |
| `/api/v1/scheduler/{job_name}/toggle` | 启停作业 |
| `/api/v1/scheduler/{job_name}/cron` | 运行时改 cron（**不落库**，见 §6） |
| `/api/v1/cross-seed-monitor/downloaders` | 辅种下载器列表 |
| `/api/v1/cross-seed-monitor/downloaders/{id}/scan` | 扫描单个辅种下载器 |
| `/api/v1/sites` | 站点列表 |
| `/api/v1/sites/{site_id}/feed-test` | 站点拉取测试 |
| `/api/v1/sites/{site_id}/user-data` | 站点用户数据 |
| `/api/v1/user-data` | 全部站点用户数据（FR-1.6） |
| `/api/v1/dashboard` | 仪表盘统计 |
| `/api/v1/downloaders` | 下载器管理（删除走 40 位 hex 白名单） |
| `/api/v1/torrent-index/sites` | 种子索引 |
| `/api/v1/torrent-index/free-types` | 免费种分类 |

### 相关服务

| 服务 | 地址 | 备注 |
|---|---|---|
| qBittorrent（容器） | `http://192.168.0.92:8081` | 本机 qB 容器，宿主 8081 → 容器 8080 |
| Transmission | `http://192.168.0.92:9092` | `tr-local`，仪表盘日志显示**当前不可达** |
| Transmission | `http://192.168.0.172:9091` | `tr-172`，**间歇性超时**（已知现象） |
| dev seedhelper | http://localhost:8080 | |
| prod seedhelper | http://192.168.0.128:8091 | |

下载器实例存在 DB 表 `downloader_instances`（字段 `id` / `name` / `type` / `url` / `username` / `password` / `save_path` / `is_default` / `enabled` / `verify_tls` / `is_cross_seed`），
共 4 条：`qb-local`(qbittorrent) / `tr-local`(transmission) / `mock-cs` / `tr-172`(transmission)。

> `192.168.0.172:9090` 是 prod 视角下 qB 的地址（compose 注释所述），
> 与 dev 库里的 `192.168.0.92:8081` 是不同环境的两套配置。

## 5. 凭据获取方式（不在此记录明文）

| 环境 | Token 位置 | 长度要求 |
|---|---|---|
| dev | `.env` → `WEB_TOKEN` | ≥ 32 字符 |
| prod | `.env.prod` → `WEB_TOKEN` | ≥ 32 字符，**必须与 dev 不同** |

密码同理存于 `WEB_PASSWORD`（≥ 12 位）。生成方式：

```bash
python -c "import secrets; print('WEB_TOKEN=' + secrets.token_urlsafe(32))"
```

`.env` / `.env.prod` 已被 `.gitignore` 排除，**严禁提交**。

### 认证方式

除登录页的 cookie 方案外，脚本 / CLI 走 Bearer Token：

```bash
curl -H "Authorization: Bearer $WEB_TOKEN" http://localhost:8080/api/v1/scheduler
```

## 6. 调度作业（6 个）

| 作业 | cron | 说明 |
|---|---|---|
| `fetch_all_sites` | `*/10 * * * *` | 全站拉取 |
| `push_free_torrents` | `*/30 * * * *` | 免费种推送（走 TaskQueue） |
| `monitor_free_window` | `5,15,25,35,45,55 * * * *` | 免费窗口看护 |
| `refresh_user_data` | `*/30 * * * *` | FR-1.6 用户数据刷新 |
| `scan_cross_seed` | `0 */6 * * *` | 辅种巡查 |
| `drain_task_queue` | `*/5 * * * *` | 任务队列排空 |

专属参数：

- `push_free_torrents`：`push_min_remaining_hours` / `push_max_per_run` / `push_download_interval_ms`
- `monitor_free_window`：`monitor_pre_pause_minutes`（`.env` 中为 10）

cron 的**持久化来源是 `.env` / `.env.prod`**（`FETCH_ALL_SITES_CRON` / `PUSH_FREE_CRON` /
`MONITOR_FREE_WINDOW_CRON` / `SCAN_CROSS_SEED_CRON` 等），
`app/core/config.py` 提供默认值，`app/services/job_registry.py` 注册作业时读取。

> ⚠️ 通过 API `POST /api/v1/scheduler/{job_name}/cron` 改 cron 是**运行时生效、不落库**的
> （`scheduler_center.update_cron()` 只改内存中的 `spec.cron`）。
> **重启容器后会回落到 `.env` 的值**。要长期生效必须改 `.env`。
> `refresh_user_data`（`*/30 * * * *`）与 `drain_task_queue`（`*/5 * * * *`）的 cron
> 目前是代码里硬编码的，没有对应的 env 变量。
> `scan_cross_seed` 有 `SCAN_CROSS_SEED_CRON` 配置项，但当前 `.env` / `.env.prod` 未设置，
> 走 `app/core/config.py` 的默认值 `0 */6 * * *`。

## 7. 站点适配器（9 站）

`app/adapters/sites/`：`mteam` / `hdsky` / `hhanclub` / `audiences` / `hdhome` / `hddolby` / `ourbits` / `soulvoice` / `springsunday`

其中 7 站共用 `app/adapters/sites/parsers/nexus_php.py` 的 module-level helper。

**时区**：`settings.timezone` 默认 `Asia/Shanghai`（`app/core/config.py`），
必须与站点规则时区一致，否则保种时长 / 免费窗口计算会偏差。

## 8. 数据与存储

| 项 | 位置 |
|---|---|
| dev SQLite | `data-dev/seedhelper.db`（另有 `.bak.*` / `.broken` 历史文件） |
| prod SQLite | 远端 `/mnt/user/appdata/seedhelper/data/` |
| 日志 | `logs-dev/` / `logs-prod/` |
| 下载目录 | `/data/torrents`（dev 挂载） |

- SQLite 走 **WAL**，但 dev compose 设了 `DB_JOURNAL_MODE=DELETE`（commit 后立即落盘，避免重启丢 info_hash）。
- `user_version` 迁移为**批次制**，老库不会重复 +8h，重复执行幂等。
- 配置目录 `config/` 挂载进容器（`local.yaml` 属本地配置，已 gitignore）。

## 9. 常用命令

```bash
# 依赖 + 静态检查（AGENTS.md 规定，静态检查不通过禁止合并）
uv sync --extra dev
uv run ruff check app/
uv run mypy app/
cd web && npx vue-tsc -b
cd web && npx vite build

# 前端测试（需先起 dev 容器 localhost:8080）
cd web && npx playwright test

# 部署
./scripts/deploy.sh dev deploy      # dev：build + up + healthcheck（本机 8080）
./scripts/deploy.sh prod deploy     # 本机 prod（8081，非 128！见 §3）
./scripts/deploy.sh status          # 查状态
./scripts/deploy.sh prod logs       # 跟日志

# 远端操作
ssh mp "docker ps --filter name=seedhelper"
ssh mp "docker logs -f seedhelper-prod --tail 100"
```

### 本机构建镜像给 prod 用

本机是 arm64，prod（128）是 x86_64，**不能直接 `docker save` 搬运**。需要跨架构时用 buildx：

```bash
docker buildx build --platform linux/amd64 -t ppw111/seedhelper:latest --push .
```

通常走 CI 即可：push 到 `main` → GitHub Actions 自动构建并推 Docker Hub → 128 上 `docker pull` 后重建。

### prod（128）的真实部署链路

CI **只构建推送镜像，不部署**（无 ssh/deploy job）。实际链路是手动三步：

```bash
# 1) 本机 push 触发 CI 构建（等 CI 绿）
git push origin main

# 2) 128 上拉新镜像
ssh mp "docker pull ppw111/seedhelper:latest"

# 3) 128 上重建容器（注意用 .128.yml 那个文件）
ssh mp "cd /mnt/user/appdata/seedhelper/app && docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d"
```

> ⚠️ 128 上的 `/mnt/user/appdata/seedhelper/app/` **不是 git 仓库**，且代码副本已陈旧
> （仍是 10 月 14 日的版本）。容器真正跑的是 Docker Hub 镜像里的代码，
> 挂载到容器的只有 `config/` `data/` `logs/` 三个目录。
> **不要以为改 128 上的 `app/` 目录就能生效** —— 改 compose 文件需同步在 128 上落地。
>
> ⚠️ compose 文件改了也要同步到 128（它不是 git 仓库，不会自动更新）。

## 10. CI

`.github/workflows/docker-image.yml`

- 触发：`push` 到 `main` 或 `v*` tag；`pull_request` 只构建不推送
- 产物：`docker.io/ppw111/seedhelper`（public）
- 基镜像：`python:3.12-slim`
- 缓存：`type=gha`
- 已硬化（`a82cd10`）：timeout、concurrency、`set -euo pipefail`、jq 安全构造 JWT、凭据不回显
