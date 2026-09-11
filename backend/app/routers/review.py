"""定期复盘报告路由（0911批次4）。

## 范围
- `GET /status`   ：采集器诊断 + 当日时点「已采/漏采」时间线（Phase A）
- `GET /daily`    ：报表 1 · 个股日度复盘明细（OHLC + 各时点静态综合评分）
- `GET /intraday` ：单股单日分时序列（供展开画趋势图）
- `GET /backtest` ：报表 2 · 趋势判断回测统计（15 个维度，含两套基准与 IC）
- `GET /export`   ：导出 xlsx（`kind=daily` 报表1 / `kind=backtest` 报表2）

文件体量说明：报表 1 与报表 2 的**聚合口径**都放在本文件（而不是拆到 services/），
原因是两者共用 `_r / _grade / _SLOT_ORDER` 等口径函数 —— 拆开后这些「同一个概念」的实现
会分家，正是本项目反复出问题的地方（格式化/计算函数必须前后端、跨报表共用一份）。

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
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
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


_INDEX_OPTIONS = [
    # 默认中证500：本池以中小盘短线票为主，用沪深300 会把「小盘跑赢大盘」算成模型功劳，
    # 反而削弱了「防伪胜率」的力度；中证500 更保守、更贴近池内构成。
    {"code": "sh000905", "label": "中证500"},
    {"code": "sh000852", "label": "中证1000"},
    {"code": "sh000300", "label": "沪深300"},
    {"code": "sh000001", "label": "上证指数"},
    {"code": "sz399006", "label": "创业板指"},
    {"code": "sh000688", "label": "科创50"},
]
_INDEX_LABEL = {o["code"]: o["label"] for o in _INDEX_OPTIONS}
_BENCH_POOL = "pool"          # 池内等权（横截面基准，默认）
_DEFAULT_BENCH = "sh000905"

_M_TIER_LABEL = {"healthy": "健康市", "weak": "弱势市", "collapse": "崩坏市", "": "未记录"}

# 评分分桶（验证 60 阈值是否处在「命中率单调递增」的正确位置）
_BUCKETS = [(None, 50.0, "<50"), (50.0, 60.0, "50-60"), (60.0, 70.0, "60-70"),
            (70.0, 80.0, "70-80"), (80.0, 90.0, "80-90"), (90.0, None, "≥90")]

_INDEX_MIN_CACHE: dict[str, tuple[float, dict]] = {}
_INDEX_MIN_TTL = 600.0        # 分钟线历史不会变，缓存 10 分钟足够


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
    kind: str = Query("daily", description="daily=报表1 个股日度明细；backtest=报表2 回测统计"),
    basis: str = Query("raw", description="raw=原始分（默认，跨日跨风格可比）；adj=环境调整后分（与盘间监控页一致）"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    codes: str | None = Query(None),
    monitor_only: int = Query(0),
    with_intraday: int = Query(0, description="1=额外导出「分时明细」sheet（长表，最多 20 个股票日）"),
    # —— 报表 2 专用参数（与 /backtest 同名同义）——
    slots: str | None = Query(None, description="报表2：监控时点，逗号分隔如 0945,1345"),
    threshold: float = Query(60, description="报表2：看涨阈值"),
    eps: float = Query(0.2, description="报表2：中性区间（百分点）"),
    bench: str = Query(_DEFAULT_BENCH, description="报表2：基准（pool 或指数代码）"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """导出 xlsx。报表 1：表头冻结 + 时点列条件色 + 另一口径对照 + 口径说明；
    报表 2：多 sheet（统计/分桶/分组透视/时间稳健性/语义分组/样本明细/口径说明）。"""
    if kind == "backtest":
        end_b = _parse_date(end) or _cn_today()
        start_b = _parse_date(start) or (end_b - dt.timedelta(days=30))
        if start_b > end_b:
            start_b, end_b = end_b, start_b
        slot_list = [s.strip() for s in (slots or "").split(",") if s.strip()] or list(
            ss._setting_list("review_monitor_slots", ss.MONITOR_SLOTS))
        bt = _build_backtest(user.id, db, start_b, end_b, slot_list,
                             float(threshold), abs(float(eps)),
                             "adj" if str(basis).lower() in ("adj", "adjusted", "score") else "raw",
                             (bench if bench in _INDEX_LABEL else _BENCH_POOL),
                             bool(monitor_only))
        if (bt.get("meta") or {}).get("empty"):
            # 宁可不给文件，也不给一份「看起来像报表的空表」——空态原因必须原样传达
            raise HTTPException(status_code=400,
                                detail=(bt.get("meta") or {}).get("note") or "区间内没有可回测的样本")
        buf = _bt_workbook(bt)
        filename = (f"回测统计_{start_b.strftime('%Y%m%d')}-{end_b.strftime('%Y%m%d')}.xlsx")
        encoded = quote(filename, safe="")
        return StreamingResponse(
            buf, media_type=_MEDIA_XLSX,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
        )

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter

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


# ============ 报表 2 · 趋势判断回测统计（Phase C） ============
#
# 目标：回答「按『评分 ≥ 阈值 → 看涨，否则看跌』这套规则做，历史上到底准不准」。
#
# ## 口径（对应设计方案 §7.1，两处必须做的修正）
# 1) **振幅无方向**，不能用来判涨跌 → 方向一律用**区间涨跌幅** `ret = (出场价 − 入场价) / 入场价`；
#    振幅只作波动维度（平均振幅 / 振幅捕获比）。
# 2) **必须做相对超额**：全市场普跌时模型全体看跌 → 看跌命中率虚高但毫无用处（正是 2026-09-11 的场景）。
#    因此除绝对方向外，另算两套基准的超额方向：
#      · 池内等权（横截面）→ 衡量**选股能力**，且不依赖任何外部数据，永远可用（默认基准）
#      · 选定指数（时序）  → 衡量**择时能力**，需指数分钟线（新浪可回溯约 8 个交易日）
#
# ## 为什么预测方向用「阈值」而不是落库的 semantic
# 落库 `semantic` 含「观望」（崩坏市 / 弱势题材逆势涨 → 不给方向），直接拿来算命中率会让分母忽大忽小、
# 无法回答「60 阈值到底行不行」这个 Phase D 要调参的问题。故：
#   · 主统计口径固定为阈值二分（每个样本都有方向，混淆矩阵/精确率/召回率才有定义）；
#   · 「观望」单独出一张**验证表**：若观望组平均收益确实≈0 且明显弱于看涨组，说明观望规则有价值。

# 常量（基准/分桶/环境标签）统一定义在文件头部，避免「函数默认参数在定义时求值」踩到未定义名。

def _pos(v):
    """正数才有效（价格为 0/负视为取数失败）。"""
    n = _r(v, 4)
    return n if (n is not None and n > 0) else None


def _exit_map(slots: list[str]) -> dict[str, str]:
    """入场时点 → 出场时点：下一个监控时点；最后一个 → 收盘。

    默认监控时点 09:45 / 13:45 → 区间「09:45→13:45」「13:45→收盘」，与设计方案 §7.1 一致。
    """
    order = sorted(set(slots), key=ss._slot_minutes)
    return {s: (order[i + 1] if i + 1 < len(order) else "CLOSE") for i, s in enumerate(order)}


def _index_slot_map(sym: str) -> dict:
    """指数分钟线 → {日期: {HHMM: 收盘价}}（带缓存）。取不到返回 {}，绝不编造。"""
    cached = _INDEX_MIN_CACHE.get(sym)
    now = time.time()
    if cached and (now - cached[0]) < _INDEX_MIN_TTL:
        return cached[1]
    out: dict[str, dict] = {}
    rows = _fetch_sina_minute_ohlc(sym, 1800, symbol=sym)
    for r in rows or []:
        p = _r(r.get("close"), 4)
        if p:
            out.setdefault(str(r.get("date")), {})[int(r["hm"])] = p
    if out:
        _INDEX_MIN_CACHE[sym] = (now, out)
    return out


def _index_price(series: dict, date: str, slot: str):
    """取指数在某时点的价格。允许 ±2 分钟容差：分钟线首根是 09:31（集合竞价不产生分钟线），
    因此 09:30 这类时点必然没有精确匹配。超出容差一律返回 None（不猜）。"""
    d = (series or {}).get(date)
    if not d:
        return None
    try:
        hm = int(slot)
    except (TypeError, ValueError):
        return None
    if hm in d:
        return d[hm]
    for delta in (1, -1, 2, -2):
        if hm + delta in d:
            return d[hm + delta]
    return None


def _exc_hit(pred: str, excess, eps: float):
    """超额方向是否命中；|超额| ≤ ε 视为无方向（返回 None，剔出命中率分母）。"""
    if excess is None:
        return None
    if excess > eps:
        return pred == "bull"
    if excess < -eps:
        return pred == "bear"
    return None


def _pearson(xs: list[float], ys: list[float]):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _ranks(vals: list[float]) -> list[float]:
    """平均秩（同值并列取平均），供 Spearman 用。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _ic_stats(samples: list[dict], eps: float) -> dict:
    """IC / RankIC：按「同一交易日 + 同一入场时点」做截面秩相关，再对期数取均值。

    样本数 < 5 的截面不参与（截面太小时秩相关没有意义，反而放大噪声）。
    ICIR = IC 均值 / IC 标准差（越稳定越可信）。
    """
    per: dict[tuple, list] = {}
    for s in samples:
        per.setdefault((s["date"], s["slot"]), []).append((s["score"], s["ret"]))
    ics: list[float] = []
    rics: list[float] = []
    periods: list[dict] = []
    for (d, slot), pairs in sorted(per.items()):
        if len(pairs) < 5:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        ic = _pearson(xs, ys)
        ric = _pearson(_ranks(xs), _ranks(ys))
        if ic is None or ric is None:
            continue
        ics.append(ic)
        rics.append(ric)
        periods.append({"date": d, "slot": slot, "n": len(pairs),
                        "ic": round(ic, 4), "rank_ic": round(ric, 4)})
    n = len(ics)
    if not n:
        return {"periods": 0, "ic_mean": None, "rank_ic_mean": None, "ic_std": None,
                "icir": None, "ic_win": None, "list": []}
    ic_mean = sum(ics) / n
    ric_mean = sum(rics) / n
    var = sum((x - ic_mean) ** 2 for x in ics) / n
    sd = var ** 0.5
    return {
        "periods": n,
        "ic_mean": round(ic_mean, 4),
        "rank_ic_mean": round(ric_mean, 4),
        "ic_std": round(sd, 4),
        "icir": round(ic_mean / sd, 3) if sd > 0 else None,
        "ic_win": round(sum(1 for x in rics if x > 0) / n, 4),
        "list": periods,
    }


