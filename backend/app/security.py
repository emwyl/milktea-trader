"""密码哈希与登录令牌（加盐 scrypt，本地不存明文）。"""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time

from app.config import SECRET_KEY, TOKEN_TTL_HOURS


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (hash_hex, salt)。使用 pbkdf2-hmac-sha256，10 万次迭代。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000)
    return dk.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000)
    return hmac.compare_digest(dk.hex(), expected_hash)


def make_token(username: str) -> str:
    """签发一个带过期时间的 HMAC token（base64-ish）。"""
    exp = int(time.time()) + TOKEN_TTL_HOURS * 3600
    payload = f"{username}.{exp}".encode("utf-8")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    raw = f"{payload.decode()}.{sig}"
    return _b64(raw)


def verify_token(token: str) -> str | None:
    """返回 username 或 None。"""
    try:
        raw = _b64d(token)
        username, exp_s, sig = raw.rsplit(".", 2)
        exp = int(exp_s)
        if time.time() > exp:
            return None
        payload = f"{username}.{exp}".encode("utf-8")
        expect = hmac.new(SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expect, sig):
            return username
    except Exception:
        return None
    return None


def _b64(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(s: str) -> str:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode("utf-8")
