"""鉴权路由。"""
from __future__ import annotations
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, text

from app.db import SessionLocal
from app.models import User, AccessLog, TrackedPool, _now
from app.schemas import LoginIn, LoginOut, PasswordChange, PasswordResetIn
from app.security import verify_password, make_token, hash_password
from app.deps import get_current_user
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter(prefix="/api/auth", tags=["auth"])


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fa5]{2,20}$")
GUEST_USERNAME = "guest"  # 方案 A：所有游客共用同一个系统账号


def _require_admin(user: User) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _cleanup_guest_data(db, guest_id: int):
    """清理游客工作区数据（由 scheduler 每周日 20:00 统一执行，不再在登录时清理）。
    保留全局 stocks/quotes/scheme_types/app_settings。
    v133: tracked_pool_tags 已加 user_id 列，可直接按 user_id 清理；同时用 pool_id 子查询兜底旧结构。"""
    tables = [
        "tracked_pool_tags",
        "tracked_pool",
        "pool_tags",
        "screen_results",
        "screens",
        "position_rules",
        "signals",
        "user_profile",
        "notify_config",
        "notify_log",
        "user_settings",
        "stock_tconfig",
        "access_logs",
    ]
    for t in tables:
        try:
            db.execute(text(f"DELETE FROM {t} WHERE user_id=:uid"), {"uid": guest_id})
        except Exception:
            pass
    # 兜底：按 pool_id 子查询清理可能未迁移到 user_id 列的孤儿关联
    try:
        db.execute(text("""
            DELETE FROM tracked_pool_tags
            WHERE pool_id IN (SELECT id FROM tracked_pool WHERE user_id=:uid)
        """), {"uid": guest_id})
    except Exception:
        pass


def cleanup_guest_periodic() -> dict:
    """供 scheduler 每周日 20:00 调用：清理唯一系统 guest 账号的数据。
    返回清理结果摘要，便于日志追溯。"""
    db = SessionLocal()
    try:
        guest = _get_guest_user_or_none(db)
        if not guest:
            return {"ok": True, "cleaned": False, "reason": "guest account not exists"}
        _cleanup_guest_data(db, guest.id)
        db.commit()
        return {"ok": True, "cleaned": True, "guest_id": guest.id, "guest_username": guest.username}
    except Exception as e:
        return {"ok": False, "cleaned": False, "error": str(e)}
    finally:
        db.close()


