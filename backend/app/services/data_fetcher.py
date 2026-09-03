"""行情数据层：HTTP 多源直连（腾讯/东财/新浪免费实时源），akshare 兜底，演示数据最后降级。
仅对外读取公开行情；其余数据全本地。隐私原则：不向任何未授权平台上报。

数据源设计（按优先级）：
- 实时盘口：腾讯 qt.gtimg.cn（含量比/分时均价/换手率/涨跌停价）→ 东财 push2 → 新浪 hq
- 分时均价：东财 trends2（返回带均价字段）→ 腾讯分钟接口
- 日线：腾讯 fqkline（前复权）→ 东财 kline → akshare → 演示数据
"""
from __future__ import annotations
import datetime as dt
import hashlib
import random
import time

import httpx

from sqlalchemy import func

from app.config import AKSHARE_ENABLED
from app.db import SessionLocal
from app.models import DailyQuote, Stock
from app.services.interfaces import Quote

# ============ HTTP 请求基础 ============
_TIMEOUT = 8.0
_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def _http_get(url: str, headers: dict | None = None, retries: int = 2) -> str | None:
    """通用 GET，返回文本；失败返回 None。
    默认带浏览器 User-Agent，避免被公开源拒绝/断开连接；
    每次新建 httpx.Client，避免连接池复用导致的偶发断连；
    对状态码非 200 / 连接或协议异常（含 RemoteProtocolError / ConnectError）
    均重试（默认 2 次），并重试前做阶梯退避，避免对免费源瞬间打爆；
    全部失败时返回 None（保留最后一次错误便于排查），不会抛异常。
    """
    h = {"User-Agent": _DEFAULT_UA}
    if headers:
        h.update(headers)
    last_err = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(headers=h, timeout=_TIMEOUT, follow_redirects=True) as client:
                r = client.get(url)
                if r.status_code == 200 and r.text:
                    return r.text
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        # 重试前短暂退避（0.3s / 0.6s ...），降低免费源瞬时抖动导致的失败
        if attempt < retries:
            time.sleep(0.3 * (attempt + 1))
    return None


def _market_of(code: str) -> str:
    """判断市场前缀：6/9 开头→沪 sh，0/3 开头→深 sz，4/8 开头→北 bj。"""
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return "bj"


def _secid(code: str) -> str:
    """东财 secid：沪 1.xxxxxx，深 0.xxxxxx。"""
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ============ 演示数据（最后兜底）============
DEMO_STOCKS = [
    ("600000", "浦发银行", "银行", "sh"), ("601398", "工商银行", "银行", "sh"),
    ("601318", "中国平安", "保险", "sh"), ("600036", "招商银行", "银行", "sh"),
    ("000001", "平安银行", "银行", "sz"), ("600519", "贵州茅台", "白酒", "sh"),
    ("000858", "五粮液", "白酒", "sz"), ("600887", "伊利股份", "食品饮料", "sh"),
    ("000333", "美的集团", "家电", "sz"), ("000651", "格力电器", "家电", "sz"),
    ("600276", "恒瑞医药", "医药", "sh"), ("300760", "迈瑞医疗", "医药", "sz"),
    ("002594", "比亚迪", "汽车", "sz"), ("601012", "隆基绿能", "光伏", "sh"),
    ("300750", "宁德时代", "新能源", "sz"), ("600900", "长江电力", "电力", "sh"),
    ("000725", "京东方A", "面板", "sz"), ("600703", "三安光电", "半导体", "sh"),
    ("002415", "海康威视", "安防", "sz"), ("600585", "海螺水泥", "建材", "sh"),
    ("601888", "中国中免", "免税", "sh"), ("600030", "中信证券", "券商", "sh"),
    ("000063", "中兴通讯", "通信", "sz"), ("600009", "上海机场", "交运", "sh"),
    ("002230", "科大讯飞", "AI", "sz"), ("688981", "中芯国际", "半导体", "sh"),
    ("603259", "药明康德", "医药", "sh"), ("600570", "恒生电子", "金融科技", "sh"),
    ("000568", "泸州老窖", "白酒", "sz"), ("601628", "中国人寿", "保险", "sh"),
    ("600104", "上汽集团", "汽车", "sh"), ("002475", "立讯精密", "消费电子", "sz"),
    ("300059", "东方财富", "券商", "sz"), ("600406", "国电南瑞", "电力", "sh"),
    ("000002", "万科A", "地产", "sz"), ("600048", "保利发展", "地产", "sh"),
    ("601899", "紫金矿业", "有色", "sh"), ("002142", "宁波银行", "银行", "sz"),
    ("600588", "用友网络", "软件", "sh"), ("300124", "汇川技术", "工控", "sz"),
]


def seed_stock_universe() -> None:
    db = SessionLocal()
    try:
        if db.query(func.count(Stock.code)).scalar() > 0:
            return
        for code, name, ind, mkt in DEMO_STOCKS:
            db.add(Stock(code=code, name=name, industry=ind, market=mkt))
        db.commit()
    finally:
        db.close()


def _demo_seed(code: str) -> random.Random:
    h = hashlib.md5(code.encode()).hexdigest()
    return random.Random(int(h, 16))


def generate_demo_quotes(code: str, days: int = 180) -> list[Quote]:
    """确定性随机游走生成演示日线（每只股票固定形态）。"""
    rng = _demo_seed(code)
    base = rng.uniform(8, 45)
    price = base
    today = dt.date.today()
    out: list[Quote] = []
    drift = rng.uniform(-0.0005, 0.0015)
    for i in range(days):
        d = today - dt.timedelta(days=days - i)
        if d.weekday() >= 5:
            continue
        vol = rng.uniform(0.01, 0.05)
        change = rng.gauss(drift, vol)
        price = max(1.0, price * (1 + change))
        o = price * (1 + rng.gauss(0, 0.01))
        c = price
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.012)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.012)))
        pre = out[-1].close if out else c
        turnover = rng.uniform(0.5, rng.choice([3, 6, 12, 25]))
        out.append(Quote(
            code=code, date=d.isoformat(), open=round(o, 2), high=round(h, 2),
            low=round(l, 2), close=round(c, 2), volume=rng.uniform(1e5, 5e6),
            amount=rng.uniform(1e8, 5e8), turnover=round(turnover, 2), pre_close=round(pre, 2),
        ))
    return out


# ============ 日线：腾讯 fqkline 优先 ============
def _fetch_daily_tencent(code: str, days: int) -> list[Quote] | None:
    """腾讯日线（前复权）。返回 [{date, open, close, high, low, volume}] 升序。
    注意:必须用 ifzq.gtimg.cn(无 web 前缀)——web.ifzq.gtimg.cn 会被腾讯 WAF 501 拦截。"""
    mkt = _market_of(code)
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt}{code},day,,,{days},qfq"
    txt = _http_get(url, headers={"Referer": "https://gu.qq.com/"})
    if not txt:
        return None
    try:
        import json
        data = json.loads(txt).get("data", {}).get(f"{mkt}{code}", {})
        # qfqday 或 day 键
        rows = data.get("qfqday") or data.get("day")
        if not rows:
            return None
        out: list[Quote] = []
        prev_close = None
        for r in rows:
            # [日期, 开, 收, 高, 低, 量] —— 注意顺序：开/收/高/低
            date, o, c, h, l, v = r[0], _as_float(r[1]), _as_float(r[2]), _as_float(r[3]), _as_float(r[4]), _as_float(r[5])
            out.append(Quote(code=code, date=str(date), open=o, high=h, low=l, close=c,
                             volume=v, amount=0.0, turnover=0.0,
                             pre_close=prev_close if prev_close is not None else c))
            prev_close = c
        return out[-days:]
    except Exception:
        return None


def _fetch_daily_eastmoney(code: str, days: int) -> list[Quote] | None:
    """东财日线（前复权），腾讯失败时兜底。"""
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt=101&fqt=1&end=20500101&lmt={days}")
    txt = _http_get(url)
    if not txt:
        return None
    try:
        import json
        data = json.loads(txt).get("data")
        klines = (data or {}).get("klines") or []
        if not klines:
            return None
        out: list[Quote] = []
        prev_close = None
        for k in klines:
            # "2026-08-24,7.68,7.58,7.68,7.48,159666,120732437.9,0.37" → 日期,开,收,高,低,量,额,振幅
            parts = k.split(",")
            date, o, c, h, l = parts[0], _as_float(parts[1]), _as_float(parts[2]), _as_float(parts[3]), _as_float(parts[4])
            v, amt = _as_float(parts[5]), _as_float(parts[6])
            out.append(Quote(code=code, date=str(date), open=o, high=h, low=l, close=c,
                             volume=v, amount=amt, turnover=0.0,
                             pre_close=prev_close if prev_close is not None else c))
            prev_close = c
        return out[-days:]
    except Exception:
        return None


def _fetch_akshare(code: str, days: int) -> list[Quote] | None:
    """akshare 日线（最后兜底，已被 HTTP 直连替代为备选）。"""
    if not AKSHARE_ENABLED:
        return None
    try:
        import akshare as ak  # 懒加载，缺失不影响核心
        df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq",
                                start_date=(dt.date.today() - dt.timedelta(days=days + 30)).strftime("%Y%m%d"))
        if df is None or df.empty:
            return None
        out: list[Quote] = []
        prev_close = None
        for _, r in df.iterrows():
            close = float(r["收盘"])
            out.append(Quote(
                code=code, date=str(r["日期"]), open=float(r["开盘"]), high=float(r["最高"]),
                low=float(r["最低"]), close=close, volume=float(r["成交量"]),
                amount=float(r["成交额"]), turnover=float(r.get("换手率", 0) or 0),
                pre_close=prev_close if prev_close is not None else float(r.get("昨收", close)),
            ))
            prev_close = close
        return out[-days:]
    except Exception:
        return None


