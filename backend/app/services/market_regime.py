"""市场环境因子 v2：大盘恐慌分 M + 板块状态 S + 个股属性(Beta/RS/市值/风格) 采集与快照。

设计依据：design/market-regime-factor-v2.md
核心原则（红线）：
  1. A/B/C 个股原始评分公式完全不动；本模块只在输出层提供「环境调节系数 + 前置过滤标记」，
     由前端 mtscore.compute 或调用方用于 adj_score 计算与语义判断。
  2. 所有外部数据源缺失/失败时优雅降级（置 None 或中性值），绝不抛异常拖垮主行情链路。
"""
from __future__ import annotations

import threading
import time
import datetime as dt

from app.services import data_fetcher as df
from app.services.data_fetcher import (
    _http_get, _as_float, _em_json, ensure_quotes, _stock_return,
    get_market_status, _sector_day_change, get_sector_trend,
    _norm_industry, _fetch_industry_em, _secid,
)

# ===================== 配置 =====================
# M 组成权重（4 指数 + 上涨家数占比），合计 1.0
#   说明：原文档用「上涨家数占比 30%」替代「沪深300/深成指」的组合；这里采用
#   「4 指数 + 上涨家数」五分量方案，使 M 同时具备宽基代表性与广度代表性。
_M_IDX_WEIGHTS = {
    "sh000001": 0.25,   # 上证（定海神针）
    "sh000688": 0.20,   # 科创50（科技锚，用户偏科技持仓，固定权重）
    "sz399006": 0.15,   # 创业板指
    "sz399001": 0.15,   # 深成指
    "breadth":   0.25,   # 全市场上涨家数占比
}

_M_TIER_LABEL = {"healthy": "健康市", "weak": "弱势市", "collapse": "崩坏市"}
_M_DISCOUNT = {"healthy": 1.0, "weak": 0.9, "collapse": 0.75}
_M_THRESH = {"healthy": 60, "weak": 65, "collapse": 72}

# S 板块状态权重（板块近期涨跌 / 板块内上涨占比 / 20 日趋势）
_S_CHANGE_W, _S_UPRATIO_W, _S_TREND_W = 0.4, 0.3, 0.3

# 风格标签乘数（仅弱势/崩坏市生效；健康市恒 1.0）
#   DS 原值 防御×1.2 / 进攻×0.6；取经验值 1.15 / 0.65，两周后据回测微调。
_STYLE_MULT = {"防御": 1.15, "护盘权重": 0.95, "均衡": 1.0, "进攻": 0.65}

# 市值分档阈值（亿元）
_MV_SMALL, _MV_MID = 100.0, 500.0
# 护盘权重判定：超大市值（≥2000 亿）且非防御行业（机构重仓龙头）
_MV_GUARDIAN = 2000.0

# 防御 / 进攻 行业白名单（二级行业名，来自 _norm_industry）
_DEFENSE_IND = {
    "银行", "保险", "公用事业", "电力", "煤炭", "石油", "燃气", "水务",
    "高速公路", "电信", "交通运输", "港口", "机场", "钢铁", "水泥", "建筑", "地产",
}
_OFFENSE_IND = {
    "半导体", "软件开发", "电子", "军工", "计算机", "通信设备", "传媒", "互联网服务",
    "证券", "汽车", "新能源", "光伏", "电池", "医药", "医疗服务", "消费电子",
}

# ===================== 缓存 / 状态 =====================
_M_SNAPSHOT = {"m": None, "ts": 0.0, "stale": False}
_SNAPSHOT_TTL = 60
_IDX_CLOSE_CACHE = {}      # secid -> (ts, [closes])，TTL 1 天
_IDX_RET_CACHE = {}        # (secid, days) -> (ts, ret)，TTL 1 小时
_BETA_CACHE = {}           # code -> (ts, beta)，TTL 1 天
_MV_CACHE = {}             # code -> (ts, total_mv_yi)，TTL 1 小时
_BREADTH_CACHE = {"ts": 0.0, "up": 0, "down": 0, "ratio": None}

_lock = threading.Lock()
_stop = False
_thread = None
_REFRESHING_UNTIL = 0.0   # single-flight: 同一 5s 窗口内只放行一次网络刷新


