"""短线可投池路由。"""
from __future__ import annotations
import datetime as dt
import json
import re as _re
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from typing import Optional

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import DailyQuote, DayViewLog, DayViewRecap, PoolTag, SchemeType, Stock, TrackedPool, TrackedPoolTag, User, UserProfile, _now
from app.schemas import (
    DayViewIn, DayViewLogIn, DayViewLogOut, MetricsSummaryOut, PoolBatchDeleteIn, PoolBatchTagsIn, PoolImportIn, PoolIn, PoolOut,
    RecapIn, TagIn, TagOut, WatchLogItem, WatchLogOut,
)
from app.services.data_fetcher import ensure_stock_name, get_pool_track, _fetch_intraday_fund, _market_return
from app.services.preference import match_scheme
from app.services.screener import get_screener
from sqlalchemy import and_, func, or_

_CODE_RE = _re.compile(r'^\d{6}$')
# 颜色格式校验（#RGB / #RRGGBB）
_HEX_COLOR_RE = _re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# v143: 强制用中国时区取"今日"——避免容器跑 UTC 导致跨日错位
_CN_TZ = dt.timezone(dt.timedelta(hours=8))


def _today_cn() -> str:
    return dt.datetime.now(_CN_TZ).date().isoformat()


router = APIRouter(prefix="/api/pool", tags=["pool"])


def _name(code: str, db) -> str | None:
    s = db.query(Stock).filter(Stock.code == code).first()
    return s.name if s and s.name else None


def _industry(code: str, db) -> str:
    s = db.query(Stock).filter(Stock.code == code).first()
    return (s.industry if s and s.industry else "") or "未知"


def _seed_from_db(code: str, db) -> dict | None:
    """v118: 纯 DB 日线秒级播种——冷启动首页绝不空白。

    与 get_pool_track 的核心口径一致:现价/涨跌幅以「日线最新一根」前复权收盘为准
    (与 MA/箱体同源同口径,避免复权差异),零网络请求。富化线程完成后会用全量
    track 覆盖;未完成的行至少保留本播种(标 seed_src='db' 供前端辨认)。
    """
    try:
        qs = (db.query(DailyQuote).filter(DailyQuote.code == code)
              .order_by(DailyQuote.date.desc()).limit(60).all())
        if not qs:
            return None
        qs = qs[::-1]  # 升序:旧→新
        closes = [q.close for q in qs if q.close]
        if not closes:
            return None
        last = qs[-1]
        out: dict = {
            "price": round(last.close, 3),
            "change_pct": round((last.close - (last.pre_close or last.close)) / (last.pre_close or last.close) * 100, 2)
                          if last.pre_close else 0.0,
            "price_date": last.date,
            "ma5": round(sum(closes[-5:]) / min(5, len(closes)), 3),
            "ma20": round(sum(closes[-20:]) / min(20, len(closes)), 3),
            "ts": int(dt.datetime.now().timestamp()),
            "seed_src": "db",
        }
        recent = closes[-20:] if len(closes) >= 20 else closes
        out["box_high"] = round(max(recent), 3)
        out["box_low"] = round(min(recent), 3)
        if out["box_high"] != out["box_low"]:
            out["box_pos"] = round(max(0.0, min(1.0, (out["price"] - out["box_low"]) / (out["box_high"] - out["box_low"]))), 2)
        out["above_ma5"] = out["price"] > out["ma5"]
        out["above_ma20"] = out["price"] > out["ma20"]
        # v186: 播种「当日最高/最低」——冷启动时富化常超 9.5s 墙钟预算,未完成的行会保留本播种值,
        #   若播种里没有高/低,可投池「最高/最低」列会整列显示 "-"(用户实测)。
        #   仅当 DB 已有【当日】日线时才播种,避免盘中/盘前把 T-1 的高低当"当日"展示。
        #   口径与 get_pool_track 的盘后分支一致(当日日线已定格,前复权,与现价/MA 同源)。
        if str(last.date or "")[:10] == _today_cn()[:10]:
            if last.high:
                out["today_high"] = round(last.high, 2)
            if last.low:
                out["today_low"] = round(last.low, 2)
        return out
    except Exception:
        return None


def _to_out(p: TrackedPool, db) -> PoolOut:
    tags = [{"id": t.id, "name": t.name, "color": t.color} for t in p.tags]
    tag_ids = [t.id for t in p.tags]
    # v143: day_view 字段保留(不再写),day_view_today/day_view_log_count 由调用方按 today 注入
    return PoolOut(id=p.id, code=p.code, name=_name(p.code, db), industry=_industry(p.code, db),
                   note=p.note,
                   cost_price=p.cost_price, position_qty=p.position_qty,
                   position_pct=p.position_pct,
                   scheme_type=p.scheme_type, status=p.status, added_at=str(p.added_at),
                   tag_ids=tag_ids, tags=tags, day_view=p.day_view or "",
                   day_view_today="", day_view_log_count=0)


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
    today: str = Query("", description="v143: 当前日期 YYYY-MM-DD,用于返回 day_view_today 字段;留空=服务器中国时区今日"),
    light: int = Query(0, description="v180: 1=仅返回本地静态字段(代码/名称/标签/行业/日初判断/备注等),不拉行情行情评分,秒回;0=默认全量富化"),
    db: SessionLocal = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出当前用户的可投池（分页返回）。
    支持按证券代码/名称(q)、备注(note)、标签(tag)过滤；空值表示不过滤。
    light=1 用于页面「静态秒开」:先渲染不依赖外部接口的信息,行情由定时刷新全量富化。
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

    # 名称补全 + 「每日跟踪数据」并发拉取:单只失败不阻塞其他。所有跟踪数据带 60s 内存缓存,重复刷新秒开。
    # 传入持仓/成本, 让操作建议能感知止盈/持仓状态。
    tracks: dict[str, dict] = {}
    positions = {p.code: {"cost_price": p.cost_price, "position_qty": p.position_qty} for p in page_rows}
    # v118: 先纯 DB 播种核心行情(现价/MA/箱体,零网络),保证首页绝不空白；
    # 富化线程完成后逐只覆盖为全量 track。
    if not light:
        for p in page_rows:
            _sd = _seed_from_db(p.code, db)
            if _sd:
                tracks[p.code] = _sd
    # v118: 冷启动性能(整页须稳定在网关 ~10s 内)——
    #   1) 每行一个 worker,避免 15 只挤 10 worker 变成「两轮」导致全部超预算;
    #   2) 盘中资金流批量预取 + 大盘收益预取也放进同一线程池(预算内),不再串行占用关键路径;
    #   3) 单只 get_pool_track 传 deadline=7.6s:可选扩展阶段(资金流/估值/板块/标签/财报风险)
    #      到点即放弃,核心字段(现价/MA/箱体/打分/建议)必回,整页稳定 ~9s 返回。
    #   4) 块9/块10 的 financials+news 已改为调用内复用(见 data_fetcher.get_pool_track)。
    if (not light) and page_rows:
        ex = ThreadPoolExecutor(max_workers=min(len(page_rows), 20))
        try:
            warm_futs: set = set()
            try:
                warm_futs.add(ex.submit(_fetch_intraday_fund, [p.code for p in page_rows]))
            except Exception:
                pass
            try:
                warm_futs.add(ex.submit(_market_return, 20))
            except Exception:
                pass
            track_futs = {ex.submit(get_pool_track, p.code, db, positions.get(p.code), 7.6): p.code for p in page_rows}
            name_futs = {}
            for p in page_rows:
                if not _name(p.code, db):
                    name_futs[ex.submit(ensure_stock_name, p.code, db)] = p.code
            done, _pending = wait(set(track_futs) | set(name_futs) | warm_futs, timeout=9.5)
            for f in done:
                if f in track_futs:
                    code = track_futs[f]
                    try:
                        tracks[code] = f.result()
                    except Exception:
                        tracks[code] = {}
            # 未完成的任务：取不到就算了(留给缓存/下次刷新)，不阻塞响应
            for f in track_futs:
                if f not in done:
                    code = track_futs[f]
                    tracks.setdefault(code, {})
            for f in name_futs:
                if f in done:
                    try:
                        f.result()
                    except Exception:
                        pass
        finally:
            # 不等未完成任务：让慢请求在后台自行结束(结果进 60s 缓存，下次刷新秒回)
            ex.shutdown(wait=False, cancel_futures=False)

    # v143: 批量拉 day_view_log 派生字段(today 最后一条 trend + 总计数),N 只股票 2 次查询
    today_map: dict[str, str] = {}
    count_map: dict[str, int] = {}
    if page_rows:
        codes = [p.code for p in page_rows]
        # 没传 today 时默认用中国时区今日(避免 UTC 跨日错位)
        eff_today = today or _today_cn()
        if eff_today and _re.match(r"^\d{4}-\d{2}-\d{2}$", eff_today):
            # 当日最后一条 trend
            day_rows = (db.query(DayViewLog)
                        .filter(DayViewLog.user_id == user.id,
                                DayViewLog.trade_date == eff_today,
                                DayViewLog.code.in_(codes))
                        .order_by(DayViewLog.code, DayViewLog.operated_at.desc())
                        .all())
            for r in day_rows:
                if r.code not in today_map:
                    today_map[r.code] = r.trend or "-"
        # 总历史条数(v185: 仍保留,用于「盯盘日志」按钮是否显示 —— 只看有没有历史,不按当天)
        cnt_rows = (db.query(DayViewLog.code, func.count(DayViewLog.id))
                    .filter(DayViewLog.user_id == user.id, DayViewLog.code.in_(codes))
                    .group_by(DayViewLog.code)
                    .all())
        count_map = {c: int(n) for c, n in cnt_rows}
        # v185: 当日条数 —— 列表「日初判断」右侧角标只统计当前交易日维护的条数,
        #   非当天维护的记录不计入(前端 0 则不显示数字),与「盯盘日志」按钮的显示条件解耦
        today_cnt_rows = (db.query(DayViewLog.code, func.count(DayViewLog.id))
                          .filter(DayViewLog.user_id == user.id,
                                  DayViewLog.trade_date == eff_today,
                                  DayViewLog.code.in_(codes))
                          .group_by(DayViewLog.code)
                          .all())
        today_count_map = {c: int(n) for c, n in today_cnt_rows}

    result = []
    for p in page_rows:
        o = _to_out(p, db).model_dump()
        o["track"] = tracks.get(p.code, {})
        o["day_view_today"] = today_map.get(p.code, "")
        o["day_view_log_count"] = count_map.get(p.code, 0)
        o["day_view_log_count_today"] = today_count_map.get(p.code, 0)   # v185: 仅当天条数
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
    # 绑定标签（多对多），显式写入关联表以带上 user_id 便于按账号清理
    if body.tag_ids:
        valid_tags = db.query(PoolTag).filter(PoolTag.user_id == user.id,
                                               PoolTag.id.in_(body.tag_ids)).all()
        for t in valid_tags:
            db.add(TrackedPoolTag(pool_id=p.id, tag_id=t.id, user_id=user.id))
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
        # 先清空旧关联再重建，确保 user_id 一致且不会残留孤儿记录
        db.query(TrackedPoolTag).filter(TrackedPoolTag.pool_id == p.id).delete(synchronize_session=False)
        for t in valid_tags:
            db.add(TrackedPoolTag(pool_id=p.id, tag_id=t.id, user_id=user.id))
    db.commit()
    return {"ok": True, "updated": len(rows)}


