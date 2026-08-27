"""通用设置：AI Key 等本地加密配置（按账号隔离）。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends

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
