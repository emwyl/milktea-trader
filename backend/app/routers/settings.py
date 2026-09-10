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


class ColumnSettingsIn(BaseModel):
    """v186: 表格列设置（按账号 + 表格 key 隔离）。

    settings 形如: { "<表格key>": [{"key": "...", "visible": true, "frozen": false, "order": 0}, ...] }
    表格 key 由前端定义(pool / pretrade / review / screens / adminUsers / accessLog ...)。
    """
    settings: dict = {}


@router.get("/column_settings")
def get_column_settings(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """读取当前账号的表格列设置；未设置过返回空对象（前端回落本地缓存/默认值）。"""
    r = _setting(db, user.id, "column_settings")
    if not r:
        return {"settings": {}, "updated": False}
    try:
        raw = json.loads(r.value_json) or {}
    except Exception:
        raw = {}
    # 兼容两种落库形态：{"settings": {...}} 或直接 {...}
    data = raw.get("settings", raw) if isinstance(raw, dict) else {}
    return {"settings": data if isinstance(data, dict) else {}, "updated": True}


@router.put("/column_settings")
def save_column_settings(payload: ColumnSettingsIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """整份覆盖式保存当前账号的列设置（前端每次改动都发全量，避免增量合并歧义）。"""
    data = payload.settings if isinstance(payload.settings, dict) else {}
    # 体积保护：最多 100 张表、每表 200 列，超出直接拒（防止异常数据撑爆单行文本）
    if len(data) > 100 or any(isinstance(v, list) and len(v) > 200 for v in data.values()):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="列设置数据过大")
    r = _setting(db, user.id, "column_settings")
    if not r:
        r = UserSetting(user_id=user.id, key="column_settings")
        db.add(r)
    r.value_json = json.dumps({"settings": data}, ensure_ascii=False)
    db.commit()
    return {"ok": True, "count": len(data)}
