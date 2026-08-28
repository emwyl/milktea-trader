#!/usr/bin/env bash
# Cloud Studio 前台启动脚本
# 用法: bash start-cloudstudio.sh        (或 PORT=8080 bash start-cloudstudio.sh)
# 说明: 与 start.sh 不同，本脚本让 uvicorn 前台运行，Cloud Studio 的「端口插件」会自动识别 8003 并给出访问链接。
set -e
cd "$(dirname "$0")/backend"

# 1) 虚拟环境（已存在则跳过创建）
if [ ! -d venv ]; then
  # 部分精简镜像缺 venv 模块，先补装
  python3 -m venv venv 2>/dev/null || { apt-get update -qq && apt-get install -y python3-venv python3-pip; python3 -m venv venv; }
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
# 国内环境优先用腾讯云 PyPI 镜像，更快更稳（失败自动回退官方源）
pip install -r requirements.txt -q -i https://mirrors.cloud.tencent.com/pypi/simple/ || pip install -r requirements.txt -q

# 2) 生产密钥：首次随机生成并持久化，重启不失效
SECRET_FILE=".secret"
if [ ! -f "$SECRET_FILE" ]; then
  python3 -c 'import secrets;open(".secret","w").write(secrets.token_hex(16))'
fi
export STOCK_ADVISOR_SECRET="$(cat "$SECRET_FILE")"
export STOCK_ADVISOR_USER="${STOCK_ADVISOR_USER:-admin}"
export STOCK_ADVISOR_PASS="${STOCK_ADVISOR_PASS:-admin123}"

# 3) 清掉已占用 8003 的旧进程（包括之前 start.sh 起的后台实例）
PORT="${PORT:-8003}"
echo ">>> 清理旧实例 ..."
# 先按进程名强杀（兼容 start.sh 起的 nohup 后台）
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "python.*app/main.py" 2>/dev/null || true
# 再按端口/ PID 文件补刀
PID=$(ss -ltnp 2>/dev/null | grep ":$PORT " | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
if [ -n "$PID" ]; then
  echo ">>> 端口 $PORT 被 PID=$PID 占用，先停止旧进程"
  kill "$PID" 2>/dev/null || true
fi
if [ -f server.pid ] && kill -0 "$(cat server.pid)" 2>/dev/null; then
  echo ">>> 停止旧实例 PID=$(cat server.pid)"
  kill "$(cat server.pid)" 2>/dev/null || true
fi
# 等待端口释放 + SQLite 文件锁释放
for i in 1 2 3 4 5; do
  if ! ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    break
  fi
  echo ">>> 等待端口 $PORT 释放 ($i/5)"
  sleep 1
done
# 若仍有残留，强制终止
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo ">>> 端口仍被占用，强制结束相关进程"
  pkill -9 -f "uvicorn" 2>/dev/null || true
  pkill -9 -f "python.*app/main" 2>/dev/null || true
  sleep 1
fi

echo ">>> 启动 milktea-trader on 0.0.0.0:${PORT} (前台运行，Cloud Studio 端口插件将自动识别)"
PID_HINT="$(cat server.pid 2>/dev/null || echo '某PID')"
echo ">>> 如需停止：按 Ctrl+C，或在另一个终端执行 kill $PID_HINT"
uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
