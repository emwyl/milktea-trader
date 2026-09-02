"""短线可投池路由。"""
from __future__ import annotations
import datetime as dt
import json
import re as _re
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import PoolTag, SchemeType, Stock, TrackedPool, TrackedPoolTag, User, UserProfile, _now
from app.schemas import PoolBatchDeleteIn, PoolBatchTagsIn, PoolImportIn, PoolIn, PoolOut, TagIn, TagOut
from app.services.data_fetcher import ensure_stock_name, get_pool_track
from app.services.preference import match_scheme
from app.services.screener import get_screener
from sqlalchemy import or_

_CODE_RE = _re.compile(r'^\d{6}$')
# 颜色格式校验（#RGB / #RRGGBB）
_HEX_COLOR_RE = _re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

router = APIRouter(prefix="/api/pool", tags=["pool"])


def _name(code: str, db) -> str | None:
    s = db.query(Stock).filter(Stock.code == code).first()
    return s.name if s and s.name else None


def _industry(code: str, db) -> str:
    s = db.query(Stock).filter(Stock.code == code).first()
    return (s.industry if s and s.industry else "") or "未知"


def _to_out(p: TrackedPool, db) -> PoolOut:
    tags = [{"id": t.id, "name": t.name, "color": t.color} for t in p.tags]
    tag_ids = [t.id for t in p.tags]
    return PoolOut(id=p.id, code=p.code, name=_name(p.code, db), industry=_industry(p.code, db),
                   note=p.note,
                   cost_price=p.cost_price, position_qty=p.position_qty,
                   position_pct=p.position_pct,
                   scheme_type=p.scheme_type, status=p.status, added_at=str(p.added_at),
                   tag_ids=tag_ids, tags=tags)


def _user_pool_q(db, user_id: int):
    """当前用户在「可投池可见范围」内的基础查询：active + archive 但有持仓。"""
    return (db.query(TrackedPool)
              .filter(TrackedPool.user_id == user_id,
                      (TrackedPool.status == "active") |
                      ((TrackedPool.status == "archive") & (TrackedPool.position_qty > 0))))


