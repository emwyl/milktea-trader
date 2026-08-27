"""个股分析（箱体做T）路由。返回6大分区计算结果，并提供自定义支撑/压力、风控备注、持仓的本地读写。"""
from __future__ import annotations
from fastapi import APIRouter, Depends

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import StockTConfig, TrackedPool, User
from app.schemas import TConfigIn, TConfigOut, PositionIn
from app.services.t_analysis import analyze_t
from app.services.data_fetcher import ensure_stock_name

router = APIRouter(prefix="/api/t-analysis", tags=["t-analysis"])


@router.get("/{code}")
def t_analysis(code: str, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """返回某标的的 6 大分区做T分析结果。"""
    return analyze_t(code, db)


@router.get("/{code}/config")
def get_config(code: str, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """读取该标的的自定义支撑/压力、特殊风控备注。"""
    c = db.query(StockTConfig).filter(StockTConfig.user_id == user.id, StockTConfig.code == code).first()
    if not c:
        return TConfigOut(code=code, custom_support=None, custom_pressure=None, risk_note="")
    return TConfigOut(code=c.code, custom_support=c.custom_support,
                      custom_pressure=c.custom_pressure, risk_note=c.risk_note)


@router.put("/{code}/config")
def put_config(code: str, body: TConfigIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """保存自定义支撑/压力、特殊风控备注（存本机数据库，跨设备可读）。"""
    c = db.query(StockTConfig).filter(StockTConfig.user_id == user.id, StockTConfig.code == code).first()
    if not c:
        c = StockTConfig(user_id=user.id, code=code)
        db.add(c)
    c.custom_support = body.custom_support
    c.custom_pressure = body.custom_pressure
    c.risk_note = body.risk_note
    db.commit()
    return TConfigOut(code=c.code, custom_support=c.custom_support,
                      custom_pressure=c.custom_pressure, risk_note=c.risk_note)


@router.post("/{code}/position")
def upsert_position(code: str, body: PositionIn,
                    db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """保存/更新持仓股数与成本价（与可投池打通，用于算总浮盈浮亏）。不在池里则自动建一条。"""
    p = db.query(TrackedPool).filter(TrackedPool.user_id == user.id,
                                      TrackedPool.code == code, TrackedPool.status == "active").first()
    if not p:
        p = TrackedPool(user_id=user.id, code=code, scheme_type="custom")
        db.add(p)
    if body.position_qty is not None:
        p.position_qty = body.position_qty
    if body.cost_price is not None:
        p.cost_price = body.cost_price
    db.commit()
    # 名称补全:新落持仓的代码若 stocks 表里没名称,顺手从实时盘口拉一次回写
    # (失败也不影响本次保存,后续 list_pool 还会再尝试)
    ensure_stock_name(code, db)
    return {"ok": True, "code": code, "position_qty": p.position_qty, "cost_price": p.cost_price}