# —— 聚合累加器：所有统计表共用同一套「加样本 → 出指标」，避免各表各写一份口径 ——

def _agg_new() -> dict:
    return {"n": 0, "n_eval": 0, "n_flat": 0, "up_n": 0, "down_n": 0,
            "bull_n": 0, "bear_n": 0, "bull_eval": 0, "bear_eval": 0,
            "bull_hits": 0, "bear_hits": 0, "tp": 0, "fp": 0, "tn": 0, "fn": 0,
            "ret_sum": 0.0, "ret_bull": 0.0, "ret_bear": 0.0,
            "pnl_sum": 0.0, "win_n": 0, "loss_n": 0, "gain_sum": 0.0, "loss_sum": 0.0,
            "amp_sum": 0.0, "amp_n": 0, "cap_sum": 0.0, "cap_n": 0,
            "p_exc_sum": 0.0, "p_hit_n": 0, "p_hits": 0,
            "b_exc_sum": 0.0, "b_hit_n": 0, "b_hits": 0}


def _agg_add(a: dict, s: dict, eps: float) -> None:
    ret, pred, act = s["ret"], s["pred"], s["actual"]
    a["n"] += 1
    a["ret_sum"] += ret
    if pred == "bull":
        a["bull_n"] += 1
        a["ret_bull"] += ret
    else:
        a["bear_n"] += 1
        a["ret_bear"] += ret
    if act == "up":
        a["up_n"] += 1
    elif act == "down":
        a["down_n"] += 1
    else:
        a["n_flat"] += 1

    if act != "flat":                       # 中性样本剔出方向命中率分母
        a["n_eval"] += 1
        if pred == "bull":
            a["bull_eval"] += 1
            if act == "up":
                a["bull_hits"] += 1
                a["tp"] += 1
            else:
                a["fp"] += 1
        else:
            a["bear_eval"] += 1
            if act == "down":
                a["bear_hits"] += 1
                a["tn"] += 1
            else:
                a["fn"] += 1

    # 策略盈亏：按信号方向操作（看涨做多、看跌做空），持有到区间末
    pnl = ret if pred == "bull" else -ret
    a["pnl_sum"] += pnl
    if pnl > 0:
        a["win_n"] += 1
        a["gain_sum"] += pnl
    elif pnl < 0:
        a["loss_n"] += 1
        a["loss_sum"] += pnl

    amp = s.get("seg_amp")
    if amp is not None:
        a["amp_sum"] += amp
        a["amp_n"] += 1
        if amp > 0:
            a["cap_sum"] += abs(ret) / amp
            a["cap_n"] += 1

    ex_p = s.get("excess_pool")
    if ex_p is not None:
        a["p_exc_sum"] += ex_p
        h = _exc_hit(pred, ex_p, eps)
        if h is not None:
            a["p_hit_n"] += 1
            if h:
                a["p_hits"] += 1
    ex_b = s.get("excess_idx")
    if ex_b is not None:
        a["b_exc_sum"] += ex_b
        h = _exc_hit(pred, ex_b, eps)
        if h is not None:
            a["b_hit_n"] += 1
            if h:
                a["b_hits"] += 1


