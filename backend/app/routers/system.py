"""系统级（全局）设置：业务参数（如游客功能开关）。仅 admin 可写。"""
from __future__ import annotations
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import SystemSetting, User
from app.deps import get_current_user

router = APIRouter(prefix="/api/system", tags=["system"])


def _sys_setting(db: Session, key: str) -> SystemSetting:
    return db.query(SystemSetting).filter(SystemSetting.key == key).first()


@router.get("/settings")
def get_system_setting(key: Optional[str] = None, db: Session = Depends(get_db)):
    """读取系统级设置。

    - 不需鉴权（登录页加载时调用，用来决定是否显示游客入口等）；
    - key 缺省时返回所有 key-value；
    - 单一 key 命中返回其 value_json 的解析结果；未命中返回 {"value": None}。
    """
    if key:
        r = _sys_setting(db, key)
        if not r:
            return {"key": key, "value": None, "exists": False}
        try:
            return {"key": key, "value": json.loads(r.value_json), "exists": True, "updated_at": r.updated_at}
        except Exception:
            return {"key": key, "value": None, "exists": True, "updated_at": r.updated_at}
    # 全量
    rows = db.query(SystemSetting).all()
    out = {}
    for r in rows:
        try:
            out[r.key] = json.loads(r.value_json)
        except Exception:
            out[r.key] = None
    return {"settings": out}


class SystemSettingIn(BaseModel):
    """系统设置入参：写一条记录；已存在则覆盖 value_json。"""
    key: str
    value: object = None


def _require_admin(user: User) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.post("/settings")
def save_system_setting(payload: SystemSettingIn, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """保存系统设置（仅 admin）。"""
    _require_admin(user)
    from app.models import _now
    r = _sys_setting(db, payload.key)
    if not r:
        r = SystemSetting(key=payload.key, value_json=json.dumps(payload.value, ensure_ascii=False),
                           updated_at=_now())
        db.add(r)
    else:
        r.value_json = json.dumps(payload.value, ensure_ascii=False)
        r.updated_at = _now()
    db.commit()
    return {"ok": True, "key": payload.key}