# ===================== 工具 =====================
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _f_pct(x):
    """指数涨跌幅(%) → 0~100（约 +2%→100, -2%→0）。"""
    if x is None:
        return None
    return _clamp(50 + 25 * x, 0, 100)


def _g_ratio(r):
    """占比(0~1) → 0~100。"""
    if r is None:
        return None
    return _clamp(r * 100, 0, 100)


def _idx_change_pct(code: str):
    """腾讯 qt.gtimg.cn 取指数实时涨跌幅(%)。"""
    try:
        txt = _http_get(f"https://qt.gtimg.cn/q={code}", headers={"Referer": "https://gu.qq.com/"})
        if txt and "=" in txt:
            f = txt.split("=", 1)[1].strip().strip('";\n').split("~")
            if len(f) > 32:
                return _as_float(f[32])
    except Exception:
        pass
    return None


# 指数代码映射：腾讯符号 <-> 东财 secid（东财 push2his 在部分环境不可达，故腾讯优先）
_EM_IDX_SECID = {
    "sh000300": "1.000300", "sh000001": "1.000001", "sh000688": "1.000688",
    "sz399006": "0.399006", "sz399001": "0.399001",
}


def _qt_symbol(code: str) -> str:
    """个股代码 → 腾讯行情符号（6/9→sh，0/3→sz，其余→bj）。"""
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


def _index_daily_closes(symbol: str, days: int):
    """指数近 days 日收盘序列。腾讯 ifzq 优先（已验证可用），东财 push2his 兜底。

    symbol 形如 'sh000300'（沪深300）/ 'sh000001'（上证）。缓存 1 天。
    背景：东财 push2his/push2 在部分运行环境被网络策略拦截（实测 push2his 返回空），
    而腾讯 web.ifzq.gtimg.cn 与 qt.gtimg.cn 稳定可达，故改为腾讯优先。
    """
    cached = _IDX_CLOSE_CACHE.get(symbol)
    if cached and time.time() - cached[0] < 86400:
        c = cached[1]
        return c[-days:] if len(c) >= days else c
    closes = None
    # 源1：腾讯指数日线（day 行 = [date, open, close, high, low, volume]）
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days + 5},qfq"
        j = _em_json(url)
        node = ((j or {}).get("data") or {}).get(symbol, {}) or {}
        rows = node.get("day") or node.get("qfqday") or []
        cl = [_as_float(r[2]) for r in rows if len(r) >= 3]
        if len(cl) >= 2:
            closes = cl
    except Exception:
        pass
    # 源2：东财 push2his 指数日线兜底
    if closes is None:
        try:
            secid = _EM_IDX_SECID.get(symbol, "")
            if secid:
                url2 = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                        f"&klt=101&fqt=1&lmt={days + 5}&end=20500101&fields1=f1,f2,f3&fields2=f51,f53")
                j2 = _em_json(url2)
                rows2 = ((j2 or {}).get("data") or {}).get("klines") or []
                cl2 = [_as_float(str(r).split(",")[1]) for r in rows2 if "," in str(r)]
                if len(cl2) >= 2:
                    closes = cl2
        except Exception:
            pass
    if closes:
        _IDX_CLOSE_CACHE[symbol] = (time.time(), closes)
        return closes[-days:]
    return None


def _index_return(symbol: str, days: int):
    """指数近 days 日收益率(%)。缓存 1 小时。symbol 形如 'sh000300'。"""
    cached = _IDX_RET_CACHE.get((symbol, days))
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    closes = _index_daily_closes(symbol, days + 1)
    ret = None
    if closes and len(closes) >= 2:
        ret = (closes[-1] - closes[0]) / closes[0] * 100
    if ret is not None:
        _IDX_RET_CACHE[(symbol, days)] = (time.time(), ret)
    return ret


def _fetch_breadth():
    """全市场上涨家数占比(0~1)。东财 clist 统计，缓存 60s。失败返回 None。"""
    now = time.time()
    if now - _BREADTH_CACHE["ts"] < _SNAPSHOT_TTL and _BREADTH_CACHE["ratio"] is not None:
        return _BREADTH_CACHE["ratio"]
    up = down = 0
    ratio = None
    try:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=6000"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f3,f12")
        j = _em_json(url)
        rows = (((j or {}).get("data") or {}).get("diff") or [])
        for r in rows:
            chg = _as_float(r.get("f3"))
            if chg is None:
                continue
            if chg > 0:
                up += 1
            elif chg < 0:
                down += 1
        total = up + down
        ratio = round(up / total, 4) if total else None
    except Exception:
        ratio = None
    _BREADTH_CACHE.update({"ts": now, "up": up, "down": down, "ratio": ratio})
    return ratio


