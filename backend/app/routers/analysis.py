"""分析中心（单标的详情）路由。融合指标 + 信号 + 风控前置 + 跟踪。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import Signal, Stock, TrackedPool, User
from app.services.data_fetcher import ensure_quotes
from app.services.indicators import compute_snapshot, history_series

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("/{code}")
def analysis(code: str, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    st = db.query(Stock).filter(Stock.code == code).first()
    pool = db.query(TrackedPool).filter(TrackedPool.user_id == user.id,
                                        TrackedPool.code == code, TrackedPool.status == "active").first()
    quotes = ensure_quotes(code, 120)
    snap = compute_snapshot(quotes) if quotes else None
    signals = db.query(Signal).filter(Signal.user_id == user.id, Signal.code == code).order_by(Signal.id.desc()).limit(20).all()
    return {
        "code": code, "name": st.name if st else "", "industry": st.industry if st else "",
        "in_pool": pool is not None,
        "pool_note": pool.note if pool else "", "cost_price": pool.cost_price if pool else None,
        "position_pct": pool.position_pct if pool else None,
        "snapshot": snap.__dict__ if snap else None,
        "history": history_series(quotes, 120) if quotes else [],
        "signals": [{"id": s.id, "signal_type": s.signal_type, "reason": s.reason,
                     "risk_level": s.risk_level, "risk_advice": s.risk_advice,
                     "confidence": s.confidence, "status": s.status,
                     "generated_at": str(s.generated_at)} for s in signals],
    }
