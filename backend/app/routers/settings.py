"""通用设置：AI Key 等本地加密配置（按账号隔离）。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import UserSetting, User

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _setting(db, user_id: int, key: str):
    return db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).first()


@router.get("/ai")
def get_ai(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = _setting(db, user.id, "ai")
    cfg = json.loads(r.value_json) if r else {}
    # 不回传明文 key，仅回传是否已配置与模型/base_url
    return {"configured": bool(cfg.get("api_key")), "base_url": cfg.get("base_url", ""),
            "model": cfg.get("model", "")}


@router.post("/ai")
def save_ai(base_url: str = "", model: str = "", api_key: str = "", db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    cfg = {"base_url": base_url, "model": model, "api_key": api_key}
    r = _setting(db, user.id, "ai")
    if not r:
        r = UserSetting(user_id=user.id, key="ai")
        db.add(r)
    r.value_json = json.dumps(cfg)
    db.commit()
    return {"ok": True, "configured": bool(api_key)}


class RiskNoticeIn(BaseModel):
    """风控提示入参：用户自定义的首页顶部风控文本（按账号隔离）。"""
    content: str = ""


@router.get("/risk_notice")
def get_risk_notice(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """返回当前用户的首页风控提示；空串表示尚未设置。"""
    r = _setting(db, user.id, "risk_notice")
    content = json.loads(r.value_json).get("content", "") if r else ""
    return {"content": content, "updated": bool(r)}


@router.post("/risk_notice")
def save_risk_notice(payload: RiskNoticeIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """保存/清空首页风控提示；最长 500 字，超长截断。"""
    content = (payload.content or "").strip()[:500]
    r = _setting(db, user.id, "risk_notice")
    if not r:
        r = UserSetting(user_id=user.id, key="risk_notice")
        db.add(r)
    r.value_json = json.dumps({"content": content})
    db.commit()
    return {"ok": True, "content": content}
