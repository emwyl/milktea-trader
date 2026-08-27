"""数据导出 / 导入：按当前登录用户隔离，便于把个人数据迁移到其它部署（如 Cloud Studio）。

导出范围：选股模型(screens)、加减仓规则(position_rules)、短线可投池(tracked_pool)、
偏好画像(user_profile)、通知配置(notify_config)、用户设置(user_settings)，及选股结果。
不含：signals / notify_log 等派生日志、全局行情(Stock/DailyQuote)、方案类型(scheme_types)、
系统级 AppSetting、按股票维度的 StockTConfig（非按用户）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import (
    User, _now,
    TrackedPool, UserProfile, NotifyConfig, UserSetting, PositionRule, Screen, ScreenResult,
)

router = APIRouter(prefix="/api/data", tags=["data"])

# 随用户迁移的个人数据表（一对一/一对多的简单表）
_SIMPLE_TABLES = [TrackedPool, UserProfile, NotifyConfig, UserSetting, PositionRule]


def _row_to_dict(row, exclude=("id",)):
    d = {}
    for c in row.__table__.columns:
        if c.name in exclude:
            continue
        d[c.name] = getattr(row, c.name)
    return d


@router.get("/export")
def export_data(user: User = Depends(get_current_user), db: SessionLocal = Depends(get_db)):
    data: dict = {}
    for m in _SIMPLE_TABLES:
        rows = db.query(m).filter(m.user_id == user.id).all()
        data[m.__tablename__] = [_row_to_dict(r) for r in rows]

    # screens 及其结果需重新关联 screen_id
    screens = db.query(Screen).filter(Screen.user_id == user.id).all()
    screen_list, results = [], []
    for s in screens:
        screen_list.append(_row_to_dict(s))
        for res in db.query(ScreenResult).filter(ScreenResult.screen_id == s.id).all():
            results.append({
                "screen_id": s.id,
                "code": res.code,
                "matched_at": res.matched_at,
                "metrics_json": res.metrics_json,
            })
    data["screens"] = screen_list
    data["screen_results"] = results

    return {
        "app": "milktea-trader",
        "version": 1,
        "exported_at": _now(),
        "username": user.username,
        "data": data,
    }


@router.post("/import")
async def import_data(request: Request,
                      user: User = Depends(get_current_user),
                      db: SessionLocal = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "msg": "JSON 解析失败"}
    if not isinstance(body, dict) or "data" not in body:
        return {"ok": False, "msg": "文件格式不正确（缺少 data 字段）"}
    if body.get("app") and body.get("app") != "milktea-trader":
        return {"ok": False, "msg": "文件来源不匹配，已拒绝导入"}
    data = body["data"]
    if not isinstance(data, dict):
        return {"ok": False, "msg": "data 字段应为对象"}

    # 替换式导入：先清空当前用户在这些表的数据（导入前请确认已备份）
    for m in _SIMPLE_TABLES:
        db.query(m).filter(m.user_id == user.id).delete(synchronize_session=False)
    my_screen_ids = [s.id for s in db.query(Screen.id).filter(Screen.user_id == user.id).all()]
    if my_screen_ids:
        db.query(ScreenResult).filter(ScreenResult.screen_id.in_(my_screen_ids)).delete(synchronize_session=False)
    db.query(Screen).filter(Screen.user_id == user.id).delete(synchronize_session=False)

    counts: dict = {}
    for m in _SIMPLE_TABLES:
        cols = {c.name for c in m.__table__.columns}
        rows = data.get(m.__tablename__, []) or []
        for rd in rows:
            if not isinstance(rd, dict):
                continue
            rd = {k: v for k, v in dict(rd).items() if k in cols and k != "id"}
            rd["user_id"] = user.id
            db.add(m(**rd))
        counts[m.__tablename__] = len(rows)

    # screens + results（重新关联 screen_id）
    old_to_new = {}
    for sd in (data.get("screens", []) or []):
        if not isinstance(sd, dict):
            continue
        orig_id = sd.get("id")
        cols = {c.name for c in Screen.__table__.columns}
        nd = {k: v for k, v in dict(sd).items() if k in cols and k != "id"}
        nd["user_id"] = user.id
        s = Screen(**nd)
        db.add(s)
        db.flush()
        old_to_new[orig_id] = s.id
    cnt_res = 0
    for res in (data.get("screen_results", []) or []):
        if not isinstance(res, dict):
            continue
        new_sid = old_to_new.get(res.get("screen_id"))
        if new_sid is None:
            continue
        db.add(ScreenResult(screen_id=new_sid, code=res.get("code"),
                             matched_at=res.get("matched_at"), metrics_json=res.get("metrics_json")))
        cnt_res += 1
    counts["screens"] = len(data.get("screens", []) or [])
    counts["screen_results"] = cnt_res

    db.commit()
    return {"ok": True, "msg": "导入完成", "counts": counts}
