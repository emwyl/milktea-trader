"""数据导出 / 导入：按当前登录用户隔离，便于把个人数据迁移到其它部署（如 Cloud Studio）。

导出范围：选股模型(screens)、加减仓规则(position_rules)、短线可投池(tracked_pool)、
偏好画像(user_profile)、通知配置(notify_config)、用户设置(user_settings)，及选股结果。
不含：signals / notify_log 等派生日志、全局行情(Stock/DailyQuote)、方案类型(scheme_types)、
系统级 AppSetting、按股票维度的 StockTConfig（非按用户）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.config import DATA_DIR
from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import (
    User, _now,
    TrackedPool, UserProfile, NotifyConfig, UserSetting, PositionRule, Screen, ScreenResult,
)
from app.services import cos_backup

router = APIRouter(prefix="/api/data", tags=["data"])

# 随用户迁移的个人数据表（一对一/一对多的简单表）
_SIMPLE_TABLES = [TrackedPool, UserProfile, NotifyConfig, UserSetting, PositionRule]

# ---------- 定期自动备份 ----------
BACKUP_DIR = Path(DATA_DIR) / "backups"   # 跟数据目录走，可被 STOCK_ADVISOR_DATA_DIR 覆盖
BACKUP_KEEP = 14                          # 服务器上保留最近 14 份，超出自动清理
_BACKUP_NAME_RE = re.compile(r"^backup-\d{8}-\d{6}\.json$")


def _build_all_export(db) -> dict:
    """构建整库（所有用户）导出字典。"""
    users = db.query(User).order_by(User.id).all()
    out: dict = {}
    for u in users:
        out[u.username] = _export_user(db, u)
    return {
        "app": "milktea-trader",
        "version": 1,
        "exported_at": _now(),
        "scope": "all-users",
        "count": len(out),
        "users": out,
    }


def run_auto_backup() -> dict:
    """把所有账号数据导出为 JSON 落盘到 backups/ 目录，并只保留最近 BACKUP_KEEP 份。

    供定时任务与「立即备份」接口共用。任何异常都不抛出（备份失败不影响主流程）。
    """
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        db = SessionLocal()
        try:
            payload = _build_all_export(db)
        finally:
            db.close()
        name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path = BACKUP_DIR / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        # 清理超出保留份数的旧备份
        old = sorted(p for p in BACKUP_DIR.glob("backup-*.json"))
        for p in old[:-BACKUP_KEEP]:
            try:
                p.unlink()
            except Exception:
                pass
        # 若配置了腾讯云 COS，则同步上传一份到云端（异地灾备，防环境重置丢数据）
        cos_upload = None
        if cos_backup.cos_enabled():
            try:
                r = cos_backup.upload_file(name, path)
                cos_upload = r.get("ok") and r.get("key") or None
            except Exception as e:  # noqa: BLE001
                cos_upload = f"upload failed: {e}"
        ret = {"ok": True, "file": name, "count": payload.get("count", 0)}
        if cos_upload is not None:
            ret["cos"] = cos_upload
        return ret
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": str(e)}


def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _row_to_dict(row, exclude=("id",)):
    d = {}
    for c in row.__table__.columns:
        if c.name in exclude:
            continue
        d[c.name] = getattr(row, c.name)
    return d


def _export_user(db, user: User) -> dict:
    """导出单个用户的全部个人数据（不含 id 主键，便于导入重新生成）。"""
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
        "username": user.username,
        "role": user.role,
        "is_guest": user.is_guest,
        # 账号自身字段（含密码哈希），用于整库恢复时原样还原账号与登录密码
        "account": {
            "password_hash": user.password_hash,
            "salt": user.salt,
            "is_active": user.is_active,
        },
        "data": data,
    }


@router.get("/export")
def export_data(user: User = Depends(get_current_user), db: SessionLocal = Depends(get_db)):
    u = _export_user(db, user)
    return {
        "app": "milktea-trader",
        "version": 1,
        "exported_at": _now(),
        "username": u["username"],
        "data": u["data"],
    }


def _replace_user_data(db, user: User, data: dict) -> dict:
    """替换式导入：清空该用户当前数据，按传入 data 重建（screens/results 重新关联）。

    供单用户导入与 admin 整库恢复共用。调用方负责 commit。
    """
    # 先清空当前用户在这些表的数据（导入前请确认已备份）
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
    return counts


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

    counts = _replace_user_data(db, user, data)
    db.commit()
    return {"ok": True, "msg": "导入完成", "counts": counts}


@router.get("/export-all")
def export_all_users(user: User = Depends(get_current_user), db: SessionLocal = Depends(get_db)):
    """admin 专用：导出所有账号的个人数据（按用户名分组），用于整库备份 / 迁移。"""
    _require_admin(user)
    return _build_all_export(db)


@router.post("/backup-now")
def backup_now(user: User = Depends(get_current_user)):
    """admin 专用：立即在服务器上生成一份自动备份文件。"""
    _require_admin(user)
    return run_auto_backup()


@router.get("/backups")
def list_backups(user: User = Depends(get_current_user)):
    """admin 专用：列出服务器上的自动备份文件（新 → 旧）。"""
    _require_admin(user)
    items = []
    if BACKUP_DIR.exists():
        for p in BACKUP_DIR.glob("backup-*.json"):
            st = p.stat()
            items.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    items.sort(key=lambda x: x["name"], reverse=True)
    return {"ok": True, "count": len(items), "items": items, "keep": BACKUP_KEEP}


@router.get("/backups/{name}")
def download_backup(name: str, user: User = Depends(get_current_user)):
    """admin 专用：下载指定的自动备份文件。文件名白名单校验，防路径穿越。"""
    _require_admin(user)
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法的备份文件名")
    path = BACKUP_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return FileResponse(str(path), media_type="application/json",
                        filename=name)


# ---------- 腾讯云 COS 云备份（异地灾备） ----------
@router.get("/cos-status")
def cos_status(user: User = Depends(get_current_user)):
    """admin 专用：查询 COS 云备份配置状态（是否已连接）。"""
    _require_admin(user)
    return cos_backup.cos_status()


@router.post("/cos-sync")
def cos_sync(user: User = Depends(get_current_user)):
    """admin 专用：把服务器上所有本地备份同步上传到 COS（云端缺的才传）。"""
    _require_admin(user)
    if not cos_backup.cos_enabled():
        return {"ok": False, "msg": "COS 未配置（需在 backend/data/cos.json 或环境变量填写密钥）"}
    try:
        cloud_names = {i["name"] for i in cos_backup.list_backups()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"读取云端列表失败：{e}"}
    uploaded, skipped = [], []
    files = sorted(BACKUP_DIR.glob("backup-*.json")) if BACKUP_DIR.exists() else []
    for p in files:
        if p.name in cloud_names:
            skipped.append(p.name)
            continue
        try:
            cos_backup.upload_file(p.name, p)
            uploaded.append(p.name)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"上传 {p.name} 失败：{e}"}
    return {"ok": True, "uploaded": uploaded, "skipped": skipped}


@router.get("/cos-backups")
def list_cos_backups(user: User = Depends(get_current_user)):
    """admin 专用：列出 COS 云端备份文件（新 → 旧）。"""
    _require_admin(user)
    if not cos_backup.cos_enabled():
        return {"ok": False, "msg": "COS 未配置"}
    try:
        items = cos_backup.list_backups()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"读取云端列表失败：{e}"}
    return {"ok": True, "count": len(items), "items": items}


@router.get("/cos-backups/{name}")
def download_cos_backup(name: str, user: User = Depends(get_current_user)):
    """admin 专用：从 COS 下载备份。优先复用本地同名文件，否则拉取云端。"""
    _require_admin(user)
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法的备份文件名")
    if not cos_backup.cos_enabled():
        raise HTTPException(status_code=503, detail="COS 未配置")
    local = BACKUP_DIR / name
    if not local.is_file():
        try:
            cos_backup.download_file(name, local)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"从 COS 下载失败：{e}")
    return FileResponse(str(local), media_type="application/json", filename=name)


@router.post("/import-all")
async def import_all(request: Request,
                     user: User = Depends(get_current_user),
                     db: SessionLocal = Depends(get_db)):
    """admin 专用：整库恢复（覆盖式）。接受「自动备份 / 导出全部用户数据」的文件。

    逐账号恢复：同名账号覆盖其个人数据（密码保持原样）；备份里存在但当前系统没有的
    账号自动创建（密码哈希一并还原，登录密码不变）。
    """
    _require_admin(user)
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "msg": "JSON 解析失败"}
    if not isinstance(body, dict) or body.get("app") != "milktea-trader":
        return {"ok": False, "msg": "文件不是 milktea-trader 的备份"}
    if body.get("scope") != "all-users" or not isinstance(body.get("users"), dict):
        return {"ok": False, "msg": "文件不是整库备份格式（缺少 all-users 数据）"}
    users_payload = body["users"]
    if not users_payload:
        return {"ok": False, "msg": "备份里没有任何用户数据"}

    # 恢复前先自动备份当前状态，防止误操作无法回退
    run_auto_backup()

    created, updated = [], []
    for username, udata in users_payload.items():
        if not isinstance(udata, dict) or not isinstance(udata.get("data"), dict):
            continue
        acct = udata.get("account") or {}
        u = db.query(User).filter(User.username == username).first()
        if u is None:
            u = User(
                username=username,
                password_hash=acct.get("password_hash") or "!",
                salt=acct.get("salt") or "",
                role=udata.get("role") or "user",
                is_guest=bool(udata.get("is_guest", False)),
                is_active=bool(acct.get("is_active", True)),
                must_change_pw=not bool(acct.get("password_hash")),
            )
            db.add(u)
            db.flush()
            created.append(username)
        else:
            # 已存在账号：密码不覆盖（保持当前密码），仅恢复个人数据
            updated.append(username)
        _replace_user_data(db, u, udata["data"])
    db.commit()
    return {
        "ok": True,
        "msg": f"整库恢复完成：新建 {len(created)} 个账号，覆盖 {len(updated)} 个账号的数据",
        "created": created,
        "updated": updated,
    }
