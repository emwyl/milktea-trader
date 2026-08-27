@echo off
chcp 65001 >nul
rem 重置 admin 密码为 admin123（如果你改过密码忘了，验证后重置）
set ROOT=D:\腾讯小龙虾\milktea-trader
set PY=%ROOT%\venv\Scripts\python.exe
if not exist "%PY%" (
  echo [错误] 找不到虚拟环境 %PY%
  pause
  exit /b 1
)
echo 正在重置 admin 密码为 admin123 ...
"%PY%" -c "
import sys
sys.path.insert(0, r'%ROOT%\\backend')
from app.db import SessionLocal
from app.models import User
from app.security import hash_password
db = SessionLocal()
u = db.query(User).filter(User.username=='admin').first()
if not u:
    print('未找到 admin 用户，请确认数据库')
else:
    salt, ph = hash_password('admin123')
    u.salt = salt
    u.password_hash = ph
    db.commit()
    print('OK: admin 密码已重置为 admin123')
db.close()
"
echo.
echo 重新打开浏览器访问 http://127.0.0.1:8000 ，用 admin / admin123 登录。
pause