def _div(x, y):
    return round(x / y, 4) if y else None


def _agg_finish(a: dict) -> dict:
    """把累加器落成展示用指标。**所有比率的分母都随行返回**，便于「N<20 灰显」与自查。"""
    out = {
        "n": a["n"], "n_eval": a["n_eval"], "n_flat": a["n_flat"],
        "up_n": a["up_n"], "down_n": a["down_n"],
        "bull_n": a["bull_n"], "bear_n": a["bear_n"],
        "bull_eval": a["bull_eval"], "bear_eval": a["bear_eval"],
        "bull_hit": _div(a["bull_hits"], a["bull_eval"]),
        "bear_hit": _div(a["bear_hits"], a["bear_eval"]),
        "hit": _div(a["bull_hits"] + a["bear_hits"], a["n_eval"]),
        "tp": a["tp"], "fp": a["fp"], "tn": a["tn"], "fn": a["fn"],
        "precision": _div(a["tp"], a["tp"] + a["fp"]),
        "recall": _div(a["tp"], a["tp"] + a["fn"]),
        "avg_ret": _div(a["ret_sum"], a["n"]),
        "avg_ret_bull": _div(a["ret_bull"], a["bull_n"]),
        "avg_ret_bear": _div(a["ret_bear"], a["bear_n"]),
        "avg_pnl": _div(a["pnl_sum"], a["n"]),
        "pnl_win": _div(a["win_n"], a["win_n"] + a["loss_n"]),
        "profit_factor": (round(a["gain_sum"] / abs(a["loss_sum"]), 3)
                          if a["loss_sum"] < 0 else None),
        "avg_amp": _div(a["amp_sum"], a["amp_n"]),
        "amp_capture": _div(a["cap_sum"], a["cap_n"]),
        "p_excess_avg": _div(a["p_exc_sum"], a["n"]),
        "p_hit": _div(a["p_hits"], a["p_hit_n"]),
        "p_hit_n": a["p_hit_n"],
        "b_excess_avg": _div(a["b_exc_sum"], a["n"]),
        "b_hit": _div(a["b_hits"], a["b_hit_n"]),
        "b_hit_n": a["b_hit_n"],
        "low_n": a["n"] < 20,
    }
    p, r = out["precision"], out["recall"]
    out["f1"] = round(2 * p * r / (p + r), 4) if (p and r and (p + r)) else None
    # 多空对称性 = 看涨命中率 − 看跌命中率。接近 0 才算「两个方向都能判」；
    # 单边虚高的模型在普跌/普涨日会给出好看但无用的数，这个差值是直接的体检指标。
    out["symmetry"] = (round(out["bull_hit"] - out["bear_hit"], 4)
                       if (out["bull_hit"] is not None and out["bear_hit"] is not None) else None)
    # 朴素基准：什么都不看，直接跟大盘同向 → 用指数基准的「涨」比例近似
    return out


def _bucket_of(score: float) -> str:
    for lo, hi, label in _BUCKETS:
        if (lo is None or score >= lo) and (hi is None or score < hi):
            return label
    return _BUCKETS[-1][2]


