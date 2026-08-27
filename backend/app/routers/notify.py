"""通知配置与测试路由。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import NotifyConfig, NotifyLog, User
from app.schemas import NotifyConfigIn, NotifyConfigOut
from app.services.notifier import get_notifier

router = APIRouter(prefix="/api/notify", tags=["notify"])


def _row(db, user_id: int):
    return db.query(NotifyConfig).filter(NotifyConfig.user_id == user_id).first()


@router.get("/config")
def get_config(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = _row(db, user.id)
    if not r:
        return NotifyConfigOut(channel="console", enabled=False, config={}, daily_review_cron="30 15 * * 1-5")
    return NotifyConfigOut(channel=r.channel, enabled=r.enabled, config=json.loads(r.config_json),
                           daily_review_cron=r.daily_review_cron)


@router.post("/config")
def save_config(body: NotifyConfigIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = _row(db, user.id)
    if not r:
        r = NotifyConfig(user_id=user.id)
        db.add(r)
    r.channel = body.channel
    r.enabled = body.enabled
    r.config_json = json.dumps(body.config)
    r.daily_review_cron = body.daily_review_cron
    db.commit()
    return {"ok": True}


@router.post("/test")
def test(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    r = _row(db, user.id)
    channel = r.channel if r else "console"
    cfg = json.loads(r.config_json) if r else {}
    notifier = get_notifier(channel)
    result = notifier.send("加油赚奶茶钱 · 通知测试", "这是一条测试消息，说明通知通道可用。", cfg)
    db.add(NotifyLog(user_id=user.id, channel=channel, content="测试消息", status="success" if result.get("ok") else "failed"))
    db.commit()
    return result
