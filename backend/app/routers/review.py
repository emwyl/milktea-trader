"""定期复盘报告路由（0911批次4）。

## 范围
- `GET /status`  ：采集器诊断 + 当日时点「已采/漏采」时间线（Phase A）
- `GET /daily`   ：报表 1 · 个股日度复盘明细（OHLC + 各时点静态综合评分）
- `GET /intraday`：单股单日分时序列（供展开画趋势图）
- `GET /export`  ：报表 1 导出 xlsx（openpyxl，表头冻结 + 时点列条件色）
- 报表 2（回测统计）在 Phase C 补充。

## 数据来源与口径
- **时点评分**：`pool_score_snapshot`（Phase A 起逐日落库）。**不做历史回填** —— 早于首次落库的
  日期没有数据，前端会明确提示，绝不编造。
- **日内 OHLC**：`daily_quotes`（长期落库，历史全有）。若某日该表还没有行（如当日收盘前），
  用该日最后一个时点的快照价兜底，并在 `ohlc_src` 标注来源，避免用户分不清数字从哪来。
- **分时趋势**：新浪 1 分钟 K，**仅近 ~7 个交易日**可回溯（上游 datalen 硬上限 1800 根），
  更早的日期 `available=false` + 说明原因。
- 行情异常（快照 `score_raw` 为 NULL）的样本**不显示成 0 分** ——
  0 分与「无数据」在复盘里含义完全不同。

## 为什么导出不用 matplotlib 内嵌 PNG
设计方案 §6.3 曾计划用 matplotlib 把分时图内嵌成 PNG。实施时改为「分时明细另存一个 sheet」：
- matplotlib 依赖 numpy 等，体积大，而本项目为规避沙箱 OOM 连 pandas 都做懒加载；
- 内嵌图片会让 xlsx 体积暴涨（100+ 行 × 每行一张图）；
- 长表格式的分时数据用户可在 Excel 里自行作图，且便于二次计算，信息量不丢。
"""
from __future__ import annotations

import datetime as dt
import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import DailyQuote, PoolScoreSnapshot, Stock, User
from app.services import score_snapshot as ss
from app.services.data_fetcher import _fetch_sina_minute_ohlc

router = APIRouter(prefix="/api/review/report", tags=["review-report"])

# 时点展示顺序（'1345' 是监控时点，被自动并入采集清单）
_SLOT_ORDER = ["0925", "0930", "0945", "1100", "1130", "1315", "1330", "1345", "1400", "1445", "1500"]
_ISO = "%Y-%m-%d"
_MEDIA_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ============ 工具 ============

def _cn_today() -> dt.date:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s.strip()[:10], _ISO).date()
    except Exception:
        return None


def _r(v, nd: int = 2):
    """安全四舍五入；None 保持 None（**不回落 0**，避免把「无数据」显示成 0）。"""
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _slot_label(slot: str) -> str:
    return f"{slot[:2]}:{slot[2:]}" if len(slot) == 4 else slot


def _grade(v) -> str:
    """档位阈值（A≥80/B≥65/C≥50）是按**原始分**标定的，因此只能对同一口径的分值判档。

    ⚠ 不能拿 `grade` 字段直接展示：落库的 `grade` 由**调节后分**（= 原始分 × 环境系数）算出，
    而崩坏市系数低至 0.488~0.75，会让 A 档在数学上不可达（进攻风格需原始分 ≥163.9），
    整池被系统性压成 D。复盘要用「可比」的口径判档，故这里按传入分值现算。
    """
    if v is None:
        return ""
    return "A" if v >= 80 else "B" if v >= 65 else "C" if v >= 50 else "D"


# ============ 采集器诊断（Phase A） ============

