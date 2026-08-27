"""选股模型路由（含方案类型）。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import Screen, ScreenResult, SchemeType, Stock, User
from app.schemas import ScreenIn, ScreenOut, CandidateOut
from app.services.screener import get_screener

router = APIRouter(prefix="/api/screens", tags=["screens"])


def _to_out(s: Screen) -> ScreenOut:
    return ScreenOut(id=s.id, name=s.name, description=s.description, is_active=s.is_active,
                     config=json.loads(s.config_json), scheme_type=s.scheme_type,
                     created_at=str(s.created_at), updated_at=str(s.updated_at))


@router.get("")
def list_screens(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Screen).filter(Screen.user_id == user.id).all()
    return [_to_out(s) for s in rows]


@router.post("")
def create_screen(body: ScreenIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    s = Screen(user_id=user.id, name=body.name, description=body.description, config_json=json.dumps(body.config),
               scheme_type=body.scheme_type)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _to_out(s)


@router.get("/{sid}")
def get_screen(sid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.query(Screen).filter(Screen.id == sid, Screen.user_id == user.id).first()
    if not s:
        raise HTTPException(404, "模型不存在")
    return _to_out(s)


@router.put("/{sid}")
def update_screen(sid: int, body: ScreenIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.query(Screen).filter(Screen.id == sid, Screen.user_id == user.id).first()
    if not s:
        raise HTTPException(404, "模型不存在")
    s.name = body.name
    s.description = body.description
    s.config_json = json.dumps(body.config)
    s.scheme_type = body.scheme_type
    s.updated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    db.commit()
    return _to_out(s)


@router.delete("/{sid}")
def delete_screen(sid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.query(Screen).filter(Screen.id == sid, Screen.user_id == user.id).first()
    if s:
        db.query(ScreenResult).filter(ScreenResult.screen_id == sid).delete()
        db.delete(s)
        db.commit()
    return {"ok": True}


@router.post("/{sid}/run")
def run_screen(sid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.query(Screen).filter(Screen.id == sid, Screen.user_id == user.id).first()
    if not s:
        raise HTTPException(404, "模型不存在")
    screener = get_screener()
    cands = screener.run(json.loads(s.config_json), db)
    # 记录结果快照
    db.query(ScreenResult).filter(ScreenResult.screen_id == sid).delete()
    for c in cands:
        db.add(ScreenResult(screen_id=sid, code=c.code, metrics_json=json.dumps(c.metrics)))
    db.commit()
    return [CandidateOut(code=c.code, name=c.name, industry=c.industry, metrics=c.metrics) for c in cands]


@router.post("/run-config")
def run_config(config: dict, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """直接跑一段 screener 参数(不落库)。用于「引导式建模」页:用户微调参数后即时试跑。
    与 /{sid}/run 的区别:不创建 Screen、不留 ScreenResult 快照。
    返回 { items, meta: {evaluated, total_stocks} } —— meta 告诉前端本次评估了多少只有日线缓存的股票。"""
    from app.services.screener import get_screener
    from app.models import Stock, DailyQuote
    from sqlalchemy import func
    try:
        # 评估范围:有 ≥ 30 根日线缓存的股票(screener.py 内部逻辑)
        sub = (db.query(DailyQuote.code, func.count(DailyQuote.id).label('cnt'))
               .group_by(DailyQuote.code).having(func.count(DailyQuote.id) >= 30).subquery())
        evaluated = db.query(Stock).join(sub, Stock.code == sub.c.code).count()
        total_stocks = db.query(Stock).count()
        cands = get_screener().run(config or {}, db)
    except Exception as e:
        raise HTTPException(400, f"参数有误: {e}")
    return {
        "items": [CandidateOut(code=c.code, name=c.name, industry=c.industry, metrics=c.metrics) for c in cands],
        "meta": {"evaluated": evaluated, "total_stocks": total_stocks},
    }


@router.get("/{sid}/results")
def screen_results(sid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (db.query(ScreenResult)
              .join(Screen, Screen.id == ScreenResult.screen_id)
              .filter(Screen.id == sid, Screen.user_id == user.id)
              .all())
    return [{"code": r.code, "matched_at": str(r.matched_at), "metrics": json.loads(r.metrics_json)} for r in rows]


@router.get("/scheme-types/list")
def scheme_types(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(SchemeType).all()
    return [{"key": r.key, "name": r.name, "risk_level": r.risk_level,
             "screener_json": json.loads(r.screener_json), "signal_json": json.loads(r.signal_json),
             "risk_json": json.loads(r.risk_json), "builtin": r.builtin} for r in rows]
