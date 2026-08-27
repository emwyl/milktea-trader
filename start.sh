#!/usr/bin/env bash
# Cloud Studio 工作空间 / 任意 Linux 一键启动脚本
# 用法: bash start.sh        (或 PORT=8080 bash start.sh)
# 说明: 首次随机生成 JWT 密钥并持久化到 backend/.secret，重启不失效（用户登录态稳定）
#       uvicorn 后台常驻，日志写 server.log，PID 写 server.pid
set -e
cd "$(dirname "$0")/backend"

# 1) 虚拟环境（已存在则跳过创建）
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 2) 生产密钥：首次随机生成并持久化，重启不失效
SECRET_FILE=".secret"
if [ ! -f "$SECRET_FILE" ]; then
  python3 -c 'import secrets;open(".secret","w").write(secrets.token_hex(16))'
fi
export STOCK_ADVISOR_SECRET="$(cat "$SECRET_FILE")"
export STOCK_ADVISOR_USER="${STOCK_ADVISOR_USER:-admin}"
export STOCK_ADVISOR_PASS="${STOCK_ADVISOR_PASS:-admin123}"

# 3) 若已有实例在跑，先停掉，避免端口冲突
if [ -f server.pid ] && kill -0 "$(cat server.pid)" 2>/dev/null; then
  echo ">>> 停止旧实例 PID=$(cat server.pid)"
  kill "$(cat server.pid)" 2>/dev/null || true
  sleep 1
fi

PORT="${PORT:-8003}"
echo ">>> 启动 milktea-trader on 0.0.0.0:${PORT} (后台运行)"
nohup uvicorn app.main:app --host 0.0.0.0 --port "$PORT" > server.log 2>&1 &
echo $! > server.pid
sleep 2
if kill -0 "$(cat server.pid)" 2>/dev/null; then
  echo ">>> 已启动 PID=$(cat server.pid)，查看日志: tail -f server.log"
  echo ">>> 停止:   kill \$(cat server.pid)"
else
  echo ">>> 启动失败，请查看 server.log:"
  tail -20 server.log
  exit 1
fi
