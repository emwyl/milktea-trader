"""初始化种子数据：默认账号、内置方案类型、示例规则、示例可投池。开箱即用。"""
from __future__ import annotations
import json

from app.config import DEFAULT_USERNAME, DEFAULT_PASSWORD
from app.db import SessionLocal
from app.models import User, SchemeType, PositionRule, TrackedPool
from app.security import hash_password
from app.services.data_fetcher import seed_stock_universe

BUILTIN_SCHEMES = [
    {"key": "steady", "name": "稳健型", "risk_level": "低",
     "screener_json": {"price_max": 30, "amplitude_window": 20, "amplitude_avg_min": 2, "amplitude_avg_max": 4,
                       "box_stability": {"enabled": True, "window": 60, "max_drawdown_pct": 12, "min_trend_slope": 0.02},
                       "sector_trend": {"enabled": True, "mode": "up", "window": 20}, "liquidity": {"min_avg_amount": 1e8}},
     "signal_json": {"rules": [
         {"name": "稳健加仓：站上20日线且量能温和", "conditions": {"logic": "AND", "conditions": [
             {"type": "above_ma", "ma": 20, "days": 3}, {"type": "vol_ratio", "op": "<", "value": 2}]},
          "action": "add", "priority": 10},
         {"name": "稳健减仓：跌破20日线", "conditions": {"logic": "AND", "conditions": [
             {"type": "below_ma", "ma": 20, "days": 3}]}, "action": "reduce", "priority": 10}]},
     "risk_json": {"max_position_pct": 15, "stop_loss_pct": 3, "max_total_pct": 40}},

    {"key": "aggressive", "name": "激进型", "risk_level": "高",
     "screener_json": {"price_max": 60, "amplitude_window": 20, "amplitude_avg_min": 4, "amplitude_avg_max": 9,
                       "box_stability": {"enabled": False}, "sector_trend": {"enabled": True, "mode": "up", "window": 20},
                       "liquidity": {"min_avg_amount": 1.5e8}},
     "signal_json": {"rules": [
         {"name": "激进加仓：MACD金叉+KDJ金叉+放量", "conditions": {"logic": "AND", "conditions": [
             {"type": "macd_cross", "dir": "gold"}, {"type": "kdj", "dir": "gold"}, {"type": "vol_ratio", "op": ">", "value": 1.5}]},
          "action": "add", "priority": 10},
         {"name": "激进减仓：下穿20日线", "conditions": {"logic": "AND", "conditions": [
             {"type": "price_cross", "dir": "down", "ma": 20}]}, "action": "reduce", "priority": 10}]},
     "risk_json": {"max_position_pct": 20, "stop_loss_pct": 5, "max_total_pct": 60}},

    {"key": "swing", "name": "短线波段", "risk_level": "中",
     "screener_json": {"price_max": 40, "amplitude_window": 20, "amplitude_avg_min": 3, "amplitude_avg_max": 6,
                       "box_stability": {"enabled": True, "window": 60, "max_drawdown_pct": 15, "min_trend_slope": 0.01},
                       "sector_trend": {"enabled": True, "mode": "up", "window": 20}, "liquidity": {"min_avg_amount": 1e8}},
     "signal_json": {"rules": [
         {"name": "波段低吸：触布林下轨+KDJ超卖", "conditions": {"logic": "AND", "conditions": [
             {"type": "boll_touch", "band": "lower"}, {"type": "kdj", "state": "oversold"}]}, "action": "add", "priority": 8},
         {"name": "波段高抛：触布林上轨", "conditions": {"logic": "AND", "conditions": [
             {"type": "boll_touch", "band": "upper"}]}, "action": "reduce", "priority": 8}]},
     "risk_json": {"max_position_pct": 20, "stop_loss_pct": 4, "max_total_pct": 50}},

    {"key": "lowvol", "name": "低波动偏好", "risk_level": "低",
     "screener_json": {"price_max": 30, "amplitude_window": 20, "amplitude_avg_min": 3, "amplitude_avg_max": 5,
                       "box_stability": {"enabled": True, "window": 60, "max_drawdown_pct": 10, "min_trend_slope": 0.02},
                       "sector_trend": {"enabled": True, "mode": "up", "window": 20}, "liquidity": {"min_avg_amount": 1e8}},
     "signal_json": {"rules": [
         {"name": "低波加仓：站上20日线且振幅可控", "conditions": {"logic": "AND", "conditions": [
             {"type": "above_ma", "ma": 20, "days": 5}, {"type": "amplitude", "op": "<=", "value": 5}]}, "action": "add", "priority": 8},
         {"name": "低波减仓：跌破60日线", "conditions": {"logic": "AND", "conditions": [
             {"type": "below_ma", "ma": 60, "days": 3}]}, "action": "reduce", "priority": 8}]},
     "risk_json": {"max_position_pct": 20, "stop_loss_pct": 3, "max_total_pct": 50}},

    {"key": "custom", "name": "自定义", "risk_level": "中",
     "screener_json": {"price_max": 30, "amplitude_window": 20, "amplitude_avg_min": 3, "amplitude_avg_max": 5,
                       "box_stability": {"enabled": True, "window": 60, "max_drawdown_pct": 15, "min_trend_slope": 0.02},
                       "sector_trend": {"enabled": True, "mode": "up", "window": 20}, "liquidity": {"min_avg_amount": 1e8}},
     "signal_json": {"rules": []},
     "risk_json": {"max_position_pct": 20, "stop_loss_pct": 5, "max_total_pct": 50}},
]

