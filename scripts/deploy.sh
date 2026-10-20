#!/usr/bin/env bash
# SeedHelper 一键部署脚本（支持 dev / prod 两套环境）。
#
# 用法：./scripts/deploy.sh [env] [command]
#   env     = dev | prod | all（默认 dev）
#   command = build | up | down | logs | status | deploy | help
#     deploy（默认）= build + up + 30s healthcheck 等待
#
# 示例：
#   ./scripts/deploy.sh                    # dev 完整 deploy
#   ./scripts/deploy.sh prod               # prod 完整 deploy
#   ./scripts/deploy.sh all deploy         # dev + prod 都 deploy
#   ./scripts/deploy.sh dev up             # 只启动 dev
#   ./scripts/deploy.sh prod logs          # 跟踪 prod 日志
#   ./scripts/deploy.sh status             # 查 dev 状态
#   ./scripts/deploy.sh all status          # 查两套状态
#   ./scripts/deploy.sh help               # 帮助
#
# 前置：每套环境各自的 .env / .env.prod 已就绪（含 WEB_PASSWORD / WEB_TOKEN）
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
ENV="${1:-dev}"
shift || true
CMD="${1:-deploy}"
shift || true

case "$ENV" in
  dev|prod) ENV_FILE=".env"           CONTAINER="seedhelper-$ENV"    PORT=$([ "$ENV" = dev ] && echo 8080 || echo 8091) ;;
  all)       ENV="all"                CONTAINER=""                    PORT=""    ;;
  help|-h|--help) CMD="help" ;;
  *) err "未知环境：$ENV（仅支持 dev / prod / all）"; exit 1 ;;
esac

# -- 每套环境的配置映射 --
dev_compose() { echo "docker compose -f docker-compose.yml --env-file .env"; }
prod_compose() { echo "docker compose -f docker-compose.prod.yml --env-file .env.prod"; }

# -- 单环境前置检查 --
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

# -- 核心动作（对单一 env）--
do_build() { local env="$1"; local cf; cf=$(eval "$( [ "$env" = dev ] && echo dev_compose || echo prod_compose )"); hdr "[$env] build"; $cf build; }
do_up() { local env="$1"; local cf; cf=$(eval "$( [ "$env" = dev ] && echo dev_compose || echo prod_compose )"); hdr "[$env] up"; $cf up -d; $cf ps; }
do_down() { local env="$1"; local cf; cf=$(eval "$( [ "$env" = dev ] && echo dev_compose || echo prod_compose )"); hdr "[$env] down"; $cf down; }
do_logs() {
  local env="$1"
  local cf
  cf=$(eval "$( [ "$env" = dev ] && echo dev_compose || echo prod_compose )")
  # all 模式下不能 -f（dev 的 -f 永不退出，prod logs 永远执行不到）
  if [ "$ENV" = "all" ]; then
    $cf logs --tail=200 seedhelper-$env
  else
    $cf logs -f seedhelper-$env
  fi
}
do_status() {
  local env="$1"
  local port=$([ "$env" = dev ] && echo 8080 || echo 8091)
  local cf
  cf=$(eval "$( [ "$env" = dev ] && echo dev_compose || echo prod_compose )")
  hdr "[$env] status"
  $cf ps
  echo "--- /healthz (port $port) ---"
  curl -fsS "http://localhost:$port/healthz" 2>/dev/null || warn "服务未响应（可能还没启动）"
  echo
}

do_deploy() {
  local env="$1"
  local env_file=".env"
  local port=8080
  [ "$env" = prod ] && { env_file=".env.prod"; port=8091; }

  hdr "[$env] 完整 deploy"
  if ! check_env "$env_file"; then return 1; fi
  do_build "$env"
  do_up "$env"

  log "[$env] 等待健康检查（start_period=15s）..."
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    if curl -fsS "http://localhost:$port/healthz" >/dev/null 2>&1; then
      log "✓ [$env] 健康检查通过"
      curl -fsS "http://localhost:$port/healthz"
      echo
      log "[$env] Web UI:  http://localhost:$port"
      log "[$env] 日志:    ./scripts/deploy.sh $env logs"
      return 0
    fi
    echo "  [$env] 等待 $((i*3))s..."
  done
  err "[$env] 健康检查 30s 内未通过"
  err "查日志：docker logs seedhelper-$env  或  ./scripts/deploy.sh $env logs"
  return 1
}

# -- 入口 --
if [ "$CMD" = "help" ]; then
  cat <<'EOF'
SeedHelper 一键部署脚本（dev / prod 双环境）

用法:
  ./scripts/deploy.sh [env] [command]

env:
  dev      开发环境（端口 8080，数据 data-dev/，.env）
  prod     生产环境（端口 8091，数据 data-prod/，.env.prod）
  all      两套都操作

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
  ./scripts/deploy.sh prod           # prod 完整 deploy
  ./scripts/deploy.sh all deploy     # dev + prod 都 deploy
  ./scripts/deploy.sh dev up
  ./scripts/deploy.sh prod logs

前置：dev .env / prod .env.prod 都必须就绪
详细：见 DEPLOY.md
EOF
  exit 0
fi

# 通用前置（只检查一次）
check_all || exit 1

if [ "$ENV" = "all" ]; then
  # all 模式：遍历 dev + prod
  failed=0
  for e in dev prod; do
    do_$CMD "$e" || failed=1
  done
  exit $failed
else
  do_$CMD "$ENV"
fi