def _today_iso() -> str:
    """按东八区取当天日期（游客每日清理以国内自然日为准）。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def _local_date(iso_str: str | None) -> str:
    """把库里存的 UTC ISO 时间戳换算成东八区日期，与 _today_iso() 同口径比较。"""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _ensure_guest_user(db, client_ip: str):
    """方案 A：确保存在唯一的系统 guest 账号。"""
    user = db.query(User).filter(User.username == GUEST_USERNAME, User.is_guest == True).first()
    if not user:
        h, s = hash_password(uuid.uuid4().hex)
        user = User(username=GUEST_USERNAME, password_hash=h, salt=s,
                    role="user", is_guest=True, last_ip=client_ip)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _get_guest_user_or_none(db):
    return db.query(User).filter(User.username == GUEST_USERNAME, User.is_guest == True).first()


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
        user.last_login_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        user.last_ip = _client_ip(request)
        db.add(AccessLog(ts=_now(), ip=_client_ip(request), ua=request.headers.get("User-Agent", ""),
                        path=request.url.path, method="POST", username=user.username, user_id=user.id,
                        is_guest=user.is_guest, event_type="login"))
        db.commit()
        token = make_token(user.username)
        return LoginOut(token=token, user={"username": user.username, "role": user.role,
                                           "is_guest": user.is_guest, "last_login_at": user.last_login_at,
                                           "must_change_pw": user.must_change_pw})
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
                        path=request.url.path, method="POST", username=user.username, user_id=user.id,
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
    """方案 A：所有游客共用唯一系统账号 guest。
    v133: 游客数据不再在登录时清理，改为 scheduler 每周日 20:00 统一清理。"""
    db = SessionLocal()
    try:
        guest = _get_guest_user_or_none(db)
        if guest is None:
            guest = _ensure_guest_user(db, _client_ip(request))
        guest.last_login_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        guest.last_ip = _client_ip(request)
        db.add(AccessLog(ts=_now(), ip=_client_ip(request), ua=request.headers.get("User-Agent", ""),
                        path=request.url.path, method="POST", username=guest.username, user_id=guest.id,
                        is_guest=True, event_type="guest"))
        db.commit()
        token = make_token(guest.username)
        return LoginOut(token=token, user={"username": guest.username, "role": guest.role,
                                           "is_guest": True, "last_login_at": guest.last_login_at,
                                           "must_change_pw": guest.must_change_pw})
    finally:
        db.close()


@router.get("/access-events")
def list_access_events(page: int = 1, page_size: int = 10,
                        user: User = Depends(get_current_user)):
    """访问记录（事件维度）：每次访问/登录/退出/注册都记一行，仅管理员可见。
    按时间倒序分页（默认 10 条/页）。

    字段：
      - id: 事件 id
      - ts: 当前行登录时间
      - username/role/is_guest: 用户/角色/游客标记
      - ip: 当前行客户端 IP
      - prev_ts: 该用户上一次事件的时间（同 username 中 id < current.id 的最大 id 对应 ts）
      - last_path: 当前行访问路径（最近停留页面）
      - login_count: 该用户累计登录次数（event_type='login' 的总数）
    """
    _require_admin(user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), 200)

    db = SessionLocal()
    try:
        # 1) 主查询：按 id 倒序分页（id 自增，id desc ≡ ts desc）
        base_q = db.query(AccessLog).order_by(AccessLog.id.desc())
        total_count = base_q.count()
        rows: list[AccessLog] = base_q.offset((page - 1) * page_size).limit(page_size).all()
        if not rows:
            return {"total": total_count, "page": page, "page_size": page_size, "items": []}

        # 2) 关联 users 表取角色（一次 LEFT JOIN，避免 N+1）
        user_ids = {r.user_id for r in rows if r.user_id}
        user_role_map: dict[int, str] = {}
        user_guest_map: dict[int, int] = {}
        if user_ids:
            for u in db.query(User).filter(User.id.in_(user_ids)).all():
                user_role_map[u.id] = u.role
                user_guest_map[u.id] = int(bool(u.is_guest))

        # 3) 批量拿本页相关 username 的 login_count（避免 10 次 count 查询）
        page_usernames = {(r.username or None) for r in rows}
        login_count_map: dict[str, int] = {}
        if page_usernames:
            # 用 OR (username IS NULL) 处理 None 的入参
            null_in = None in page_usernames
            non_null = [u for u in page_usernames if u is not None]
            clauses = []
            if non_null:
                clauses.append(AccessLog.username.in_(non_null))
            if null_in:
                clauses.append(AccessLog.username.is_(None))
            from sqlalchemy import or_
            lc_q = (db.query(AccessLog.username, func.count(AccessLog.id))
                    .filter(AccessLog.event_type == "login")
                    .filter(or_(*clauses))
                    .group_by(AccessLog.username))
            for u, c in lc_q.all():
                login_count_map[u or ""] = int(c or 0)

        # 4) 逐行拿 prev_ts（一页最多 10 条，单条子查询走索引，可接受）
        items: list[dict] = []
        for r in rows:
            u = r.username or None
            prev_q = (db.query(AccessLog.ts)
                      .filter(AccessLog.id < r.id))
            if u is None:
                prev_q = prev_q.filter(AccessLog.username.is_(None))
            else:
                prev_q = prev_q.filter(AccessLog.username == u)
            prev_row = prev_q.order_by(AccessLog.id.desc()).first()
            prev_ts = prev_row[0] if prev_row else None

            # 角色：优先 user_id 查；user_id 为空（未登录访客）→ 'anonymous'
            if r.user_id and r.user_id in user_role_map:
                role = user_role_map[r.user_id]
                is_guest = bool(user_guest_map.get(r.user_id, 0))
            else:
                role = "anonymous"
                is_guest = True  # 未登录访客按游客口径展示

            items.append({
                "id": r.id,
                "ts": r.ts,
                "username": r.username or "未登录访客",
                "role": role,
                "is_guest": is_guest,
                "ip": r.ip,
                "prev_ts": prev_ts,
                "last_path": r.path,
                "login_count": int(login_count_map.get(r.username or "", 0)),
                "event_type": r.event_type or "page",
            })

        return {"total": total_count, "page": page, "page_size": page_size, "items": items}
    finally:
        db.close()


@router.get("/access-logs")
def list_access_logs(page: int = 1, page_size: int = 10, user: User = Depends(get_current_user)):
    """访问记录（含游客/已注册用户/未登录访客），仅管理员可见。按用户最近活动时间倒序分页（默认10条/页）。每行展示一个用户/访客最近一次活动信息（含最新访问页面路径，供前端展示中文名）。"""
    _require_admin(user)
    db = SessionLocal()
    try:
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), 200)

        # 统一构造一个用户/访客行
        rows: dict[str, dict] = {}

        # 1) 先按 last_login_at 倒序取所有用户（含游客），并关联其最新一条访问记录
        users_all = db.query(User).order_by(User.last_login_at.desc().nullslast(), User.id.desc()).all()
        usernames = [u.username for u in users_all]
        latest_map: dict[str, AccessLog] = {}
        if usernames:
            latest_id_sq = (
                db.query(
                    AccessLog.username.label("u"),
                    func.max(AccessLog.id).label("max_id"),
                )
                .filter(AccessLog.username.in_(usernames))
                .group_by(AccessLog.username)
                .subquery()
            )
            for ev in (db.query(AccessLog)
                       .join(latest_id_sq, AccessLog.id == latest_id_sq.c.max_id)
                       .all()):
                latest_map[ev.username] = ev

        for u in users_all:
            ev = latest_map.get(u.username)
            last_ts = (ev.ts if ev is not None else None) or u.last_login_at or u.created_at
            last_event_type = (ev.event_type if ev is not None else None) or ("login" if u.last_login_at else None)
            last_path = (ev.path if ev is not None else None)
            last_ip = (ev.ip if ev is not None else None) or u.last_ip
            last_ua = (ev.ua if ev is not None else None)
            rows[u.username] = {
                "username": u.username,
                "role": u.role,
                "is_guest": u.is_guest,
                "is_active": u.is_active,
                "last_login_at": u.last_login_at,
                "created_at": u.created_at,
                "last_ts": last_ts,
                "last_event_type": last_event_type,
                "last_path": last_path,
                "last_ip": last_ip,
                "last_ua": last_ua,
            }

        # 2) Fallback / 增强：把 access_logs 中出现过、但不在 users 表里的 username（含 NULL=未登录访客）也聚合进来
        #    避免"用户表被重置/为空"或"旧日志 username 未回填"时访问记录一片空白
        since_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        anon_id_sq = (
            db.query(
                AccessLog.username.label("u"),
                func.max(AccessLog.id).label("max_id"),
            )
            .filter(AccessLog.ts >= since_ts)
            .group_by(AccessLog.username)
            .subquery()
        )
        for ev in (db.query(AccessLog)
                   .join(anon_id_sq, AccessLog.id == anon_id_sq.c.max_id)
                   .all()):
            uname = ev.username
            key = uname if uname else "__anonymous__"
            if key in rows:
                # 已用 users 表信息展示，无需覆盖；但可以用 access_log 更新最后活动时间
                existing = rows[key]
                if ev.ts and (not existing["last_ts"] or ev.ts > existing["last_ts"]):
                    existing["last_ts"] = ev.ts
                    existing["last_event_type"] = ev.event_type
                    existing["last_path"] = ev.path
                    existing["last_ip"] = ev.ip or existing["last_ip"]
                    existing["last_ua"] = ev.ua
                continue
            rows[key] = {
                "username": uname or "未登录访客",
                "role": "anonymous",
                "is_guest": True,
                "is_active": True,
                "last_login_at": None,
                "created_at": ev.ts,
                "last_ts": ev.ts,
                "last_event_type": ev.event_type,
                "last_path": ev.path,
                "last_ip": ev.ip,
                "last_ua": ev.ua,
            }

        # 3) 按最后活动时间倒序排列并分页
        items = sorted(rows.values(), key=lambda x: x["last_ts"] or "", reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        items_page = items[start:start + page_size]

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items_page,
        }
    finally:
        db.close()


@router.post("/visit")
def record_visit(request: Request, user: User = Depends(get_current_user)):
    """前端应用加载后显式上报一次访问（带 token），解决静态首页 GET 请求无法携带 Authorization 头导致访问记录大量 username=NULL 的问题。"""
    db = SessionLocal()
    try:
        db.add(AccessLog(
            ts=_now(),
            ip=_client_ip(request),
            ua=(request.headers.get("User-Agent", "") or "")[:512],
            path=request.headers.get("Referer", "/"),
            method="POST",
            username=user.username,
            user_id=user.id,
            is_guest=bool(user.is_guest),
            event_type="page",
        ))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"username": user.username, "role": user.role, "is_guest": user.is_guest,
            "last_login_at": user.last_login_at, "must_change_pw": user.must_change_pw}


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
        tables = ["tracked_pool_tags", "tracked_pool", "pool_tags", "screens", "position_rules", "signals",
                  "user_profile", "notify_config", "notify_log", "stock_tconfig", "user_settings", "access_logs"]
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


class _PoolRestore(BaseModel):
    code: str
    added_at: Optional[str] = None
    note: str = ""
    cost_price: Optional[float] = None
    position_qty: Optional[float] = None
    position_pct: Optional[float] = None
    scheme_type: str = "custom"
    status: str = "active"


class RestoreUserIn(BaseModel):
    """从备份导出的单用户结构还原（精确恢复被删账号及其个人数据）。"""
    username: str
    role: str = "user"
    is_guest: bool = False
    password_hash: str
    salt: str
    is_active: bool = True
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None
    last_ip: Optional[str] = None
    tracked_pool: List[_PoolRestore] = []


@router.post("/admin-restore-user")
def admin_restore_user(body: RestoreUserIn, user: User = Depends(get_current_user)):
    """管理员专用：从备份结构精确恢复一个被硬删除的账号（含其可投池等个人数据）。"""
    _require_admin(user)
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == body.username).first():
            raise HTTPException(status_code=400, detail="账号已存在，无需恢复")
        u = User(username=body.username, password_hash=body.password_hash, salt=body.salt,
                 role=body.role, is_guest=body.is_guest, is_active=body.is_active,
                 created_at=body.created_at or _now(),
                 last_login_at=body.last_login_at, last_ip=body.last_ip)
        db.add(u)
        db.flush()  # 取回自增 id
        for p in body.tracked_pool:
            db.add(TrackedPool(user_id=u.id, code=p.code, added_at=p.added_at or _now(),
                               note=p.note, cost_price=p.cost_price, position_qty=p.position_qty,
                               position_pct=p.position_pct, scheme_type=p.scheme_type, status=p.status))
        db.commit()
        db.refresh(u)
        return {"ok": True, "id": u.id, "username": u.username, "restored_pool": len(body.tracked_pool)}
    finally:
        db.close()
