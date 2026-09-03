"""FastAPI 入口：建表、种子、挂载路由与前端静态、启动定时任务。"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIR
from app.db import engine, Base, SessionLocal, run_migrations
from app.models import AccessLog
from app.security import verify_token
from app.seed import seed_all
from app.scheduler import start_scheduler

import threading
import datetime as dt

from app.routers import auth, stocks, screens, pool, rules, analysis, preference, notify, settings, t_analysis
from app.routers import engine as engine_router
from app.routers import data as data_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)  # 首次建表（含新增表 user_settings 等）
    run_migrations(engine)  # 为旧库补充 user_id/role 等列，并把无主数据归到 admin
    # 兼容旧库：为 tracked_pool 补加 position_qty 列（已存在则忽略）
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tracked_pool ADD COLUMN position_qty FLOAT"))
    except Exception:
        pass  # 列已存在时 SQLite 报错，忽略
    seed_all()
    start_scheduler()
    yield


app = FastAPI(title="加油赚奶茶钱", version="0.1.0", lifespan=lifespan)


# 禁用所有响应的浏览器缓存(避免用户改数据后看到陈旧响应,这是个人工具,实时性优先)
@app.middleware("http")
async def _no_cache(request: Request, call_next):
    # 记录"进入网站"事件（GET 首页），无 token 的游客也记录
    if request.method == "GET" and request.url.path in ("/", "/index.html"):
        _record_page_visit(request)
    response: Response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _record_page_visit(request: Request):
    """后台线程异步记录一次页面访问（含游客）。"""
    auth = request.headers.get("Authorization")
    x_token = request.headers.get("X-Auth-Token")
    token = None
    if x_token and x_token.strip():
        token = x_token.strip()
    elif auth and auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1]
    username = verify_token(token) if token else None
    is_guest = not username or (username and username.startswith("游客_"))
    ip = request.client.host if request.client else None
    ua = (request.headers.get("User-Agent", "") or "")[:512]
    threading.Thread(
        target=_write_access_log,
        args=(request.url.path, "GET", ip, ua, username, is_guest, "page"),
        daemon=True,
    ).start()


def _write_access_log(path, method, ip, ua, username, is_guest, event_type):
    try:
        db = SessionLocal()
        db.add(AccessLog(
            ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            ip=ip, ua=ua, path=path, method=method,
            username=username, is_guest=is_guest, event_type=event_type,
        ))
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


for r in (auth, stocks, screens, pool, rules, engine_router, analysis, preference, notify, settings, t_analysis, data_router):
    app.include_router(r.router)

@app.get("/api/health")
def health():
    return {"ok": True, "name": "加油赚奶茶钱"}


# 托管前端（D 盘 frontend 目录）。StaticFiles 挂在最后，/api 路由优先匹配。
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
