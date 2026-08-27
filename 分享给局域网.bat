@echo off
chcp 65001 >nul
rem ============================================================
rem  加油赚奶茶钱 - 局域网分享启动脚本
rem  本机启动后,同一局域网的同事用 http://本机IP:8000 打开
rem  上公网给别人用:见 README「云服务器部署」章节
rem ============================================================
setlocal
set ROOT=D:\腾讯小龙虾\milktea-trader
set PY=%ROOT%\venv\Scripts\python.exe

echo [1/3] 显示本机局域网 IP(供同事访问)...
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo   局域网地址: http://%%a:8000

echo [2/3] 启动后端服务(监听 0.0.0.0:8000)...
start "" /min cmd /c ""%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

echo [3/3] 等待就绪...
set /a n=0
:wait
timeout /t 2 >nul
set /a n+=1
curl -s -m 2 http://127.0.0.1:8000/api/health >nul 2>&1
if not errorlevel 1 goto open
if %n% geq 30 (
  echo [错误] 服务 30 秒内未就绪,请检查 8000 端口占用
  pause
  exit /b 1
)
goto wait

:open
echo 本机打开: http://127.0.0.1:8000
echo 局域网同事打开: 上面的 局域网地址 (防火墙需放行 8000 端口)
echo 关闭本窗口不会停止服务;停止请运行 stop.bat
pause
endlocal