EXAMPLE_RULES = [
    {"name": "示例·跌破20日线减仓", "scope": "all",
     "conditions": {"logic": "AND", "conditions": [{"type": "below_ma", "ma": 20, "days": 3}]},
     "action": "reduce", "priority": 10, "scheme_type": "custom", "enabled": True},
    {"name": "示例·放量突破箱体加仓", "scope": "all",
     "conditions": {"logic": "AND", "conditions": [
         {"type": "box_break", "dir": "up"}, {"type": "vol_ratio", "op": ">", "value": 1.8}]},
     "action": "add", "priority": 6, "scheme_type": "custom", "enabled": True},
]


def seed_all() -> None:
    # 先初始化股票池（独立 session），避免与下方 admin/scheme/rule 写入产生 SQLite 写锁冲突
    seed_stock_universe()

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == DEFAULT_USERNAME).first()
        if not admin:
            h, s = hash_password(DEFAULT_PASSWORD)
            admin = User(username=DEFAULT_USERNAME, password_hash=h, salt=s, role="admin", must_change_pw=True)
            db.add(admin)
            db.flush()  # 取得 admin.id
            db.commit()  # 释放写锁，避免后续长事务阻塞
        if db.query(SchemeType).count() == 0:
            for st in BUILTIN_SCHEMES:
                db.add(SchemeType(key=st["key"], name=st["name"], risk_level=st["risk_level"],
                                  screener_json=json.dumps(st["screener_json"]),
                                  signal_json=json.dumps(st["signal_json"]),
                                  risk_json=json.dumps(st["risk_json"]), builtin=True))
            db.commit()
        if db.query(PositionRule).count() == 0:
            for r in EXAMPLE_RULES:
                db.add(PositionRule(user_id=admin.id, name=r["name"], scope=r["scope"],
                                    conditions_json=json.dumps(r["conditions"]),
                                    action=r["action"], priority=r["priority"], enabled=r["enabled"],
                                    scheme_type=r["scheme_type"]))
            db.commit()
        if db.query(TrackedPool).count() == 0:
            db.add(TrackedPool(user_id=admin.id, code="000725", note="示例标的：观察面板板块箱体突破", scheme_type="custom"))
            db.commit()
    finally:
        db.close()
