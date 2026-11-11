#!/usr/bin/env bash
# SeedHelper 一键部署脚本（**仅 dev 环境**）。
#
# 用法：./scripts/deploy.sh [command]
#   command = build | up | down | logs | status | deploy | help
#     deploy（默认）= build + up + 30s healthcheck 等待
#
# 示例：
#   ./scripts/deploy.sh                    # dev 完整 deploy
#   ./scripts/deploy.sh up                 # 只启动 dev
#   ./scripts/deploy.sh logs               # 跟踪 dev 日志
#   ./scripts/deploy.sh status             # 查 dev 状态
#   ./scripts/deploy.sh help               # 帮助
#
# ⚠️ 生产环境（192.168.0.128 Unraid）**不走本脚本**：
#    - CI 只构建推送镜像，不部署（见 .github/workflows/docker-image.yml）
#    - 128 上手动执行：
#      cd /mnt/user/appdata/medialab
#      TAG=latest docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d
#    - 完整链路见 LOCAL_ENV.md §9「prod（128）的真实部署链路」
#
# 前置：dev 的 .env 已就绪（含 WEB_PASSWORD / WEB_TOKEN）
set -euo pipefail

cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err() { echo -e "${RED}[ERR]${NC} $*" >&2; }
hdr() { echo -e "\n${BLUE}========== $* ==========${NC}"; }

# -- 解析参数 --
CMD="${1:-deploy}"
shift || true

# -- dev 环境配置 --
ENV_FILE=".env"
CONTAINER="medialab-dev"
PORT=8092
dev_compose() { docker compose -f docker-compose.yml --env-file .env "$@"; }

# -- 前置检查 --
check_env() {
  local env_file="$1"
  if [ ! -f "$env_file" ]; then
    err "$env_file 不存在"
    err "  cp .env.example $env_file  并填入 WEB_PASSWORD / WEB_TOKEN"
    err "  生成命令：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    return 1
  fi
  for key in WEB_PASSWORD WEB_TOKEN; do
    if ! grep -q "^${key}=" "$env_file" || [ -z "$(grep "^${key}=" "$env_file" | cut -d= -f2)" ]; then
      err "$env_file 缺少或为空：${key}"
      err "  生成：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
      return 1
    fi
  done
  return 0
}

check_all() {
  if ! command -v docker >/dev/null 2>&1; then err "docker 未安装"; return 1; fi
  if ! docker compose version >/dev/null 2>&1; then err "docker compose v2 未安装"; return 1; fi
  return 0
}

# -- 核心动作 --
do_build() { hdr "[dev] build"; dev_compose build; }
do_up() { hdr "[dev] up"; dev_compose up -d; dev_compose ps; }
do_down() { hdr "[dev] down"; dev_compose down; }
do_logs() { dev_compose logs -f "$CONTAINER"; }
do_status() {
  hdr "[dev] status"
  dev_compose ps
  echo "--- /healthz (port $PORT) ---"
  curl -fsS "http://localhost:$PORT/healthz" 2>/dev/null || warn "服务未响应（可能还没启动）"
  echo
}

do_deploy() {
  hdr "[dev] 完整 deploy"
  if ! check_env "$ENV_FILE"; then return 1; fi
  do_build
  do_up

  log "[dev] 等待健康检查（start_period=15s）..."
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
      log "✓ [dev] 健康检查通过"
      curl -fsS "http://localhost:$PORT/healthz"
      echo
      log "[dev] Web UI:  http://localhost:$PORT"
      log "[dev] 日志:    ./scripts/deploy.sh logs"
      return 0
    fi
    echo "  [dev] 等待 $((i*3))s..."
  done
  err "[dev] 健康检查 30s 内未通过"
  err "查日志：docker logs $CONTAINER  或  ./scripts/deploy.sh logs"
  return 1
}

# -- 入口 --
if [ "$CMD" = "help" ]; then
  cat <<'EOF'
SeedHelper 一键部署脚本（**仅 dev 环境**）

用法:
  ./scripts/deploy.sh [command]

command:
  deploy    build + up + 30s 健康检查等待（默认）
  build     只构建镜像
  up        只启动容器
  down      停止容器
  logs      跟踪容器日志
  status    查看状态 + /healthz
  help      本帮助

示例:
  ./scripts/deploy.sh                # dev 完整 deploy
  ./scripts/deploy.sh up
  ./scripts/deploy.sh logs

前置：dev 的 .env 必须就绪（WEB_PASSWORD / WEB_TOKEN）

⚠️ 生产环境（192.168.0.128 Unraid）不走本脚本：
   CI 只构建推送镜像不部署；在 128 上手动执行
     cd /mnt/user/appdata/medialab
     TAG=latest docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d
   完整链路见 LOCAL_ENV.md §9。
EOF
  exit 0
fi

# 通用前置（只检查一次）
check_all || exit 1

do_$CMD
