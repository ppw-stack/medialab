# 部署指南

两套环境**跑在两台不同的机器上**：

- **dev** — 本机（192.168.0.92，arm64），本地 `build` 镜像，端口 8092
- **prod** — 远端 128（192.168.0.128，Unraid Tower，x86_64），拉 Docker Hub 镜像，端口 8092

镜像由 GitHub Actions 构建并推送到 Docker Hub（`docker.io/ppw111/medialab`，public）。
**CI 只构建推送，不部署** —— 部署是手动两步（见 §4）。

## 0. 安全声明（先读）

> **本指南只做 HTTP 部署（无 TLS / 无反代）。请仅在以下场景使用：**
> - 内网/家庭网络部署（不暴露公网）
> - 已有外部反代（Nginx Proxy Manager / Traefik / Caddy）做 TLS 终止
> - 自承担中间人攻击 / Cookie 劫持风险
>
> 公网直连 **强烈建议** 在反代后做 HTTPS（cookie `Secure` 标志 + Let's Encrypt）。

**未实现的项**（用户已确认）：
- HTTPS / 反代（仅暴露 HTTP 8092）
- 数据库自动备份（用户自备份）
- 监控告警（仅暴露 `/healthz` + `/metrics` 端点）
- 滚动发布 / 多实例（SQLite 单进程单 worker，每套环境单容器）

## 1. 双环境架构

| | dev | prod |
|---|---|---|
| 位置 | 本机 `192.168.0.92`（arm64） | 远端 `192.168.0.128`（Unraid Tower, x86_64） |
| 端口 | **8092** | **8092** |
| 容器名 | `medialab-dev` | `medialab-prod` |
| 镜像 | 本地 `docker compose build` | `ppw111/medialab:latest`（Docker Hub） |
| Compose 文件 | `docker-compose.yml` | `docker-compose.prod.128.yml` |
| 数据目录 | `./data-dev/` | `/mnt/user/appdata/medialab/data` |
| 日志目录 | `./logs-dev/` | `/mnt/user/appdata/medialab/logs` |
| 凭据文件 | `.env` | `.env.prod` |
| 资源上限 | 1GB / 2 核 | 1536MB / 2 核 |

两套**完全独立**：不同机器、不同端口、不同 SQLite DB、不同 Web Token。
数据不自动同步，需要时手工迁移（见 §7）。

> ⚠️ **prod 的 compose 文件必须同步到 128**——那里不是 git 仓库，
> `app/` 目录只是放 compose 文件的落点，容器跑的是镜像里的代码。

## 2. 前置条件

| 依赖 | 最低版本 | 检查 |
|---|---|---|
| Docker Engine | 24.0+ | `docker --version` |
| Docker Compose v2 | 2.20+ | `docker compose version` |
| 主机端口 | 8092 空闲（dev） | `lsof -i :8092` |
| 磁盘 | >= 5 GB 可用 | `df -h .` |

生产机（128）另需 SSH 访问：`ssh mp`（`~/.ssh/config` 别名 → `root@192.168.0.128`）。

## 3. 准备凭据

### dev（`.env`）

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
LOG_LEVEL=INFO
LOG_JSON=true
```

> ⚠️ **调度配置不在 env 里**（2026-10-24 起）。cron 与作业运行参数都
> 落库，用户在「设置 → 调度」页改的**跨重启保持**；env 改这些**无效**。
> 默认值在代码里（`app/services/job_registry.py` 的 `DEFAULT_CRON` 与
> `app/services/runtime_params.py` 的 `ParamSpec.default`）。
> 见 `LOCAL_ENV.md` §6。

### prod（`.env.prod`）

**生产 Token 必须与 dev 不同。**

```bash
cp .env.example .env.prod
# 用上面的 secrets 命令生成 2 个新值，覆盖 .env.prod 里的占位
```

`.env.prod` 需要同步到 128 的 `/mnt/user/appdata/medialab/app/`。

> `.env` 和 `.env.prod` 都已在 `.gitignore`，不会进 git。
> 但应**额外备份到密码管理器**。

## 4. 部署

### dev（本机）

```bash
# 一键（build + up + 健康检查）
./scripts/deploy.sh

