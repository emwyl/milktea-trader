"""鉴权路由。"""
from __future__ import annotations
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import SessionLocal
from app.models import User, AccessLog, _now
from app.schemas import LoginIn, LoginOut, PasswordChange, PasswordResetIn
from app.security import verify_password, make_token, hash_password
from app.deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fa5]{2,20}$")


def _require_admin(user: User) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（兼容 Cloud Studio / Render 等反向代理）。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=LoginOut)
def login(request: Request, body: LoginIn):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == body.username).first()
        if not user or not verify_password(body.password, user.salt, user.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        user.last_login_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
        user.last_ip = _client_ip(request)
        db.add(AccessLog(ts=_now(), ip=_client_ip(request), ua=request.headers.get("User-Agent", ""),
                        path=request.url.path, method="POST", username=user.username,
                        is_guest=user.is_guest, event_type="login"))
        db.commit()
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "is_guest": user.is_guest, "last_login_at": user.last_login_at})
    finally:
        db.close()


@router.post("/register", response_model=LoginOut)
def register(request: Request, body: LoginIn):
    db = SessionLocal()
    try:
        if not _USERNAME_RE.match(body.username):
            raise HTTPException(status_code=400, detail="用户名2-20位，支持中文/字母/数字/下划线")
        if len(body.password) < 4:
            raise HTTPException(status_code=400, detail="密码至少4位")
        if db.query(User).filter(User.username == body.username).first():
            raise HTTPException(status_code=400, detail="用户名已存在")
        h, s = hash_password(body.password)
        user = User(username=body.username, password_hash=h, salt=s, role="user", is_guest=False,
                    last_ip=_client_ip(request))
        db.add(user)
        db.add(AccessLog(ts=_now(), ip=_client_ip(request), ua=request.headers.get("User-Agent", ""),
                        path=request.url.path, method="POST", username=user.username,
                        is_guest=user.is_guest, event_type="register"))
        db.commit()
        db.refresh(user)
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "last_login_at": user.last_login_at})
    finally:
        db.close()


@router.post("/guest", response_model=LoginOut)
def guest_login(request: Request):
    db = SessionLocal()
    try:
        username = f"游客_{uuid.uuid4().hex[:8]}"
        raw_pw = uuid.uuid4().hex
        h, s = hash_password(raw_pw)
        user = User(username=username, password_hash=h, salt=s, role="user", is_guest=True,
                    last_ip=_client_ip(request))
        db.add(user)
        db.add(AccessLog(ts=_now(), ip=_client_ip(request), ua=request.headers.get("User-Agent", ""),
                        path=request.url.path, method="POST", username=user.username,
                        is_guest=True, event_type="guest"))
        db.commit()
        db.refresh(user)
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "is_guest": True, "last_login_at": user.last_login_at})
    finally:
        db.close()


@router.get("/access-logs")
def list_access_logs(page: int = 1, page_size: int = 50, user: User = Depends(get_current_user)):
    """访问记录（含游客），仅管理员可见。按时间倒序分页。"""
    _require_admin(user)
    db = SessionLocal()
    try:
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), 200)
        q = db.query(AccessLog)
        total = q.count()
        items = (q.order_by(AccessLog.ts.desc())
                   .offset((page - 1) * page_size).limit(page_size).all())
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [{
                "id": a.id, "ts": a.ts, "ip": a.ip, "ua": a.ua, "path": a.path,
                "method": a.method, "username": a.username,
                "is_guest": a.is_guest, "event_type": a.event_type,
            } for a in items],
        }
    finally:
        db.close()


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"username": user.username, "role": user.role, "is_guest": user.is_guest,
            "last_login_at": user.last_login_at}


@router.post("/logout")
def logout(user: User = Depends(get_current_user)):
    return {"ok": True}


@router.put("/password")
def change_password(body: PasswordChange, user: User = Depends(get_current_user)):
    if len(body.new_password) < 4:
        raise HTTPException(400, "新密码至少4位")
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == user.username).first()
        if not u:
            raise HTTPException(404, "用户不存在")
        if not verify_password(body.old_password, u.salt, u.password_hash):
            raise HTTPException(400, "原密码错误")
        h, s = hash_password(body.new_password)
        u.password_hash = h
        u.salt = s
        u.must_change_pw = False
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.get("/must-change")
def must_change(user: User = Depends(get_current_user)):
    return {"must_change_pw": user.must_change_pw}


@router.get("/users")
def list_users(user: User = Depends(get_current_user)):
    _require_admin(user)
    db = SessionLocal()
    try:
        rows = db.query(User).order_by(User.id.desc()).all()
        return [{"id": u.id, "username": u.username, "role": u.role, "is_guest": u.is_guest,
                 "is_active": u.is_active, "created_at": u.created_at, "last_login_at": u.last_login_at,
                 "last_ip": u.last_ip}
                for u in rows]
    finally:
        db.close()


@router.put("/users/{uid}/toggle")
def toggle_user(uid: int, user: User = Depends(get_current_user)):
    _require_admin(user)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")
        if u.id == user.id:
            raise HTTPException(status_code=400, detail="不能禁用自己")
        u.is_active = not u.is_active
        db.commit()
        return {"ok": True, "id": u.id, "is_active": u.is_active}
    finally:
        db.close()


@router.delete("/users/{uid}")
def delete_user(uid: int, user: User = Depends(get_current_user)):
    _require_admin(user)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")
        if u.id == user.id:
            raise HTTPException(status_code=400, detail="不能删除自己")
        # 级联清理该用户个人数据（全局 stocks/quotes 保留）
        from sqlalchemy import text
        tables = ["tracked_pool", "screens", "position_rules", "signals",
                  "user_profile", "notify_config", "notify_log", "stock_tconfig", "user_settings"]
        for t in tables:
            try:
                db.execute(text(f"DELETE FROM {t} WHERE user_id=:uid"), {"uid": uid})
            except Exception:
                pass
        db.delete(u)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.put("/users/{uid}/reset-password")
def reset_password(uid: int, body: PasswordResetIn, user: User = Depends(get_current_user)):
    """管理员重置指定账号密码（免原密码）。重置后要求该账号下次登录改密。"""
    _require_admin(user)
    if len(body.new_password) < 4:
        raise HTTPException(400, "新密码至少4位")
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(404, "用户不存在")
        h, s = hash_password(body.new_password)
        u.password_hash = h
        u.salt = s
        u.must_change_pw = True
        db.commit()
        return {"ok": True}
    finally:
        db.close()
