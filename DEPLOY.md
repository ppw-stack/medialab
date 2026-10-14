# 部署指南

Docker 单机双环境部署（dev + prod），共享同一镜像但完全独立的数据/配置。SQLite + WAL + bind mount 持久化。

## 0. 安全声明（先读）

> **本指南只做 HTTP 部署（无 TLS / 无反代）。请仅在以下场景使用：**
> - 内网/家庭网络部署（不暴露公网）
> - 已有外部反代（Nginx Proxy Manager / Traefik / Caddy）做 TLS 终止
> - 自承担中间人攻击 / Cookie 劫持风险
>
> 公网直连 **强烈建议** 在反代后做 HTTPS（cookie `Secure` 标志 + Let's Encrypt）。

**未实现的项**（用户已确认）：
- HTTPS / 反代（仅暴露 HTTP 8080/8091）
- 数据库自动备份（用户自备份）
- 监控告警（仅暴露 `/healthz` + `/metrics` 端点）
- 滚动发布 / 多实例（SQLite 单写者，每套环境单容器 + 1 worker）

## 1. 双环境架构

| 环境 | 端口 | 数据目录 | 凭据文件 | 容器名 |
|---|---|---|---|---|
| **dev**  | 8080 | `./data-dev/`  | `.env`      | `seedhelper-dev`  |
| **prod** | 8091 | `./data-prod/` | `.env.prod` | `seedhelper-prod` |

> 注：prod 用 8091 而非 8081 是因为本机 8081 已被 qBittorrent 容器占用。生产服务器无此冲突时改回 8081 也可（修改 `docker-compose.prod.yml` 的 ports）。

两套**完全独立**：不同端口、不同 SQLite DB、不同 Web Token。**生产从 dev 导入数据**用 `sqlite3 .backup`（见第 7 节）。

## 1. 前置条件

| 依赖 | 最低版本 | 检查 |
|---|---|---|
| Docker Engine | 24.0+ | `docker --version` |
| Docker Compose v2 | 2.20+ | `docker compose version` |
| 主机端口 | 8080 空闲 | `lsof -i :8080` |
| 磁盘 | >= 5 GB 可用 | `df -h .` |

## 2. 准备 `.env`（dev）

```bash
cp .env.example .env
```

用密码管理器生成以下值（**不能留默认值**）：

```bash
# Web 面板密码（>= 12 位）
python -c "import secrets; print('WEB_PASSWORD=' + secrets.token_urlsafe(16))"

# Bearer Token（CLI / 脚本入口；>= 32 字符）
python -c "import secrets; print('WEB_TOKEN=' + secrets.token_urlsafe(32))"
```

填入 `.env`：

```ini
WEB_USERNAME=admin                  # 默认
WEB_PASSWORD=...                    # 必填
WEB_TOKEN=...                       # 必填
TZ=Asia/Shanghai                    # 或 UTC
LOG_LEVEL=INFO
LOG_JSON=true
FETCH_ALL_SITES_CRON=*/10 * * * *
PUSH_FREE_CRON=*/30 * * * *
```

## 2.5 准备 `.env.prod`（生产）

**生产 Token 必须与 dev 不同**（直接用 `deploy.sh prod` 第一次会引导生成）。

```bash
# 方式 A：一键脚本自动创建 .env.prod（如果不存在）
./scripts/deploy.sh prod deploy

# 方式 B：手工
cp .env.example .env.prod
# 用上面的 secrets 命令生成 2 个新值，覆盖 .env.prod 里的占位
```

**注意**：`.env` 和 `.env.prod` 都已在 `.gitignore`，不会进 git。但应**额外备份到密码管理器**。

## 3. 启动

### 一键脚本

```bash
./scripts/deploy.sh                # dev deploy（8080）
./scripts/deploy.sh prod           # prod deploy（8091）
./scripts/deploy.sh all deploy     # dev + prod 都 deploy
```

### 手动

```bash
# dev
docker compose build
docker compose up -d

# prod
docker compose -f docker-compose.prod.yml --env-file .env.prod build
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

启动后等待 15 秒让 healthcheck 通过。

## 4. 验证

```bash
# 健康检查
curl -fsS http://localhost:8080/healthz  # dev
curl -fsS http://localhost:8091/healthz  # prod
# 期望：{"status":"ok","db":true,"version":"0.1.0"}

# 指标端点
curl -fsS http://localhost:8080/metrics | head

# Web UI
xdg-open http://localhost:8080  # dev
xdg-open http://localhost:8091  # prod
# 浏览器登录：用户名=WEB_USERNAME、密码=WEB_PASSWORD

# 状态总览
./scripts/deploy.sh all status
```

## 5. 升级

```bash
# 1) 拉新代码
git pull