# 或手动
docker compose build
docker compose up -d
```

> `scripts/deploy.sh` **只管 dev**。生产走下面的手动链路。

### prod（128）

三步：

```bash
# 1) 本机 push 触发 CI（闸门绿 → 自动构建推送镜像）
git push origin main

# 2) 等 docker-image workflow 完成（确认镜像已推上去），然后 128 上拉新镜像
ssh mp "docker pull ppw111/medialab:latest"

# 3) 128 上重建容器
ssh mp "cd /mnt/user/appdata/medialab/app && TAG=latest docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d"
```

> **CI 是有依赖链的**（2026-10-23 改）：`push` → `ci`（ruff/mypy/pytest/
> vue-tsc/oxlint 闸门）→ **全绿才** `docker-image`（构建推送）。
> 闸门红 → **不产出镜像**，第 2 步拉到的还是旧镜像（不会拉到半成品）。
> 所以第 2 步前先确认 `docker-image` workflow 是 success。
>
> ⚠️ **`TAG=latest` 必须显式指定**：compose 里默认值是 `0.1.0`，
> 而 Docker Hub 上只有 `latest` 和 `main`，`0.1.0` 不存在，直接 `up -d` 会拉取失败。
>
> ⚠️ 若容器是旧版本手动创建的（没有 compose label），`up -d` 不会替换它，
> 需先 `docker stop -t 60 medialab-prod && docker rm medialab-prod`。
> 60s 宽限期对应 `stop_grace_period`，让在途作业收尾。

启动后等待 15 秒让 healthcheck 通过。

## 5. 验证

```bash
# dev
curl -fsS http://localhost:8092/healthz
# 期望：{"status":"ok","db":true,"version":"0.2.0"}

# prod（远端）
ssh mp "curl -fsS http://localhost:8092/healthz"

# 指标端点
curl -fsS http://localhost:8092/metrics | head

# Web UI
open http://localhost:8092          # dev
open http://192.168.0.128:8092      # prod
# 浏览器登录：用户名=WEB_USERNAME、密码=WEB_PASSWORD

# 状态总览
./scripts/deploy.sh status
```

## 6. 升级

```bash
# 1) 本机提交并推送（CI 闸门绿后自动构建镜像）
git push origin main

# 2) 128 上拉镜像 + 重建
ssh mp "docker pull ppw111/medialab:latest"
ssh mp "cd /mnt/user/appdata/medialab/app && TAG=latest docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d"
```

`stop_grace_period: 60s` 保证 SIGTERM 优雅停机（FR-8.2：等待在途作业收敛）。
SQLite 数据文件在各自的数据目录，**升级不会丢数据**。

## 7. 排错

| 现象 | 排查 |
|---|---|
| 容器启动后立即 Exit 1 | `docker logs medialab-dev` 看 WEB_PASSWORD / WEB_TOKEN 是否缺失 |
| `curl /healthz` connection refused | `docker compose ps` 看是否 healthy；`docker logs` 看启动日志 |
| `db: false` in /healthz | SQLite 文件权限：`ls -la ./data-dev/`；`docker exec medialab-dev ls -la /app/data` |
| 浏览器登录后 session 立即丢失 | 检查浏览器是否禁用第三方 cookie；本机 HTTP 部署下应为同站 cookie，**不会**因 Secure 标志被拒 |
| 调度作业不执行 | `curl -H "Authorization: Bearer $WEB_TOKEN" http://localhost:8092/api/v1/scheduler` 看 `jobs[].enabled`；`docker logs medialab-dev \| grep "已注册"` |
| 某作业重启后又启用了 | 启停状态持久化在 `scheduler_job_settings` 表（2026-10-21 起）。用 UI toggle 或 API 改，不要直接改内存 |
| 推送失败 | UI 设置 → 调度作业 → 立即执行 `push_free_torrents`；查容器日志 |
| 容器内存超限被 kill | `docker stats medialab-dev`；`deploy.resources.limits.memory` 调到 1G |
| `git push` 报 HTTP2 framing error | 不是网络问题，`git config http.version HTTP/1.1` 后重试 |
| prod 拉不到镜像 | compose 默认 tag `0.1.0` 不存在，必须显式 `TAG=latest` |
| push 后没有新镜像 | 闸门红了就不构建镜像（2026-10-23 起 CI 有依赖链）。看 `ci` workflow 是否红；或 `docker-image` 被 `workflow_run` 的 job 级 `if` 跳过了（PR 分支不构建） |
| 镜像 tag 指向错误的 commit | `workflow_run` 下 `github.sha` 是默认分支 HEAD 而非被测 commit。checkout 必须用 `ref: ${{ github.event.workflow_run.head_sha }}`，tag 计算必须自己来（metadata-action 的 `context.sha` 同样不可信） |

