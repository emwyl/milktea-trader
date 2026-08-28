"""定时任务：收盘后更新行情 → 跑规则引擎 → 风险优先推送复盘。"""
from __future__ import annotations
import json

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import SessionLocal
from app.models import NotifyConfig, NotifyLog, Signal, TrackedPool, User
from app.routers.data import run_auto_backup
from app.services.runner import run_engine
from app.services.notifier import get_notifier
from app.services.data_fetcher import ensure_quotes
from app.services.warmup import start_warmup


def _notify_user(db, user: User):
    cfg = db.query(NotifyConfig).filter(NotifyConfig.user_id == user.id).first()
    if not cfg or not cfg.enabled:
        return
    pending = db.query(Signal).filter(Signal.user_id == user.id, Signal.status == "pending").all()
    if not pending:
        return
    risk = [s for s in pending if s.risk_level == "高"]
    add = [s for s in pending if s.signal_type == "add"]
    reduce = [s for s in pending if s.signal_type == "reduce"]
    lines = [f"【加油赚奶茶钱 · 每日复盘 · {user.username}】",
             f"⚠️ 高风险信号: {len(risk)} 条（优先关注）",
             f"加仓信号: {len(add)} 条 | 减仓信号: {len(reduce)} 条 | 共: {len(pending)} 条",
             "---"]
    for s in risk[:15]:
        lines.append(f"  [{s.code}] {s.reason}\n     建议: {s.risk_advice}")
    for s in (add + reduce)[:15]:
        lines.append(f"  [{s.code}] {s.signal_type}: {s.reason}")
    content = "\n".join(lines) or "今日无信号"
    notifier = get_notifier(cfg.channel)
    result = notifier.send("加油赚奶茶钱 · 每日复盘", content, json.loads(cfg.config_json))
    db.add(NotifyLog(user_id=user.id, channel=cfg.channel, content=content[:500],
                     status="success" if result.get("ok") else "failed"))
    db.commit()


def daily_review() -> None:
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active == True).all()
        for user in users:
            # 更新该用户可投池行情缓存
            pool = (db.query(TrackedPool)
                      .filter(TrackedPool.user_id == user.id, TrackedPool.status == "active")
                      .all())
            for p in pool:
                try:
                    ensure_quotes(p.code, 90)
                except Exception:
                    pass
            run_engine(db, user)
            _notify_user(db, user)
    finally:
        db.close()


_sched = None


def start_scheduler() -> BackgroundScheduler:
    global _sched
    _sched = BackgroundScheduler()
    # 每个交易日 15:30 复盘（可后续按 notify_config.daily_review_cron 扩展）
    _sched.add_job(daily_review, "cron", hour=15, minute=30, day_of_week="mon-fri", id="daily_review")
    # 每个交易日 16:00 盘后自动预热全 A 日线(方案 C:每天盘后拉,第二天全市场可筛)
    _sched.add_job(start_warmup, "cron", hour=16, minute=0, day_of_week="mon-fri", id="daily_warmup")
    # 每天 15:35（复盘任务之后）自动整库备份到 data/backups/，保留最近 14 份
    _sched.add_job(run_auto_backup, "cron", hour=15, minute=35, id="daily_backup",
                   misfire_grace_time=3600)
    _sched.start()
    return _sched