# 2) 改 TAG（默认 0.1.0）—— 一套 .env 改 TAG 即升级对应环境
echo "TAG=0.1.1" >> .env         # dev
echo "TAG=0.1.1" >> .env.prod    # prod

# 3) 重新构建 + 滚动重启
./scripts/deploy.sh              # 仅 dev
./scripts/deploy.sh prod         # 仅 prod
./scripts/deploy.sh all deploy   # 两套都升级
```

`stop_grace_period: 60s` 保证 SIGTERM 优雅停机（FR-8.2：等待在途作业收敛）。SQLite WAL 文件在 `./data-{dev,prod}/seedhelper.db*`，**升级不会丢数据**。

## 6. 排错

| 现象 | 排查 |
|---|---|
| 容器启动后立即 Exit 1 | `docker logs seedhelper` 看 WEB_PASSWORD / WEB_TOKEN 是否缺失 |
| `curl /healthz` connection refused | `docker compose ps` 看是否 healthy；`docker logs seedhelper` 看启动日志 |
| `db: false` in /healthz | SQLite WAL 文件权限：`ls -la ./data/`；`docker exec seedhelper ls -la /app/data` |
| 浏览器登录后 session 立即丢失 | 检查浏览器是否禁用第三方 cookie；本机 HTTP 部署下应为同站 cookie，**不会**因 Secure 标志被拒 |
| 调度作业不执行 | `curl -H "Authorization: Bearer $WEB_TOKEN" http://localhost:8080/api/v1/scheduler` 看 jobs[].enabled；`docker logs seedhelper \| grep "已注册"` |
| 推送失败 | UI 设置 → 调度作业 → 立即执行 push_free_torrents；查容器日志 |
| 容器内存超限被 kill | `docker stats seedhelper`；`deploy.resources.limits.memory` 调到 1G |
| 时区不对 | `docker exec seedhelper date`；改 `.env` 的 `TZ=`，`docker compose up -d` |

## 7. 数据备份（手工）

虽然本指南不自动备份，但 `seedhelper.db` 需用户自备份：

```bash
# 停服（防 SQLite 写中）—— 注意会中断推送/拉取
docker compose stop  # dev
docker compose -f docker-compose.prod.yml stop  # prod

# 拷贝
mkdir -p ./backups/$(date +%Y%m%d)
cp ./data-dev/seedhelper.db* ./backups/$(date +%Y%m%d)/
# 或 prod：cp ./data-prod/seedhelper.db* ./backups/$(date +%Y%m%d)/

# 起服
docker compose start
```

更安全的方式（无需停服，但依赖 `sqlite3` CLI 在主机上）：

```bash
# dev
sqlite3 ./data-dev/seedhelper.db ".backup './backups/$(date +%Y%m%d)/seedhelper.db'"
# prod
sqlite3 ./data-prod/seedhelper.db ".backup './backups/$(date +%Y%m%d)/seedhelper-prod.db'"
```

**dev → prod 数据迁移**（首次或同步）：

```bash
# 1) 停 prod 防写中
docker compose -f docker-compose.prod.yml stop

# 2) 备份式拷贝（保持事务一致）
sqlite3 ./data-prod/seedhelper.db ".backup './data-prod/seedhelper.db.bak'"
sqlite3 ./data-dev/seedhelper.db ".backup './data-prod/seedhelper.db'"

# 3) 起 prod
docker compose -f docker-compose.prod.yml start
```

## 8. 目录约定

```
./                      # 仓库根（部署主机）
├── .env                # dev 凭据（git 忽略）
├── .env.prod           # prod 凭据（git 忽略，必须与 .env 不同）
├── docker-compose.yml  # dev 配置
├── docker-compose.prod.yml  # prod 配置
├── Dockerfile
├── data-dev/           # dev SQLite WAL（git 忽略）
├── data-prod/          # prod SQLite WAL（git 忽略）
├── logs-dev/           # dev 日志（git 忽略）
├── logs-prod/          # prod 日志（git 忽略）
├── config/             # 配置文件预留
├── backups/            # 用户自备份（git 忽略）
└── scripts/
    └── deploy.sh
```

容器内部：

```
/app/data/    # 对应 ./data-{dev,prod}
/app/logs/     # 对应 ./logs-{dev,prod}
/app/config/  # 对应 ./config
/app/web/dist # 前端构建产物（已 bake 进镜像）
```

## 9. 资源基线

| 资源 | dev | prod |
|---|---|---|
| Memory limit | 512 MB | 1 GB |
| Memory reserved | 128 MB | 256 MB |
| CPU limit | 1.0 核 | 2.0 核 |
| 日志 | 20 MB × 5 = 100 MB | 20 MB × 10 = 200 MB |

实测空闲 ~120 MB RSS、CPU < 3%（非功能 §6）。如需更高并发 / 更大 SQLite 连接池，可调高。**两套同时跑**总资源 ~1.5 GB 内存 / 3 核。