# ===== 导出列定义(v186) =====
# 每项是 (key, excel表头, 值)。key 与前端表格列 key 一一对应，前端传 cols 即可按
# 「当前可见列 + 当前列序」导出（真正的所见即所得）；同一 key 可占多列(如 code_name
# 拆成代码/名称)，只要 key 命中就整组导出。
# 约定：key 为空字符串 "" 的列属于「仅全量导出」(前端没有对应列)，传 cols 时不导出。
def _exp(o: dict, t: dict, user_score=None) -> list[tuple[str, str, object]]:
    """把一行(可投池+track)展开为 (列key, 表头, 值) 列表。
    数值直接给数值，文本给字符串，缺失统一给空串；箱体给 0~1 数值。"""
    cells: list[tuple[str, str, object]] = []
    def add(key: str, label: str, value):
        cells.append((key, label, "" if value is None else value))

    gap = t.get("gap") or {}
    sig = t.get("main_signal") or {}
    sec = t.get("sector_strength") or {}
    er = t.get("event_risk_tags") or {}
    fr = t.get("finance_risk") or {}
    adv = t.get("operation_advice") or {}
    ts = t.get("tech_signals") or {}
    # —— 基础(复合列拆两列,保证 Excel 里代码/名称可各自排序筛选) ——
    add("code_name", "证券代码", o.get("code"))
    add("code_name", "证券名称", o.get("name") or "")
    add("industry", "行业", o.get("industry") or "")
    # —— 实时/成交量 ——
    add("price", "现价", t.get("price"))
    add("change_pct", "涨跌幅%", t.get("change_pct"))
    # v186: 当日最高/最低(实时行情 track.today_high/today_low;盘间无数据 → 空)
    add("high", "最高", t.get("today_high"))
    add("low", "最低", t.get("today_low"))
    add("realtime_volume", "当天实时成交量(万手)", t.get("realtime_volume"))
    add("yesterday_volume_at_time", "T-1日同期成交量(万手)", t.get("yesterday_volume_at_time"))
    add("avg_volume_20", "20日平均成交量(万手)", t.get("avg_volume_20"))
    add("yesterday_volume_total", "T-1日全天成交量(万手)", t.get("yesterday_volume_total"))
    add("vol_ratio", "量比", t.get("vol_ratio"))
    add("turnover", "换手%", t.get("turnover"))
    add("intra_amplitude", "日内振幅%", t.get("intra_amplitude"))
    add("ma5", "MA5", t.get("ma5"))
    add("ma20", "MA20", t.get("ma20"))
    # —— 箱体/趋势 ——
    add("box_pos", "箱体位置(0~1)", t.get("box_pos"))
    add("pct_5d", "5日涨跌幅%", t.get("pct_5d"))
    add("gap", "跳空缺口", gap.get("text") if isinstance(gap, dict) else None)
    # —— 资金流(当日) ——
    add("main_net_pct", "主力净流入%", t.get("main_net_pct"))
    add("main_signal", "主力信号", sig.get("text") if isinstance(sig, dict) else None)
    add("big_order_net", "大单流向(万元)", t.get("big_order_net"))
    # —— 估值/强弱 ——
    add("valuation_pct_3y", "估值分位(近3年)%", t.get("valuation_pct_3y"))
    add("sector_strength", "板块强度", sec.get("text") if isinstance(sec, dict) else None)
    add("pe_ttm", "市盈率TTM", t.get("pe_ttm"))
    add("pb", "市净率", t.get("pb"))
    add("pe_pct_3y", "PE近3年分位%", t.get("pe_pct_3y"))
    # —— 财报(与页面口径一致:商誉转「亿元」) ——
    add("net_profit_yoy", "净利同比%", t.get("net_profit_yoy"))
    add("revenue_yoy", "营收同比%", t.get("revenue_yoy"))
    add("report_date", "财报日期", t.get("report_date"))
    gw = t.get("goodwill")
    add("goodwill", "商誉(亿)", round(gw / 1e8, 2) if isinstance(gw, (int, float)) else None)
    add("next_ratio", "解禁占比%", t.get("next_ratio"))
    add("next_date", "解禁日期", t.get("next_date"))
    # —— 评分/风险/建议 ——
    # 综合评分:页面显示「实时重算分 user_score」,缺失才回落后端官方分
    add("total_score", "综合评分",
        user_score if user_score is not None else t.get("total_score"))
    add("event_risk_tags", "事件风险", "/".join(str(k) for k in er.keys()) if er else "")
    add("finance_risk", "财报风险", fr.get("summary") if isinstance(fr, dict) else None)
    add("operation_advice", "操作建议", adv.get("title") if isinstance(adv, dict) else None)
    if isinstance(ts, dict) and ts:
        add("tech_signals", "MACD/KDJ/布林", "/".join(str(ts.get(k, "中")) for k in ("macd", "kdj", "boll")))
    else:
        add("tech_signals", "MACD/KDJ/布林", "")
    add("must_pass", "必看-达标项(分)", t.get("must_pass_score"))
    add("key_pass", "重要-达标项(分)", t.get("key_pass_score"))
    add("", "辅助-达标项(分)", t.get("aux_pass_score"))
    add("", "AI评等级", t.get("ai_grade"))
    # —— 持仓/备注/标签 ——
    add("position_qty", "持仓", o.get("position_qty"))
    add("cost_price", "成本", o.get("cost_price"))
    add("note", "备注", o.get("note") or "")
    add("tags", "标签", "/".join(str(x.get("name", "")) for x in (o.get("tags") or [])))
    add("", "方案", o.get("scheme_type") or "")
    # —— 日初判断 ——
    add("day_view", "日初判断(当日)", o.get("day_view_today") or "")
    add("", "日初判断历史数", o.get("day_view_log_count") or 0)
    return cells


