"""共享的引擎运行逻辑：供 API 路由与定时任务复用。"""
from __future__ import annotations
import json
import re

from app.models import PositionRule, Signal, TrackedPool, SchemeType, User
from app.services.rule_engine import get_signal_engine
from app.services.risk import get_risk_advisor


def rule_level_text(priority: int | None) -> str:
    """v173：priority int → 风险等级文案（与前端 riskLevelOf 保持完全一致）。
    >=8 紧急（最先触发）；5~7 重要；<=4 关注（最后触发）。"""
    p = priority or 0
    if p >= 8:
        return "紧急"
    if p >= 5:
        return "重要"
    return "关注"


def enrich_rule_info(db, rule_id, user_id, reason: str):
    """反查触发信号的规则，返回 (rule_name, rule_detail, rule_risk_level, rule_notice)。

    - 新信号：按 rule_id 直接查（run_engine 已写入 Signal.rule_id）。
    - 旧信号：rule_id 为空时，从 reason 的 "[规则名] 条件描述" 前缀解析规则名再按 (user, name) 查，
      保证升级后历史信号也能显示风险等级/风控提示，而不是一片空白。
    - 查不到规则时降级为 (解析出的规则名, 条件描述, None, None)，前端回落到系统自动建议。
    """
    detail = reason or ""
    name_from_reason = None
    m = re.match(r"^\[(.*?)\]\s*", detail)
    if m:
        name_from_reason = m.group(1)
        detail = detail[m.end():]
    rule = None
    if rule_id:
        rule = db.query(PositionRule).filter(PositionRule.id == rule_id).first()
    if rule is None and name_from_reason and user_id is not None:
        rule = (db.query(PositionRule)
                  .filter(PositionRule.user_id == user_id, PositionRule.name == name_from_reason)
                  .first())
    if rule is None:
        return name_from_reason, detail, None, None
    notice = (getattr(rule, "risk_notice", "") or "").strip() or None
    return rule.name, detail, rule_level_text(rule.priority), notice


def run_engine(db, user: User | None = None) -> dict:
    """对可投池跑全部启用规则 → 生成 pending 信号(含风控软引导)。返回 {count, signals}。

    池子范围与 /api/pool 保持一致:「active」+「archive 但有持仓」,同一 code 去重
    保留有持仓的那条。否则会出现「可投池能看到这只票、引擎却没跑」的割裂体验。
    """
    user_id = user.id if user else None
    rows = (db.query(TrackedPool)
              .filter(TrackedPool.user_id == user_id,
                      (TrackedPool.status == "active") |
                      ((TrackedPool.status == "archive") & (TrackedPool.position_qty > 0)))
              .all())
    # 同一 code 重复时优先保留有持仓的那条(与 list_pool 一致)
    seen: dict[str, TrackedPool] = {}
    for p in rows:
        cur = seen.get(p.code)
        if cur is None or (p.position_qty and (not cur.position_qty or p.position_qty > cur.position_qty)):
            seen[p.code] = p
    pool = [p.code for p in seen.values()]
    if not pool:
        return {"count": 0, "signals": [], "msg": "可投池为空(可手工加代码或选股后推送)"}
    rules = db.query(PositionRule).filter(PositionRule.user_id == user_id, PositionRule.enabled == True).all()
    if not rules:
        return {"count": 0, "signals": [], "msg": "无启用规则"}
    # v173：带上规则 id，让信号能反查到是哪条规则触发的（信号表要展示规则的名称/等级/风控提示）
    rule_dicts = [{"id": r.id, "name": r.name, "scope": r.scope, "conditions_json": json.loads(r.conditions_json),
                   "action": r.action, "priority": r.priority, "scheme_type": r.scheme_type} for r in rules]
    engine = get_signal_engine()
    raw = engine.evaluate(pool, rule_dicts, db)
    advisor = get_risk_advisor()
    db.query(Signal).filter(Signal.user_id == user_id, Signal.status == "pending").delete()
    stored = []
    for sig in raw:
        rc = {}
        if sig.metrics.get("scheme_type"):
            st = db.query(SchemeType).filter(SchemeType.key == sig.metrics["scheme_type"]).first()
            if st:
                rc = json.loads(st.risk_json)
        sig = advisor.assess(sig, rc)
        row = Signal(user_id=user_id, code=sig.code, rule_id=getattr(sig, "rule_id", None),
                     signal_type=sig.signal_type, action=sig.action,
                     reason=sig.reason, metrics_json=json.dumps(sig.metrics),
                     risk_level=sig.risk_level, risk_advice=sig.risk_advice, confidence=sig.confidence)
        db.add(row)
        stored.append(row)
    db.commit()
    return {"count": len(stored), "signals": stored}
