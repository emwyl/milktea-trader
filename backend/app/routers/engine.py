"""规则引擎与信号、看板路由。"""
from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import PositionRule, Signal, TrackedPool, Stock, User
from app.schemas import SignalOut
from app.services.runner import run_engine, enrich_rule_info
from app.services.data_fetcher import ensure_quotes

router = APIRouter(prefix="/api/engine", tags=["engine"])


def _name(code: str, db) -> str | None:
    s = db.query(Stock).filter(Stock.code == code).first()
    return s.name if s else None


def _to_out(sig: Signal, db) -> SignalOut:
    # v173：反查触发规则，带出用户自定义的名称 / 风险等级 / 风控提示
    rname, rdetail, rlevel, rnotice = enrich_rule_info(db, sig.rule_id, sig.user_id, sig.reason)
    return SignalOut(id=sig.id, code=sig.code, name=_name(sig.code, db), signal_type=sig.signal_type,
                     action=sig.action, reason=sig.reason, risk_level=sig.risk_level,
                     risk_advice=sig.risk_advice, confidence=sig.confidence,
                     metrics=json.loads(sig.metrics_json), generated_at=str(sig.generated_at),
                     status=sig.status,
                     rule_name=rname, rule_detail=rdetail, rule_risk_level=rlevel, rule_notice=rnotice)


@router.post("/run")
def run_engine_endpoint(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    res = run_engine(db, user)
    return {"signals": [_to_out(s, db) for s in res["signals"]], "count": res["count"],
            "msg": res.get("msg", "")}


@router.get("/signals")
def list_signals(status: str = Query("pending"), db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(Signal).filter(Signal.user_id == user.id)
    if status and status != "all":
        q = q.filter(Signal.status == status)
    rows = q.order_by(Signal.id.desc()).all()
    return [_to_out(s, db) for s in rows]


@router.put("/signals/{sid}")
def update_signal(sid: int, status: str, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.query(Signal).filter(Signal.id == sid, Signal.user_id == user.id).first()
    if not s:
        raise HTTPException(404, "信号不存在")
    s.status = status
    db.commit()
    return _to_out(s, db)


@router.get("/dashboard")
def dashboard(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    # 池子范围与 /api/pool、run_engine 保持一致:active + (archive 且有持仓),按 code 去重
    rows = (db.query(TrackedPool)
              .filter(TrackedPool.user_id == user.id,
                      (TrackedPool.status == "active") |
                      ((TrackedPool.status == "archive") & (TrackedPool.position_qty > 0)))
              .all())
    seen: dict[str, TrackedPool] = {}
    for p in rows:
        cur = seen.get(p.code)
        if cur is None or (p.position_qty and (not cur.position_qty or p.position_qty > cur.position_qty)):
            seen[p.code] = p
    pool_rows = list(seen.values())
    pool_codes = [p.code for p in pool_rows]
    pending = db.query(Signal).filter(Signal.user_id == user.id, Signal.status == "pending").all()
    industry_dist = {}
    up = down = 0
    for p in pool_rows:
        st = db.query(Stock).filter(Stock.code == p.code).first()
        # 防御:stocks.industry 为空或 None 时归"未知"标签,避免空 key 污染 dict
        ind = (st.industry if st else "") or "未知"
        industry_dist[ind] = industry_dist.get(ind, 0) + 1
    # v180 性能:大盘环境(可投池上涨/下跌)从「串行逐只拉行情」改为并发,避免登录看板首屏被拖到 10s+
    def _one_dir(p) -> int:
        """返回 1=涨(含平) -1=跌 0=无数据,口径与旧串行一致:chg>=0 计 up。"""
        try:
            qs = ensure_quotes(p.code, 5)
            if qs:
                last = qs[-1]
                if last.pre_close:
                    chg = (last.close - last.pre_close) / last.pre_close * 100
                    return 1 if chg >= 0 else -1
                return 0
            return 0
        except Exception:
            return 0
    if pool_rows:
        workers = min(len(pool_rows), 8)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for d in ex.map(_one_dir, pool_rows):
                if d > 0:
                    up += 1
                elif d < 0:
                    down += 1
    risk_alerts = sum(1 for s in pending if s.risk_level == "高")
    add_n = sum(1 for s in pending if s.signal_type == "add")
    reduce_n = sum(1 for s in pending if s.signal_type == "reduce")
    recent = [_to_out(s, db) for s in pending[:10]]
    return {
        "pool_count": len(pool_codes), "pending_signals": len(pending),
        "risk_alerts": risk_alerts, "add_signals": add_n, "reduce_signals": reduce_n,
        "industry_dist": industry_dist, "recent_signals": recent,
        "market_env": {"up": up, "down": down,
                       "note": "涨跌家数基于可投池样本，仅供参考；完整大盘环境二期接入涨跌家数指数"},
    }