def _sort_value(v):
    """导出排序用：空值恒排最后；数字比数字，其余按字符串。"""
    if v is None or v == "":
        return (1, 0.0, "")
    if isinstance(v, bool):
        return (0, float(v), "")
    if isinstance(v, (int, float)):
        return (0, float(v), "")
    return (0, 0.0, str(v))


@router.get("/export")
def export_pool(
    q: str = Query("", description="证券代码或名称模糊查询"),
    tag: str = Query("", description="按标签 ID 筛选(多个用逗号分隔)"),
    cols: str = Query("", description="按当前表格列序导出，逗号分隔的列 key；留空则导出全部列"),
    sort: str = Query("", description="v186: 按当前排序列 key 导出；留空=默认序(加池顺序)"),
    sort_dir: str = Query("asc", description="v186: 排序方向 asc|desc"),
    view: str = Query("pool", description="v186: pool=盘间监控 | pretrade=盘前预判 | review=复盘管理"),
    trade_date: str = Query("", description="v186: 交易日 YYYY-MM-DD（pretrade/review 视图使用）"),
    db: SessionLocal = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导出当前用户可投池为 Excel（所见即所得）。

    - 按当前筛选(q/tag)导出【全部】匹配行（不只当前页）；
    - v186: 按前端 cols(当前可见列+列序) 出列，按 sort/sort_dir 出行序；
    - v186: view 决定数据源 —— pool(实时行情) / pretrade(盘前预判) / review(复盘对比)。
    """
    if view in ("pretrade", "review"):
        return _export_pretrade_or_review(view, q, tag, cols, sort, sort_dir, trade_date, db, user)
    # 筛选 + 去重（与 list_pool 口径一致：active + archive 有持仓）
    rows = _user_pool_q(db, user.id).order_by(TrackedPool.id).all()
    q = (q or "").strip()
    if q:
        rows = [p for p in rows
                if q in p.code or q in (_name(p.code, db) or "")]
    tag_ids: list[int] = []
    tag = (tag or "").strip()
    if tag:
        try:
            tag_ids = [int(x) for x in tag.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="标签参数必须是数字 ID")
    seen: dict[str, TrackedPool] = {}
    for p in rows:
        if tag_ids:
            pid_tags = {t.id for t in p.tags}
            if not pid_tags.intersection(tag_ids):
                continue
        cur = seen.get(p.code)
        if cur is None or (p.position_qty and (not cur.position_qty or p.position_qty > cur.position_qty)):
            seen[p.code] = p
    page_rows = list(seen.values())

    # 全量拉 track（复用并发拉取；与列表页一致预取资金流与大盘收益）
    tracks: dict[str, dict] = {}
    if page_rows:
        try:
            _fetch_intraday_fund([p.code for p in page_rows])
        except Exception:
            pass
        try:
            _market_return(20)
        except Exception:
            pass
        positions = {p.code: {"cost_price": p.cost_price, "position_qty": p.position_qty} for p in page_rows}
        with ThreadPoolExecutor(max_workers=min(8, max(2, len(page_rows)))) as ex:
            futs = {ex.submit(get_pool_track, p.code, db, positions.get(p.code)): p.code for p in page_rows}
            for f in as_completed(futs):
                code = futs[f]
                try:
                    tracks[code] = f.result()
                except Exception:
                    tracks[code] = {}

    # 列展开：展开成 (key, 表头, 值)，再按前端 cols / sort 裁剪排序
    rows_cells: list[list[tuple[str, str, object]]] = []
    for p in page_rows:
        o = _to_out(p, db).model_dump()
        o["code"] = p.code
        t = tracks.get(p.code, {}) or {}
        rows_cells.append(_exp(o, t, user_score=o.get("user_score")))

    # v186: 按当前可见列(及列序)裁剪 —— 真正「所见即所得」
    keys = [k.strip() for k in (cols or "").split(",") if k.strip()]
    if keys:
        def _pick(cells: list[tuple[str, str, object]]):
            picked: list[tuple[str, object]] = []
            for k in keys:                       # 按前端列序，同一 key 可展开多列
                for ck, cl, cv in cells:
                    if ck == k:
                        picked.append((cl, cv))
            return picked
        rows_cells = [_pick(c) for c in rows_cells]
    else:
        rows_cells = [[(cl, cv) for _k, cl, cv in c] for c in rows_cells]

    # v186: 按当前表格排序导出（空值恒排最后）
    if sort:
        # 建立 key → 表头 映射(同一 key 展开多列时取第一列)，据此在每行里定位排序列
        label_of_key: dict[str, str] = {}
        for ck, cl, _cv in _exp({}, {}):
            label_of_key.setdefault(ck, cl)
        target_label = label_of_key.get(sort)
        if target_label:
            def _sv(cells: list[tuple[str, object]]):
                for cl, cv in cells:
                    if cl == target_label:
                        return _sort_value(cv)
                return _sort_value(None)
            rows_cells.sort(key=_sv, reverse=(sort_dir.lower() == "desc"))

    data: list[dict] = []
    for cells in rows_cells:
        row: dict[str, object] = {}
        for cl, cv in cells:
            if cl in row and row[cl] != "":
                continue          # 重名表头(仅理论可能):保留首个非空值
            row[cl] = cv
        data.append(row)

    # v166: pandas 懒加载——本函数(导出)是唯一用到 DataFrame.to_excel 的地方，
    #       放到函数内 import 可避免 FastAPI 启动即加载 pandas+numpy(常驻数百 MB)，降低沙箱 OOM 概率
    import pandas as pd
    df = pd.DataFrame(data, dtype=object)  # dtype=object：证券代码等文本列保持字符串，避免前导0被吞
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    filename = f"可投池_全量_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
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
    """从 Excel/CSV 导入证券到可投池（导入新增）。

    模板列：证券代码(必填) / 证券名称(非必填) / 标签(非必填)。
    规则（v117，用户确认）：
      - 按证券代码判重：可投池已有则【跳过】，不重复导入、不改动其标签；
      - 证券代码格式错误 / 找不到对应市场证券 => 计入导入失败；
      - 新导入的证券若带「标签」，按名称匹配该用户已有标签并打上（不存在则新建）；
      - 提示文案：导入成功 x 条，导入失败 x 条，请检查证券代码是否重复导入。
    返回 {ok, added:[], failed:[], msg}。
    """
    filename = (file.filename or "").lower()
    try:
        import pandas as pd  # v166: 懒加载，仅导入文件时加载 pandas
        if filename.endswith(".csv"):
            df = pd.read_csv(file.file, dtype=str)
        else:
            df = pd.read_excel(file.file, dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")

    if df.empty:
        raise HTTPException(status_code=400, detail="文件为空或没有数据")

    # 智能匹配列：代码/名称/标签
    code_col = name_col = tag_col = None
    for c in df.columns:
        cs = str(c).strip()
        if "代码" in cs or cs.lower() == "code":
            code_col = c
        elif "名称" in cs or cs.lower() == "name":
            name_col = c
        elif "标签" in cs or cs.lower() == "tag":
            tag_col = c
    if code_col is None and name_col is None:
        raise HTTPException(status_code=400, detail="未找到证券代码/名称列，请确保表头包含「证券代码」或「证券名称」")

    # 当前用户已有标签 map：名称 → PoolTag
    tag_map = {t.name: t for t in db.query(PoolTag).filter(PoolTag.user_id == user.id).all()}
    # 已在池的 code 集合（active + archive 有持仓的可见范围，按 code 判重）
    existing_codes = {p.code for p in _user_pool_q(db, user.id).all()}

    added: list[dict] = []
    failed: list[dict] = []
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
        if code in existing_codes:
            # 已存在：跳过（保留其原标签），计入失败提示避免重复导入
            failed.append({"code": code, "name": name, "reason": "已在可投池，重复导入"})
            continue
        p = TrackedPool(user_id=user.id, code=code, scheme_type="custom")
        db.add(p)
        existing_codes.add(code)
        # 标签：新导入才打标签
        if tag_col is not None:
            db.flush()  # 确保 p.id 生成，供 TrackedPoolTag 关联
            tag_names = [s for s in _re.split(r"[,，/、;；]", str(row.get(tag_col, "") or "")) if s.strip()]
            for tn in tag_names[:5]:
                t = tag_map.get(tn.strip())
                if t is None:
                    t = PoolTag(user_id=user.id, name=tn.strip(), color="#3b82f6")
                    db.add(t)
                    db.flush()
                    tag_map[t.name] = t
                db.add(TrackedPoolTag(pool_id=p.id, tag_id=t.id, user_id=user.id))
        added.append({"code": code, "name": name})
    db.commit()
    return {"ok": True, "added": added, "failed": failed,
            "msg": f"导入成功 {len(added)} 条，导入失败 {len(failed)} 条，请检查证券代码是否重复导入"}


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
                # v138: 修复「改了非标签字段就把标签全删掉」的 bug——
                #   旧实现默认 `Query("")`，FastAPI 把「URL 里不传」和「传空串」都映射成 ""，
                #   导致前端只改持仓/成本/备注时，后端总是进入「清空所有标签」分支。
                #   改为 Optional + Query(None)，让 None = 「不动标签」(语义对照 docstring)：
                #     · tag_ids is None   → 不动现有标签 (前端未传)
                #     · tag_ids == ""     → 清空所有标签 (前端显式传空)
                #     · tag_ids == "1,2"  → 替换为这些标签
                tag_ids: Optional[str] = Query(None, description="标签ID,多个逗号分隔；None=不动,空串=清空,逗号串=替换"),
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
    if tag_ids is not None:  # v138: None 表示「不动」,仅在显式传入(空或非空)时才触碰标签
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
        # 清空旧关联后重建，显式写入 user_id
        db.query(TrackedPoolTag).filter(TrackedPoolTag.pool_id == exists.id).delete(synchronize_session=False)
        for t in valid_tags:
            db.add(TrackedPoolTag(pool_id=exists.id, tag_id=t.id, user_id=user.id))
    db.commit()
    db.refresh(exists)
    return _to_out(exists, db)
# v143: 日初判断重构——历史走 day_view_log 表,旧 PUT /{code}/day-view 标记 deprecated
# 新接口:POST /{code}/day-view-log(追加一条);GET /{code}/day-view-log(取历史);GET /{code}/watch-log(盯盘日志聚合)
_V143_TREND = ("看涨", "看跌", "风险", "-")
_RE_DATE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.put("/{code}/day-view")
def update_day_view(code: str, body: DayViewIn,
                    db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """v135 旧接口,v143 标记 deprecated。新写入请走 POST /{code}/day-view-log。

    旧字段(TrackedPool.day_view)只读保留,不写新值;若需"恢复"逻辑可读旧值。
    """
    # v143: 不再写旧字段,直接返回当前 code 的最新 day_view(从 log 派生 today)
    exists = _user_pool_q(db, user.id).filter(TrackedPool.code == code).order_by(TrackedPool.id).first()
    if not exists:
        raise HTTPException(status_code=404, detail="该股票不在可投池中")
    return _to_out(exists, db)


# v178: 录入日初判断前的预判参考信息快照端点（不写库,只读快照）
@router.get("/{code}/day-view-log/preview", response_model=MetricsSummaryOut)
def preview_day_view_log(code: str,
                          db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """v178: 录入日初判断前的预判参考信息快照——给前端录入弹窗的「综合评分 + 预判参考信息」两列
    预览使用。来源是 get_pool_track() 的实时结果,日线/盘口缺失时字段退化(允许为 None)。

    字段语义:
      - composite_score: 默认用后端官方分 (track.total_score);前端可用列表里的
        user_score 实时分覆盖该值(列表显示的就是 user_score)。
      - reference_text: 按 6 个核心字段(量比/换手/日内振幅/MA5/MA20/箱体位置)拼成的
        中文短句,作为弹窗预览 + 入库快照。任一字段缺失时只展示已有部分。
      - metrics: 同一组数据,JSON 结构,前端可二次格式化兜底。
      - official_score: track.total_score 原值,前端用来对照"实时 vs 官方"。
    """
    name = _name(code, db)
    if not _user_pool_q(db, user.id).filter(TrackedPool.code == code).first():
        raise HTTPException(status_code=404, detail="该股票不在可投池中")
    try:
        track = get_pool_track(code, db) or {}
    except Exception:
        track = {}

    # 综合评分默认用后端官方分。允许 list 端用 user_score 覆盖(客户端自行决定)
    composite = track.get("total_score")
    official = track.get("total_score")
    if composite is not None:
        try:
            composite = round(float(composite), 2)
        except Exception:
            composite = None
    if official is not None:
        try:
            official = round(float(official), 2)
        except Exception:
            official = None

    # 6 个指标取数 + 中文短句组装
    def _f(x, n=2):
        try:
            if x is None: return None
            v = float(x)
            return round(v, n)
        except Exception:
            return None

    vr = _f(track.get("vol_ratio"), 2)        # 量比
    to = _f(track.get("turnover"), 2)         # 换手率(%)
    # 日内振幅:优先用盘口 high/low - pre_close 实时计算;若不可用留 None
    amp = None
    try:
        hi = track.get("today_high"); lo = track.get("today_low"); pc = track.get("pre_close")
        if hi and lo and pc and pc > 0:
            amp = round((float(hi) - float(lo)) / float(pc) * 100.0, 2)
    except Exception:
        amp = None
    # 兜底:从 daily_quotes 最近一根日线拿 amplitude
    if amp is None:
        try:
            recent = (db.query(DailyQuote)
                      .filter(DailyQuote.code == code)
                      .order_by(DailyQuote.date.desc()).first())
            if recent and recent.high and recent.low and recent.pre_close and recent.pre_close > 0:
                amp = round((float(recent.high) - float(recent.low)) / float(recent.pre_close) * 100.0, 2)
        except Exception:
            pass

    ma5 = _f(track.get("ma5"), 3)
    ma20 = _f(track.get("ma20"), 3)
    bp = track.get("box_pos")
    box_pct = None
    if bp is not None:
        try:
            box_pct = round(float(bp) * 100, 0)  # 按百分制整数展示,与样例 "箱体位置:72%" 对齐
        except Exception:
            box_pct = None

    metrics = {
        "vol_ratio": vr,
        "turnover": to,         # %
        "amplitude": amp,       # %
        "ma5": ma5,
        "ma20": ma20,
        "box_pos": bp,          # 0~1
        "box_pos_pct": box_pct, # 0~100
        "price": _f(track.get("price"), 3),
        "change_pct": _f(track.get("change_pct"), 2),
    }

    parts = []
    if vr is not None:            parts.append(f"量比:{vr:.2f}")
    if to is not None:            parts.append(f"换手:{to:.2f}%")
    if amp is not None:           parts.append(f"日内振幅:{amp:.2f}%")
    if ma5 is not None:           parts.append(f"MA5:{ma5:.3f}")
    if ma20 is not None:          parts.append(f"MA20:{ma20:.3f}")
    if box_pct is not None:       parts.append(f"箱体位置:{box_pct:.0f}%")
    ref_text = "、".join(parts)
    if not ref_text:
        ref_text = "暂无可用参考指标(日线/盘口均缺数据)"

    return MetricsSummaryOut(
        code=code, composite_score=composite, official_score=official,
        reference_text=ref_text, metrics=metrics,
    )


@router.post("/{code}/day-view-log")
def add_day_view_log(code: str, body: DayViewLogIn,
                     db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """v143: 追加一条日初判断修改记录。

    校验:trend ∈ {看涨,看跌,风险,-};target_price ≥ 0;target_note ≤ 20 字;trade_date YYYY-MM-DD;
    同一交易日允许多次写入(append-only)。
    v178: 顺便支持复合评分快照 + 预判参考信息文本 + 结构化指标 JSON 一并入库。
    """
    if not _RE_DATE.match(body.trade_date or ""):
        raise HTTPException(status_code=400, detail="trade_date 必须是 YYYY-MM-DD")
    if body.trend not in _V143_TREND:
        raise HTTPException(status_code=400, detail=f"trend 必须是: {','.join(_V143_TREND)}")
    if body.target_price is not None and body.target_price < 0:
        raise HTTPException(status_code=400, detail="target_price 必须 ≥ 0")
    # v185: 目标买入 / 目标卖出(拆分后的两个端,可只填其一)
    tb = body.target_buy
    ts = body.target_sell
    if tb is not None:
        try:
            tb = float(tb)
        except Exception:
            raise HTTPException(status_code=400, detail="target_buy 必须是数字")
        if tb < 0:
            raise HTTPException(status_code=400, detail="target_buy 必须 ≥ 0")
    if ts is not None:
        try:
            ts = float(ts)
        except Exception:
            raise HTTPException(status_code=400, detail="target_sell 必须是数字")
        if ts < 0:
            raise HTTPException(status_code=400, detail="target_sell 必须 ≥ 0")
    if tb and ts and tb > ts:
        raise HTTPException(status_code=400, detail="目标买入价不应高于目标卖出价")
    note = (body.target_note or "").strip()
    if len(note) > 20:
        raise HTTPException(status_code=400, detail="target_note 不能超过 20 字")

    exists = _user_pool_q(db, user.id).filter(TrackedPool.code == code).order_by(TrackedPool.id).first()
    if not exists:
        raise HTTPException(status_code=404, detail="该股票不在可投池中")

    # v178: 综合评分快照(0-100,允许为 None 表示不入快照——例如旧客户端未发该字段)
    cs = body.composite_score
    if cs is not None:
        try:
            cs = float(cs)
        except Exception:
            raise HTTPException(status_code=400, detail="composite_score 必须是数字")
        if cs < 0 or cs > 100:
            raise HTTPException(status_code=400, detail="composite_score 必须在 0-100 之间")
    # 预判参考信息文本(默认 500 字封顶,与「偏离原因复盘」一致)
    rt = (body.reference_text or "").strip()
    if len(rt) > 500:
        raise HTTPException(status_code=400, detail="reference_text 长度不能超过 500 字")
    rj = (body.reference_metrics_json or "").strip()
    if len(rj) > 4000:
        raise HTTPException(status_code=400, detail="reference_metrics_json 长度过长(>4000 字节)")

    row = DayViewLog(
        user_id=user.id, code=code, trade_date=body.trade_date,
        trend=body.trend,
        target_price=body.target_price if body.target_price is not None and body.target_price > 0 else None,
        # v185: 目标买入 / 目标卖出(落库;0 视为未填)
        target_buy=(tb if tb and tb > 0 else None),
        target_sell=(ts if ts and ts > 0 else None),
        target_note=note[:20],
        operator=user.username or "", operator_id=user.id,
        operated_at=_now(),
        # v178
        composite_score=cs,
        reference_text=rt[:500],
        reference_metrics_json=rj[:4000],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return DayViewLogOut(
        id=row.id, code=row.code, trade_date=row.trade_date,
        trend=row.trend, target_price=row.target_price,
        target_buy=row.target_buy, target_sell=row.target_sell,   # v185
        target_note=row.target_note or "",
        operator=row.operator or "", operated_at=row.operated_at or "",
        composite_score=row.composite_score,
        reference_text=row.reference_text or "",
        reference_metrics_json=row.reference_metrics_json or "",
    )


@router.get("/{code}/day-view-log")
def list_day_view_log(code: str, trade_date: str = Query("", description="可选:YYYY-MM-DD 过滤单日"),
                      limit: int = Query(200, ge=1, le=2000),
                      db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """v143: 列出该 code 的所有日初判断历史,按 trade_date desc, operated_at desc。
    v178: 返回中携带 composite_score / reference_text / reference_metrics_json 3 个快照字段。
    """
    q = (db.query(DayViewLog)
         .filter(DayViewLog.user_id == user.id, DayViewLog.code == code))
    if trade_date and _RE_DATE.match(trade_date):
        q = q.filter(DayViewLog.trade_date == trade_date)
    rows = q.order_by(DayViewLog.trade_date.desc(), DayViewLog.operated_at.desc()).limit(limit).all()
    return [DayViewLogOut(
        id=r.id, code=r.code, trade_date=r.trade_date,
        trend=r.trend or "-", target_price=r.target_price,
        target_buy=r.target_buy, target_sell=r.target_sell,       # v185
        target_note=r.target_note or "",
        operator=r.operator or "", operated_at=r.operated_at or "",
        composite_score=r.composite_score,
        reference_text=r.reference_text or "",
        reference_metrics_json=r.reference_metrics_json or "",
    ).model_dump() for r in rows]


def _deviation_reason(trend: str, dev: float | None, has_close: bool = True) -> str:
    """v143: 偏离原因复盘——按 trend × deviation 方向生成纯模板话术。

    v185 偏离度口径改为「买卖区间口径」后,dev 的符号含义同步调整为:
        deviation = (收盘价 C − 参考价) / 参考价
        参考价 = 收盘低于买入预判时取买入价 B;高于卖出预判时取卖出价 S;落在 [B,S] 内 → 0
      · dev > 0 → 收盘价高于预判卖出位(卖飞 / 上涨超预期)
      · dev < 0 → 收盘价低于预判买入位(比预判更便宜 / 走势弱于预期)
      · dev = 0 → 收盘落在预判买卖区间内(预判准确)
    """
    if dev is None:
        # v185: 区分「没填目标价」与「当日还没收盘/无行情」——后者填了价也算不出来
        if not has_close:
            return "当日尚无收盘行情,暂无法评估偏离"
        return "未设置目标买入/卖出价,无法评估偏离"
    abs_d = abs(dev)
    if dev == 0 or abs_d <= 0.005:
        return "符合预判,收盘落在预判买卖区间内"
    if trend == "看涨":
        if dev > 0:
            return "收盘高于预判卖出位,上涨超预期,留意止盈窗口"
        return "收盘低于预判买入位,走势弱于预期,观察是否止跌"
    if trend == "看跌":
        if dev < 0:
            return "收盘低于预判买入位,下行符合或强于预期"
        return "收盘高于预判卖出位,未如预期走弱,需重新评估逻辑"
    if trend == "风险":
        if abs_d <= 0.05:
            return "已按风控要求处置,波动可控"
        return "已按风控要求处置,关注进一步信号"
    # 趋势"-"或未设置
    if dev > 0:
        return "收盘高于预判卖出位,需结合其他判断"
    return "收盘低于预判买入位,需结合其他判断"


@router.get("/{code}/watch-log")
def watch_log(code: str, limit: int = Query(60, ge=1, le=500),
              db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """v143: 盯盘日志——按 trade_date desc 聚合,每交易日取当日最后一条 log 作为复盘输入,
    结合 daily_quotes(open/close/分时均价)计算偏离度与复盘原因。

    v146:每行的偏离原因复盘可被用户编辑,join day_view_recap 取最新用户编辑内容。

    偏离度算法口径（v185「买卖区间口径」,前端表头 ? 说明同步）:
        参考价 = 收盘低于买入预判时取 目标买入价 B;高于卖出预判时取 目标卖出价 S;落在 [B,S] 内 → 偏离 0
        deviation = (收盘价 C − 参考价) / 参考价
        正值(dev > 0):收盘高于预判卖出位 → 卖飞/上涨超预期
        负值(dev < 0):收盘低于预判买入位 → 比预判更便宜/走势弱于预期
        dev = 0     :收盘落在预判买卖区间内 → 预判准确
        买卖两端都为空:回退旧 target_price 视作卖出端;仍为空 → deviation=None,
                     reason=「未设置目标买入/卖出价,无法评估偏离」
    分时均价:amount(元)/(volume*100),volume 单位=手;若 amount=0 用 volume*close*100 兜底
    """
    name = _name(code, db)
    # 1) 拉所有 log(按 trade_date desc, operated_at desc)
    logs = (db.query(DayViewLog)
            .filter(DayViewLog.user_id == user.id, DayViewLog.code == code)
            .order_by(DayViewLog.trade_date.desc(), DayViewLog.operated_at.desc())
            .all())
    if not logs:
        return WatchLogOut(code=code, name=name, items=[]).model_dump()

    # 2) 每交易日保留最后一条
    last_by_date: dict[str, DayViewLog] = {}
    for r in logs:
        if r.trade_date not in last_by_date:
            last_by_date[r.trade_date] = r

    dates = list(last_by_date.keys())[:limit]
    # 3) 一次性取这些日期的日线(收盘/开盘/均价=amount/(volume*100))
    quotes = {}
    if dates:
        qrows = (db.query(DailyQuote)
                 .filter(DailyQuote.code == code, DailyQuote.date.in_(dates))
                 .all())
        for q in qrows:
            quotes[q.date] = q

    # 3.5) v146:批量取这些日期的复盘编辑(user 隔离,只存最新一份)
    recaps_by_date: dict[str, DayViewRecap] = {}
    if dates:
        rrows = (db.query(DayViewRecap)
                 .filter(DayViewRecap.user_id == user.id, DayViewRecap.code == code,
                         DayViewRecap.trade_date.in_(dates))
                 .all())
        for r in rrows:
            recaps_by_date[r.trade_date] = r

    items: list[WatchLogItem] = []
    for d in dates:
        lg = last_by_date[d]
        q = quotes.get(d)
        open_p = q.open if q and q.open else None
        close_p = q.close if q and q.close else None
        # v185: 当日最高/最低(盯盘日志收盘价后追加两列,用于判断预判价位当天是否曾被触及)
        high_p = q.high if q and q.high else None
        low_p = q.low if q and q.low else None
        intraday_avg = None
        # v146:amount 单位=元、volume 单位=手(1手=100股)→均价=元/股 = amount/(volume*100)
        #   兜底:若 amount=0(腾讯日线无成交额),用 volume*close*100 估算(与 data_fetcher 一致),
        #   至少保证分时均价有值,不显示 null
        if q and q.volume and q.volume > 0 and q.close:
            try:
                amt = q.amount if (q.amount and q.amount > 0) else (q.volume * q.close * 100.0)
                intraday_avg = round(amt / (q.volume * 100.0), 3)
            except Exception:
                intraday_avg = None
        # v185: 偏离度改为「买卖区间口径」——收盘价 C 相对用户预判区间 [买入价 B, 卖出价 S] 的偏离
        #   C 落在 [B, S] 内 → 0.00%(预判准确,收盘就在你预判的买卖区间里)
        #   C < B(收盘低于买入价) → (C-B)/B 为负: 比预判还能更低买入,预判偏乐观
        #   C > S(收盘高于卖出价) → (C-S)/S 为正: 比预判卖得更高(卖飞),预判偏保守
        #   只填一端 → 以该端为参考;两端都没填 → 回退旧的单一 target_price(视作卖出端)兼容历史数据
        # v186: 偏离算法抽成 _calc_deviation,与「复盘管理」页共用同一份实现
        deviation, deviation_pct = _calc_deviation(lg, close_p)
        # v146:用户编辑的复盘优先;否则用模板话术
        rec = recaps_by_date.get(d)
        recap_text = (rec.recap or "") if rec else ""
        recap_user = (rec.operator or "") if rec else ""
        recap_at = (rec.updated_at or "") if rec else ""
        items.append(WatchLogItem(
            trade_date=d,
            open=open_p, close=close_p, intraday_avg=intraday_avg,
            high=high_p, low=low_p,                      # v185: 最高 / 最低
            trend=lg.trend or "-",
            target_price=lg.target_price,                # 旧单值(兼容)
            target_buy=lg.target_buy,                    # v185: 目标买入
            target_sell=lg.target_sell,                  # v185: 目标卖出
            target_note=lg.target_note or "",
            deviation=deviation, deviation_pct=deviation_pct,
            deviation_reason=_deviation_reason(lg.trend or "-", deviation, has_close=bool(close_p)),
            recap=recap_text,
            recap_user=recap_user,
            recap_updated_at=recap_at,
            # v178: 录入时刻的快照
            composite_score=lg.composite_score,
            reference_text=lg.reference_text or "",
            reference_metrics_json=lg.reference_metrics_json or "",
        ))
    return WatchLogOut(code=code, name=name, items=items).model_dump()


@router.post("/{code}/watch-log/{trade_date}/recap")
def save_watch_log_recap(code: str, trade_date: str, body: RecapIn,
                          db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    """v146:upsert 单条偏离原因复盘，按 (user, code, trade_date) 唯一覆盖,更新 updated_at。
    便于事后按 (user, trade_date range) 聚合做月度预判-复盘准确率统计。
    """
    if not _CODE_RE.match(code or ""):
        raise HTTPException(status_code=400, detail="代码格式必须为 6 位数字")
    if not _RE_DATE.match(trade_date or ""):
        raise HTTPException(status_code=400, detail="交易日格式必须为 YYYY-MM-DD")
    # 允许空字符串(=清除,降级回模板话术),但限制最大长度防滥用
    recap_text = (body.recap or "").strip()
    if len(recap_text) > 500:
        raise HTTPException(status_code=400, detail="复盘内容不超过 500 字")
    row = (db.query(DayViewRecap)
           .filter(DayViewRecap.user_id == user.id, DayViewRecap.code == code,
                   DayViewRecap.trade_date == trade_date)
           .first())
    if row:
        row.recap = recap_text
        row.operator = user.username
        row.operator_id = user.id
        row.updated_at = _now()
    else:
        row = DayViewRecap(
            user_id=user.id, code=code, trade_date=trade_date,
            recap=recap_text,
            operator=user.username, operator_id=user.id,
            updated_at=_now(),
        )
        db.add(row)
    db.commit()
    return {"ok": True, "code": code, "trade_date": trade_date,
            "recap": recap_text, "user": user.username,
            "updated_at": row.updated_at}


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
    db.query(TrackedPoolTag).filter(TrackedPoolTag.tag_id == tid,
                                      TrackedPoolTag.user_id == user.id).delete(synchronize_session=False)
    db.delete(t)
    db.commit()
    return {"ok": True}


# ==================== v186: 盘前预判 / 复盘管理 聚合接口 ====================
# 背景：池内有 120+ 只票，若沿用单只接口(day-view-log / watch-log)按只轮询会退化成
#       N×3 次请求，页面必然卡死。故这两个页面改为「一次分页 + 固定几次批量查询」。
# 设计约束（长期扩展性）：
#   1) 查询数恒定：无论多少只股票，都是 log / quote / recap 三次批量查询，不随 N 增长；
#   2) 与列表页共用 _pool_rows_filtered，避免筛选/去重口径在三处漂移；
#   3) 不调用 get_pool_track 实时富化：盘前/盘后场景实时价无意义且耗时数秒，
#      价格类字段一律取 daily_quotes 日线，保证秒级响应；
#   4) 排序在「全量组装后、分页前」执行，保证翻页时全局有序（与前端列表一致）。

def _pool_rows_filtered(db, user_id: int, q: str = "", tag: str = "") -> list[TrackedPool]:
    """用户的可投池行（与列表页/导出同一口径）：q 模糊 + tag 过滤 + 同代码去重。"""
    rows = _user_pool_q(db, user_id).order_by(TrackedPool.id).all()
    q = (q or "").strip()
    if q:
        rows = [p for p in rows if q in p.code or q in (_name(p.code, db) or "")]
    tag_ids: list[int] = []
    tag = (tag or "").strip()
    if tag:
        try:
            tag_ids = [int(x) for x in tag.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="标签参数必须是数字 ID")
    seen: dict[str, TrackedPool] = {}
    for p in rows:
        if tag_ids and not {t.id for t in p.tags}.intersection(tag_ids):
            continue
        cur = seen.get(p.code)
        # 同代码多行(如归档+持仓)时保留持仓量最大的那行
        if cur is None or (p.position_qty and (not cur.position_qty or p.position_qty > cur.position_qty)):
            seen[p.code] = p
    return list(seen.values())


def _last_logs_of_date(db, user_id: int, codes: list[str], trade_date: str) -> dict[str, DayViewLog]:
    """指定交易日每只代码「最后一条」日初判断（按 id desc 取首条）。"""
    if not codes:
        return {}
    rows = (db.query(DayViewLog)
            .filter(DayViewLog.user_id == user_id,
                    DayViewLog.code.in_(codes),
                    DayViewLog.trade_date == trade_date)
            .order_by(DayViewLog.id.desc())
            .all())
    out: dict[str, DayViewLog] = {}
    for r in rows:
        out.setdefault(r.code, r)
    return out


def _log_counts_of_date(db, user_id: int, codes: list[str], trade_date: str) -> dict[str, int]:
    """指定交易日每只代码录入了几条（同日多次修改会累加）。"""
    if not codes:
        return {}
    rows = (db.query(DayViewLog.code, func.count(DayViewLog.id))
            .filter(DayViewLog.user_id == user_id,
                    DayViewLog.code.in_(codes),
                    DayViewLog.trade_date == trade_date)
            .group_by(DayViewLog.code).all())
    return {c: int(n) for c, n in rows}


def _latest_quotes(db, codes: list[str], on_or_before: str = "") -> dict[str, DailyQuote]:
    """每只代码取 on_or_before(含)之前最近一根日线；留空则取各自最新一根。
    用 group_by + max(date) 子查询一次拿全，避免每只拉全量历史。"""
    if not codes:
        return {}
    sub = db.query(DailyQuote.code.label("code"), func.max(DailyQuote.date).label("mdate"))
    sub = sub.filter(DailyQuote.code.in_(codes))
    if on_or_before:
        sub = sub.filter(DailyQuote.date <= on_or_before)
    sub = sub.group_by(DailyQuote.code).subquery()
    rows = (db.query(DailyQuote)
            .join(sub, and_(DailyQuote.code == sub.c.code, DailyQuote.date == sub.c.mdate))
            .all())
    return {r.code: r for r in rows}


def _quotes_on(db, codes: list[str], trade_date: str) -> dict[str, DailyQuote]:
    """精确取指定交易日的日线（复盘页：该日无行情则返回空）。"""
    if not codes:
        return {}
    rows = (db.query(DailyQuote)
            .filter(DailyQuote.code.in_(codes), DailyQuote.date == trade_date)
            .all())
    return {r.code: r for r in rows}


def _recaps_on(db, user_id: int, codes: list[str], trade_date: str) -> dict[str, DayViewRecap]:
    if not codes:
        return {}
    rows = (db.query(DayViewRecap)
            .filter(DayViewRecap.user_id == user_id,
                    DayViewRecap.code.in_(codes),
                    DayViewRecap.trade_date == trade_date)
            .all())
    return {r.code: r for r in rows}


def _recent_trade_dates(db, user_id: int, limit: int = 30) -> list[str]:
    """交易日下拉可选值：全市场有日线的日期 ∪ 该用户录入过日初判断的日期，倒序取最近 N 个。"""
    ds: set[str] = set()
    for (d,) in db.query(DailyQuote.date).distinct().order_by(DailyQuote.date.desc()).limit(limit).all():
        ds.add(d)
    for (d,) in (db.query(DayViewLog.trade_date)
                 .filter(DayViewLog.user_id == user_id)
                 .distinct().order_by(DayViewLog.trade_date.desc()).limit(limit).all()):
        ds.add(d)
    today = _today_cn()
    ds.add(today)
    return sorted(ds, reverse=True)[:limit]


def _sort_items(items: list[dict], key: str, desc: bool) -> list[dict]:
    """通用排序：空值恒排最后；数字比数字，其余按字符串。"""
    if not key:
        return items
    def sv(v):
        if v is None or v == "":
            return (1, 0.0, "")
        if isinstance(v, bool):
            return (0, float(v), "")
        if isinstance(v, (int, float)):
            return (0, float(v), "")
        return (0, 0.0, str(v))
    items.sort(key=lambda it: sv(it.get(key)), reverse=desc)
    return items


# ===== v186: 盘前预判 / 复盘管理 共用的行组装（列表接口与导出接口同一口径，避免两套算法）=====
def _build_pretrade_items(db, user, q, tag, trade_date) -> list[dict]:
    """组装盘前预判全量行（不分页，导出直接用）。"""
    rows = _pool_rows_filtered(db, user.id, q, tag)
    codes = [p.code for p in rows]
    logs = _last_logs_of_date(db, user.id, codes, trade_date)
    counts = _log_counts_of_date(db, user.id, codes, trade_date)
    quotes = _latest_quotes(db, codes, trade_date)
    items: list[dict] = []
    for p in rows:
        it = _pool_base(p, db)
        lg = logs.get(p.code)
        qt = quotes.get(p.code)
        close_p = qt.close if qt and qt.close else None
        pre_close = qt.pre_close if qt and qt.pre_close else None
        it.update({
            "trade_date": trade_date,
            "log_count": counts.get(p.code, 0),
            "last_date": qt.date if qt else "",
            "last_close": close_p,
            "last_change_pct": (round((close_p - pre_close) / pre_close * 100, 2)
                                if (close_p and pre_close) else None),
            "trend": (lg.trend or "-") if lg else "-",
            "target_buy": lg.target_buy if lg else None,
            "target_sell": lg.target_sell if lg else None,
            "target_note": (lg.target_note or "") if lg else "",
            "composite_score": lg.composite_score if lg else None,
            "reference_text": (lg.reference_text or "") if lg else "",
            "operator": (lg.operator or "") if lg else "",
            "operated_at": (lg.operated_at or "") if lg else "",
        })
        items.append(it)
    return items


def _build_review_items(db, user, q, tag, trade_date) -> list[dict]:
    """组装复盘管理全量行（不分页，导出直接用）。"""
    rows = _pool_rows_filtered(db, user.id, q, tag)
    codes = [p.code for p in rows]
    logs = _last_logs_of_date(db, user.id, codes, trade_date)
    counts = _log_counts_of_date(db, user.id, codes, trade_date)
    quotes = _quotes_on(db, codes, trade_date)
    recaps = _recaps_on(db, user.id, codes, trade_date)
    items: list[dict] = []
    for p in rows:
        it = _pool_base(p, db)
        lg = logs.get(p.code)
        qt = quotes.get(p.code)
        rec = recaps.get(p.code)
        open_p = qt.open if qt and qt.open else None
        close_p = qt.close if qt and qt.close else None
        high_p = qt.high if qt and qt.high else None
        low_p = qt.low if qt and qt.low else None
        pre_close = qt.pre_close if qt and qt.pre_close else None
        intraday_avg = None
        if qt and qt.volume and qt.volume > 0 and close_p:
            try:
                amt = qt.amount if (qt.amount and qt.amount > 0) else (qt.volume * close_p * 100.0)
                intraday_avg = round(amt / (qt.volume * 100.0), 3)
            except Exception:
                intraday_avg = None
        dev = dev_pct = None
        if lg:
            dev, dev_pct = _calc_deviation(lg, close_p)
        it.update({
            "trade_date": trade_date,
            "log_count": counts.get(p.code, 0),
            "open": open_p, "close": close_p, "high": high_p, "low": low_p,
            "intraday_avg": intraday_avg,
            "change_pct": (round((close_p - pre_close) / pre_close * 100, 2)
                           if (close_p and pre_close) else None),
            "trend": (lg.trend or "-") if lg else "-",
            "target_price": lg.target_price if lg else None,
            "target_buy": lg.target_buy if lg else None,
            "target_sell": lg.target_sell if lg else None,
            "target_note": (lg.target_note or "") if lg else "",
            "composite_score": lg.composite_score if lg else None,
            "reference_text": (lg.reference_text or "") if lg else "",
            "deviation": dev, "deviation_pct": dev_pct,
            "deviation_reason": (_deviation_reason(lg.trend or "-", dev, has_close=bool(close_p))
                                 if lg else "当日未录入日初预判"),
            "recap": (rec.recap or "") if rec else "",
            "recap_user": (rec.operator or "") if rec else "",
            "recap_updated_at": (rec.updated_at or "") if rec else "",
        })
        items.append(it)
    return items


# v186: 两个新页面的导出列映射（key → (Excel表头, 取值函数)），与前端列 key 一一对应
_PRETRADE_EXP_COLS: dict[str, tuple[str, object]] = {
    "code_name":       ("代码/名称",   lambda o: o.get("name") or ""),
    "industry":        ("行业",        lambda o: o.get("industry") or ""),
    "last_close":      ("参考价(最近收盘)", lambda o: o.get("last_close")),
    "last_change_pct": ("参考涨跌幅%",  lambda o: o.get("last_change_pct")),
    "trend":           ("涨跌趋势",    lambda o: o.get("trend") or ""),
    "target_buy":      ("目标买入",    lambda o: o.get("target_buy")),
    "target_sell":     ("目标卖出",    lambda o: o.get("target_sell")),
    "target_note":     ("目标依据",    lambda o: o.get("target_note") or ""),
    "composite_score": ("评分快照",    lambda o: o.get("composite_score")),
    "reference_text":  ("参考信息",    lambda o: o.get("reference_text") or ""),
    "log_count":       ("当日次数",    lambda o: o.get("log_count") or 0),
    "operated_at":     ("最近操作",    lambda o: o.get("operated_at") or ""),
}
_REVIEW_EXP_COLS: dict[str, tuple[str, object]] = {
    "code_name":     ("代码/名称",   lambda o: o.get("name") or ""),
    "industry":      ("行业",        lambda o: o.get("industry") or ""),
    "trend":         ("日初趋势",    lambda o: o.get("trend") or ""),
    "target_buy":    ("目标买入",    lambda o: o.get("target_buy")),
    "target_sell":   ("目标卖出",    lambda o: o.get("target_sell")),
    "open":          ("开盘",        lambda o: o.get("open")),
    "close":         ("收盘",        lambda o: o.get("close")),
    "high":          ("最高",        lambda o: o.get("high")),
    "low":           ("最低",        lambda o: o.get("low")),
    "change_pct":    ("涨跌幅%",     lambda o: o.get("change_pct")),
    "intraday_avg":  ("分时均价",    lambda o: o.get("intraday_avg")),
    "deviation_pct": ("偏离度%",     lambda o: o.get("deviation_pct")),
    "target_note":   ("目标依据",    lambda o: o.get("target_note") or ""),
    "recap":         ("复盘思考",    lambda o: o.get("recap") or ""),
}


def _export_pretrade_or_review(view, q, tag, cols, sort, sort_dir, trade_date, db, user):
    """v186: 盘前预判 / 复盘管理 导出（与各自页面同口径：同筛选、同列、同行序）。"""
    is_review = (view == "review")
    d = (trade_date or "").strip()
    if is_review:
        dates = _recent_trade_dates(db, user.id)
        if d and _RE_DATE.match(d):
            td = d
        else:
            today = _today_cn()
            td = today if (today in dates or not dates) else dates[0]
        items = _build_review_items(db, user, q, tag, td)
        colmap, title = _REVIEW_EXP_COLS, "复盘管理"
    else:
        td = d if (d and _RE_DATE.match(d)) else _today_cn()
        items = _build_pretrade_items(db, user, q, tag, td)
        colmap, title = _PRETRADE_EXP_COLS, "盘前预判"

    _sort_items(items, (sort or "").strip(), (sort_dir or "asc").lower() == "desc")

    # 按前端传来的可见列 key 与顺序出列；未识别的 key 忽略；无 cols 则输出全部
    keys = [k.strip() for k in (cols or "").split(",") if k.strip()]
    keys = [k for k in keys if k in colmap]
    if not keys:
        keys = list(colmap.keys())
    headers = [colmap[k][0] for k in keys]

    # 与可投池导出保持同一套写法（pandas + openpyxl），避免引入额外依赖
    import pandas as pd
    df = pd.DataFrame(
        [[("" if colmap[k][1](it) is None else colmap[k][1](it)) for k in keys] for it in items],
        columns=headers,
    )
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    fname = f"{title}_{td}.xlsx"
    encoded = quote(fname)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


def _pool_base(p: TrackedPool, db) -> dict:
    """一行里与场景无关的公共基本信息（两个页面共用，保证列口径一致）。"""
    return {
        "code": p.code,
        "name": _name(p.code, db) or p.code,
        "industry": _industry(p.code, db),
        "note": p.note or "",
        "cost_price": p.cost_price,
        "position_qty": p.position_qty,
        "scheme_type": p.scheme_type or "",
        "status": p.status or "",
        "tags": [{"id": t.id, "name": t.name, "color": t.color} for t in p.tags],
    }


def _calc_deviation(lg: DayViewLog, close_p):
    """v185「买卖区间口径」偏离度抽成公共函数（盯盘日志 / 复盘管理共用同一算法）。

    参考价 = 收盘低于买入预判取买入价 B；高于卖出预判取卖出价 S；落在 [B,S] 内 → 0
    deviation = (收盘价 C − 参考价) / 参考价
    返回 (deviation, deviation_pct)，两者可能为 None（无收盘价或没填目标价）。
    """
    buy_p = lg.target_buy if (lg.target_buy and lg.target_buy > 0) else None
    sell_p = lg.target_sell if (lg.target_sell and lg.target_sell > 0) else None
    if buy_p is None and sell_p is None:
        # 历史数据兼容：旧的单值 target_price 视作卖出端
        sell_p = lg.target_price if (lg.target_price and lg.target_price > 0) else None
    if not close_p or (not buy_p and not sell_p):
        return None, None
    if buy_p and sell_p:
        if close_p < buy_p:
            ref = buy_p
        elif close_p > sell_p:
            ref = sell_p
        else:
            return 0.0, 0.0        # 落在预判区间内 → 预判准确
    else:
        ref = buy_p or sell_p
    dev = round((close_p - ref) / ref, 4)
    return dev, round(dev * 100, 2)


@router.get("/pretrade")
def list_pretrade(
    date: str = Query("", description="交易日 YYYY-MM-DD；留空=中国时区今日"),
    trade_date_q: str = Query("", alias="trade_date", description="同 date(前端参数名)"),
    q: str = Query("", description="证券代码或名称模糊查询"),
    tag: str = Query("", description="按标签 ID 筛选(多个用逗号分隔)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    sort: str = Query("", description="排序列 key；留空=按加池顺序"),
    sort_dir: str = Query("asc", description="asc|desc"),
    db: SessionLocal = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v186 盘前预判：可投池基本信息 + 指定交易日的日初判断，一次请求拿全。

    每行 = 一只股票。日初判断取该交易日【最后一条】记录（同日可多次修改，append-only），
    未录入过则该组字段为空，前端行内填写后调用 POST /pool/{code}/day-view-log 落库。
    另附 last_close/last_change_pct 作为「填目标价时的参考价」（取 <= 该日最近一根日线）。
    """
    _d = (date or trade_date_q or "").strip()
    trade_date = _d if (_d and _RE_DATE.match(_d)) else _today_cn()
    items = _build_pretrade_items(db, user, q, tag, trade_date)

    total_before = len(items)
    _sort_items(items, (sort or "").strip(), (sort_dir or "asc").lower() == "desc")
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "total": total_before, "page": page, "page_size": page_size,
        "trade_date": trade_date,
        "available_dates": _recent_trade_dates(db, user.id),
    }