@router.get("/status")
def report_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """采集器状态 + 今日各时点落库情况 + 最近有数据的日期。

    存在的意义：本功能的数据是**从部署当天起逐日积累**的（不做历史回填）。
    若没有可见的落库确认手段，用户无法判断采集器到底有没有在工作 ——
    这类「看不见的后台任务」最容易静默失效。
    """
    today = _cn_today().strftime(_ISO)
    rows = (db.query(PoolScoreSnapshot.slot, func.count(PoolScoreSnapshot.id),
                     func.sum(PoolScoreSnapshot.in_monitor))
            .filter(PoolScoreSnapshot.trade_date == today)
            .group_by(PoolScoreSnapshot.slot).all())
    by_slot = {str(s): {"rows": int(c or 0), "monitored": int(m or 0)} for s, c, m in rows}

    recent = (db.query(PoolScoreSnapshot.trade_date, func.count(PoolScoreSnapshot.id))
              .group_by(PoolScoreSnapshot.trade_date)
              .order_by(PoolScoreSnapshot.trade_date.desc()).limit(7).all())
    total = db.query(func.count(PoolScoreSnapshot.id)).scalar() or 0
    with_score = (db.query(func.count(PoolScoreSnapshot.id))
                  .filter(PoolScoreSnapshot.score_raw.isnot(None)).scalar()) or 0

    now_cn = dt.datetime.utcnow() + dt.timedelta(hours=8)
    cur = now_cn.hour * 60 + now_cn.minute
    slots = ss.effective_slots()
    timeline = []
    for s in slots:
        got = by_slot.get(s)
        if got:
            state = "done"
        elif cur - ss._slot_minutes(s) > ss._TOL_MIN:
            state = "missed" if cur > ss._slot_minutes(s) else "pending"
        else:
            state = "pending"
        item = {"slot": s, "label": _slot_label(s), "state": state}
        if got:
            item.update({"rows": got["rows"], "monitored": got["monitored"]})
        timeline.append(item)

    return {
        "collector": ss.status(),
        "today": {"date": today, "timeline": timeline,
                  "slots_done": sum(1 for t in timeline if t["state"] == "done"),
                  "slots_total": len(slots)},
        "recent": [{"date": d, "rows": int(c or 0)} for d, c in recent],
        "storage": {"total_rows": int(total), "rows_with_score": int(with_score)},
        "note": ("时点快照自本功能部署起逐日积累，不做历史回填；报表日期早于首次落库日将无数据。"
                 "语义口径：市场环境崩坏或弱势题材逆势涨时记为「观望」(只守不攻)，"
                 "其余按档位动态阈值判「看涨/看跌」。"),
    }


# ============ 报表 1 · 个股日度明细 ============