# ===================== M（大盘恐慌分）=====================
def _compute_m():
    parts = []
    for code, w in _M_IDX_WEIGHTS.items():
        if code == "breadth":
            continue
        chg = _idx_change_pct(code)
        # 上证兜底用 get_market_status（已带缓存）
        if chg is None and code == "sh000001":
            chg = get_market_status().get("change_pct")
        if chg is not None:
            parts.append((w, _f_pct(chg)))
    br = _fetch_breadth()
    if br is not None:
        parts.append((_M_IDX_WEIGHTS["breadth"], _g_ratio(br)))
    if not parts:
        return None
    s = sum(w * v for w, v in parts)
    tot = sum(w for w, _ in parts)
    return round(s / tot, 2)


def _refresh():
    m = _compute_m()
    with _lock:
        _M_SNAPSHOT["m"] = m
        _M_SNAPSHOT["ts"] = time.time()
        _M_SNAPSHOT["stale"] = m is None


def _build_regime(snap: dict) -> dict:
    m = snap.get("m")
    if m is None:
        tier, label = "weak", _M_TIER_LABEL["weak"]
    elif m >= 75:
        tier, label = "healthy", _M_TIER_LABEL["healthy"]
    elif m >= 50:
        tier, label = "weak", _M_TIER_LABEL["weak"]
    else:
        tier, label = "collapse", _M_TIER_LABEL["collapse"]
    return {"m": m, "tier": tier, "label": label, "ts": snap.get("ts", 0), "stale": snap.get("stale", False)}


def get_market_regime() -> dict:
    """返回当前大盘恐慌分快照 {m, tier, label, ts, stale}。

    无采集线程时也即时计算；用 single-flight 避免在列表并发(15 只)首次拉取时
    重复打网络：同一 5s 窗口内只允许一次真实刷新。
    """
    global _REFRESHING_UNTIL
    with _lock:
        snap = dict(_M_SNAPSHOT)
    now = time.time()
    need = (snap["m"] is None) or ((now - snap["ts"]) > _SNAPSHOT_TTL)
    if need:
        with _lock:
            if now < _REFRESHING_UNTIL:
                return _build_regime(dict(_M_SNAPSHOT))   # 别人正在刷新，直接用现有快照
            _REFRESHING_UNTIL = now + 5.0
        _refresh()
        with _lock:
            snap = dict(_M_SNAPSHOT)
    return _build_regime(snap)


# ===================== S（板块状态）=====================
def _sector_up_ratio(industry: str, db) -> float | None:
    """板块内样本股最近一日上涨占比(0~1)。样本<3 返回 None。"""
    try:
        from app.models import Stock, DailyQuote
        from sqlalchemy import func
        codes = [s.code for s in db.query(Stock.code).filter(Stock.industry == industry).limit(15).all()]
        if len(codes) < 3:
            return None
        latest = (db.query(DailyQuote.code, func.max(DailyQuote.date).label("d"))
                  .filter(DailyQuote.code.in_(codes)).group_by(DailyQuote.code).subquery())
        rows = (db.query(DailyQuote.code, DailyQuote.close, DailyQuote.pre_close)
                .join(latest, (DailyQuote.code == latest.c.code) & (DailyQuote.date == latest.c.d)).all())
        if len(rows) < 3:
            return None
        up = sum(1 for _, c, pc in rows if c and pc and c > pc)
        return round(up / len(rows), 4)
    except Exception:
        return None


