#!/usr/bin/env bash
# 一键部署 SeedHelper。
# 前置：.env 已就绪（参考 DEPLOY.md 第 2 步）。
#
# 用法：./scripts/deploy.sh [build|up|down|logs|status]
#   无参数 = 完整 build + up + 验证 healthz
set -euo pipefail

cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err() { echo -e "${RED}[ERR]${NC} $*" >&2; }

# -- 1. 前置检查 --
if [ ! -f .env ]; then
  err ".env 不存在"
  err "  cp .env.example .env  并填入 ENCRYPTION_KEY / WEB_PASSWORD / WEB_TOKEN"
  err "  生成命令：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  err "docker 未安装"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  err "docker compose v2 未安装（需要 Docker Engine 24.0+）"
  exit 1
fi

# -- 2. 检查 .env 必填项 --
for key in ENCRYPTION_KEY WEB_PASSWORD WEB_TOKEN; do
  if ! grep -q "^${key}=" .env || [ -z "$(grep "^${key}=" .env | cut -d= -f2)" ]; then
    err ".env 缺少或为空：${key}"
    err "  生成：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    exit 1
  fi
done

log "前置检查通过"

# -- 3. 命令分发 --
cmd="${1:-deploy}"

case "$cmd" in
  build)
    log "构建镜像..."
    docker compose build
    ;;
  up)
    log "启动容器..."
    docker compose up -d
    docker compose ps
    ;;
  down)
    log "停止容器..."
    docker compose down
    ;;
  logs)
    docker compose logs -f seedhelper
    ;;
  status)
    docker compose ps
    echo
    echo "--- /healthz ---"
    curl -fsS http://localhost:8080/healthz || warn "服务未响应（可能还在启动）"
    echo
    echo "--- 调度作业 ---"
    # 直接 grep .env 拿 WEB_TOKEN（subshell . .env 不会影响父 shell 变量）
    token=$(grep '^WEB_TOKEN=' .env | cut -d= -f2-)
    if [ -n "$token" ]; then
      curl -fsS -H "Authorization: Bearer ${token}" \
        http://localhost:8080/api/v1/scheduler \
        | python3 -c "import sys, json; d=json.load(sys.stdin); print(f'enabled={d[\"enabled\"]} inflight={d[\"inflight\"]} jobs={len(d[\"jobs\"])}')" \
        2>/dev/null || warn "调度 API 不可用"
    else
      warn "未找到 WEB_TOKEN（检查 .env）"
    fi
    ;;
  deploy|"")
    log "完整部署：build + up + 验证"
    docker compose build
    docker compose up -d
    docker compose ps

    log "等待健康检查（start_period=15s）..."
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 3
      if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then
        log "✓ 健康检查通过"
        curl -fsS http://localhost:8080/healthz
        echo
        log "Web UI:  http://localhost:8080"
        log "日志:    docker compose logs -f seedhelper"
        log "状态:    ./scripts/deploy.sh status"
        exit 0
      fi
      echo "  等待 $((i*3))s..."
    done

    err "健康检查 30s 内未通过"
    err "查日志：docker compose logs --tail=50 seedhelper"
    exit 1
    ;;
  help|-h|--help)
    cat <<'EOF'
SeedHelper 一键部署脚本

用法:
  ./scripts/deploy.sh              完整 build + up + 验证
  ./scripts/deploy.sh build        只构建镜像
  ./scripts/deploy.sh up           只启动容器
  ./scripts/deploy.sh down         停止容器
  ./scripts/deploy.sh logs         跟踪容器日志
  ./scripts/deploy.sh status       查看状态 + /healthz
  ./scripts/deploy.sh help         本帮助

前置：.env 已就绪（含 ENCRYPTION_KEY / WEB_PASSWORD / WEB_TOKEN）
详细：见 DEPLOY.md
EOF
    ;;
  *)
    err "未知命令：$cmd"
    echo "用法：$0 [build|up|down|logs|status]" >&2
    exit 1
    ;;
esac