## 8. 数据备份（手工）

不自动备份，`medialab.db` 需用户自备份。

### dev

```bash
sqlite3 ./data-dev/medialab.db ".backup './backups/$(date +%Y%m%d)/medialab.db'"
```

### prod（远端）

```bash
ssh mp "cd /mnt/user/appdata/medialab/data && \
  sqlite3 medialab.db \".backup './medialab.db.bak.\$(date +%Y%m%d_%H%M%S)'\""
```

> 远程查库时加 `PRAGMA busy_timeout=8000;` 防锁；SQL 里的字符串用**单引号**
> （双引号会被 SQLite 当成标识符）。
>
> 备份**无自动轮转**（按决策不引入新机制），需人工清理。清理前逐个
> `PRAGMA integrity_check` 确认——损坏的副本没有抢救价值，可直接删。

### dev → prod 数据迁移

```bash
# 1) 停 prod 防写中
ssh mp "cd /mnt/user/appdata/medialab/app && docker compose -f docker-compose.prod.128.yml stop"

# 2) 备份式拷贝（保持事务一致）
ssh mp "cd /mnt/user/appdata/medialab/data && sqlite3 medialab.db \".backup './medialab.db.bak.\$(date +%Y%m%d_%H%M%S)'\""
scp ./data-dev/medialab.db mp:/mnt/user/appdata/medialab/data/medialab.db

# 3) 起 prod
ssh mp "cd /mnt/user/appdata/medialab/app && docker compose -f docker-compose.prod.128.yml start"
```

## 9. 目录约定

### dev（本机仓库根）

```
./
├── .env                # dev 凭据（git 忽略）
├── docker-compose.yml  # dev 配置
├── Dockerfile
├── data-dev/           # dev SQLite（git 忽略）
├── logs-dev/           # dev 日志（git 忽略）
├── config/             # 配置文件预留（dev compose 挂载点）
├── backups/            # 用户自备份（git 忽略）
└── scripts/
    └── deploy.sh       # 仅 dev
```

### prod（128）

```
/mnt/user/appdata/medialab/
├── app/
│   ├── docker-compose.prod.128.yml   # 手工同步（不是 git 仓库）
│   └── .env.prod                     # 手工同步
├── config/     # → 容器 /app/config
├── data/       # → 容器 /app/data（SQLite）
└── logs/       # → 容器 /app/logs
```

容器内部：

```
/app/data/    # SQLite 库文件
/app/logs/    # 应用日志
/app/config/  # 配置文件
/app/web/dist # 前端构建产物（已 bake 进镜像）
```

## 10. 资源基线

| 资源 | dev | prod |
|---|---|---|
| Memory limit | 512 MB | 1 GB |
| Memory reserved | 128 MB | 256 MB |
| CPU limit | 1.0 核 | 2.0 核 |
| 日志 | 20 MB × 5 = 100 MB | 20 MB × 10 = 200 MB |

实测空闲 ~120 MB RSS、CPU < 3%（非功能 §6）。如需更高并发 / 更大 SQLite 连接池，可调高。
