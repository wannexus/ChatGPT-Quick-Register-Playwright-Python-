#!/usr/bin/env bash
# 启动 ChatGPT Quick Register 的 Web UI
# 用法：./start_webui.sh [端口]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-${QR_WEB_PORT:-8765}}"
HOST="${QR_WEB_HOST:-127.0.0.1}"

# 自动激活 .venv（如果存在）
if [ -d ".venv" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# 检查依赖
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
  echo "缺 fastapi / uvicorn，正在 pip 安装..."
  python3 -m pip install -r requirements.txt
}

echo "==> 启动 Web UI: http://${HOST}:${PORT}/"
QR_WEB_HOST="${HOST}" QR_WEB_PORT="${PORT}" exec python3 webui/server.py