@router.get("/review")
def list_review(
    date: str = Query("", description="交易日 YYYY-MM-DD；留空=最近有行情的交易日"),
    trade_date_q: str = Query("", alias="trade_date", description="同 date(前端参数名)"),
    q: str = Query("", description="证券代码或名称模糊查询"),
    tag: str = Query("", description="按标签 ID 筛选(多个用逗号分隔)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    sort: str = Query("", description="排序列 key；留空=按加池顺序"),
    sort_dir: str = Query("asc", description="asc|desc"),
    db: SessionLocal = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v186 复盘管理：某交易日下，每只股票的「日初预判 vs 当日收盘」对比 + 复盘输入。

    每行 = 一只股票 × 一个交易日。字段覆盖原「盯盘日志弹窗」全部内容（开/收/高/低/
    分时均价/综合评分/预判参考信息/趋势/目标买卖/依据/偏离度/偏离原因复盘），
    行内编辑复盘后调用 POST /pool/{code}/watch-log/{trade_date}/recap 落库。
    """
    dates = _recent_trade_dates(db, user.id)
    _d = (date or trade_date_q or "").strip()
    if _d and _RE_DATE.match(_d):
        trade_date = _d
    else:
        # 默认「当天」；若当天尚无行情则回落到最近一个交易日，避免默认空表
        today = _today_cn()
        trade_date = today
        if today not in dates and dates:
            trade_date = dates[0]

    items = _build_review_items(db, user, q, tag, trade_date)

    total_before = len(items)
    _sort_items(items, (sort or "").strip(), (sort_dir or "asc").lower() == "desc")
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "total": total_before, "page": page, "page_size": page_size,
        "trade_date": trade_date,
        "available_dates": dates or [trade_date],
    }
