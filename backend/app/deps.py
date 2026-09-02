"""鉴权依赖：校验登录 token，注入当前用户。"""
from __future__ import annotations
from fastapi import Depends, HTTPException, Header
from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import verify_token


def _extract_token(authorization: str | None, x_auth_token: str | None) -> str | None:
    """从请求头里取登录 token。

    优先读自定义头 X-Auth-Token：部分部署平台（反向代理网关）会占用 Authorization 头
    传递自家 JWT，把业务 token 覆盖掉，导致服务端验签失败（表现为"登录失效"）。
    """
    if x_auth_token and x_auth_token.strip():
        return x_auth_token.strip()
    if authorization and authorization.startswith("Bearer "):
        return authorization.split(" ", 1)[1]
    return None


def get_current_user(
    authorization: str = Header(None),
    x_auth_token: str = Header(None),
) -> User:
    token = _extract_token(authorization, x_auth_token)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
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