@router.get("")
def list_pool(
    q: str = Query("", description="证券代码或名称模糊查询"),
    note: str = Query("", description="备注模糊查询"),
    tag: str = Query("", description="按标签 ID 筛选(多个用逗号分隔)"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(15, ge=1, le=100, description="每页条数，默认 15"),
    db: SessionLocal = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出当前用户的可投池（分页返回）。
    支持按证券代码/名称(q)、备注(note)、标签(tag)过滤；空值表示不过滤。
    返回: { items, total, page, page_size }
    """
    query = _user_pool_q(db, user.id)

    # 代码/名称模糊：同时匹配 code 与 stock name
    q = (q or "").strip()
    if q:
        query = query.join(Stock, Stock.code == TrackedPool.code, isouter=True).filter(
            or_(TrackedPool.code.like(f"%{q}%"), Stock.name.like(f"%{q}%"))
        )

    # 备注模糊
    note = (note or "").strip()
    if note:
        query = query.filter(TrackedPool.note.like(f"%{note}%"))

    # 标签筛选：支持单个 ID 或多个逗号分隔 ID（多对多关联表）
    tag = (tag or "").strip()
    if tag:
        try:
            tag_ids = [int(x) for x in tag.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="标签参数必须是数字 ID")
        if tag_ids:
            query = (query.join(TrackedPoolTag, TrackedPoolTag.pool_id == TrackedPool.id)
                          .filter(TrackedPoolTag.tag_id.in_(tag_ids))
                          .distinct())

    rows = query.order_by(TrackedPool.id).all()
    # 同一 code 重复时(如历史添加/移除循环),优先保留「有持仓」的那条
    seen: dict[str, TrackedPool] = {}
    for p in rows:
        cur = seen.get(p.code)
        if cur is None or (p.position_qty and (not cur.position_qty or p.position_qty > cur.position_qty)):
            seen[p.code] = p
    rows = list(seen.values())
    total = len(rows)

    # 分页(在合并重复 code 之后切分,保证总数准确)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    # 名称补全 + 「每日跟踪数据」并发拉取:4 只股票同时拉实时+日线,1~2s 完成,
    # 单只失败不阻塞其他。所有跟踪数据带 60s 内存缓存,重复刷新秒开。
    # 传入持仓/成本, 让操作建议能感知止盈/持仓状态。
    tracks: dict[str, dict] = {}
    positions = {p.code: {"cost_price": p.cost_price, "position_qty": p.position_qty} for p in page_rows}
    with ThreadPoolExecutor(max_workers=min(8, max(2, len(page_rows)))) as ex:
        track_futs = {ex.submit(get_pool_track, p.code, db, positions.get(p.code)): p.code for p in page_rows}
        name_futs = {}
        for p in page_rows:
            if not _name(p.code, db):
                name_futs[ex.submit(ensure_stock_name, p.code, db)] = p.code
        for f in as_completed(track_futs):
            code = track_futs[f]
            try:
                tracks[code] = f.result()
            except Exception:
                tracks[code] = {}
        # 名称补全的 future 只需要等结束,结果写到 db 不必回传
        for f in as_completed(name_futs):
            try:
                f.result()
            except Exception:
                pass

    result = []
    for p in page_rows:
        o = _to_out(p, db).model_dump()
        o["track"] = tracks.get(p.code, {})
        result.append(o)
    return {"items": result, "total": total, "page": page, "page_size": page_size}


# 「系统推荐」基于偏好 + 选股模型
_RECOMMEND_CACHE: dict[str, tuple[float, dict]] = {}
_RECOMMEND_TTL = 300  # 5 分钟,避免频繁全市场筛选


@router.get("/recommendations")
def recommendations(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """基于用户偏好(archetype)自动匹配方案类型,跑 screener 给出推荐标的。
    自动排除已在可投池的 code(避免重复)。缓存 5min。
    返回:{ items, msg, archetype, scheme, source: 'pref'|'empty'|'no_scheme' }"""
    cache_key = user.username
    now = dt.datetime.now().timestamp()
    cached = _RECOMMEND_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _RECOMMEND_TTL:
        return cached[1]

    prof = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
    if not prof or not prof.archetype:
        out = {"items": [], "msg": "请先完成「偏好分析」,系统会基于你的风险偏好自动推荐",
               "archetype": None, "scheme": None, "source": "empty"}
        _RECOMMEND_CACHE[cache_key] = (now, out)
        return out

    scheme_key = match_scheme(prof.archetype)
    st = db.query(SchemeType).filter(SchemeType.key == scheme_key).first()
    if not st:
        out = {"items": [], "msg": f"未找到方案类型 {scheme_key}(请确认偏好匹配函数)",
               "archetype": prof.archetype, "scheme": scheme_key, "source": "no_scheme"}
        _RECOMMEND_CACHE[cache_key] = (now, out)
        return out

    try:
        cands = get_screener().run(json.loads(st.screener_json), db)
    except Exception as e:
        out = {"items": [], "msg": f"筛选失败: {e}", "archetype": prof.archetype, "scheme": scheme_key, "source": "error"}
        _RECOMMEND_CACHE[cache_key] = (now, out)
        return out

    # 已在可投池的(复用 list_pool 可见范围,避免推重复)
    rows = _user_pool_q(db, user.id).all()
    seen: dict[str, TrackedPool] = {}
    for p in rows:
        cur = seen.get(p.code)
        if cur is None or (p.position_qty and (not cur.position_qty or p.position_qty > cur.position_qty)):
            seen[p.code] = p
    in_pool = set(seen.keys())

    items = []
    for c in cands[:20]:
        if c.code in in_pool:
            continue
        items.append({"code": c.code, "name": c.name, "industry": c.industry, "metrics": c.metrics})

    # 兜底:若筛选后空(常见原因:demo 数据稀疏/过滤太严),返回 stocks 表前 10 只(已在池排除)作为热门参考
    is_fallback = False
    if not items:
        hot = db.query(Stock).order_by(Stock.code).limit(15).all()
        for s in hot:
            if s.code in in_pool:
                continue
            items.append({"code": s.code, "name": s.name, "industry": s.industry, "metrics": {}})
            if len(items) >= 10:
                break
        is_fallback = True

    msg = f"基于你的「{prof.archetype}」偏好,共 {len(items)} 只(已过滤已在池)"
    if is_fallback and items:
        msg = f"基于「{prof.archetype}」筛选暂无结果,以下是 stocks 池热门候选兜底展示(demo 数据限制,真实 A 股全市场会有大量结果)"

    out = {
        "items": items,
        "msg": msg,
        "archetype": prof.archetype, "scheme": scheme_key, "source": "fallback" if is_fallback else "pref",
    }
    _RECOMMEND_CACHE[cache_key] = (now, out)
    return out


def _resolve_stock_code(raw: str, db) -> str:
    """把用户输入解析为6位数字证券代码。支持代码直传或名称(精确/模糊)反查。"""
    v = (raw or "").strip()
    if not v:
        raise HTTPException(400, "代码/名称不能为空")
    if _CODE_RE.match(v):
        return v
    # 先精确匹配名称
    s = db.query(Stock).filter(Stock.name == v).first()
    if s:
        return s.code
    # 再模糊匹配，取最相关第一条
    s = db.query(Stock).filter(Stock.name.like(f'%{v}%')).first()
    if s:
        return s.code
    raise HTTPException(400, f"未找到证券：{v}")


@router.post("")
def add_to_pool(body: PoolIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    code = _resolve_stock_code(body.code, db)
    if db.query(TrackedPool).filter(TrackedPool.user_id == user.id,
                                     TrackedPool.code == code, TrackedPool.status == "active").first():
        raise HTTPException(400, "已在可投池")
    p = TrackedPool(user_id=user.id, code=code, note=body.note, cost_price=body.cost_price,
                    position_qty=body.position_qty, position_pct=body.position_pct,
                    scheme_type=body.scheme_type)
    db.add(p)
    db.commit()
    db.refresh(p)
    # 绑定标签（多对多）
    if body.tag_ids:
        valid_tags = db.query(PoolTag).filter(PoolTag.user_id == user.id,
                                               PoolTag.id.in_(body.tag_ids)).all()
        p.tags.extend(valid_tags)
        db.commit()
        db.refresh(p)
    return _to_out(p, db)


def _remove_one(code: str, user_id: int, db) -> dict:
    """单条移除的内部实现,供 delete 与 batch-delete 复用。"""
    p = (db.query(TrackedPool)
         .filter(TrackedPool.user_id == user_id,
                 TrackedPool.code == code,
                 ((TrackedPool.status == "active") |
                  ((TrackedPool.status == "archive") & (TrackedPool.position_qty > 0))))
         .order_by(TrackedPool.id)
         .first())
    if not p:
        return {"ok": False, "not_found": True, "code": code}
    hard_delete = p.status == "archive" and (p.position_qty or 0) > 0
    if hard_delete:
        db.delete(p)
    else:
        p.status = "archive"
    return {"ok": True, "hard_deleted": hard_delete, "code": code}


@router.delete("/{code}")
def remove(code: str, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """移除可投池记录:复用 list_pool 可见范围。
    - active:软删除(改 status=archive,可恢复)
    - archive 且有持仓:已经是归档态,再点就是真删——物理删除(持仓/成本一并清空,不可恢复)
    - 不存在:返回 not_found
    """
    r = _remove_one(code, user.id, db)
    db.commit()
    return r


@router.post("/batch-delete")
def batch_remove(body: PoolBatchDeleteIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """批量移除可投池:逐条复用 _remove_one 逻辑,active 软删,archive+持仓 物理删除。
    返回移除总数与硬删除明细,前端据此给出汇总提示。
    """
    results = []
    for code in set(body.codes):
        results.append(_remove_one(code, user.id, db))
    db.commit()
    removed = [r for r in results if r["ok"] and not r.get("not_found")]
    hard_codes = [r["code"] for r in removed if r.get("hard_deleted")]
    return {"ok": True, "removed": len(removed), "hard_deleted": hard_codes}


@router.post("/batch-tags")
def batch_set_tags(body: PoolBatchTagsIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """批量为选中的可投池设置标签（覆盖式）。
    tag_ids 为空数组时清空所选股票的标签。
    """
    codes = set(body.codes or [])
    if not codes:
        raise HTTPException(status_code=400, detail="请先选择股票")
    # 只处理当前用户可见范围内的记录
    rows = _user_pool_q(db, user.id).filter(TrackedPool.code.in_(codes)).all()
    if not rows:
        raise HTTPException(status_code=404, detail="未找到可设置标签的股票")
    valid_tags = []
    if body.tag_ids:
        valid_tags = db.query(PoolTag).filter(PoolTag.user_id == user.id,
                                               PoolTag.id.in_(body.tag_ids)).all()
    for p in rows:
        p.tags = list(valid_tags)
    db.commit()
    return {"ok": True, "updated": len(rows)}


@router.get("/export")
def export_pool(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """导出当前用户可投池(active)为 Excel，字段：证券代码、证券名称。"""
    rows = _user_pool_q(db, user.id).filter(TrackedPool.status == "active").order_by(TrackedPool.id).all()
    seen = set()
    data = []
    for p in rows:
        if p.code in seen:
            continue
        seen.add(p.code)
        data.append({"证券代码": p.code, "证券名称": _name(p.code, db) or ""})
    df = pd.DataFrame(data)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    filename = f"可投池_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    # RFC 5987 / RFC 6266：中文文件名必须编码，否则 Starlette header 用 latin-1 会 500
    encoded = quote(filename, safe="")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@router.post("/import")
def import_pool(
    file: UploadFile = File(...),
    db: SessionLocal = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从 Excel/CSV 导入证券代码或名称到可投池。
    支持列名包含「代码/code」或「名称/name」；校验证券信息，已在池的跳过。
    返回 {added:[], skipped:[], failed:[]}。
    """
    filename = (file.filename or "").lower()
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(file.file, dtype=str)
        else:
            df = pd.read_excel(file.file, dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")

    if df.empty:
        raise HTTPException(status_code=400, detail="文件为空或没有数据")

    # 智能匹配列
    code_col = name_col = None
    for c in df.columns:
        cs = str(c).strip()
        if "代码" in cs or cs.lower() == "code":
            code_col = c
        if "名称" in cs or cs.lower() == "name":
            name_col = c
    if code_col is None and name_col is None:
        raise HTTPException(status_code=400, detail="未找到证券代码/名称列，请确保表头包含「代码」或「名称」")

    added, skipped, failed = [], [], []
    for _, row in df.iterrows():
        raw = ""
        if code_col is not None:
            raw = str(row.get(code_col, "") or "").strip()
        if not raw and name_col is not None:
            raw = str(row.get(name_col, "") or "").strip()
        if not raw:
            continue
        try:
            code = _resolve_stock_code(raw, db)
        except HTTPException as e:
            failed.append({"input": raw, "reason": e.detail})
            continue
        name = _name(code, db) or ""
        # 已 active 在池则跳过
        exists = db.query(TrackedPool).filter(
            TrackedPool.user_id == user.id,
            TrackedPool.code == code,
            TrackedPool.status == "active",
        ).first()
        if exists:
            skipped.append({"code": code, "name": name})
            continue
        p = TrackedPool(user_id=user.id, code=code, scheme_type="custom")
        db.add(p)
        added.append({"code": code, "name": name})
    db.commit()
    return {"ok": True, "added": added, "skipped": skipped, "failed": failed,
            "msg": f"成功添加 {len(added)} 只，跳过 {len(skipped)} 只，失败 {len(failed)} 只"}


@router.post("/{code}/restore")
def restore(code: str, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """恢复 archive 记录回 active(可投池)。若该 code 已有 active 记录,把持仓/成本/备注合并过去,再删除 archive 重复项。"""
    archive_p = (db.query(TrackedPool)
                 .filter(TrackedPool.user_id == user.id,
                         TrackedPool.code == code, TrackedPool.status == "archive",
                         TrackedPool.position_qty > 0)
                 .order_by(TrackedPool.id)
                 .first())
    if not archive_p:
        return {"ok": False, "not_found": True}
    active_p = db.query(TrackedPool).filter(TrackedPool.user_id == user.id,
                                           TrackedPool.code == code, TrackedPool.status == "active").first()
    if active_p:
        # 已有 active 记录:合并持仓数据后删掉 archive 重复项
        if archive_p.position_qty is not None:
            active_p.position_qty = archive_p.position_qty
        if archive_p.cost_price is not None:
            active_p.cost_price = archive_p.cost_price
        if archive_p.note:
            active_p.note = archive_p.note
        db.delete(archive_p)
    else:
        archive_p.status = "active"
    db.commit()
    return {"ok": True, "code": code}


@router.put("/{code}")
def update_pool(code: str, note: str = "", cost_price: float | None = None,
                position_qty: float | None = None, position_pct: float | None = None,
                tag_ids: str = Query("", description="标签ID,多个逗号分隔,传空则清空,不传保持原标签"),
                db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    # 复用 list_pool 的可见范围(active + archive 且有持仓),避免对 archive 持仓更新时
    # 查不到而「新建一条重复记录」——那是之前造成 id=6/id=7 重复的根因。
    exists = (db.query(TrackedPool)
              .filter(TrackedPool.user_id == user.id,
                      TrackedPool.code == code,
                      ((TrackedPool.status == "active") |
                       ((TrackedPool.status == "archive") & (TrackedPool.position_qty > 0))))
              .order_by(TrackedPool.id)
              .first())
    if not exists:
        # 真不在池里(或 archive 且无持仓):新建一条(仅持仓信息,无备注)
        exists = TrackedPool(user_id=user.id, code=code, scheme_type="custom")
        db.add(exists)
    if note != "":
        exists.note = note
    if cost_price is not None:
        exists.cost_price = cost_price
    if position_qty is not None:
        exists.position_qty = position_qty
    if position_pct is not None:
        exists.position_pct = position_pct
    if tag_ids is not None:
        # 空字符串/0 都视为清空标签；否则按逗号解析后替换
        raw = (tag_ids or "").strip()
        if raw == "":
            new_ids = []
        else:
            try:
                new_ids = [int(x) for x in raw.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(status_code=400, detail="标签参数必须是数字 ID")
        valid_tags = db.query(PoolTag).filter(PoolTag.user_id == user.id,
                                               PoolTag.id.in_(new_ids)).all() if new_ids else []
        exists.tags = list(valid_tags)
    db.commit()
    db.refresh(exists)
    return _to_out(exists, db)
@router.get("/tags")
def list_tags(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """列出当前用户的可投池自定义标签。"""
    rows = db.query(PoolTag).filter(PoolTag.user_id == user.id).order_by(PoolTag.id).all()
    return [TagOut(id=t.id, name=t.name, color=t.color, created_at=t.created_at or "").model_dump() for t in rows]


@router.post("/tags")
def create_tag(body: TagIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """新建自定义标签。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="标签名称不能为空")
    color = (body.color or "").strip() or "#3b82f6"
    if not _HEX_COLOR_RE.match(color):
        raise HTTPException(status_code=400, detail="颜色格式不正确")
    t = PoolTag(user_id=user.id, name=name, color=color, created_at=_now())
    db.add(t); db.commit(); db.refresh(t)
    return TagOut(id=t.id, name=t.name, color=t.color, created_at=t.created_at or "").model_dump()


@router.put("/tags/{tid}")
def update_tag(tid: int, body: TagIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """修改标签名称/颜色。"""
    t = db.query(PoolTag).filter(PoolTag.id == tid, PoolTag.user_id == user.id).first()
    if not t:
        raise HTTPException(status_code=404, detail="标签不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="标签名称不能为空")
    color = (body.color or "").strip() or "#3b82f6"
    if not _HEX_COLOR_RE.match(color):
        raise HTTPException(status_code=400, detail="颜色格式不正确")
    t.name = name
    t.color = color
    db.commit(); db.refresh(t)
    return TagOut(id=t.id, name=t.name, color=t.color, created_at=t.created_at or "").model_dump()


@router.delete("/tags/{tid}")
def delete_tag(tid: int, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """删除标签；同时清理 tracked_pool_tags 关联记录。"""
    t = db.query(PoolTag).filter(PoolTag.id == tid, PoolTag.user_id == user.id).first()
    if not t:
        raise HTTPException(status_code=404, detail="标签不存在")
    db.query(TrackedPoolTag).filter(TrackedPoolTag.tag_id == tid).delete(synchronize_session=False)
    db.delete(t)
    db.commit()
    return {"ok": True}
