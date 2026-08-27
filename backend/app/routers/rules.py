"""加减仓规则路由。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import PositionRule, User
from app.schemas import RuleIn, RuleOut

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _to_out(r: PositionRule) -> RuleOut:
    return RuleOut(id=r.id, name=r.name, scope=r.scope, conditions=json.loads(r.conditions_json),
                   action=r.action, priority=r.priority, enabled=r.enabled, scheme_type=r.scheme_type)


@router.get("")
def list_rules(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    return [_to_out(r) for r in db.query(PositionRule).filter(PositionRule.user_id == user.id).all()]


@router.post("")
def create_rule(body: RuleIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = PositionRule(user_id=user.id, name=body.name, scope=body.scope, conditions_json=json.dumps(body.conditions),
                     action=body.action, priority=body.priority, enabled=body.enabled,
                     scheme_type=body.scheme_type)
    db.add(r)
    db.commit()
    db.refresh(r)
    return _to_out(r)


@router.put("/{rid}")
def update_rule(rid: int, body: RuleIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.query(PositionRule).filter(PositionRule.id == rid, PositionRule.user_id == user.id).first()
    if not r:
        raise HTTPException(404, "规则不存在")
    r.name = body.name
    r.scope = body.scope
    r.conditions_json = json.dumps(body.conditions)
    r.action = body.action
    r.priority = body.priority
    r.enabled = body.enabled
    r.scheme_type = body.scheme_type
    db.commit()
    return _to_out(r)


@router.delete("/{rid}")
def delete_rule(rid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.query(PositionRule).filter(PositionRule.id == rid, PositionRule.user_id == user.id).first()
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


@router.post("/{rid}/toggle")
def toggle_rule(rid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.query(PositionRule).filter(PositionRule.id == rid, PositionRule.user_id == user.id).first()
    if not r:
        raise HTTPException(404, "规则不存在")
    r.enabled = not r.enabled
    db.commit()
    return _to_out(r)
