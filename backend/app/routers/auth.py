"""鉴权路由。"""
from __future__ import annotations
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.db import SessionLocal
from app.models import User
from app.schemas import LoginIn, LoginOut, PasswordChange, PasswordResetIn
from app.security import verify_password, make_token, hash_password
from app.deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fa5]{2,20}$")


def _require_admin(user: User) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == body.username).first()
        if not user or not verify_password(body.password, user.salt, user.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        user.last_login_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
        db.commit()
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "is_guest": user.is_guest, "last_login_at": user.last_login_at})
    finally:
        db.close()


@router.post("/register", response_model=LoginOut)
def register(body: LoginIn):
    db = SessionLocal()
    try:
        if not _USERNAME_RE.match(body.username):
            raise HTTPException(status_code=400, detail="用户名2-20位，支持中文/字母/数字/下划线")
        if len(body.password) < 4:
            raise HTTPException(status_code=400, detail="密码至少4位")
        if db.query(User).filter(User.username == body.username).first():
            raise HTTPException(status_code=400, detail="用户名已存在")
        h, s = hash_password(body.password)
        user = User(username=body.username, password_hash=h, salt=s, role="user", is_guest=False)
        db.add(user)
        db.commit()
        db.refresh(user)
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "last_login_at": user.last_login_at})
    finally:
        db.close()


@router.post("/guest", response_model=LoginOut)
def guest_login():
    db = SessionLocal()
    try:
        username = f"游客_{uuid.uuid4().hex[:8]}"
        raw_pw = uuid.uuid4().hex
        h, s = hash_password(raw_pw)
        user = User(username=username, password_hash=h, salt=s, role="user", is_guest=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "is_guest": True, "last_login_at": user.last_login_at})
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
                 "is_active": u.is_active, "created_at": u.created_at, "last_login_at": u.last_login_at}
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