def _build_backtest(user_id: int, db: Session, start: dt.date, end: dt.date,
                    slots: list[str], threshold: float, eps: float, basis: str,
                    bench: str, monitor_only: bool) -> dict:
    """组装报表 2（API 与导出共用，保证「所见即所得」）。"""
    s0, s1 = start.strftime(_ISO), end.strftime(_ISO)
    slots = sorted(set(slots), key=ss._slot_minutes)
    ex_map = _exit_map(slots)
    key = "score" if basis == "adj" else "raw"

    snaps = (db.query(PoolScoreSnapshot)
             .filter(PoolScoreSnapshot.user_id == user_id,
                     PoolScoreSnapshot.trade_date >= s0,
                     PoolScoreSnapshot.trade_date <= s1).all())

    params = {"start": s0, "end": s1, "slots": slots, "threshold": threshold, "eps": eps,
              "basis": basis, "bench": bench, "monitor_only": int(bool(monitor_only)),
              "segments": [{"entry": s, "exit": ex_map[s],
                            "label": f"{_slot_label(s)} → " +
                                     ("收盘" if ex_map[s] == "CLOSE" else _slot_label(ex_map[s]))}
                           for s in slots]}
    if not snaps:
        return {"samples_n": 0, "params": params,
                "meta": {"empty": True, "samples": 0, "dates": 0, "codes": 0,
                         "note": "区间内没有落库的时点快照 —— 快照自采集器上线当日起逐日积累，"
                                 "早于上线日的日期没有数据（按设计不做历史回填）。"}}

    groups: dict[tuple, dict] = {}
    for s in snaps:
        groups.setdefault((s.code, s.trade_date), {})[s.slot] = s

    codes = sorted({c for c, _ in groups})
    names = {c: (n or "") for c, n in
             db.query(Stock.code, Stock.name).filter(Stock.code.in_(codes or [""])).all()}
    close_map = {(r.code, r.date): _r(r.close) for r in
                 db.query(DailyQuote).filter(DailyQuote.code.in_(codes or [""]),
                                             DailyQuote.date >= s0,
                                             DailyQuote.date <= s1).all()}

    # —— 样本构造（先建**全池**样本，基准需要全池参与；monitor_only 只在聚合前过滤）——
    drop = {"no_score": 0, "no_entry": 0, "no_exit": 0}
    raw: list[dict] = []
    for (code, date) in sorted(groups.keys()):
        sl = groups[(code, date)]
        in_monitor = max(int((x.in_monitor or 0)) for x in sl.values())
        for e in slots:
            snap_e = sl.get(e)
            if not snap_e:
                drop["no_entry"] += 1
                continue
            ep = _pos(snap_e.price)
            if not ep:
                drop["no_entry"] += 1
                continue
            score = getattr(snap_e, "score_raw") if key == "raw" else snap_e.score
            if score is None:
                # 行情异常时后端写 NULL（不是 0）→ 绝不把「无数据」当「0 分」参与回测
                drop["no_score"] += 1
                continue
            ex = ex_map[e]
            if ex == "CLOSE":
                bar1500 = sl.get("1500")
                xp = _pos(bar1500.price) if bar1500 else None
                if not xp:
                    xp = _pos(close_map.get((code, date)))   # 兜底：日线收盘
            else:
                xp = _pos(sl[ex].price) if sl.get(ex) else None
            if not xp:
                drop["no_exit"] += 1
                continue

            # 先定精度再判方向：方向判定 / 展示 / 聚合必须用**同一个** ret。
            # 否则会出现「明细里写着 0.200%，却被 ε=0.2 判成涨」这种表里不一。
            ret = round((xp - ep) / ep * 100.0, 3)
            # 区间采样振幅：只用区间内**落库的时点价**（11 个时点），不是逐笔振幅。
            # 名字里带「采样」就是为了防止被误当成日线振幅口径。
            e_m = ss._slot_minutes(e)
            x_m = 1440 if ex == "CLOSE" else ss._slot_minutes(ex)
            hi = lo = None
            for s2, sn2 in sl.items():
                if e_m <= ss._slot_minutes(s2) <= x_m:
                    p2 = _pos(sn2.price)
                    if p2:
                        hi = p2 if hi is None else max(hi, p2)
                        lo = p2 if lo is None else min(lo, p2)
            if ex == "CLOSE":
                hi = xp if hi is None else max(hi, xp)
                lo = xp if lo is None else min(lo, xp)
            seg_amp = round((hi - lo) / ep * 100.0, 3) if (hi and lo) else None

            raw.append({
                "date": date, "code": code, "name": names.get(code, ""),
                "slot": e, "exit_slot": ex,
                "exit_label": "收盘" if ex == "CLOSE" else _slot_label(ex),
                "in_monitor": in_monitor,
                "score": _r(score, 1),
                "entry": ep, "exit": xp, "ret": ret,
                "seg_amp": seg_amp,
                "pred": "bull" if float(score) >= threshold else "bear",
                "actual": "up" if ret > eps else ("down" if ret < -eps else "flat"),
                "semantic": snap_e.semantic or "",
                "regime": snap_e.market_regime or "",
                "style": snap_e.style_tag or "",
                "mv": snap_e.mv_tier or "",
                "factor": _r(snap_e.adj_factor, 3),
                "bench_pool": None, "excess_pool": None,
                "bench_idx": None, "excess_idx": None,
            })

    # —— 基准 1：池内等权（横截面）——按 (日期, 入场时点) 对**全池**等权平均 ——
    acc: dict[tuple, list] = {}
    for s in raw:
        acc.setdefault((s["date"], s["slot"]), []).append(s["ret"])
    pool_mean = {k: sum(v) / len(v) for k, v in acc.items() if v}

    # —— 基准 2：选定指数（时序）——
    idx_series = _index_slot_map(bench) if bench in _INDEX_LABEL else {}
    idx_covered = sorted(idx_series.keys())
    for s in raw:
        pm = pool_mean.get((s["date"], s["slot"]))
        if pm is not None:
            s["bench_pool"] = round(pm, 3)
            s["excess_pool"] = round(s["ret"] - pm, 3)
        if idx_series:
            pe = _index_price(idx_series, s["date"], s["slot"])
            px = _index_price(idx_series, s["date"],
                              "1500" if s["exit_slot"] == "CLOSE" else s["exit_slot"])
            if pe and px:
                ir = (px - pe) / pe * 100.0
                s["bench_idx"] = round(ir, 3)
                s["excess_idx"] = round(s["ret"] - ir, 3)

    samples = [s for s in raw if (not monitor_only or s["in_monitor"])]
    # 命中标记（阈值口径）与策略盈亏，导出「样本明细」时要用
    for s in samples:
        s["hit"] = (None if s["actual"] == "flat"
                    else (s["actual"] == "up") if s["pred"] == "bull"
                    else (s["actual"] == "down"))
        s["pnl"] = round(s["ret"] if s["pred"] == "bull" else -s["ret"], 3)
        s["hit_p"] = _exc_hit(s["pred"], s["excess_pool"], eps)
        s["hit_b"] = _exc_hit(s["pred"], s["excess_idx"], eps)

    if not samples:
        return {"samples_n": 0, "params": params,
                "meta": {"empty": True, "samples": 0, "dates": 0, "codes": len(codes),
                         "drop": drop,
                         "note": "区间内有快照，但没有任何「入场价+出场价+有效评分」齐全的样本 —— "
                                 "监控时点可能不在采集清单内（采集时点为 11 个基准时点 ∪ 监控时点）。"}}

    # —— 汇总 ——
    total = _agg_new()
    for s in samples:
        _agg_add(total, s, eps)
    summary = _agg_finish(total)
    summary["ic"] = _ic_stats(samples, eps)

    # —— 分表 ——
    def rows_by(pick) -> list[dict]:
        """按 pick(sample)→key 分组；key 为 None 的样本丢弃（如未记录的市值档）。"""
        buckets: dict = {}
        for s in samples:
            k = pick(s)
            if k is None or k == "":
                continue
            b = buckets.get(k)
            if b is None:
                b = buckets[k] = (set(), _agg_new())
            b[0].add(s["code"])
            _agg_add(b[1], s, eps)
        return [{**{"key": k}, **_agg_finish(v[1]), "codes": len(v[0])}
                for k, v in buckets.items()]

    by_slot: list[dict] = []
    for slot in slots:
        grp = [s for s in samples if s["slot"] == slot]
        if not grp:
            continue
        a = _agg_new()
        for s in grp:
            _agg_add(a, s, eps)
        row = _agg_finish(a)
        row["slot"] = slot
        row["label"] = _slot_label(slot)
        row["seg"] = f"{_slot_label(slot)} → " + ("收盘" if ex_map[slot] == "CLOSE"
                                                else _slot_label(ex_map[slot]))
        row["codes"] = len({s["code"] for s in grp})
        by_slot.append(row)

    by_bucket: list[dict] = []
    for lo, hi, label in _BUCKETS:
        grp = [s for s in samples
               if (lo is None or s["score"] >= lo) and (hi is None or s["score"] < hi)]
        if not grp:
            continue
        a = _agg_new()
        for s in grp:
            _agg_add(a, s, eps)
        row = _agg_finish(a)
        row["bucket"] = label
        # 上涨占比对每个桶都有意义（不只是最低桶）：它不依赖阈值，
        # 直接回答「这个分数段的样本真的更容易涨吗」，是分桶单调性最直观的读数。
        row["up_share"] = _div(a["up_n"], a["n"])
        by_bucket.append(row)

    by_time: list[dict] = []
    for d in sorted({s["date"] for s in samples}):
        grp = [s for s in samples if s["date"] == d]
        a = _agg_new()
        for s in grp:
            _agg_add(a, s, eps)
        row = _agg_finish(a)
        row["date"] = d
        row["codes"] = len({s["code"] for s in grp})
        row["seg_n"] = len({s["slot"] for s in grp})
        by_time.append(row)

    by_semantic: list[dict] = []
    for sem in ("看涨", "观望", "看跌", ""):
        grp = [s for s in samples if (s["semantic"] or "") == sem]
        if not grp:
            continue
        a = _agg_new()
        for s in grp:
            _agg_add(a, s, eps)
        row = _agg_finish(a)
        row["key"] = sem or "未记录"
        row["label"] = {"看涨": "看涨（落库语义）", "观望": "观望（落库语义）",
                        "看跌": "看跌（落库语义）"}.get(sem, "未记录")
        row["up_share"] = _div(a["up_n"], a["n"])
        by_semantic.append(row)

    # 分组透视：环境 / 风格 / 市值 / 是否监控池
    dims = [
        ("env", "市场环境", lambda s: _M_TIER_LABEL.get(s["regime"], s["regime"] or None),
         {"healthy": "健康市", "weak": "弱势市", "collapse": "崩坏市"}),
        ("style", "个股风格", lambda s: s["style"] or None, {}),
        ("mv", "市值档位", lambda s: s["mv"] or None, {}),
        ("monitor", "是否监控池", lambda s: ("监控池" if s["in_monitor"] else "仅全池"), {}),
    ]
    by_dim = []
    for dk, dlabel, pick, _order in dims:
        rows = rows_by(pick)
        by_dim.append({"dim": dk, "label": dlabel, "rows": rows})

    return {
        "samples_n": len(samples),
        "params": params,
        "summary": summary,
        "tables": {"by_slot": by_slot, "by_bucket": by_bucket, "by_dim": by_dim,
                   "by_time": by_time, "by_semantic": by_semantic},
        "bench": {
            "code": bench,
            "label": (_INDEX_LABEL.get(bench) if bench in _INDEX_LABEL
                      else "池内等权（横截面）"),
            "kind": "index" if bench in _INDEX_LABEL else "pool",
            "index_available": bool(idx_series),
            "index_window": {"from": idx_covered[0], "to": idx_covered[-1]} if idx_covered else None,
            "index_options": [{"code": _BENCH_POOL, "label": "池内等权（横截面）"}] +
                             [dict(o) for o in _INDEX_OPTIONS],
            "note": ("池内等权 = 同一时点全池样本的等权平均收益，衡量**选股能力**（不依赖外部数据）；"
                     "指数 = 衡量**择时能力**，受上游分钟线可回溯窗口限制（约 8 个交易日）。"
                     "两者差异很大时说明「选股有效但方向择时无效」或反之。"),
        },
        # 逐笔明细：只有导出会用到，API 响应里会被 pop 掉（页面每次请求不必扛几千行）。
        # **这个键不能漏** —— 漏了导出「样本明细」sheet 就是空表，而页面上完全看不出来。
        "_samples": samples,
        "meta": {
            "empty": False, "samples": len(samples), "raw_samples": len(raw),
            "dates": len({s["date"] for s in samples}),
            "codes": len({s["code"] for s in samples}),
            "slots": slots, "drop": drop,
            "note": (f"共 {len(samples)} 个样本（{len({s['code'] for s in samples})} 只 × "
                     f"{len({s['date'] for s in samples})} 个交易日 × {len(slots)} 个监控时点）。"
                     "样本要求「入场价 / 出场价 / 有效评分」三者齐全；"
                     "行情异常时评分写 NULL（不是 0）→ 该行不参与回测。"
                     "实际方向以区间涨跌幅判定，|涨跌幅| ≤ ε 视为中性并剔出命中率分母。"),
        },
    }