def ensure_quotes(code: str, days: int = 180) -> list[Quote]:
    """取日线：HTTP 直连（腾讯→东财）优先 → DB 缓存 → akshare → 演示数据。
    直连成功会写回缓存（覆盖旧演示数据）；直连失败才回退缓存/演示。"""
    db = SessionLocal()
    try:
        # 1. HTTP 直连真实日线（盘间保证最新，且覆盖演示缓存）
        # 腾讯 kline 不含成交额,若日线需要金额类指标(如日均成交额),优先尝试东财接口。
        quotes = _fetch_daily_tencent(code, days)
        if quotes and all(q.amount == 0 for q in quotes):
            east = _fetch_daily_eastmoney(code, days)
            if east:
                quotes = east
        if not quotes:
            quotes = _fetch_daily_eastmoney(code, days)
        if quotes:
            # 写回缓存（先清旧，避免唯一约束重复 + 覆盖旧演示数据）
            db.query(DailyQuote).filter(DailyQuote.code == code).delete()
            for q in quotes:
                db.add(DailyQuote(code=q.code, date=q.date, open=q.open, high=q.high, low=q.low,
                                  close=q.close, volume=q.volume, amount=q.amount,
                                  turnover=q.turnover, pre_close=q.pre_close))
            db.commit()
            return quotes[-days:]

        # 2. 直连失败 → DB 缓存（可能是历史真实数据）
        rows = (db.query(DailyQuote).filter(DailyQuote.code == code)
                .order_by(DailyQuote.date).all())
        if rows and len(rows) >= max(20, days // 2):
            return [Quote(code=r.code, date=r.date, open=r.open, high=r.high, low=r.low,
                          close=r.close, volume=r.volume, amount=r.amount,
                          turnover=r.turnover, pre_close=r.pre_close) for r in rows[-days:]]

        # 3. akshare 兜底
        quotes = _fetch_akshare(code, days)
        if quotes:
            db.query(DailyQuote).filter(DailyQuote.code == code).delete()
            for q in quotes:
                db.add(DailyQuote(code=q.code, date=q.date, open=q.open, high=q.high, low=q.low,
                                  close=q.close, volume=q.volume, amount=q.amount,
                                  turnover=q.turnover, pre_close=q.pre_close))
            db.commit()
            return quotes[-days:]

        # 4. 演示数据（所有真实源都不可用）
        quotes = generate_demo_quotes(code, days)
        db.query(DailyQuote).filter(DailyQuote.code == code).delete()
        for q in quotes:
            db.add(DailyQuote(code=q.code, date=q.date, open=q.open, high=q.high, low=q.low,
                              close=q.close, volume=q.volume, amount=q.amount,
                              turnover=q.turnover, pre_close=q.pre_close))
        db.commit()
        return quotes[-days:]
    finally:
        db.close()


def _get_cached_quotes(code: str, days: int = 180) -> list[Quote]:
    """只读 daily_quotes 缓存(不调 HTTP/akshare)。用于 screener 等需要快速跑全市场的场景。
    无缓存或缓存不足 20 根 → 返回 [],由调用方跳过。"""
    db = SessionLocal()
    try:
        rows = (db.query(DailyQuote).filter(DailyQuote.code == code)
                .order_by(DailyQuote.date).all())
        if not rows or len(rows) < 20:
            return []
        return [Quote(code=r.code, date=r.date, open=r.open, high=r.high, low=r.low,
                      close=r.close, volume=r.volume, amount=r.amount,
                      turnover=r.turnover, pre_close=r.pre_close) for r in rows[-days:]]
    finally:
        db.close()


def get_sector_trend(industry: str, db_session=None) -> dict:
    """行业板块趋势：用该行业样本股近 20 日平均收盘价斜率近似。
    只读本地 daily_quotes 缓存,不触发网络请求(避免选股/推荐时被打爆)。"""
    own = db_session is None
    db = db_session or SessionLocal()
    try:
        codes = [s.code for s in db.query(Stock.code).filter(Stock.industry == industry).all()]
        if not codes:
            return {"industry": industry, "trend": "unknown", "slope": 0.0}
        slopes = []
        for c in codes[:8]:
            try:
                qs = _get_cached_quotes(c, 20)
            except Exception:
                continue
            if len(qs) >= 10:
                closes = [q.close for q in qs]
                slopes.append((closes[-1] - closes[0]) / closes[0])
        avg = sum(slopes) / len(slopes) if slopes else 0.0
        trend = "up" if avg > 0.01 else ("down" if avg < -0.01 else "flat")
        return {"industry": industry, "trend": trend, "slope": round(avg, 4)}
    finally:
        if own:
            db.close()


# ============ 实时盘口 / 分时数据（做T分析页专用）============
# 设计：HTTP 直连真实优先（腾讯 qt / 东财 push2 / 新浪 hq 互备），失败降级演示数据。
# 为防频繁刷新打爆免费源，实时快照做 60 秒内存缓存。
_SPOT_CACHE: dict[str, tuple[float, dict]] = {}
_SPOT_TTL = 30.0  # 30 秒缓存：盘间保证近实时，又不频繁打源


def _demo_seed2(code: str) -> random.Random:
    h = hashlib.md5(("rt" + code + dt.date.today().isoformat()).encode()).hexdigest()
    return random.Random(int(h, 16))


def _demo_ticks(code: str, open0: float, current: float, n: int = 48):
    rng = _demo_seed2(code)
    prices, vols = [], []
    for i in range(n):
        t = i / (n - 1)
        base = open0 + (current - open0) * t
        p = base * (1 + rng.gauss(0, 0.004))
        prices.append(round(p, 2))
        vols.append(rng.uniform(200, 5000))
    return prices, vols


def _fetch_spot_tencent(code: str) -> dict | None:
    """腾讯实时盘口。字段：现价/昨收/今开/成交量/涨跌额/涨跌幅/今高/今低/成交额(万)/换手率/量比/均价/振幅/涨停/跌停。"""
    mkt = _market_of(code)
    txt = _http_get(f"https://qt.gtimg.cn/q={mkt}{code}", headers={"Referer": "https://gu.qq.com/"})
    if not txt or "=" not in txt:
        return None
    try:
        payload = txt.split("=", 1)[1].strip().strip('";\n')
        f = payload.split("~")
        if len(f) < 50:
            return None
        price = _as_float(f[3])
        pre_close = _as_float(f[4])
        open0 = _as_float(f[5])
        change = _as_float(f[31]) if len(f) > 31 else round(price - pre_close, 2)
        change_pct = _as_float(f[32]) if len(f) > 32 else round((price - pre_close) / pre_close * 100 if pre_close else 0, 2)
        high = _as_float(f[33]) if len(f) > 33 else price
        low = _as_float(f[34]) if len(f) > 34 else price
        amount_wan = _as_float(f[37]) if len(f) > 37 else 0.0
        turnover = _as_float(f[38]) if len(f) > 38 else 0.0
        vol_ratio = _as_float(f[49]) if len(f) > 49 else 0.0
        avg_price = _as_float(f[51]) if len(f) > 51 else price
        volume = _as_float(f[36]) if len(f) > 36 else _as_float(f[6])    # 单位: 股
        volume_wan = volume / 1e6 if volume else 0.0                          # 万手 (1手=100股)
        return {
            "name": f[1], "price": price, "pre_close": pre_close, "open": open0,
            "change": change, "change_pct": change_pct, "high": high, "low": low,
            "volume": volume, "volume_wan": volume_wan, "amount": amount_wan * 1e4,
            "turnover": turnover,
            "vol_ratio": vol_ratio, "avg_price": avg_price,
            "ts": f[30] if len(f) > 30 else "",
            "src": "tencent",
        }
    except Exception:
        return None


def _fetch_spot_eastmoney(code: str) -> dict | None:
    """东财 push2 实时盘口（腾讯失败时兜底）。"""
    url = (f"https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={_secid(code)}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f170,f171,f86")
    txt = _http_get(url)
    if not txt:
        return None
    try:
        import json
        d = (json.loads(txt).get("data") or {})
        if not d:
            return None
        price = _as_float(d.get("f43")) / 100
        vol_raw = _as_float(d.get("f47"))                          # 单位: 手(东财成交量的字段约定: 手)
        return {
            "name": d.get("f58", ""), "price": price,
            "pre_close": _as_float(d.get("f60")) / 100,
            "open": _as_float(d.get("f46")) / 100,
            "change": _as_float(d.get("f170")) / 100,
            "change_pct": _as_float(d.get("f171")) / 100,
            "high": _as_float(d.get("f44")) / 100,
            "low": _as_float(d.get("f45")) / 100,
            "volume": vol_raw, "volume_wan": vol_raw / 1e4 if vol_raw else 0.0,
            "amount": _as_float(d.get("f48")),
            "turnover": _as_float(d.get("f168")) / 100,
            "vol_ratio": _as_float(d.get("f50")) / 100,
            "avg_price": price, "ts": str(d.get("f86", "")), "src": "eastmoney",
        }
    except Exception:
        return None


def _fetch_spot_sina(code: str) -> dict | None:
    """新浪 hq 实时盘口（最后兜底）。"""
    mkt = _market_of(code)
    txt = _http_get(f"https://hq.sinajs.cn/list={mkt}{code}",
                    headers={"Referer": "https://finance.sina.com.cn/"})
    if not txt or '="' not in txt:
        return None
    try:
        payload = txt.split('="', 1)[1].strip().strip('";\n')
        f = payload.split(",")
        if len(f) < 10:
            return None
        name = f[0]
        open0 = _as_float(f[1]); pre_close = _as_float(f[2]); price = _as_float(f[3])
        high = _as_float(f[4]); low = _as_float(f[5]); volume = _as_float(f[8]); amount = _as_float(f[9])
        volume_wan = volume / 1e4 if volume else 0.0  # 新浪 hq 也是「股」单位, /1e4 → 万手
        return {
            "name": name, "price": price, "pre_close": pre_close, "open": open0,
            "change": round(price - pre_close, 2),
            "change_pct": round((price - pre_close) / pre_close * 100, 2) if pre_close else 0,
            "high": high, "low": low, "volume": volume, "volume_wan": volume_wan,
            "amount": amount,
            "turnover": 0.0, "vol_ratio": 0.0, "avg_price": price,
            "ts": f[30] if len(f) > 30 else "", "src": "sina",
        }
    except Exception:
        return None


def _fetch_spot_direct(code: str) -> dict | None:
    """多源直连实时盘口（腾讯→东财→新浪）。"""
    for fn in (_fetch_spot_tencent, _fetch_spot_eastmoney, _fetch_spot_sina):
        try:
            data = fn(code)
            if data and data.get("price", 0) > 0:
                return data
        except Exception:
            continue
    return None


def _fetch_intraday_tencent(code: str):
    """腾讯分时。返回 (prices, vols, avg_price)。
    每根数据: "0930 7.68 553 424704.00" = 时间 价格 量(手) 累计额(元)。
    均价 = 最后一根累计额 / 累计成交量(手*100股)。"""
    mkt = _market_of(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={mkt}{code}"
    txt = _http_get(url, headers={"Referer": "https://gu.qq.com/"})
    if not txt:
        return None
    try:
        import json
        node = json.loads(txt)["data"][f"{mkt}{code}"]["data"]["data"]
        prices, vols = [], []
        for line in node:
            parts = line.split()
            if len(parts) >= 3:
                prices.append(_as_float(parts[1]))
                vols.append(_as_float(parts[2]))  # 累计量(手)
        if not prices:
            return None
        # 腾讯分时第三/四列是"累计量(手)/累计额(元)"，直接用最后一根算均价
        last_line = node[-1].split()
        cum_vol = _as_float(last_line[2]) if len(last_line) >= 3 else 0.0
        cum_amount = _as_float(last_line[3]) if len(last_line) >= 4 else 0.0
        avg_price = round(cum_amount / (cum_vol * 100.0), 2) if cum_vol else prices[-1]
        # 换算成每根增量量（供画分时量用，非累计）
        vols = [vols[0]] + [max(0.0, vols[i] - vols[i - 1]) for i in range(1, len(vols))]
        return prices, vols, avg_price
    except Exception:
        return None


def _fetch_intraday_eastmoney(code: str, ndays: int = 1):
    """东财分时（trends2），腾讯失败时兜底。返回 (prices, vols, avg_price, times, amounts)。
    最后两个字段仅在调用方需要时解析，兼容旧的 `prices, vols, avg_price = ...` 写法。"""
    url = (f"https://push2his.eastmoney.com/api/qt/stock/trends2/get"
           f"?secid={_secid(code)}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays={ndays}")
    txt = _http_get(url)
    if not txt:
        return None
    try:
        import json
        data = (json.loads(txt).get("data") or {})
        trends = data.get("trends") or []
        if not trends:
            return None
        # 每行: "时间,开,收,高,低,量,额,均价"
        prices, vols, times, amounts = [], [], [], []
        for t in trends:
            parts = t.split(",")
            if len(parts) >= 8:
                times.append(parts[0])
                prices.append(_as_float(parts[2]))  # 收(现价)
                vols.append(_as_float(parts[5]))    # 成交量
                amounts.append(_as_float(parts[6])) # 成交额(累计)
        if not prices:
            return None
        avg_price = _as_float(trends[-1].split(",")[7])
        return prices, vols, avg_price, times, amounts
    except Exception:
        return None


# 分时数据短缓存：避免可投池批量刷新时对同一标的重复请求东财
_INTRADAY_CACHE: dict[str, tuple[float, tuple]] = {}
_INTRADAY_CACHE_TTL = 30  # 秒


def _fetch_intraday_cached(code: str, ndays: int = 2) -> tuple | None:
    """带短缓存的东财分时获取，减少批量调用时的请求频率。"""
    key = f"{code}:{ndays}"
    now = dt.datetime.now().timestamp()
    cached = _INTRADAY_CACHE.get(key)
    if cached and (now - cached[0]) < _INTRADAY_CACHE_TTL:
        return cached[1]
    res = _fetch_intraday_eastmoney(code, ndays=ndays)
    if res:
        _INTRADAY_CACHE[key] = (now, res)
    return res


def _fetch_sina_minute_range(code: str, days_back: int = 10) -> list[tuple[str, int, float]] | None:
    """新浪分钟级 K 线。返回 [(date_str, hm_int, cum_amount_yuan), ...] 或 None。

    说明(2026-09-03):
      旧实现用腾讯 web.ifzq.gtimg.cn/appstock/app/minute/query?date=YYYY-MM-DD 取「T-1 同期累计成交额」，
      但经实测验证: 该接口完全不识别 date 参数, 无论传哪天都返回当天实时分时,
      导致 _yesterday_amount_at_time 拿到的就是「今天的实时累计」, 与前一天应拿到的完全不同,
      表现就是用户在屏幕上看到的「T-1 同期成交额 == 当天实时成交额」。

    改用新浪 quotes.sina.cn 的 K 线接口:
      - scale=1 (1 分钟线), datalen 足够取 N 天。
      - 数据格式: [{day: "YYYY-MM-DD HH:MM:00", amount: "该分钟成交额(元)"...}, ...]
      - 单根 amount 是「该分钟成交额」而非累计, 本函数内部把它累加成 cum_amount_yuan。
      - day 字段带日期, 可以按日期切分到具体某天。

    缓存：_INTRADAY_CACHE（30s, 同其它分钟接口, 避免连续刷新打爆新浪）。
    """
    mkt = _market_of(code)  # sh/sz/bj, 新浪也是同样前缀
    sym = f"{mkt}{code}"
    datalen = max(days_back * 240 + 30, 800)
    url = (
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php/="
        f"/CN_MarketDataService.getKLineData?symbol={sym}&scale=1&datalen={datalen}"
    )
    txt = _http_get(url, headers={"Referer": "https://quotes.sina.cn/"})
    if not txt:
        return None
    try:
        import json as _json, re as _re
        # 去掉 jsonp 包装 =(...); 或 =([...]);
        s = txt.strip()
        # =(...) 格式
        if s.startswith("=("):
            body = s[2:-2] if s.endswith(");") else s[2:]
        else:
            m = _re.search(r'=\(\[', s)
            body = s[m.end() - 1:-2] if m else s
        arr = _json.loads(body)
        if not isinstance(arr, list) or not arr:
            return None
        out: list[tuple[str, int, float, float]] = []  # (date, hm, cum_amount_yuan, cum_volume_hand)
        cum_amt = 0.0
        cum_vol = 0.0
        for a in arr:
            date_str = str(a.get("day", ""))  # "2026-09-02 09:45:00"
            if not date_str or len(date_str) < 16:
                continue
            amt = _as_float(a.get("amount", 0))
            vol = _as_float(a.get("volume", 0))  # 手(单根)
            cum_amt += amt
            cum_vol += vol
            # hm_int: HHMM 整数, 例 09:45 -> 945
            hm = int(date_str[11:13]) * 100 + int(date_str[14:16])
            out.append((date_str[:10], hm, cum_amt, cum_vol))
        return out if out else None
    except Exception:
        return None


def _yesterday_amount_at_time(code: str) -> float | None:
    """[兼容保留] 取前一交易日「同样 30 分钟整数点」的累计成交额（元）。

    新版逻辑改到 _yesterday_day_metrics()，统一缓存 + 一次拉新浪数据返回 4 项指标
    （amount_at_time / volume_at_time / amount_total / volume_total）。本函数委托之。
    """
    m = _yesterday_day_metrics(code)
    return m.get("amount_at_time") if m else None


def _yesterday_volume_at_time(code: str) -> float | None:
    """[新增] 前一交易日同时点的累计成交量（手）。"""
    m = _yesterday_day_metrics(code)
    return m.get("volume_at_time") if m else None


def _yesterday_volume_total(code: str) -> float | None:
    """[新增] 前一交易日全天累计成交量（手）= 15:00 收盘那一刻的累计成交量。

    口径：
      - 数据源 = 新浪分时 minute 接口（与 _yesterday_amount_at_time 共享缓存）
      - 取 T-1 日所有 1 分钟点中最后一根（理论上就是 15:00）的累计 volume
      - 若 T-1 当天没有数据（极端长假） -> 返回 None
    """
    m = _yesterday_day_metrics(code)
    return m.get("volume_total") if m else None


def _yesterday_day_metrics(code: str) -> dict | None:
    """取前一交易日的 4 项分钟级指标（统一一次新浪请求 + 缓存复用）。

    返回（无数据时 None）：
      {
        'date':                'YYYY-MM-DD',
        'amount_at_time':  float,   # 元，target_hm 时刻的累计成交额
        'volume_at_time':  float,   # 手，target_hm 时刻的累计成交量
        'amount_total':    float,   # 元，T-1 收盘后的累计成交额
        'volume_total':    float,   # 手，T-1 收盘后的累计成交量
        'target_hm':       int,     # HHMM，按 30 分钟向下取整的"目标时点"
        'market_phase':    'pre' | 'live' | 'post',  # 用于排查/前端展示
      }

    口径（2026-09-03 调整 + 2026-09-03 重构）:
      - 目标时点 = 当前时间按 30 分钟向下取整, 但下限 09:30（开盘整点）、上限 15:00。
        - 当前 < 09:30（盘前）  → target 强制 09:30
        - 当前 09:30 ~ 15:00   → target = (cur // 30) * 30
        - 当前 >= 15:00（盘后） → target 强制 15:00
      - "前一交易日" = 日期最大的 date < today 且 date 不在周末
        （新浪分时 day 字段带日期, 直接按字符串字典序比较即可, 跳过周末）
      - amount_at_time = T-1 日 ≤ target_hm 最近的 1 个点的累计成交额
      - volume_at_time = 上面这个点的累计成交量（手）
      - amount_total / volume_total = T-1 日最后一根（理论上 15:00）的累计值
        若 T-1 在 1 分钟粒度下最后一根不是 15:00（如停牌），就取 day_pairs[-1]

    数据源：新浪 quotes.sina.cn K 线（内部已把单根 amount/volume 累加成累计）。
    缓存：_INTRADAY_CACHE (30s)。
    """
    now = dt.datetime.now()
    cur_min = now.hour * 60 + now.minute
    target_min = (cur_min // 30) * 30
    market_open_min = 9 * 60 + 30    # 570
    market_close_min = 15 * 60       # 900
    phase = "live"
    # 边界修正: 15:00 整点算 post(不能 < close 严格大于, 而是 >= close)
    if cur_min < market_open_min:
        target_min = market_open_min
        phase = "pre"
    elif cur_min >= market_close_min:
        target_min = market_close_min
        phase = "post"
    target_hm = (target_min // 60) * 100 + (target_min % 60)

    # 一次性拉最近 10 天的分钟线，新接口返回 4-tuple
    cache_key = ("sina_min_yest", code)
    cached = _INTRADAY_CACHE.get(cache_key)
    if cached and (now.timestamp() - cached[0]) < _INTRADAY_CACHE_TTL:
        triples = cached[1]
    else:
        triples = _fetch_sina_minute_range(code, days_back=10)
        if triples:
            _INTRADAY_CACHE[cache_key] = (now.timestamp(), triples)

    if not triples:
        return None

    today = now.date().isoformat()
    # 按日期分组，存 (hm, cum_amount, cum_volume)
    by_date: dict[str, list[tuple[int, float, float]]] = {}
    for date_str, hm, cum_amt, cum_vol in triples:
        if date_str >= today:
            continue  # 跳过今天及以后的数据
        by_date.setdefault(date_str, []).append((hm, cum_amt, cum_vol))

    candidates = [
        d for d in sorted(by_date.keys(), reverse=True)
        if d < today and dt.datetime.strptime(d, "%Y-%m-%d").weekday() < 5
    ]
    if not candidates:
        return None
    target_date = candidates[0]
    day_pairs = by_date[target_date]
    if not day_pairs:
        return None

    # 1) at_time：≤ target_hm 最近的一个点的累计值
    best_amt: float | None = None
    best_vol: float | None = None
    best_diff: int | None = None
    for hm, cum_amt, cum_vol in day_pairs:
        point_min = (hm // 100) * 60 + (hm % 100)
        if point_min > target_min:
            continue
        diff = target_min - point_min
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_amt = cum_amt
            best_vol = cum_vol
    if best_amt is None:  # 兜底：取当日首点（开盘瞬间）
        best_amt = day_pairs[0][1]
        best_vol = day_pairs[0][2]

    # 2) total：最后一根的累计值
    total_amt = day_pairs[-1][1]
    total_vol = day_pairs[-1][2]

    return {
        "date": target_date,
        "amount_at_time": best_amt,
        "volume_at_time": best_vol,
        "amount_total": total_amt,
        "volume_total": total_vol,
        "target_hm": target_hm,
        "market_phase": phase,
    }


def _fetch_intraday(code: str):
    """分时多源互备：腾讯 → 东财。"""
    return _fetch_intraday_tencent(code) or _fetch_intraday_eastmoney(code)


def get_t_realtime(code: str, daily_quotes: list) -> dict:
    """聚合做T页所需的实时数据：HTTP 直连真实优先，失败降级演示。

    关键修复：腾讯实时盘口 price 是「未复权」原始价，而日线/箱体/MA 都是「前复权」价。
    若直接把未复权价拿来做 T 判断，除权股会出现「现价 6.5 元、箱体支撑 10 元」的荒诞结论。
    因此本函数会把实时盘口的价格序列按「日线最新 close / 实时 price」做前复权修正，
    使日内指标(现价/均价/今高今低)与日线 MA/箱体同口径。

    返回：{ source, price, change, change_pct, open, high, low, amount,
           turnover, vol_ratio, pre_close, avg_price, today_high, today_low,
           realtime_confidence, realtime_diff_pct, ts }
    """
    # 缓存
    cached = _SPOT_CACHE.get(code)
    now = dt.datetime.now().timestamp()
    if cached and (now - cached[0]) < _SPOT_TTL:
        return cached[1]

    spot = _fetch_spot_direct(code)
    # 日线最新收盘价(前复权),用于给实时盘口做复权修正
    last_daily = daily_quotes[-1] if daily_quotes else None
    last_close = last_daily.close if last_daily else 0.0

    def _adjust_spot(spot_dict: dict, factor: float) -> None:
        """把 spot 里的价格字段统一乘以复权因子,并重新计算涨跌额/涨跌幅。"""
        keys = ["price", "open", "high", "low", "pre_close"]
        for k in keys:
            if k in spot_dict and spot_dict[k] is not None:
                spot_dict[k] = round(spot_dict[k] * factor, 3)
        # 重新计算涨跌额/涨跌幅(基于复权后的 price/pre_close)
        spot_dict["change"] = round(spot_dict["price"] - spot_dict["pre_close"], 3)
        if spot_dict["pre_close"]:
            spot_dict["change_pct"] = round(spot_dict["change"] / spot_dict["pre_close"] * 100, 2)

    def _confidence(diff_pct: float) -> str:
        if diff_pct < 5:
            return "ok"
        if diff_pct < 20:
            return "adjusted"  # 轻微除权,已修正
        if diff_pct < 50:
            return "diverged"  # 明显除权或数据源异常,已修正
        return "abnormal"      # 数据源异常,已修正但需谨慎

    if spot is None:
        # ===== 演示降级：用日线最后一根推导 =====
        pre_close = last_daily.pre_close if last_daily else 10.0
        rng = _demo_seed2(code)
        open0 = pre_close * (1 + rng.uniform(-0.01, 0.01))
        current = pre_close * (1 + rng.uniform(-0.03, 0.03))
        prices, vols = _demo_ticks(code, open0, current)
        today_high = round(max(prices + [current]), 2)
        today_low = round(min(prices + [current]), 2)
        avg = sum(p * v for p, v in zip(prices, vols)) / sum(vols)
        spot = {
            "name": "", "price": round(current, 2), "change": round(current - pre_close, 2),
            "change_pct": round((current - pre_close) / pre_close * 100, 2),
            "open": round(open0, 2), "high": today_high, "low": today_low,
            "amount": rng.uniform(5e7, 2e9), "turnover": rng.uniform(0.3, 12),
            "vol_ratio": round(rng.uniform(0.3, 2.5), 2), "pre_close": round(pre_close, 2),
            "avg_price": round(avg, 2), "ts": "", "src": "demo",
        }
        source = "demo"
        avg_price = spot["avg_price"]
        confidence, diff_pct = "ok", 0.0
    else:
        # ===== 真实数据：先判断是否需要复权修正 =====
        factor = 1.0
        if last_close > 0 and spot.get("price", 0) > 0:
            diff_pct = abs(spot["price"] - last_close) / last_close * 100
            confidence = _confidence(diff_pct)
            if diff_pct >= 5:  # 差异 ≥5% 认为盘口是未复权,做前复权修正
                factor = last_close / spot["price"]
                _adjust_spot(spot, factor)
            else:
                confidence = "ok"
                diff_pct = 0.0
        else:
            confidence, diff_pct = "ok", 0.0

        # ===== 分时均价：分时明细也是未复权价,需要同步复权 =====
        source = spot["src"]
        intra = _fetch_intraday(code)
        if intra:
            _, _, avg_price = intra
            avg_price = round(avg_price * factor, 2)
            today_high, today_low = spot["high"], spot["low"]
        else:
            avg_price = round((spot.get("avg_price") or spot["price"]) * factor, 2)
            today_high, today_low = spot["high"], spot["low"]

    result = {
        "source": source, "name": spot.get("name", ""),
        "price": spot["price"], "change": spot["change"], "change_pct": spot["change_pct"],
        "open": spot["open"], "high": spot["high"], "low": spot["low"],
        "amount": spot["amount"], "turnover": spot["turnover"],
        "volume": spot.get("volume", 0),  # 成交量(手),用于「今日 vs 上一交易日」对比
        "vol_ratio": spot["vol_ratio"], "pre_close": spot["pre_close"],
        "avg_price": avg_price, "today_high": today_high, "today_low": today_low,
        "realtime_confidence": confidence,
        "realtime_diff_pct": round(diff_pct, 1),
        "ts": spot.get("ts", ""),
    }
    _SPOT_CACHE[code] = (now, result)
    return result


# ============ 大盘/市场风险过滤（持仓风控用）============
_INDEX_CACHE: dict[str, tuple[float, dict]] = {}
_INDEX_TTL = 60.0


def get_market_status() -> dict:
    """上证指数实时涨跌，用于“大盘环境过滤”。
    返回：{name, price, change_pct, level, level_label}
    level: red(大跌<1.5%) / yellow(跌0.5~1.5%) / green(正常)"""
    cached = _INDEX_CACHE.get("sh000001")
    now = dt.datetime.now().timestamp()
    if cached and (now - cached[0]) < _INDEX_TTL:
        return cached[1]
    data = {"name": "上证指数", "price": 0.0, "change_pct": 0.0, "level": "green", "level_label": "正常"}
    try:
        txt = _http_get("https://qt.gtimg.cn/q=sh000001", headers={"Referer": "https://gu.qq.com/"})
        if txt and "=" in txt:
            f = txt.split("=", 1)[1].strip().strip('";\n').split("~")
            if len(f) > 32:
                price = _as_float(f[3])
                chg = _as_float(f[32])  # 涨跌幅%
                if chg <= -1.5:
                    level, label = "red", "大盘大跌"
                elif chg < -0.5:
                    level, label = "yellow", "大盘偏弱"
                else:
                    level, label = "green", "大盘正常"
                data = {"name": f[1], "price": price, "change_pct": chg,
                        "level": level, "level_label": label}
    except Exception:
        pass
    _INDEX_CACHE["sh000001"] = (now, data)
    return data


def ensure_stock_name(code: str, db) -> bool:
    """补全 stocks 表的 name 字段。

    适用场景:用户从「个股分析」页或可投池直接落持仓时,代码可能不在 stocks 表里,
    或虽然有记录但 name 为空(早期 seed 数据不全)。该函数从实时盘口(腾讯/东财/新浪三源)
    拿一次名称,upsert 回 stocks 表,后续 list 即可直接展示。

    返回 True=已写回(含原本就有),False=三源全失败(保持原状)。
    _SPOT_CACHE 30s 缓存可避免短期内重复拉网络。
    """
    from app.models import Stock
    s = db.get(Stock, code)
    if s and (s.name or "").strip():
        return True
    spot = _fetch_spot_direct(code)
    if not spot or not (spot.get("name") or "").strip():
        return False
    name = spot["name"].strip()
    if s:
        s.name = name
    else:
        db.add(Stock(code=code, name=name))
    db.commit()
    return True


# ============== 短线可投池「每日跟踪」聚合（实时 + 日线） ==============
# 60s 内存缓存,避免每次列表都拉网络。失败时返回空 dict,由前端降级显示。
_POOL_TRACK_CACHE: dict[str, tuple[float, dict]] = {}
_POOL_TRACK_TTL = 60


def _ema(values: list[float], n: int) -> list[float]:
    """指数移动平均。"""
    if len(values) < n:
        return []
    mult = 2.0 / (n + 1)
    ema = [sum(values[:n]) / n]
    for v in values[n:]:
        ema.append((v - ema[-1]) * mult + ema[-1])
    return ema


def _macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    """返回 dif/dea/hist 序列。"""
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if not ema12 or not ema26 or len(ema12) < len(ema26):
        return [], [], []
    dif = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    dea = _ema(dif, 9)
    if not dea:
        return [], [], []
    hist = [d - dea[i] for i, d in enumerate(dif[-len(dea):])]
    return dif, dea, hist


def _rsi(closes: list[float], n: int = 14) -> list[float]:
    """相对强弱指标。"""
    if len(closes) < n + 1:
        return []
    rsi: list[float] = []
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)]
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    rsi.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    for i in range(n + 1, len(closes)):
        gain = max(closes[i] - closes[i - 1], 0)
        loss = max(closes[i - 1] - closes[i], 0)
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
        rsi.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return rsi


def _boll(closes: list[float], n: int = 20, k: float = 2.0) -> dict | None:
    """布林带。"""
    if len(closes) < n:
        return None
    mb = sum(closes[-n:]) / n
    std = (sum((c - mb) ** 2 for c in closes[-n:]) / n) ** 0.5
    return {"upper": mb + k * std, "mid": mb, "lower": mb - k * std}


def _kdj(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> tuple[float, float, float] | None:
    """KDJ 指标，返回 (k, d, j)。"""
    if len(closes) < n:
        return None
    rsvs = []
    for i in range(n - 1, len(closes)):
        window_h = max(highs[i - n + 1 : i + 1])
        window_l = min(lows[i - n + 1 : i + 1])
        if window_h == window_l:
            rsv = 50.0
        else:
            rsv = (closes[i] - window_l) / (window_h - window_l) * 100
        rsvs.append(rsv)
    k = d = 50.0
    for rsv in rsvs:
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    j = 3 * k - 2 * d
    return k, d, j


def _vol_ratio_history(volumes: list[float]) -> list[float]:
    """根据日线成交量计算历史量比(当日/前5日均量)。"""
    if len(volumes) < 6:
        return []
    out = []
    for i in range(5, len(volumes)):
        avg5 = sum(volumes[i - 5:i]) / 5
        if avg5 > 0:
            out.append(round(volumes[i] / avg5, 2))
    return out


def _amplitude_history(quotes: list[Quote]) -> list[float]:
    """历史日振幅序列(高-低/昨收)。"""
    out = []
    for q in quotes:
        if q.pre_close:
            out.append(round((q.high - q.low) / q.pre_close * 100, 2))
    return out


def _pick_intra_amplitude(spot: dict | None, daily_quotes: list[Quote]) -> tuple[float, str]:
    """按当前交易时段选择最准的「日内振幅」数据源。
    返回 (amplitude_pct, source_label)。

    取数策略(对齐用户意图,2026-09-03 调整):
      盘中(09:30 ~ 15:00):优先用腾讯实时盘口的 high/low/pre_close(分钟级跳动,与现价/量比同源)。
        若 spot 拉取失败,回退到日线最新一根 K 线(盘中也会刷新但有分钟级延迟)。
      盘前(<09:30):用日线最新一根(此时腾讯接口通常还没推"今天"的 K,落到的就是 T-1),
        也就是 T-1 日的真实振幅,语义上等于"昨天"。
      盘后(>=15:00 且次日 0:00 前):用实时盘口已定格 high/low(若可达),否则日线最新一根。
      非交易日(周末/节假日)按盘前处理,整天取 T-1 日的振幅。

    source_label 取值(供前端 tooltip 展示):
      "盘中·实时" / "盘中·日线" / "盘前·昨" / "盘后·定格" / "盘后·日线" / "无数据"
    """
    from datetime import time as _t

    def _amp(high: float, low: float, pre_close: float) -> float:
        if not pre_close:
            return 0.0
        return round((high - low) / pre_close * 100, 2)

    now_t = dt.datetime.now().time()
    in_session = _t(9, 30) <= now_t < _t(15, 0)

    last = daily_quotes[-1] if daily_quotes else None

    if in_session:
        # 盘中:优先用 spot 的 high/low/pre_close,失败回退日线
        if spot:
            pre_close = spot.get("pre_close", 0) or 0
            high = spot.get("high", 0) or 0
            low = spot.get("low", 0) or 0
            # 盘前那一刻 spot 的 high==low==pre_close(还没开盘),也不要误判为"实时"
            if high > 0 and low > 0 and pre_close and not (high == pre_close and low == pre_close):
                return _amp(high, low, pre_close), "盘中·实时"
        if last and last.pre_close:
            return _amp(last.high, last.low, last.pre_close), "盘中·日线"
        return 0.0, "无数据"

    # 盘前或盘后(非盘中):用日线最后一根;若接口还没推当天 K 线,等于 T-1 真实振幅
    if last and last.pre_close:
        if now_t < _t(9, 30):
            return _amp(last.high, last.low, last.pre_close), "盘前·昨"
        # 盘后:有 spot 用 spot(已定格),否则日线
        if spot:
            pre_close = spot.get("pre_close", 0) or 0
            high = spot.get("high", 0) or 0
            low = spot.get("low", 0) or 0
            if high > 0 and low > 0 and pre_close:
                return _amp(high, low, pre_close), "盘后·定格"
        return _amp(last.high, last.low, last.pre_close), "盘后·日线"
    return 0.0, "无数据"


def _calc_pass_scores(code: str, out: dict, quotes: list[Quote], spot: dict | None, db) -> None:
    """按用户截图规则计算短线可投池 AI 评分(总分 100 分)与等级 A/B/C/D。

    规则摘要:
      1. 一票否决项:命中任意一条,total_score=0,grade=D。
      2. A. 流动性与波动(40 分):成交额/振幅/换手率。
      3. B. 趋势与位置(35 分):MA5/MA20 位置/箱体位置/MACD。
      4. C. 资金与板块(25 分):量比/主力资金净流入/板块共振。
      5. 扣分项:从总分中直接扣(黑名单/前高缩量/大阴线/获利盘/超仓)。
      6. 等级:>=80 A, 65~79 B, 50~64 C, <50 D。

    数据缺失维度给中性分并标注,避免误杀;无法自动识别的规则(如黑名单、
    重大利空、题材高潮)暂不纳入自动评分,由用户人工复核。
    """
    closes = [q.close for q in quotes if q.close]
    if not closes or len(closes) < 5:
        return

    last_q = quotes[-1]
    volumes = [q.volume for q in quotes if q.volume]            # 单位: 手(1手=100股)
    amounts = [q.amount for q in quotes if q.amount]
    # 若日线无成交额(如腾讯 kline 不含成交额),用 成交量*收盘价*100 估算。
    # 腾讯/东财日线成交量单位是「手」(1手=100股),因此必须乘100才能到「股×元=元」。
    if not amounts or sum(amounts) == 0:
        amounts = [q.volume * q.close * 100 for q in quotes if q.volume and q.close]
    highs = [q.high for q in quotes if q.high]
    lows = [q.low for q in quotes if q.low]
    price = out.get("price") or closes[-1]

    # ---- 基础指标 ----
    # avg_volume_20: 近20个交易日日均成交量, 单位「万手」。前端列名"20日平均 成交量(万)"
    avg_volume_20 = (sum(volumes[-20:]) / min(20, len(volumes))) / 1e4 if volumes else 0.0
    out["avg_volume_20"] = round(avg_volume_20, 2)
    # 同步估算 avg_amount_20 (元) 用于流动性阈值判断(保留"日均成交额<2亿"原业务语义,
    # 但底层 source 已从 amount 转为 volume), 估算式 = 万手→手→股 × 均价(元/股) = 元
    avg_close_20 = (sum(closes[-20:]) / min(20, len(closes))) if closes else 0.0
    avg_amount_20 = avg_volume_20 * 1e4 * avg_close_20 * 100  # 单位: 元
    # 日内振幅:按当前交易时段选择最准的数据源(盘中用实时盘口,盘前/盘后回退日线),
    # 并把 source 标签一起带上,前端可悬停展示"取自 xxx"。
    intra_amp, intra_src = _pick_intra_amplitude(spot, quotes)
    out["intra_amplitude"] = intra_amp
    out["intra_amplitude_source"] = intra_src
    vol_ratios = _vol_ratio_history(volumes)
    amplitudes = _amplitude_history(quotes)
    avg_amplitude_5 = (sum(amplitudes[-5:]) / min(5, len(amplitudes))) if amplitudes else 0.0
    turnover = out.get("turnover") or 0.0

    # ---- 技术指标 ----
    dif, dea, hist = _macd(closes)
    rsi_vals = _rsi(closes)
    boll = _boll(closes)
    box_pos = out.get("box_pos")
    box_high = out.get("box_high")
    box_low = out.get("box_low")
    above_ma5 = out.get("above_ma5")
    above_ma20 = out.get("above_ma20")

    detail: list[str] = []
    score_a = score_b = score_c = 0

    # ===== 一票否决项 =====
    veto_reasons: list[str] = []

    # 1) 日均成交额<2亿
    if avg_amount_20 < 2e8:
        veto_reasons.append("日均成交额<2亿")
    # 2) 日内振幅长期<2%(近5日平均<2%)
    if 0 < avg_amplitude_5 < 2.0:
        veto_reasons.append("日内振幅长期<2%")
    # 3) 股价同时在MA5和MA20下方,且MACD绿柱放大
    if above_ma5 is False and above_ma20 is False and hist and len(hist) >= 2:
        if hist[-1] < hist[-2] < 0:  # 绿柱放大
            veto_reasons.append("MA5/MA20下方且MACD绿柱放大")
    # 4) 题材处于全网热议的高潮末期(数据未接入,需人工复核)
    # 5) 近期有重大利空(数据未接入,需人工复核)

    # ===== A. 流动性与波动(40分) =====
    # 1) 日均成交额(20日) 15分
    if avg_amount_20 >= 10e8:
        score_a += 15
        detail.append("成交额≥10亿(+15)")
    elif avg_amount_20 >= 5e8:
        score_a += 12
        detail.append("成交额5~10亿(+12)")
    elif avg_amount_20 >= 3e8:
        score_a += 9
        detail.append("成交额3~5亿(+9)")
    elif avg_amount_20 >= 2e8:
        score_a += 5
        detail.append("成交额2~3亿(+5)")
    else:
        detail.append("成交额<2亿(否决)")

    # 2) 日内振幅(近5日平均) 15分
    if avg_amplitude_5 >= 6.0:
        score_a += 15
        detail.append("振幅≥6%(+15)")
    elif avg_amplitude_5 >= 4.0:
        score_a += 12
        detail.append("振幅4~6%(+12)")
    elif avg_amplitude_5 >= 3.0:
        score_a += 9
        detail.append("振幅3~4%(+9)")
    elif avg_amplitude_5 >= 2.0:
        score_a += 5
        detail.append("振幅2~3%(+5)")
    else:
        detail.append("振幅<2%(否决)")

    # 3) 换手率 10分
    if 5.0 <= turnover <= 10.0:
        score_a += 10
        detail.append("换手率5~10%(+10)")
    elif 3.0 <= turnover < 5.0:
        score_a += 8
        detail.append("换手率3~5%(+8)")
    elif 10.0 < turnover <= 15.0:
        score_a += 7
        detail.append("换手率10~15%(+7)")
    elif 1.0 <= turnover < 3.0:
        score_a += 4
        detail.append("换手率1~3%(+4)")
    elif turnover > 15.0:
        score_a += 3
        detail.append("换手率>15%过热(+3)")
    else:
        detail.append("换手率<1%(+0)")

    # ===== B. 趋势与位置(35分) =====
    # 1) MA5/MA20 位置 15分
    if above_ma5 is True and above_ma20 is True:
        score_b += 15
        detail.append("站上MA5且MA20(+15)")
    elif above_ma5 is True and above_ma20 is False:
        score_b += 8
        detail.append("站上MA5但MA20下方(+8)")
    elif above_ma5 is False and above_ma20 is False:
        detail.append("MA5/MA20下方(+0)")
    else:
        detail.append("MA位置数据缺失(+0)")

    # 2) 箱体位置 10分
    if box_pos is not None:
        if 0.2 <= box_pos <= 0.5:
            score_b += 10
            detail.append("箱体位置20~50%低吸区(+10)")
        elif 0.5 < box_pos <= 0.7:
            score_b += 7
            detail.append("箱体位置50~70%操作区(+7)")
        elif 0.1 <= box_pos < 0.2:
            score_b += 6
            detail.append("箱体位置10~20%接近下沿(+6)")
        elif 0.7 < box_pos <= 0.9:
            score_b += 3
            detail.append("箱体位置70~90%偏高(+3)")
        elif box_pos > 0.9:
            detail.append("箱体位置>90%高位风险(+0)")
        else:
            score_b += 10
            detail.append("箱体位置<10%极低位(+10)")
    else:
        detail.append("箱体位置缺失(+0)")

    # 3) MACD 状态 10分
    if dif and dea and hist and len(hist) >= 2:
        dif_last = dif[-1]
        hist_last, hist_prev = hist[-1], hist[-2]
        above_zero = dif_last > 0
        red_expand = hist_last > hist_prev > 0
        red_shrink = hist_prev > hist_last > 0
        green_shrink = 0 > hist_prev > hist_last
        green_expand = 0 > hist_last > hist_prev
        if above_zero and red_expand:
            score_b += 10
            detail.append("MACD零轴上方红柱放大(+10)")
        elif above_zero and red_shrink:
            score_b += 7
            detail.append("MACD零轴上方红柱缩短(+7)")
        elif not above_zero and green_shrink:
            score_b += 4
            detail.append("MACD零轴下方绿柱缩短(+4)")
        elif not above_zero and green_expand:
            detail.append("MACD零轴下方绿柱放大(+0/否决)")
        else:
            detail.append("MACD状态中性(+0)")
    else:
        detail.append("MACD数据不足(+0)")

    # ===== C. 资金与板块(25分) =====
    # 1) 量比 10分
    if vol_ratios:
        vr = vol_ratios[-1]
        if 1.0 <= vr <= 2.0:
            score_c += 10
            detail.append(f"量比{vr}温和放量(+10)")
        elif 0.8 <= vr < 1.0:
            score_c += 7
            detail.append(f"量比{vr}略缩量(+7)")
        elif 2.0 < vr <= 3.0:
            score_c += 6
            detail.append(f"量比{vr}放量过猛(+6)")
        elif 0.5 <= vr < 0.8:
            score_c += 3
            detail.append(f"量比{vr}明显缩量(+3)")
        elif vr > 3.0:
            score_c += 2
            detail.append(f"量比{vr}异常(+2)")
        else:
            detail.append(f"量比{vr}<0.5(+0)")
    else:
        detail.append("量比数据不足(+0)")

    # 2) 主力资金净流入 8分(数据未接入,给中性分 4 分并标注)
    score_c += 4
    detail.append("主力资金净流入:数据未接入(+4/8)")

    # 3) 板块共振 7分
    try:
        from app.models import Stock
        stock = db.get(Stock, code)
        industry = (stock.industry or "").strip() if stock else ""
        if industry:
            sector = get_sector_trend(industry, db)
            sector_pct = sector.get("change_pct", 0)
            stock_pct = out.get("change_pct", 0)
            sector_up_strong = sector_pct > 2.0
            sector_up = 1.0 < sector_pct <= 2.0
            sector_flat = abs(sector_pct) <= 1.0
            sector_down = sector_pct < -1.0
            stock_up = stock_pct > 0
            if sector_up_strong and stock_up:
                score_c += 7
                detail.append("板块涨>2%且个股领涨(+7)")
            elif sector_up:
                score_c += 5
                detail.append("板块涨1~2%(+5)")
            elif sector_flat:
                score_c += 3
                detail.append("板块平盘(+3)")
            elif sector_down and stock_up:
                score_c += 1
                detail.append("板块跌但个股涨(+1)")
            elif sector_down and not stock_up:
                detail.append("板块跌个股跌(+0)")
            else:
                score_c += 3
                detail.append("板块共振中性(+3)")
        else:
            score_c += 3
            detail.append("行业信息缺失(+3/7)")
    except Exception:
        score_c += 3
        detail.append("板块共振数据未接入(+3/7)")

    # ===== 扣分项 =====
    penalty = 0
    # 1) 历史黑名单(-15):无法自动识别,跳过
    # 2) 股价接近前高压位且量能萎缩(-10)
    if box_high is not None and box_high > 0 and vol_ratios and volumes:
        near_high = (box_high - price) / box_high < 0.03
        vol_shrink = vol_ratios[-1] < 0.8
        if near_high and vol_shrink:
            penalty += 10
            detail.append("接近前高且缩量(-10)")
    # 3) 近期出现过跌停或单日跌幅>8%的大阴线(-10)
    if len(closes) >= 6:
        for i in range(-6, 0):
            q = quotes[i]
            if q.pre_close:
                pct = (q.close - q.pre_close) / q.pre_close * 100
                if pct <= -8.0:
                    penalty += 10
                    detail.append(f"近期大阴线 {q.date} {pct:.1f}%(-10)")
                    break
    # 4) 筹码获利盘>90%且股价在高位(-8):数据未接入
    # 5) 试仓阶段已有2只在仓(-5):无法自动识别,跳过

    # ===== 三维技术投票(MACD/KDJ/布林) =====
    tech_signals = {"macd": "-", "kdj": "-", "boll": "-"}
    # MACD: DIF>DEA 且红柱=看多; DIF<DEA 且绿柱=看空
    if dif and dea and hist and len(dif) >= 1 and len(dea) >= 1 and len(hist) >= 1:
        if dif[-1] > dea[-1] and hist[-1] > 0:
            tech_signals["macd"] = "看多"
        elif dif[-1] < dea[-1] and hist[-1] < 0:
            tech_signals["macd"] = "看空"
        else:
            tech_signals["macd"] = "中性"
    # KDJ: K>D=看多, K<D=看空
    kdj_vals = _kdj(highs, lows, closes, n=9)
    if kdj_vals:
        k_val, d_val, _ = kdj_vals
        if k_val > d_val:
            tech_signals["kdj"] = "看多"
        elif k_val < d_val:
            tech_signals["kdj"] = "看空"
        else:
            tech_signals["kdj"] = "中性"
    # 布林: 收盘价>=上轨=看空(超买), 收盘价<=下轨=看多(超卖), 否则中性
    if boll and price:
        if price >= boll["upper"]:
            tech_signals["boll"] = "看空"
        elif price <= boll["lower"]:
            tech_signals["boll"] = "看多"
        else:
            tech_signals["boll"] = "中性"
    out["tech_signals"] = tech_signals

    # ===== 汇总 =====
    total = score_a + score_b + score_c - penalty
    if veto_reasons:
        total = 0

    # 向后兼容:保留旧的 must/key/aux 字段,但值按新规则映射
    out["must_pass"] = detail[:5] if detail else ["数据不足"]
    out["must_pass_score"] = score_a
    out["key_pass"] = detail[5:11] if len(detail) > 5 else ["数据不足"]
    out["key_pass_score"] = score_b
    out["aux_pass"] = detail[11:] if len(detail) > 11 else ["数据不足"]
    out["aux_pass_score"] = score_c

    out["score_a"] = score_a
    out["score_b"] = score_b
    out["score_c"] = score_c
    out["penalty"] = penalty
    out["veto_reasons"] = veto_reasons
    out["total_score"] = total
    if total >= 80:
        out["ai_grade"] = "A"
    elif total >= 65:
        out["ai_grade"] = "B"
    elif total >= 50:
        out["ai_grade"] = "C"
    else:
        out["ai_grade"] = "D"


def _operation_advice_for_pool(track: dict, position: dict | None = None) -> dict:
    """基于 pool track 已有数据生成简化操作建议,供可投池表格一列展示。

    输出结构与 t_analysis.section6 保持一致(title/summary/text/points),
    便于前端用同一套弹窗渲染完整操作结论。
    """
    price = track.get("price") or 0.0
    change_pct = track.get("change_pct") or 0.0
    vol_ratio = track.get("vol_ratio") or 0.0
    turnover = track.get("turnover") or 0.0
    box_pos = track.get("box_pos")
    box_high = track.get("box_high")
    box_low = track.get("box_low")
    avg_price = track.get("avg_price") or price
    intra_amplitude = track.get("intra_amplitude") or 0.0
    tech_signals = track.get("tech_signals") or {"macd": "-", "kdj": "-", "boll": "-"}
    above_ma5 = track.get("above_ma5")
    above_ma20 = track.get("above_ma20")

    pos = position or {}
    cost_price = pos.get("cost_price") or 0.0
    position_qty = pos.get("position_qty") or 0
    has_position = bool(position_qty and cost_price > 0)

    # 箱体位置描述
    if box_pos is not None:
        if box_pos <= 0.15:
            box_text = f"箱体下沿附近({box_pos*100:.0f}%), 接近支撑, 可考虑低吸机会"
        elif box_pos >= 0.85:
            box_text = f"箱体上沿附近({box_pos*100:.0f}%), 接近压力, 注意高抛或回落风险"
        else:
            box_text = f"箱体中间区域({box_pos*100:.0f}%), 方向不明, 建议观望"
    elif box_high is not None and box_low is not None and price:
        box_text = f"箱体上沿 {box_high} / 下沿 {box_low}, 当前价 {price}"
    else:
        box_text = "箱体数据不足"

    # 默认观望
    main_key, main_title = "wait", "观望，不操作"
    main_summary = "无明显做T点位, 建议观望, 等待箱体上下沿信号。"
    main_text = main_summary

    # 1) 持仓止盈优先
    if has_position and cost_price:
        pnl_pct = (price - cost_price) / cost_price * 100
        if pnl_pct >= 10:
            main_key, main_title = "reduce", "建议分批止盈/减仓"
            main_summary = "持仓已有较大浮盈, 建议先分批减仓锁定利润, 不再净加仓。"
            main_text = (f"持仓成本 {cost_price:.2f}, 当前价 {price:.2f}, 浮盈约 {pnl_pct:.1f}%。"
                         f"建议分批减仓锁定利润, 剩余仓位按移动止盈保护。")

    # 2) 风控硬拦
    if main_key == "wait" and (
        change_pct <= -5.0
        or (vol_ratio < 0.5 and above_ma5 is False)
        or turnover > 20.0
        or (intra_amplitude > 10 and change_pct < -3)
    ):
        main_key, main_title = "wait", "观望，不操作"
        main_summary = "当前存在风险信号, 不建议开新T仓, 等待企稳后再评估。"
        main_text = "当前存在风险信号(缩量阴跌/量比过低/特殊风控), 不开T仓, 等待企稳。"

    # 3) 等待高抛
    if main_key == "wait" and box_pos is not None and box_pos >= 0.85 and change_pct > 0 and vol_ratio < 0.8:
        main_key, main_title = "sell", "等待高抛机会（卖出T仓）"
        main_summary = "价格接近箱体上沿且上涨无量, 持有T仓可分批卖出止盈。"
        main_text = "到达压力区间, 上涨无量, 持有T仓可分批卖出止盈。"

    # 4) 等待低吸
    if main_key == "wait" and box_pos is not None and box_pos <= 0.15 and vol_ratio < 1.2 and change_pct >= -0.5:
        main_key, main_title = "buy", "等待低吸机会（小仓T）"
        main_summary = "价格接近箱体下沿, 等待放量站稳后可极小仓位试做T。"
        main_text = "到达支撑区间, 等待放量站稳分时均价, 极小仓位试做T, 提前设置止损。"

    # 5) 箱体中间震荡可做T
    if main_key == "wait" and box_pos is not None and 0.15 < box_pos < 0.85 \
            and 2.5 <= intra_amplitude <= 8.0 and 0.5 <= vol_ratio <= 2.5 \
            and 2.0 <= turnover <= 10.0 and -1.0 <= change_pct <= 3.0:
        main_key, main_title = "t", "可日内高抛低吸"
        main_summary = "箱体中间区域, 日内波动适中, 可在分时均价附近小仓位做T。"
        main_text = "箱体中间区域, 日内波动适中、量能配合, 可在分时均价附近小仓位做T, 严格止损。"

    # 6) 日内弱势+缩量
    if main_key == "wait" and price and avg_price and price < avg_price and vol_ratio < 0.8:
        main_key, main_title = "wait", "观望，不操作"
        main_summary = "日内弱势且缩量无承接, 建议等待企稳信号。"
        main_text = "日内弱势, 缩量无承接, 等待企稳信号, 不开T仓。"

    # ---- 各维度说明 ----
    points = []
    if has_position and cost_price:
        pnl_pct = (price - cost_price) / cost_price * 100
        points.append({
            "dim": "持仓风控",
            "text": (f"持有成本 {cost_price:.2f}, 当前价 {price:.2f}, 浮盈/亏约 {pnl_pct:.1f}%。"
                     f"{'已达止盈区间, 优先落袋' if pnl_pct >= 10 else '按原计划持仓, 双线跟踪止盈止损。'}")
        })
    else:
        points.append({
            "dim": "持仓风控",
            "text": "未填写持仓成本/股数, 操作建议仅基于盘面指标, 不含个人盈亏约束。"
        })

    points.append({"dim": "箱体位置", "text": box_text})

    vol_text = (
        f"量比 {vol_ratio:.2f}({'成交清淡' if vol_ratio < 1 else '正常/放量'}), "
        f"换手率 {turnover:.2f}%({'流动性合适, 适合做T' if 3 <= turnover <= 10 else '流动性偏弱/偏高'})。"
    )
    if track.get("realtime_volume") is not None and track.get("yesterday_volume_at_time") is not None:
        vol_text += (
            f" 当天实时成交 {track['realtime_volume']:.0f}万手, "
            f"T-1 同期 {track['yesterday_volume_at_time']:.0f}万手"
        )
    vol_text += " 当前量能不支持追涨, 更适合高抛或观望。"
    points.append({"dim": "量能信号", "text": vol_text})

    ma5_text = "站上MA5" if above_ma5 is True else ("跌破MA5" if above_ma5 is False else "MA5数据缺失")
    ma20_text = "站上MA20" if above_ma20 is True else ("跌破MA20" if above_ma20 is False else "MA20数据缺失")
    points.append({
        "dim": "短线信号",
        "text": f"{ma5_text}；{ma20_text}。短期趋势{'偏强' if above_ma5 is True else ('偏弱' if above_ma5 is False else '震荡')}, 但尚未形成明确单边信号。"
    })

    vote_overall = "数据不足"
    bull = bear = neutral = 0
    ts = tech_signals
    if ts.get("macd") == "看多":
        bull += 1
    elif ts.get("macd") == "看空":
        bear += 1
    else:
        neutral += 1
    if ts.get("kdj") == "看多":
        bull += 1
    elif ts.get("kdj") == "看空":
        bear += 1
    else:
        neutral += 1
    if ts.get("boll") == "看多":
        bull += 1
    elif ts.get("boll") == "看空":
        bear += 1
    else:
        neutral += 1
    if bull > bear:
        vote_overall = "偏多"
    elif bear > bull:
        vote_overall = "偏空"
    elif bull == bear and bull > 0:
        vote_overall = "中性"
    points.append({
        "dim": "技术投票",
        "text": (f"MACD/KDJ/布林综合结论：{vote_overall}"
                 f"（看多 {bull} / 看空 {bear} / 中性 {neutral}）。"
                 f"日线趋势{'向好' if vote_overall == '偏多' else ('向淡' if vote_overall == '偏空' else '不明')}, 但需服从短期止盈/风控纪律。")
    })

    return {
        "key": main_key,
        "title": main_title,
        "summary": main_summary,
        "text": main_text,
        "points": points,
    }


def get_pool_track(code: str, db, position: dict | None = None) -> dict:
    """单只标的的「每日跟踪」数据,供短线可投池表格使用。

    返回字段:
      - price/change_pct/vol_ratio/turnover: 实时(腾讯/东财/新浪)
      - box_high/box_low/box_pos: 近 20 日箱体上下沿及当前价相对位置(0~1,1=触上沿)
      - ma5/ma20: 均线;above_ma5/above_ma20: 当前价是否站上
      - operation_advice: 基于当前盘口生成的操作建议对象
      - src: 数据源标识(tencent/eastmoney/sina/demo),失败时为空
      - ts: 拉取时间戳(秒),供前端展示「X秒前」

    失败策略:任意一步失败就返回 {},不抛异常,保证 list_pool 不被单只拖死。
    """
    now = dt.datetime.now().timestamp()
    cached = _POOL_TRACK_CACHE.get(code)
    if cached and (now - cached[0]) < _POOL_TRACK_TTL:
        return cached[1]

    out: dict = {}
    try:
        # ===== 数据源口径说明 =====
        # 实时盘口(腾讯 qt.gtimg.cn):价格是「不复权」原始价
        # 日线接口(腾讯 ifzq.gtimg.cn):价格是「前复权」价
        # 当股票发生除权除息/送股/大比例拆分时,两个接口会给出完全不同的价格。
        # 本表「现价」必须和 MA5/MA20 同源(都用日线前复权),否则会因为复权差异数学上"必错"。
        # 实时盘口仅用于「量比/换手」这种"日内活跃度"指标(与除权无关)。
        spot = _fetch_spot_direct(code)
        if spot:
            # 实时指标(日内活跃度,与除权无关)
            out["vol_ratio"] = round(spot.get("vol_ratio", 0), 2)
            out["turnover"] = round(spot.get("turnover", 0), 2)
            # 当天实时成交量(万手) - volume_wan 已在 _fetch_spot_xxx 三个函数内部归一化好
            out["realtime_volume"] = round(spot.get("volume_wan", 0), 2)
            out["src"] = spot.get("src", "")
        # 昨日同时点 + 全天累计(成交量/万手), 由 _yesterday_day_metrics 统一提供(共享新浪拉数 + 缓存)
        yest_metrics = _yesterday_day_metrics(code)
        if yest_metrics:
            if yest_metrics.get("volume_at_time") is not None:
                # T-1 同期累计成交量(万手) - /1e4 把手 → 万手
                out["yesterday_volume_at_time"] = round(yest_metrics["volume_at_time"] / 1e4, 2)
            if yest_metrics.get("volume_total") is not None:
                # T-1 全天累计成交量(万手) - 新增字段,口径在 _yesterday_volume_total docstring
                out["yesterday_volume_total"] = round(yest_metrics["volume_total"] / 1e4, 2)

        # 日线只取近 30 天足够(算 MA20/箱体)
        quotes = ensure_quotes(code, days=60)
        closes = [q.close for q in quotes if q.close]
        if closes:
            # 现价/涨跌幅以「日线最新一根」(前复权)为准,跟 MA 系列同源同口径
            last_q = quotes[-1]
            out["price"] = round(last_q.close, 3)
            out["change_pct"] = round((last_q.close - (last_q.pre_close or last_q.close)) / (last_q.pre_close or last_q.close) * 100, 2) if last_q.pre_close else 0.0
            out["price_date"] = last_q.date
            out["ma5"] = round(sum(closes[-5:]) / min(5, len(closes)), 3)
            out["ma20"] = round(sum(closes[-20:]) / min(20, len(closes)), 3)
            recent = closes[-20:] if len(closes) >= 20 else closes
            out["box_high"] = round(max(recent), 3)
            out["box_low"] = round(min(recent), 3)
            if out.get("price") and out["box_high"] != out["box_low"]:
                pos = (out["price"] - out["box_low"]) / (out["box_high"] - out["box_low"])
                out["box_pos"] = round(max(0.0, min(1.0, pos)), 2)
            # 实时(未复权)价:返回但带可信度标记,前端按等级显示
            #   ok: 差异 < 20% 正常(无除权/数据正常)
            #   diverged: 差异 20-50% 可能除权,标黄提示
            #   abnormal: 差异 > 50% 数据源异常(沙箱常见),标红警示
            if spot and last_q.close > 0:
                out["price_realtime_unadjusted"] = round(spot["price"], 3)
                out["change_pct_realtime"] = round(spot.get("change_pct", 0), 2)
                diff_ratio = abs(spot["price"] - last_q.close) / last_q.close
                if diff_ratio < 0.2:
                    out["realtime_confidence"] = "ok"
                elif diff_ratio < 0.5:
                    out["realtime_confidence"] = "diverged"
                    out["realtime_diff_pct"] = round(diff_ratio * 100, 1)
                else:
                    out["realtime_confidence"] = "abnormal"
                    out["realtime_diff_pct"] = round(diff_ratio * 100, 1)
                    # 差异过大时,日线缓存极可能是演示/过期数据,而实时盘口仍来自真实源;
                    # 此时用实时价覆盖「现价」,避免主价格显示一个明显错误的数字。
                    # 注意:MA/箱体仍基于日线,因此同时标记 price_note 供前端提示口径不一致。
                    if spot.get("src") not in ("demo",):
                        out["price"] = out["price_realtime_unadjusted"]
                        out["change_pct"] = out["change_pct_realtime"]
                        out["price_note"] = "实时价(日线数据源异常 fallback)"
        if out.get("price") is not None and out.get("ma5") is not None:
            out["above_ma5"] = out["price"] > out["ma5"]
        if out.get("price") is not None and out.get("ma20") is not None:
            out["above_ma20"] = out["price"] > out["ma20"]
        # 短线可投池三类达标项打分
        if quotes:
            _calc_pass_scores(code, out, quotes, spot, db)
        # 基于 track 数据生成操作建议(结构同 t_analysis.section6)
        out["operation_advice"] = _operation_advice_for_pool(out, position)
        out["ts"] = int(now)
    except Exception:
        # 任意异常都吞掉,返回当前已聚合的部分(可能为空)
        pass
    _POOL_TRACK_CACHE[code] = (now, out)
    return out