def _sector_state(industry: str, db) -> dict:
    """板块状态 S：板块近期涨跌 + 板块内上涨占比 + 20 日趋势 → S 分(0-100) + 档位。"""
    if not industry:
        return {"s": None, "tier": "unknown", "label": "未知", "industry": industry,
                "change": None, "up_ratio": None, "trend": None}
    s_change = _sector_day_change(industry, db)
    trend = get_sector_trend(industry, db) or {}
    trend_score = {"up": 100, "flat": 50, "down": 0}.get(trend.get("trend"), 50)
    up_ratio = _sector_up_ratio(industry, db)
    comps = []
    if s_change is not None:
        comps.append((_S_CHANGE_W, _f_pct(s_change)))
    if up_ratio is not None:
        comps.append((_S_UPRATIO_W, _g_ratio(up_ratio)))
    comps.append((_S_TREND_W, trend_score))
    s = sum(w * v for w, v in comps) / sum(w for w, _ in comps)
    s = round(s, 2)
    if s >= 70:
        tier, label = "strong", "板块强"
    elif s >= 40:
        tier, label = "weak", "板块弱"
    else:
        tier, label = "collapse", "板块崩坏"
    return {"s": s, "tier": tier, "label": label, "industry": industry,
            "change": s_change, "up_ratio": up_ratio, "trend": trend.get("trend")}


# ===================== 个股属性 =====================
def _stock_beta(code: str) -> float | None:
    """个股 vs 沪深300(sh000300) 近 60 日 Beta。缓存 1 天。"""
    cached = _BETA_CACHE.get(code)
    if cached and time.time() - cached[0] < 86400:
        return cached[1]
    beta = None
    try:
        qs = ensure_quotes(code, days=60)
        s_closes = [q.close for q in qs if q.close][-60:]
        i_closes = _index_daily_closes("sh000300", 60)
        if s_closes and i_closes and len(s_closes) >= 20 and len(i_closes) >= 20:
            n = min(len(s_closes), len(i_closes))
            s_closes = s_closes[-n:]
            i_closes = i_closes[-n:]
            sr = [s_closes[i] / s_closes[i - 1] - 1 for i in range(1, n)]
            ir = [i_closes[i] / i_closes[i - 1] - 1 for i in range(1, n)]
            m_s = sum(sr) / len(sr)
            m_i = sum(ir) / len(ir)
            cov = sum((sr[i] - m_s) * (ir[i] - m_i) for i in range(len(sr))) / len(sr)
            var = sum((x - m_i) ** 2 for x in ir) / len(ir)
            if var > 1e-9:
                beta = round(cov / var, 2)
    except Exception:
        pass
    if beta is not None:
        _BETA_CACHE[code] = (time.time(), beta)
    return beta


def _stock_rs(code: str, quotes) -> float | None:
    """个股 20 日相对强度 = 个股收益 − 沪深300 收益(%)。"""
    try:
        s_ret = _stock_return(quotes, 20)
        i_ret = _index_return("sh000300", 20)
        if s_ret is not None and i_ret is not None:
            return round(s_ret - i_ret, 2)
    except Exception:
        pass
    return None


def _total_mv_yi(code: str) -> float | None:
    """总市值(亿元)。腾讯 qt.gtimg.cn f[45] 优先（已验证可用），东财 push2 兜底。缓存 1 小时。

    重要：取不到时必须返回 None，绝不可回落成 0.0 —— 否则 _mv_tier(0) 会把大市值票
    误判为「小盘」（历史 bug：京东方A 总市值 ~1974 亿被显示为小盘，进而误配风格乘数）。
    """
    cached = _MV_CACHE.get(code)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    mv = None
    # 源1：腾讯 qt —— f[45] = 总市值(亿)
    try:
        txt = _http_get(f"https://qt.gtimg.cn/q={_qt_symbol(code)}", headers={"Referer": "https://gu.qq.com/"})
        if txt and "=" in txt:
            f = txt.split("=", 1)[1].strip().strip('";\n').split("~")
            if len(f) > 45:
                v = _as_float(f[45])
                if v and v > 0:
                    mv = round(v, 1)
    except Exception:
        pass
    # 源2：东财 push2 兜底
    if mv is None:
        try:
            url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={_secid(code)}&fields=f57,f116"
            j = _em_json(url)
            d = ((j or {}).get("data") or {})
            v = _as_float(d.get("f116"))  # 总市值(元)
            if v and v > 0:
                mv = round(v / 1e8, 1)
        except Exception:
            pass
    if mv is not None:
        _MV_CACHE[code] = (time.time(), mv)
    return mv