@router.get("/backtest")
def report_backtest(
    start: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    slots: str | None = Query(None, description="监控时点，逗号分隔如 0945,1345；缺省取系统配置"),
    threshold: float = Query(60, description="看涨阈值：评分 ≥ 该值判看涨，否则看跌"),
    eps: float = Query(0.2, description="中性区间（百分点）：|区间涨跌幅| ≤ ε 视为无方向，剔出命中率"),
    basis: str = Query("raw", description="raw=原始分（默认，跨日跨风格可比）；adj=环境调整后分"),
    bench: str = Query(_DEFAULT_BENCH, description="基准：pool=池内等权；或指数代码如 sh000905"),
    monitor_only: int = Query(0, description="1=只用盘间监控池样本（基准仍按全池计算）"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """报表 2 · 趋势判断回测统计。"""
    end_d = _parse_date(end) or _cn_today()
    start_d = _parse_date(start) or (end_d - dt.timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    slot_list = [s.strip() for s in (slots or "").split(",") if s.strip()] or list(
        ss._setting_list("review_monitor_slots", ss.MONITOR_SLOTS))
    if bench not in _INDEX_LABEL:
        bench = _BENCH_POOL
    out = _build_backtest(user.id, db, start_d, end_d, slot_list,
                          float(threshold), abs(float(eps)),
                          "adj" if str(basis).lower() in ("adj", "adjusted", "score") else "raw",
                          bench, bool(monitor_only))
    out["range"] = {"start": start_d.strftime(_ISO), "end": end_d.strftime(_ISO)}

    # 明细行体积大（120 只 × 2 时点 × 30 日 ≈ 7 千行），API 响应里不带；
    # 仅导出时补上，避免把每次页面请求都撑大。
    out.pop("_samples", None)
    return out


# ============ 报表 2 导出 ============

# 展示列定义集中一处：页面表格与导出表头共用同一套（列名/顺序不会两处漂移）
_BT_MAIN_COLS = [
    ("label", "监控时点"), ("seg", "区间"), ("codes", "标的数"), ("n", "样本数N"),
    ("bull_n", "看涨样本"), ("bear_n", "看跌样本"),
    ("bull_hit", "看涨命中率"), ("bear_hit", "看跌命中率"), ("hit", "总命中率"),
    ("precision", "精确率"), ("recall", "召回率"), ("f1", "F1"), ("symmetry", "多空对称性"),
    ("avg_ret", "平均区间收益%"), ("avg_amp", "平均采样振幅%"), ("amp_capture", "振幅捕获比"),
    ("avg_pnl", "策略期望收益%"), ("pnl_win", "策略胜率"), ("profit_factor", "盈亏比"),
    ("p_hit", "池内超额命中率"), ("b_hit", "指数超额命中率"),
]
_BT_RATE_KEYS = {"bull_hit", "bear_hit", "hit", "precision", "recall", "f1", "symmetry",
                 "pnl_win", "p_hit", "b_hit", "up_share"}


def _bt_detail_rows(data: dict) -> list[dict]:
    """样本明细行（导出用）。截断上限防止超大区间把工作簿撑爆。"""
    out = []
    for s in (data.get("_samples") or [])[:20000]:
        out.append({
            **s,
            "in_monitor": "是" if s.get("in_monitor") else "否",
            "pred": "看涨" if s.get("pred") == "bull" else "看跌",
            "actual": {"up": "涨", "down": "跌", "flat": "中性"}.get(s.get("actual"), ""),
            "hit": {True: "命中", False: "未中", None: "-"}.get(s.get("hit")),
            "hit_p": {True: "命中", False: "未中", None: "-"}.get(s.get("hit_p")),
            "hit_b": {True: "命中", False: "未中", None: "-"}.get(s.get("hit_b")),
            "regime": _M_TIER_LABEL.get(s.get("regime"), s.get("regime") or ""),
            "amp_capture": (round(abs(s["ret"]) / s["seg_amp"], 3)
                            if s.get("seg_amp") else None),
        })
    return out


def _bt_write_table(ws, cols: list, rows: list, title: str = "") -> int:
    """写一张表：可选标题 → 表头 → 数据行。返回表头行号（供冻结/列宽使用）。"""
    from openpyxl.styles import Font, PatternFill, Alignment

    if title:
        ws.append([title])
        c = ws.cell(ws.max_row, 1)
        c.font = Font(bold=True, size=12, color="1D4ED8")
        ws.append([])
    head_row = ws.max_row + 1
    ws.append([c[1] for c in cols])
    for cell in ws[head_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2563EB")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r in rows:
        ws.append([r.get(c[0]) for c in cols])
    # 比率先按 0-1 原值写，再套百分比格式（Excel 里仍是数字，可直接参与计算，不是文本）
    for i, (k, _lab) in enumerate(cols, start=1):
        if k not in _BT_RATE_KEYS:
            continue
        col = ws.cell(head_row, i).column_letter
        for rr in range(head_row + 1, ws.max_row + 1):
            ws[f"{col}{rr}"].number_format = "0.0%"
    return head_row


def _bt_widths(ws, head_row: int, widths: list) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(head_row, i).column_letter].width = w


def _bt_workbook(data: dict):
    """把回测结果写成多 sheet 工作簿。返回 BytesIO。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    p = data.get("params") or {}
    su = data.get("summary") or {}
    bn = data.get("bench") or {}
    t = data.get("tables") or {}
    mt = data.get("meta") or {}
    ic = su.get("ic") or {}

    _head_fill = PatternFill("solid", fgColor="2563EB")
    _head_font = Font(bold=True, color="FFFFFF")

    def _kv_header(ws):
        for cell in ws[ws.max_row]:
            cell.font = _head_font
            cell.fill = _head_fill

    # —— sheet1：回测统计（参数 + 摘要 + 主表）——
    ws = wb.active
    ws.title = "回测统计"
    ws.append(["项目", "值"])
    _kv_header(ws)
    segs = "；".join(s["label"] for s in (p.get("segments") or []))
    idx_win = bn.get("index_window")
    drop = mt.get("drop") or {}
    for k, v in [
        ("数据区间", f"{p.get('start')} ~ {p.get('end')}"),
        ("监控时点与区间", segs),
        ("预测规则", f"评分 ≥ {p.get('threshold')} → 看涨；否则看跌"
                     f"（评分口径：{'环境调整后分' if p.get('basis') == 'adj' else '原始分'}）"),
        ("中性阈值 ε", f"{p.get('eps')} 个百分点（|区间涨跌幅| ≤ ε 视为无方向，剔出命中率分母）"),
        ("基准", f"{bn.get('label')}（{bn.get('code')}）"),
        ("指数基准可用性",
         f"可用，{idx_win['from']} ~ {idx_win['to']}" if idx_win
         else "本次不可用（上游分钟线仅回溯约 8 个交易日，或所选指数取不到数据）"),
        ("样本范围", "仅盘间监控池" if p.get("monitor_only") else "全池"),
        ("有效样本数 N", su.get("n")),
        ("可判方向样本", (f"{su.get('n_eval')}"
                         + (f"（占比 {su['n_eval'] / su['n']:.1%}）" if su.get("n") else "")
                         + f"；中性剔出 {su.get('n_flat')}")),
        ("覆盖标的/交易日", f"{mt.get('codes')} 只 / {mt.get('dates')} 个交易日"),
        ("丢弃样本", f"无有效评分 {drop.get('no_score', 0)}、缺入场价 {drop.get('no_entry', 0)}、"
                     f"缺出场价 {drop.get('no_exit', 0)}"),
    ]:
        ws.append([k, v])
    ws.append([])
    ws.append(["—— 摘要指标 ——", ""])
    ws.cell(ws.max_row, 1).font = Font(bold=True, color="1D4ED8")
    ws.append(["指标", "值"])
    _kv_header(ws)
    for k, v, is_rate in [
        ("总命中率（剔中性）", su.get("hit"), True),
        ("看涨命中率", su.get("bull_hit"), True),
        ("看跌命中率", su.get("bear_hit"), True),
        ("多空对称性（看涨−看跌）", su.get("symmetry"), True),
        ("精确率（以看涨为正类）", su.get("precision"), True),
        ("召回率（以看涨为正类）", su.get("recall"), True),
        ("F1", su.get("f1"), True),
        ("混淆矩阵 TP/FP/TN/FN",
         f"{su.get('tp')} / {su.get('fp')} / {su.get('tn')} / {su.get('fn')}", False),
        ("平均区间收益%", su.get("avg_ret"), False),
        ("看涨组平均收益%", su.get("avg_ret_bull"), False),
        ("看跌组平均收益%", su.get("avg_ret_bear"), False),
        ("策略期望收益%（按信号方向操作一次）", su.get("avg_pnl"), False),
        ("策略胜率", su.get("pnl_win"), True),
        ("盈亏比（总盈利/总亏损）", su.get("profit_factor"), False),
        ("平均采样振幅%", su.get("avg_amp"), False),
        ("振幅捕获比（|收益|/采样振幅）", su.get("amp_capture"), False),
        ("池内等权超额命中率", su.get("p_hit"), True),
        ("池内等权平均超额%", su.get("p_excess_avg"), False),
        ("指数超额命中率", su.get("b_hit"), True),
        ("指数平均超额%", su.get("b_excess_avg"), False),
        ("IC 均值（评分 vs 未来收益）", ic.get("ic_mean"), False),
        ("RankIC 均值", ic.get("rank_ic_mean"), False),
        ("ICIR（IC均值/IC标准差）", ic.get("icir"), False),
        ("RankIC 胜率（正比例）", ic.get("ic_win"), True),
        ("IC 期数（截面 ≥5 样本的期）", ic.get("periods"), False),
    ]:
        ws.append([k, v])
        if is_rate and v is not None:
            ws.cell(ws.max_row, 2).number_format = "0.0%"
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 62
    for row_cells in ws.iter_rows(min_row=2):
        for c in row_cells:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.append([])
    head = _bt_write_table(ws, _BT_MAIN_COLS, t.get("by_slot") or [], "—— 按监控时点 ——")
    _bt_widths(ws, head, [13, 15, 8, 9, 9, 9, 11, 11, 10, 9, 9, 8, 11,
                          13, 13, 11, 13, 10, 9, 13, 13])
    ws.freeze_panes = f"A{head + 1}"

    # —— sheet2：分桶有效性 ——
    ws2 = wb.create_sheet("分桶有效性")
    h = _bt_write_table(ws2, [("bucket", "评分区间"), ("n", "样本数N"), ("up_n", "实际上涨数"),
                              ("up_share", "上涨占比"), ("hit", "阈值口径命中率"),
                              ("avg_ret", "平均区间收益%"), ("avg_amp", "平均采样振幅%"),
                              ("p_hit", "池内超额命中率"), ("b_hit", "指数超额命中率")],
                        t.get("by_bucket") or [],
                        "评分分桶 → 命中率/收益（理想：随评分升高单调递增；"
                        "若在某桶后走平或反转，说明该阈值区分度不够）")
    _bt_widths(ws2, h, [12, 10, 11, 10, 14, 14, 14, 14, 14])

    # —— sheet3：分组透视（环境/风格/市值/监控池）——
    ws3 = wb.create_sheet("分组透视")
    cols3 = [("key", "分组"), ("codes", "标的数"), ("n", "样本数N"),
             ("bull_n", "看涨样本"), ("bear_n", "看跌样本"),
             ("hit", "总命中率"), ("bull_hit", "看涨命中率"), ("bear_hit", "看跌命中率"),
             ("avg_ret", "平均区间收益%"), ("avg_amp", "平均采样振幅%"),
             ("p_hit", "池内超额命中率"), ("b_hit", "指数超额命中率")]
    h3 = 0
    for grp in t.get("by_dim") or []:
        h3 = _bt_write_table(ws3, cols3, grp.get("rows") or [], f"—— 按{grp.get('label')} ——")
        ws3.append([])
    _bt_widths(ws3, h3 or 1, [12, 9, 10, 10, 10, 10, 11, 11, 14, 14, 14, 14])

    # —— sheet4：时间稳健性 ——
    ws4 = wb.create_sheet("时间稳健性")
    h = _bt_write_table(ws4, [("date", "交易日"), ("codes", "标的数"), ("seg_n", "区间数"),
                              ("n", "样本数N"), ("hit", "总命中率"),
                              ("bull_hit", "看涨命中率"), ("bear_hit", "看跌命中率"),
                              ("avg_ret", "平均区间收益%"), ("p_excess_avg", "池内平均超额%"),
                              ("p_hit", "池内超额命中率"), ("b_excess_avg", "指数平均超额%"),
                              ("b_hit", "指数超额命中率")],
                        t.get("by_time") or [],
                        "按交易日逐日看命中率：单日高或低都不能说明问题，要看整段是否稳定"
                        "（样本不足的日子请以 N 为准）")
    _bt_widths(ws4, h, [12, 9, 9, 9, 10, 11, 11, 14, 14, 14, 14, 14])

    # —— sheet5：语义分组（验证「观望」规则）——
    ws5 = wb.create_sheet("语义分组")
    h = _bt_write_table(ws5, [("label", "落库语义"), ("n", "样本数N"), ("up_share", "上涨占比"),
                              ("avg_ret", "平均区间收益%"), ("avg_amp", "平均采样振幅%"),
                              ("hit", "阈值口径命中率")],
                        t.get("by_semantic") or [],
                        "验证「观望」是否有价值：若观望组平均收益确实≈0 且明显弱于看涨组，"
                        "说明「崩坏市 / 弱势逆势涨 → 不给方向」这条规则有效；"
                        "若观望组与看涨组一样强，则观望过严、错过了机会")
    _bt_widths(ws5, h, [20, 10, 10, 14, 14, 14])

    # —— sheet6：样本明细（逐笔可复核）——
    ws6 = wb.create_sheet("样本明细")
    h = _bt_write_table(ws6, [
        ("date", "日期"), ("code", "代码"), ("name", "名称"),
        ("in_monitor", "监控池"), ("slot", "入场时点"), ("exit_label", "出场"),
        ("score", "评分"), ("pred", "预测"), ("entry", "入场价"), ("exit", "出场价"),
        ("ret", "区间涨跌幅%"), ("seg_amp", "采样振幅%"), ("amp_capture", "振幅捕获比"),
        ("actual", "实际方向"), ("hit", "方向命中"),
        ("bench_pool", "池内基准%"), ("excess_pool", "池内超额%"), ("hit_p", "池内超额命中"),
        ("bench_idx", "指数%"), ("excess_idx", "指数超额%"), ("hit_b", "指数超额命中"),
        ("pnl", "策略盈亏%"), ("semantic", "落库语义"), ("regime", "市场环境"),
        ("style", "风格"), ("mv", "市值档"),
    ], _bt_detail_rows(data), "逐笔样本（用于人工复核任意一格的数字怎么来的）")
    _bt_widths(ws6, h, [11, 8, 10, 8, 9, 7, 7, 7, 9, 9, 12, 11, 11,
                        9, 9, 11, 11, 12, 9, 11, 12, 11, 12, 9, 9, 8])
    ws6.freeze_panes = f"D{h + 1}"

    # —— sheet7：口径说明 ——
    ws7 = wb.create_sheet("口径说明")
    for r in [
        ["项目", "说明"],
        ["方向目标（主）", "ret =（出场价 − 入场价）/ 入场价。出场价 = 下一个监控时点价；"
                          "最后一个监控时点取当日收盘（优先 15:00 快照，缺失时回退日线收盘）。"],
        ["实际方向", "ret > +ε → 涨；ret < −ε → 跌；否则视为中性并**剔出**命中率分母（ε 见参数）。"
                     "这是设计上的关键：把「窄幅震荡」和「猜错方向」混在一起会让命中率失真。"],
        ["预测方向", "评分 ≥ 阈值 → 看涨，否则看跌。**每个样本都有方向**，"
                     "混淆矩阵/精确率/召回率才有定义（落库的「观望」另出一张验证表）。"],
        ["为什么要做超额方向", "全市场普跌时模型全体看跌 → 看跌命中率虚高但毫无用处。"
                              "因此除绝对方向外，另按「池内等权」与「指数」两套基准算超额方向命中率。"],
        ["池内等权基准（横截面）", "同一时点**全池**样本的等权平均收益，不依赖任何外部数据、永远可用。"
                                  "它衡量**选股能力**：横截面超额均值必然≈0，所以要看的是"
                                  "「看涨组是否系统性跑赢、看跌组是否系统性跑输」。"],
        ["指数基准（时序）", "衡量**择时能力**（绝对方向对不对）。受上游分钟线可回溯窗口限制"
                            "（约 8 个交易日），超出窗口的日期该列为空 —— 不猜、不近似。"],
        ["振幅（用户点名）", "振幅本身**没有方向**，不能判涨跌，故只作波动维度："
                            "「采样振幅」= 区间内 11 个落库时点价的最大最小差 / 入场价（不是逐笔振幅）；"
                            "「振幅捕获比」= |区间收益| / 采样振幅，越接近 1 说明这段走势越单边。"],
        ["IC / RankIC", "每个「交易日 × 入场时点」的截面里，算评分与未来收益的相关系数"
                        "（IC=Pearson、RankIC=Spearman），再对期数取均值。截面样本 < 5 的期不参与。"
                        "ICIR = IC 均值 / IC 标准差，越稳定越可信。"],
        ["多空对称性", "看涨命中率 − 看跌命中率。接近 0 才说明两个方向都能判；"
                       "若某一侧明显高，多半是这段行情单边（并非模型真的会双向判断）。"],
        ["策略期望收益与盈亏比", "按信号方向操作一次（看涨做多、看跌做空）、持有到区间末："
                                "期望收益 = 每次盈亏的均值；盈亏比 = 总盈利 / 总亏损。"
                                "注意：不含手续费与滑点，也未考虑个股实际无法做空。"],
        ["样本量 N 与 N<20", "每格都显示 N；N < 20 的格子页面会灰显，"
                             "此时命中率波动极大，不要据此下结论。"],
        ["不做历史回填", "时点快照自采集器上线当日起逐日积累；更早的日期没有数据（空白 ≠ 0 分）。"],
        ["行情异常处理", "行情异常时评分写 NULL（而不是 0）→ 该样本直接不参与回测，"
                         "避免「数据异常」被当成「0 分」污染统计。"],
        ["免责声明", "仅用于个人学习与研究，不构成投资建议。回测结果不代表未来表现。"],
    ]:
        ws7.append(r)
    _kv_header(ws7)
    ws7.column_dimensions["A"].width = 22
    ws7.column_dimensions["B"].width = 100
    for row_cells in ws7.iter_rows(min_row=2):
        for c in row_cells:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