def _build_daily(user_id: int, db: Session, start: dt.date, end: dt.date,
                 codes: list[str] | None, monitor_only: bool) -> dict:
    """组装报表 1 的数据（API 与导出共用，保证「所见即所得」）。"""
    s0, s1 = start.strftime(_ISO), end.strftime(_ISO)

    # —— 选择器用的「可选宇宙」：区间内出现过的全部标的，**不受当前 codes / monitor_only 干扰** ——
    #    否则用户一筛选，选择面板就跟着塌缩，出现「选了 A 之后 B 就从面板消失、加不回来」的死循环。
    #    同时带 monitored 标记，前端才能提供「仅监控池」一键快选。
    uni = (db.query(PoolScoreSnapshot.code,
                    func.max(PoolScoreSnapshot.in_monitor))
           .filter(PoolScoreSnapshot.user_id == user_id,
                   PoolScoreSnapshot.trade_date >= s0,
                   PoolScoreSnapshot.trade_date <= s1)
           .group_by(PoolScoreSnapshot.code).all())
    uni_codes = [c for c, _ in uni]
    uni_names = {c: (n or "") for c, n in
                 db.query(Stock.code, Stock.name).filter(Stock.code.in_(uni_codes or [""])).all()}
    universe = sorted(({"code": c, "name": uni_names.get(c, ""), "monitored": int(m or 0)}
                       for c, m in uni), key=lambda x: x["code"])

    q = (db.query(PoolScoreSnapshot)
         .filter(PoolScoreSnapshot.user_id == user_id,
                 PoolScoreSnapshot.trade_date >= s0,
                 PoolScoreSnapshot.trade_date <= s1))
    if codes:
        q = q.filter(PoolScoreSnapshot.code.in_(codes))
    if monitor_only:
        q = q.filter(PoolScoreSnapshot.in_monitor == 1)
    snaps = q.all()

    # 按 (code, date) 归组时点评分
    grouped: dict[tuple, dict] = {}
    for s in snaps:
        k = (s.code, s.trade_date)
        g = grouped.setdefault(k, {"in_monitor": 0, "slots": {}})
        g["in_monitor"] = max(g["in_monitor"], int(s.in_monitor or 0))
        raw = _r(s.score_raw, 1)
        adj = _r(s.score, 1)
        g["slots"][s.slot] = {
            "score": adj,          # 调节后分（= raw × 环境系数），与盘间监控页所见一致
            "raw": raw,            # 原始分（未乘环境系数）—— 跨日/跨风格可比，复盘用这个
            "grade": s.grade or "",        # 由调节后分判档（≠ 复盘口径，仅供追溯落库值）
            "grade_raw": _grade(raw),      # 由原始分判档（复盘口径）
            "semantic": s.semantic or "",
            "vetoed": int(s.vetoed or 0), "price": _r(s.price),
            "factor": _r(s.adj_factor, 3),  # 环境系数：让用户能自己从 raw 反推 adj
        }
    if not grouped:
        return {"rows": [], "slots": _SLOT_ORDER, "codes": universe,
                "meta": {"empty": True, "rows": 0, "codes": 0, "dates": 0,
                         "note": "快照自采集器上线当日起逐日积累，早于上线日的日期没有数据（按设计不做历史回填）。"
                                 "采集器会在每个交易日的 " +
                                 "、".join(_slot_label(s) for s in _SLOT_ORDER) +
                                 " 自动落库，之后本页即逐日有数据。"}}

    uniq_codes = sorted({c for c, _ in grouped})
    dates = sorted({d for _, d in grouped})

    # 日线 OHLC（历史全有）
    dq = (db.query(DailyQuote)
          .filter(DailyQuote.code.in_(uniq_codes),
                  DailyQuote.date >= s0, DailyQuote.date <= s1).all())
    qmap = {(r.code, r.date): r for r in dq}

    names = uni_names   # 已在上方随「可选宇宙」一次取回，无需重复查（uniq_codes ⊆ uni_codes）
    # 快照里的涨跌幅（用于昨日收盘兜底反推）
    chg = {}
    for s in snaps:
        if s.change_pct is not None:
            chg[(s.code, s.trade_date)] = s.change_pct

    rows: list[dict] = []
    for (code, date) in sorted(grouped.keys(), key=lambda k: (k[1], k[0]), reverse=True):
        g = grouped[(code, date)]
        bar = qmap.get((code, date))
        open_ = high = low = close = pre_close = turnover = volume = amount = None
        src = "snapshot"
        if bar:
            open_, high, low, close = _r(bar.open), _r(bar.high), _r(bar.low), _r(bar.close)
            pre_close = _r(bar.pre_close)
            turnover, volume, amount = _r(bar.turnover), _r(bar.volume), _r(bar.amount)
            src = "daily"

        # 兜底 1：日线还没有当日行 → 用该日最后一个有价的时点当收盘
        if close is None:
            for sl in reversed(_SLOT_ORDER):
                p = (g["slots"].get(sl) or {}).get("price")
                if p:
                    close = p
                    src = "snapshot"
                    break
        # 兜底 2：昨收仍缺 → 用「收盘 / (1 + 快照涨跌幅)」反推
        if not pre_close and close and (code, date) in chg:
            try:
                denom = 1 + chg[(code, date)] / 100.0
                if denom:
                    pre_close = round(close / denom, 3)
            except Exception:
                pre_close = None

        change_pct = amp = None
        if close is not None and pre_close:
            change_pct = round((close - pre_close) / pre_close * 100, 2)
            if high is not None and low is not None:
                amp = round((high - low) / pre_close * 100, 2)

        rows.append({
            "date": date, "code": code, "name": names.get(code, ""),
            "in_monitor": g["in_monitor"],
            "open": open_, "high": high, "low": low, "close": close, "pre_close": pre_close,
            "change_pct": change_pct, "amplitude_pct": amp,
            "volume": volume, "amount": amount, "turnover": turnover,
            "ohlc_src": src, "scores": g["slots"],
        })

    return {
        "rows": rows,
        "slots": _SLOT_ORDER,
        "codes": universe,
        "meta": {
            "rows": len(rows), "codes": len(uniq_codes), "dates": len(dates),
            "first_date": min(dates), "empty": False,
            "note": (f"共 {len(rows)} 行（{len(uniq_codes)} 只 × {len(dates)} 个交易日）。"
                     "时点评分来自落库快照，早于首次落库的日期没有数据（不做回填）。"),
        },
    }


