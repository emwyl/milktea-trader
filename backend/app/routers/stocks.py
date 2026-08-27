"""股票与行情路由。"""
from __future__ import annotations
from fastapi import APIRouter, Depends, Query

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import Stock, User
from app.services.data_fetcher import ensure_quotes, get_sector_trend
from app.services.indicators import compute_snapshot, history_series
from app.services.industry_filler import seed_industries, get_first_level_industries
from app.services.warmup import start_warmup, warmup_status

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/search")
def search(q: str = "", limit: int = 30, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    pat = f"%{q}%"
    rows = db.query(Stock).filter((Stock.code.like(pat)) | (Stock.name.like(pat))).limit(limit).all()
    return [{"code": s.code, "name": s.name, "industry": s.industry, "market": s.market} for s in rows]


@router.get("/count")
def count(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """返回 stocks 表现有股票数(用于选股模型页显示"已导入 X 只")。"""
    return {"count": db.query(Stock).count()}


@router.post("/seed-all")
def seed_all(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """一键导入全 A 股(akshare 拉 5500+ 代码+名称),upsert 到 stocks 表。
    注:akshare 仅取代码列表(轻量,无行情);行情按需 ensure_quotes 拉取。
    重复执行安全(已存在的跳过,缺的补)。"""
    try:
        import akshare as ak
    except Exception as e:
        return {"ok": False, "msg": f"akshare 未安装: {e}"}
    try:
        df = ak.stock_info_a_code_name()
    except Exception as e:
        return {"ok": False, "msg": f"akshare 拉取失败: {e}"}
    # df 列:code, name
    added = updated = 0
    for _, row in df.iterrows():
        code = str(row["code"]).strip()
        name = str(row["name"]).strip()
        if not code or not name:
            continue
        s = db.get(Stock, code)
        if s:
            if s.name != name:
                s.name = name  # 同步名称(剔除空格差异)
                updated += 1
        else:
            db.add(Stock(code=code, name=name))
            added += 1
    db.commit()
    total = db.query(Stock).count()
    return {"ok": True, "added": added, "updated": updated, "total": total}


@router.post("/warmup")
def warmup(user: User = Depends(get_current_user)):
    """后台预热全 A 日线(50 worker 并发,增量:只拉无缓存的)。立即返回,不阻塞。"""
    return start_warmup()


@router.get("/warmup/status")
def warmup_status_ep(user: User = Depends(get_current_user)):
    return warmup_status()


@router.post("/seed-industries")
def seed_industries_ep(user: User = Depends(get_current_user)):
    """补全 stocks.industry 字段(申万一级)。多源 fallback,失败返回错误信息。"""
    return seed_industries()


@router.get("/industries")
def list_industries(user: User = Depends(get_current_user)):
    """返回申万一级行业列表(供前端做行业 chip 多选)。"""
    return {"industries": get_first_level_industries()}


@router.get("/{code}/quote")
def quote(code: str, days: int = 90, user: User = Depends(get_current_user)):
    quotes = ensure_quotes(code, days)
    if not quotes:
        return {"code": code, "error": "无行情数据"}
    snap = compute_snapshot(quotes)
    st = SessionLocal().query(Stock).filter(Stock.code == code).first()
    return {
        "code": code, "name": st.name if st else "", "industry": st.industry if st else "",
        "snapshot": snap.__dict__,
    }


@router.get("/{code}/history")
def history(code: str, days: int = 120, user: User = Depends(get_current_user)):
    quotes = ensure_quotes(code, days)
    return {"code": code, "series": history_series(quotes, days)}


@router.get("/sector/{industry}")
def sector(industry: str, user: User = Depends(get_current_user)):
    return get_sector_trend(industry)
