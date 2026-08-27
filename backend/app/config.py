"""全局配置：路径、密钥、可配置项。所有敏感配置从环境变量读取，本地文件不落明文密钥。"""
from __future__ import annotations
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "app.db"
SQLITE_URL = f"sqlite:///{DB_PATH}"

# 会话密钥：用于签发登录 token。生产请通过环境变量覆盖。
SECRET_KEY = os.getenv("STOCK_ADVISOR_SECRET", "change-me-in-prod-" + os.urandom(8).hex())
TOKEN_TTL_HOURS = int(os.getenv("STOCK_ADVISOR_TOKEN_TTL", "720"))  # 默认 30 天

# 初始化默认管理员账号（首次启动创建，可改）
DEFAULT_USERNAME = os.getenv("STOCK_ADVISOR_USER", "admin")
DEFAULT_PASSWORD = os.getenv("STOCK_ADVISOR_PASS", "admin123")

# 行情源：akshare 不可用时降级为内置演示数据，保证系统可运行
AKSHARE_ENABLED = os.getenv("STOCK_ADVISOR_AKSHARE", "1") != "0"

# 前端静态目录（由 main.py 挂载）
FRONTEND_DIR = BASE_DIR.parent / "frontend"