@router.get("/daily")
def report_daily(
    start: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    codes: str | None = Query(None, description="股票代码，逗号分隔；缺省=全部有数据的标的"),
    monitor_only: int = Query(0, description="1=只看盘间监控池"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """报表 1 · 个股日度复盘明细。"""
    end_d = _parse_date(end) or _cn_today()
    start_d = _parse_date(start) or (end_d - dt.timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    code_list = [c.strip() for c in (codes or "").split(",") if c.strip()] or None
    out = _build_daily(user.id, db, start_d, end_d, code_list, bool(monitor_only))
    out["range"] = {"start": start_d.strftime(_ISO), "end": end_d.strftime(_ISO)}
    return out


# ============ 分时趋势 ============

@router.get("/intraday")
def report_intraday(
    code: str = Query(..., description="股票代码"),
    date: str = Query(..., description="交易日 YYYY-MM-DD"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单股单日分时序列（供展开画趋势图）。仅近 ~7 个交易日可回溯。"""
    code = (code or "").strip()
    d = _parse_date(date)
    if not code or not d:
        return {"code": code, "date": date, "available": False, "reason": "参数不完整"}

    rows = _fetch_sina_minute_ohlc(code)
    if not rows:
        return {"code": code, "date": d.strftime(_ISO), "available": False,
                "reason": "分时数据源暂时不可用，请稍后重试"}

    want = d.strftime(_ISO)
    pts = [r for r in rows if r["date"] == want]
    if not pts:
        avail = sorted({r["date"] for r in rows})
        return {"code": code, "date": want, "available": False,
                "window": {"from": avail[0], "to": avail[-1]} if avail else None,
                "reason": (f"超出分时可回溯窗口（上游仅提供最近约 7 个交易日："
                           f"{avail[0]} ~ {avail[-1]}）") if avail else "无分时数据"}

    bar = db.query(DailyQuote).filter(DailyQuote.code == code, DailyQuote.date == want).first()
    pre_close = _r(bar.pre_close) if bar else None
    if not pre_close:
        pre_close = _r(pts[0]["open"])   # 兜底：当日第一分钟开盘价作基准

    prices = [p["close"] for p in pts if p["close"]]
    return {
        "code": code, "date": want, "available": True,
        "pre_close": pre_close,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        # 前端画图只需这三列；均价用上游 ma_price5 近似（不足 5 根时为空，前端自行跳过）
        "points": [{"hm": p["hm"], "price": p["close"], "avg": p["ma_price5"],
                    "vol": p["vol_hand"]} for p in pts],
        "reason": "",
    }


# ============ 导出 xlsx ============

def _style_sheet(ws, sem_by_cell: dict) -> None:
    """表头冻结 + 时点列条件色（A 股配色：涨=红、跌=绿、观望=灰）。"""
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2563EB")
    for cell in ws[1]:
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "D2"           # 冻结表头 + 日期/代码/名称三列
    ws.row_dimensions[1].height = 30

    up_font = Font(color="DC2626", bold=True)      # 涨/看涨 = 红
    dn_font = Font(color="16A34A")                 # 跌 = 绿
    flat_font = Font(color="9CA3AF", italic=True)  # 观望/无数据 = 灰
    for idx, cell in enumerate(ws[1], start=1):
        if not isinstance(cell.value, str) or ":" not in cell.value:
            continue
        col = get_column_letter(idx)
        for r in range(2, ws.max_row + 1):
            c = ws[f"{col}{r}"]
            if c.value is None or c.value == "":
                c.font = flat_font
                continue
            # 条件色直接复用快照落库时的语义判定（同一口径），避免导出与页面两套标准
            sem = sem_by_cell.get(f"{col}{r}")
            if sem == "观望":
                c.font = flat_font
                continue
            try:
                v = float(c.value)
            except (TypeError, ValueError):
                continue
            c.font = up_font if sem == "看涨" else dn_font


@router.get("/export")
def report_export(
    kind: str = Query("daily", description="daily=报表1（报表2 在 Phase C）"),
    basis: str = Query("raw", description="raw=原始分（默认，跨日跨风格可比）；adj=环境调整后分（与盘间监控页一致）"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    codes: str | None = Query(None),
    monitor_only: int = Query(0),
    with_intraday: int = Query(0, description="1=额外导出「分时明细」sheet（长表，最多 20 个股票日）"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """报表 1 导出 xlsx：表头冻结、时点列条件色、附「口径说明」sheet + 另一口径对照 sheet。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter

    if kind != "daily":
        return {"ok": False, "detail": "报表 2 导出将在 Phase C 提供"}

    # 口径：sheet1 与页面所见保持一致（所见即所得）；另一口径另起一张 sheet 做对照，
    # 两套分值都不丢——复盘既要「可比」的原始分，也要「当时实际看到」的调整后分。
    basis = "adj" if str(basis).lower() in ("adj", "adjusted", "score") else "raw"
    key = "score" if basis == "adj" else "raw"
    alt_key = "raw" if basis == "adj" else "score"
    alt_label = "原始分" if basis == "adj" else "调整后分"

    end_d = _parse_date(end) or _cn_today()
    start_d = _parse_date(start) or (end_d - dt.timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    code_list = [c.strip() for c in (codes or "").split(",") if c.strip()] or None
    data = _build_daily(user.id, db, start_d, end_d, code_list, bool(monitor_only))
    rows, slots = data["rows"], data["slots"]

    def write_main(ws, score_key: str) -> dict:
        """把明细写进工作表；返回 {单元格: 语义} 供条件着色（页面与导出同一口径）。"""
        ws.append(["日期", "代码", "名称", "监控池", "开盘", "最高", "最低", "收盘", "昨收",
                   "涨跌幅%", "振幅%", "成交量(万手)", "成交额(万元)", "换手%", "数据源"] +
                  [_slot_label(s) for s in slots])
        sem: dict[str, str] = {}
        for r_i, row in enumerate(rows, start=2):
            ws.append([row["date"], row["code"], row["name"],
                       "是" if row["in_monitor"] else "否",
                       row["open"], row["high"], row["low"], row["close"], row["pre_close"],
                       row["change_pct"], row["amplitude_pct"],
                       _r(row["volume"], 2),
                       round(row["amount"] / 1e4, 0) if row["amount"] else None,
                       row["turnover"],
                       "日线" if row["ohlc_src"] == "daily" else "快照兜底"] +
                      [(row["scores"].get(s) or {}).get(score_key) for s in slots])
            for c_i, s in enumerate(slots, start=16):
                sem[f"{get_column_letter(c_i)}{r_i}"] = (row["scores"].get(s) or {}).get("semantic") or ""
        _style_sheet(ws, sem)
        for i, w in enumerate([11, 9, 12, 8, 8, 8, 8, 8, 8, 10, 8, 13, 14, 8, 10] + [8] * len(slots), start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        return sem

    wb = Workbook()
    ws = wb.active
    ws.title = "个股日度明细"
    write_main(ws, key)

    # 对照 sheet：同一批行、另一个口径的分值（列结构与 sheet1 完全一致，便于并排比对/公式引用）
    ws_alt = wb.create_sheet(f"{alt_label}对照")
    write_main(ws_alt, alt_key)

    # —— 口径说明 sheet：让用户知道每个数字怎么来的（复盘可信度的关键）——
    ws2 = wb.create_sheet("口径说明")
    for r in [
        ["项目", "说明"],
        ["数据区间", f"{start_d.strftime(_ISO)} ~ {end_d.strftime(_ISO)}"],
        ["行数", f"{len(rows)} 行（{'仅监控池' if monitor_only else '全池'}）"],
        ["时点评分来源", "后台采集器在固定时点落库的静态综合评分（pool_score_snapshot）"],
        ["采集时点", "、".join(_slot_label(s) for s in slots)],
        ["不做历史回填", "报表仅覆盖首次落库日之后的日期；更早日期无数据，空白≠0 分"],
        ["分数为空", "该时点未采集成功，或行情异常（行情异常写 NULL 而非 0，避免污染回测）"],
        ["「观望」含义", "市场环境崩坏、或弱势市+题材股逆势上涨 → 只守不攻，不给方向（既不算看涨也不算看跌）"],
        ["评分权重口径", "本表用后端默认权重口径，保证整段时间可比；前端列表若自定义过权重，数字可能略有差异"],
        ["时点分口径", f"sheet1「个股日度明细」为{'环境调整后分（与盘间监控页所见一致）' if basis == 'adj' else '原始分'}；"
                       f"另一口径已另存为「{alt_label}对照」sheet，两套分值都能取到"],
        ["原始分 vs 调整后分",
         "调整后分 = 原始分 × 环境系数（市场/板块/风格）。环境系数对当日所有个股近乎同乘一个数："
         "它不影响横截面排序，但会整体压低分值——崩坏市系数 0.488~0.862，"
         "会使「A≥80」在数学上不可达（如进攻风格需原始分≥163.9），整池被系统性压成 D 档。"],
        ["复盘建议用哪个", "做跨日/跨风格比较、看个股真实强弱 → 用「原始分」；"
                          "复现「当时实际看到的分数与方向」→ 用「调整后分」。"
                          "方向判定（看涨/观望/看跌）始终基于调整后分 + 市场状态，与盘间监控页同源。"],
        ["OHLC 来源", "「日线」=daily_quotes 已落库；「快照兜底」=该日最后一个时点的快照价（日线尚无当日行时）"],
        ["分时趋势图", "在页面内展开查看；仅近约 7 个交易日可回溯（上游限制），更早日期无分时数据"],
        ["免责声明", "本工具仅用于个人学习与研究，行情、评分、回测均为辅助参考，不构成投资建议。"],
    ]:
        ws2.append(r)
    for c in ("A1", "B1"):
        ws2[c].font = Font(bold=True, color="FFFFFF")
        ws2[c].fill = PatternFill("solid", fgColor="2563EB")
    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 95
    for row_cells in ws2.iter_rows(min_row=2):
        for c in row_cells:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    # —— 可选：分时明细（长表；上限 20 个股票日，避免工作簿过大）——
    if with_intraday and rows:
        ws3 = wb.create_sheet("分时明细")
        ws3.append(["日期", "代码", "时间", "价格", "均价", "成交量(手)"])
        wrote = 0
        for row in rows[:20]:
            pts = _fetch_sina_minute_ohlc(row["code"])
            if not pts:
                continue
            for p in (x for x in pts if x["date"] == row["date"]):
                ws3.append([row["date"], row["code"],
                            f"{p['hm'] // 100:02d}:{p['hm'] % 100:02d}",
                            p["close"], p["ma_price5"], p["vol_hand"]])
                wrote += 1
        for i, w in enumerate([11, 9, 8, 8, 8, 12], start=1):
            ws3.column_dimensions[get_column_letter(i)].width = w
        ws3.freeze_panes = "A2"
        if wrote == 0:
            ws3.append(["（无分时数据：超出可回溯窗口或数据源不可用）"])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"复盘明细_{start_d.strftime('%Y%m%d')}-{end_d.strftime('%Y%m%d')}.xlsx"
    encoded = quote(filename, safe="")   # 中文文件名必须 RFC5987 编码，否则 header latin-1 报错
    return StreamingResponse(
        buf, media_type=_MEDIA_XLSX,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )
