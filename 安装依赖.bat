@echo off
chcp 65001 >nul
rem 安装「加油赚奶茶钱」所需 Python 依赖（只需运行一次）
set ROOT=D:\腾讯小龙虾\milktea-trader
set PY=%ROOT%\venv\Scripts\python.exe
if not exist "%PY%" (
  echo [错误] 找不到虚拟环境，请确认 D:\腾讯小龙虾\milktea-trader\venv 存在。
  pause
  exit /b 1
)
echo 正在安装依赖（可能需要几分钟，请耐心等待）...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install fastapi uvicorn sqlalchemy pandas numpy httpx apscheduler python-multipart "passlib[bcrypt]" bcrypt akshare
if errorlevel 1 (
  echo [失败] 依赖安装出错，请检查网络后重试，或把报错发给我。
  pause
  exit /b 1
)
echo [完成] 依赖已安装。现在可以双击 start.bat 启动。
pause
