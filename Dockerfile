# 加油赚奶茶钱 · 云服务器部署镜像
# 构建: docker build -t milktea-trader .
# 运行: docker compose up -d --build  (或 docker run -p 8000:8000 milktea-trader)
FROM python:3.12-slim

WORKDIR /app

# 系统依赖(akshare/pandas 运行需要的基础库,最小化)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖(先拷 requirements 单独装,利用 Docker 层缓存)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 代码
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

ENV PYTHONPATH=/app/backend
WORKDIR /app/backend

# 数据目录(挂载卷持久化 SQLite)
VOLUME ["/app/backend/data"]

EXPOSE 8000

# 0.0.0.0:任何来源可访问;生产建议前面套 nginx/HTTPS,并把默认账号改密
# 端口取环境变量 $PORT(Render 等平台注入),缺省 8000(本地/Compose 兼容)
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