def _mv_tier(mv) -> str | None:
    if mv is None:
        return None
    if mv < _MV_SMALL:
        return "小盘"
    if mv < _MV_MID:
        return "中盘"
    return "大盘蓝筹"


def _style_tag(industry: str, mv, beta) -> str:
    ind = (industry or "").strip()
    if ind in _DEFENSE_IND or "红利" in ind:
        return "防御"
    # 护盘权重：超大市值且非防御行业（机构重仓龙头，如比亚迪/白酒/医药龙头）
    if mv is not None and mv >= _MV_GUARDIAN and ind not in _DEFENSE_IND:
        return "护盘权重"
    if ind in _OFFENSE_IND:
        return "进攻"
    return "均衡"


# ===================== 融合：写入 track =====================
def enrich_track(out: dict, code: str, position: dict | None, db, quotes, deadline=None):
    """在 get_pool_track 的 out 上追加市场环境因子字段（不改动任何原有字段）。

    追加：market_regime / sector_regime / beta / relative_strength / mv_tier /
          style_tag / regime_adj(调节系数 + 前置过滤标记)。
    所有网络/计算均在 try 内，失败仅缺失该字段，不影响主 track。
    """
    if not out:
        return
    # 1) 大盘 M
    mreg = get_market_regime()

    # 2) 行业（优先 position，其次 DB，再次东财兜底）
    industry = (position or {}).get("industry") or ""
    if not industry:
        try:
            from app.models import Stock
            s = db.get(Stock, code)
            industry = (s.industry or "") if s else ""
        except Exception:
            industry = ""
    if not industry:
        try:
            industry = _fetch_industry_em(code)
        except Exception:
            industry = ""

    # 3) 板块 S
    sreg = _sector_state(industry, db)

    # 4) 个股属性（尊重 deadline，超时即跳过，确保主 track 不被拖垮）
    beta = rs = mv = mv_tier = style = None
    if not (deadline and df._over_deadline(deadline)):
        try:
            beta = _stock_beta(code)
        except Exception:
            pass
    if not (deadline and df._over_deadline(deadline)):
        try:
            rs = _stock_rs(code, quotes)
        except Exception:
            pass
    if not (deadline and df._over_deadline(deadline)):
        try:
            mv = _total_mv_yi(code)
            mv_tier = _mv_tier(mv)
            style = _style_tag(industry, mv, beta)
        except Exception:
            pass

    # 5) 调节系数
    m_discount = _M_DISCOUNT.get(mreg["tier"], 1.0)
    style_mult = _STYLE_MULT.get(style, 1.0) if mreg["tier"] != "healthy" else 1.0
    adj_factor = round(m_discount * style_mult, 3)

    # 6) 前置过滤标记（豆包 + DS）
    chg = out.get("change_pct") or 0.0
    block_new_long = (mreg["tier"] == "collapse")
    pulse_rebound = (mreg["tier"] in ("weak", "collapse") and chg > 0 and style == "进攻")
    flags = []
    if block_new_long:
        flags.append("环境崩坏·只守不攻·观察" if style in ("防御", "护盘权重") else "环境崩坏·屏蔽")
    if pulse_rebound:
        flags.append("脉冲反弹·不做开仓依据")

    out["market_regime"] = mreg
    out["sector_regime"] = sreg
    out["beta"] = beta
    out["relative_strength"] = rs
    out["mv_tier"] = mv_tier
    out["style_tag"] = style
    out["regime_adj"] = {
        "m_discount": m_discount,
        "style_mult": style_mult,
        "adj_factor": adj_factor,
        "block_new_long": block_new_long,
        "pulse_rebound": pulse_rebound,
        "flags": flags,
        "threshold": _M_THRESH.get(mreg["tier"], 60),
    }


# ===================== 后台采集线程 =====================
def _loop():
    while not _stop:
        try:
            _refresh()
        except Exception:
            pass
        for _ in range(60):
            if _stop:
                break
            time.sleep(1)


def start_collector():
    """启动后台采集线程（60s 刷新 M 快照）。幂等。"""
    global _thread, _stop
    if _thread and _thread.is_alive():
        return
    _stop = False
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    _refresh()  # 立即填充，避免首屏空快照


def stop_collector():
    """停止后台采集线程。"""
    global _stop
    _stop = True
