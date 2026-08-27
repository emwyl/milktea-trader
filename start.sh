#!/usr/bin/env bash
# Cloud Studio 工作空间 / 任意 Linux 一键启动脚本
# 用法: bash start.sh   (或 PORT=8080 bash start.sh)
set -e
cd "$(dirname "$0")/backend"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# 生产密钥：优先用环境变量，未设置则随机生成（重启会失效，建议 Cloud Studio 环境变量里固定）
export STOCK_ADVISOR_SECRET="${STOCK_ADVISOR_SECRET:-$(python3 -c 'import secrets;print(secrets.token_hex(16))')}"
export STOCK_ADVISOR_USER="${STOCK_ADVISOR_USER:-admin}"
export STOCK_ADVISOR_PASS="${STOCK_ADVISOR_PASS:-admin123}"
PORT="${PORT:-8003}"
echo ">>> 启动 milktea-trader on 0.0.0.0:${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
