# 部署指南

Docker 单容器生产部署。SQLite + WAL + 命名卷持久化。

## 0. 安全声明（先读）

> **本指南只做 HTTP 部署（无 TLS / 无反代）。请仅在以下场景使用：**
> - 内网/家庭网络部署（不暴露公网）
> - 已有外部反代（Nginx Proxy Manager / Traefik / Caddy）做 TLS 终止
> - 自承担中间人攻击 / Cookie 劫持风险
>
> 公网直连 **强烈建议** 在反代后做 HTTPS（cookie `Secure` 标志 + Let's Encrypt）。

**未实现的项**（用户已确认）：
- HTTPS / 反代（`compose.yml` 仅暴露 `8080`）
- 数据库自动备份（`./data/seedhelper.db` 用户自备份）
- 监控告警（仅暴露 `/healthz` + `/metrics` 端点）
- 滚动发布 / 多实例（SQLite 单写者，单容器 + 1 worker）

## 1. 前置条件

| 依赖 | 最低版本 | 检查 |
|---|---|---|
| Docker Engine | 24.0+ | `docker --version` |
| Docker Compose v2 | 2.20+ | `docker compose version` |
| 主机端口 | 8080 空闲 | `lsof -i :8080` |
| 磁盘 | >= 5 GB 可用 | `df -h .` |

## 2. 准备 `.env`

```bash
cp .env.example .env
```

用密码管理器生成以下三个值（**不能留默认值**）：

```bash
# 凭据加密密钥（FR-7.1，缺失则启动失败）
python -c "import secrets; print('ENCRYPTION_KEY=' + secrets.token_urlsafe(32))"

# Web 面板密码（>= 12 位）
python -c "import secrets; print('WEB_PASSWORD=' + secrets.token_urlsafe(16))"

# Bearer Token（CLI / 脚本入口；>= 32 字符）
python -c "import secrets; print('WEB_TOKEN=' + secrets.token_urlsafe(32))"
```

填入 `.env`：

```ini
ENCRYPTION_KEY=...                  # 必填
WEB_USERNAME=admin                  # 默认
WEB_PASSWORD=...                    # 必填
WEB_TOKEN=...                       # 必填
TZ=Asia/Shanghai                    # 或 UTC
LOG_LEVEL=INFO
LOG_JSON=true
FETCH_ALL_SITES_CRON=*/10 * * * *
PUSH_FREE_CRON=*/30 * * * *
```

**注意**：`.env` 已在 `.gitignore`，不会进 git。但应**额外备份到密码管理器**（密钥丢失 = 凭据不可恢复，FR-7.1）。

## 3. 启动

### 一键脚本

```bash
./scripts/deploy.sh
```

### 手动

```bash
docker compose build
docker compose up -d
docker compose ps
```

启动后等待 15 秒让 healthcheck 通过。

## 4. 验证

```bash
# 健康检查
curl -fsS http://localhost:8080/healthz
# 期望：{"status":"ok","db":true,"version":"0.1.0"}

# 指标端点
curl -fsS http://localhost:8080/metrics | head
# 期望：Prometheus 文本（# HELP / # TYPE）

# Web UI
xdg-open http://localhost:8080  # macOS: open
# 浏览器登录：用户名=WEB_USERNAME、密码=WEB_PASSWORD
```

## 5. 升级

```bash
# 1) 拉新代码
git pull

# 2) 改 TAG（默认 0.1.0）
echo "TAG=0.1.1" >> .env

# 3) 重新构建 + 滚动重启
docker compose build
docker compose up -d
```

`stop_grace_period: 60s` 保证 SIGTERM 优雅停机（FR-8.2：等待在途作业收敛）。SQLite WAL 文件在 `./data/seedhelper.db*`，**升级不会丢数据**。

## 6. 排错

| 现象 | 排查 |
|---|---|
| 容器启动后立即 Exit 1 | `docker logs seedhelper` 看 ENCRYPTION_KEY / WEB_TOKEN 是否缺失 |
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
docker compose stop

# 拷贝
mkdir -p ./backups/$(date +%Y%m%d)
cp ./data/seedhelper.db* ./backups/$(date +%Y%m%d)/

# 起服
docker compose start
```

更安全的方式（无需停服，但依赖 `sqlite3` CLI 在主机上）：

```bash
sqlite3 ./data/seedhelper.db ".backup './backups/$(date +%Y%m%d)/seedhelper.db'"
```

## 8. 目录约定

```
./                  # 仓库根（部署主机）
├── .env            # 凭据（git 忽略）
├── docker-compose.yml
├── Dockerfile
├── data/           # SQLite WAL 持久化（git 忽略）
├── logs/           # 应用日志（git 忽略）
├── config/         # 配置文件预留
├── backups/        # 用户自备份（git 忽略）
└── scripts/
    └── deploy.sh
```

容器内部：

```
/app/data/    # 对应 ./data
/app/logs/     # 对应 ./logs
/app/config/  # 对应 ./config
/app/web/dist # 前端构建产物（已 bake 进镜像）
```

## 9. 资源基线

`compose.yml` 默认值（可按主机调整）：

| 资源 | 限制 | 预留 |
|---|---|---|
| Memory | 512 MB | 128 MB |
| CPU | 1.0 核 | 无（best-effort） |
| 日志 | 20 MB × 5 = 100 MB | json-file 驱动 |

实测空闲 ~120 MB RSS、CPU < 3%（非功能 §6）。如需更高并发 / 更大 SQLite 连接池，可调高。
