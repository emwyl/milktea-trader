@echo off
chcp 65001 >nul
rem 停止「加油赚奶茶钱」本地服务（关闭 8000 端口的 uvicorn）
echo 正在停止服务...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
  taskkill /pid %%a /f >nul 2>&1
)
echo 已尝试停止。若仍占用 8000 端口，请手动结束 uvicorn 进程。
pause
