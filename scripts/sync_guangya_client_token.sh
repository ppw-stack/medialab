#!/usr/bin/env bash
# 定时把 macOS 客户端的最新 access_token 同步进 medialab（覆盖层）。
#
# 为什么需要：客户端 token 只有 2 小时有效期，而且**不要**用 refresh_token
# 自己刷（guangyaclient 把 client_id 硬编码成 Web 值，且可能轮换 refresh_token
# 把客户端踢下线）。客户端自己会刷新并写回 Credentials.db，定时重读即可。
#
# 依赖：本机能读客户端数据目录；ssh 免密到生产机；生产容器已部署新版
# GuangyaCredential（旧代码 from_json 会丢弃 client_* 字段）。
#
# 环境变量：
#   MEDIALAB_DRIVER_ID   目标驱动 id（默认 5 = 小号3）
#   MEDIALAB_SSH_HOST    ssh 别名（默认 unraid）
#   MEDIALAB_REPO        仓库路径（默认本脚本所在仓库）
#   MEDIALAB_APP_PATH    容器内 app 包路径（默认 /app；临时注入时用 /tmp/gyapp）
#
# cron（每 30 分钟）：
#   */30 * * * * /Users/pw/orca/workspaces/medialab/seabass/scripts/sync_guangya_client_token.sh >> /tmp/gy-sync.log 2>&1
#
# launchd（每 1800 秒）：
#   见 AGENTS.md「光鸭客户端 token 覆盖层」；plist 里 ProgramArguments 指向本脚本。
set -euo pipefail

DRIVER_ID="${MEDIALAB_DRIVER_ID:-5}"
SSH_HOST="${MEDIALAB_SSH_HOST:-unraid}"
REPO="${MEDIALAB_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_PATH="${MEDIALAB_APP_PATH:-/app}"

cd "$REPO"

# 1) 本机导出（只读客户端库；失败直接退出，不推送旧 token）
TOKEN_JSON="$(uv run python scripts/export_guangya_client_token.py)"
if [[ -z "$TOKEN_JSON" ]]; then
  echo "导出为空，放弃同步" >&2
  exit 1
fi

# 2) base64 传过 ssh（避免 JSON 里的引号/空格被 shell 拆开）
TOKEN_B64="$(printf '%s' "$TOKEN_JSON" | base64 | tr -d '\n')"

# 3) 容器内写覆盖层（凭据不出容器；脚本从 stdin 进）
ssh "$SSH_HOST" \
  "docker exec -i -e GUANGYA_CLIENT_TOKEN_B64='$TOKEN_B64' \
   -e MEDIALAB_DRIVER_ID=$DRIVER_ID -e MEDIALAB_APP_PATH=$APP_PATH \
   medialab /opt/venv/bin/python - " \
  < scripts/import_guangya_client_token.py
