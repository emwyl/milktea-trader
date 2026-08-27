"""鉴权依赖：校验登录 token，注入当前用户。"""
from __future__ import annotations
from fastapi import Depends, HTTPException, Header
from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import verify_token


def get_current_user(authorization: str = Header(None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ", 1)[1]
    username = verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="登录失效")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        return user
    finally:
        db.close()
