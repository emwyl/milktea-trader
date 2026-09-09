#!/bin/sh
cd "$(dirname "$0")"
# 固定会话密钥：沙箱多进程/多实例时，若用默认随机 SECRET_KEY 会导致 A 进程签发的 token 在 B 进程验签失败（登录失效）
export STOCK_ADVISOR_SECRET="milktea-trader-prod-2026-fixed-key"
export STOCK_ADVISOR_AKSHARE="0"
PY=$(command -v python3 || command -v python)
echo "python=$PY PORT=${PORT:-8000}"
# 清掉残留的旧 uvicorn 进程:若沙箱同时存在多个实例且 SECRET_KEY 不一致,
# 会出现「登录成功但调接口 401 登录失效」(A 签发 token, B 验签失败)。
# 2026-09-09: 启动前先杀旧进程,保证只有一个实例在用同一份密钥。
pkill -f "app.main:app" 2>/dev/null || echo "no old uvicorn process"
sleep 2
# 启动前自愈行情库:合并随包上传的干净日线种子(quotes.seed)进运行库 app.db,
# 只覆盖缺失/滞后/带 demo 假行(成交量小数)的代码日线,不动业务数据(详见 seed_quotes.py)
# 2026-09-08: 加 150s 硬超时 —— 自愈脚本若慢/卡住,不得拖住 uvicorn 启动
if command -v timeout >/dev/null 2>&1; then
  timeout 150 "$PY" backend/seed_quotes.py || echo "WARN seed_quotes timeout/failed(忽略,继续启动)"
else
  "$PY" backend/seed_quotes.py || echo "WARN seed_quotes failed(忽略,继续启动)"
fi
exec "$PY" -m uvicorn --app-dir backend app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
