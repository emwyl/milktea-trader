@echo off
chcp 65001 >nul
rem ============================================================
rem  加油赚奶茶钱 - 本地启动脚本（Windows）
rem  说明：本脚本会检查依赖、启动后端、自动打开浏览器。
rem  若启动失败，黑色窗口不会关闭，请把上面的红色报错发给我。
rem ============================================================
setlocal
set ROOT=D:\腾讯小龙虾\milktea-trader
set PY=%ROOT%\venv\Scripts\python.exe
set LOG=%ROOT%\startup.log

echo [1/4] 检查虚拟环境...
if not exist "%PY%" (
  echo [错误] 找不到虚拟环境：%PY%
  echo 请先运行一次“安装依赖.bat”（或联系我帮你装依赖）。
  pause
  exit /b 1
)

echo [2/4] 检查依赖（fastapi / uvicorn / akshare）...
"%PY%" -c "import fastapi, uvicorn, akshare" >nul 2>&1
if errorlevel 1 (
  echo [警告] 依赖未安装或不完整，正在尝试安装（可能需要几分钟）...
  "%PY%" -m pip install fastapi uvicorn sqlalchemy pandas numpy httpx apscheduler python-multipart "passlib[bcrypt]" bcrypt akshare >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo [错误] 依赖安装失败，详见 %LOG%
    pause
    exit /b 1
  )
)

echo [3/4] 启动后端服务（端口 8000）...
echo 启动时间：%date% %time% > "%LOG%"
start "" /min cmd /c ""%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >>"%LOG%" 2>&1"

echo [4/4] 等待服务就绪并打开浏览器...
set /a n=0
:wait
timeout /t 2 >nul
set /a n+=1
curl -s -m 2 http://127.0.0.1:8000/api/health >nul 2>&1
if not errorlevel 1 goto open
if %n% geq 30 (
  echo [错误] 服务 30 秒内未就绪，详见 %LOG%
  pause
  exit /b 1
)
goto wait

:open
start "" http://127.0.0.1:8000
echo 已打开浏览器。若页面空白，请检查 %LOG% 或告诉我。
echo （服务在后台运行，关闭窗口不会停止服务；停止请运行 stop.bat）
pause
endlocal
