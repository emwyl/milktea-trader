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
import json
import random
import re
import time
from collections import OrderedDict

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

    v118 快速失败保护：连接被拒 / 域名被 WAF 拦截 / 对端秒断（单次 <1s 即失败）说明
    源当前不可达，重试只会放大延迟（曾实测单只股票因 3×3 重试退避空转 7s+，拖垮整页）。
    此类快速失败最多补 1 次即放弃，让调用方立即降级到 DB 缓存/留空；仅对「慢失败」
    （超时、5xx 等瞬时抖动）保留完整重试。
    """
    h = {"User-Agent": _DEFAULT_UA}
    if headers:
        h.update(headers)
    last_err = None
    fast_fails = 0
    for attempt in range(retries + 1):
        _t0 = time.monotonic()
        fast_fail = False
        try:
            with httpx.Client(headers=h, timeout=_TIMEOUT, follow_redirects=True) as client:
                r = client.get(url)
                if r.status_code == 200 and r.text:
                    return r.text
                last_err = f"HTTP {r.status_code}"
                # WAF/网关类拦截页(501 等)通常秒回,视同快速失败
                if time.monotonic() - _t0 < 1.0:
                    fast_fail = True
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if time.monotonic() - _t0 < 1.0:
                fast_fail = True
        # 快速失败：最多补 1 次(应对偶发瞬时断连)；慢失败：按阶梯完整重试
        if fast_fail:
            fast_fails += 1
            if fast_fails >= 2:
                break
            continue
        # 重试前短暂退避（0.3s / 0.6s ...），降低免费源瞬时抖动导致的失败
        if attempt < retries:
            time.sleep(0.3 * (attempt + 1))
    return None


def _market_of(code: str) -> str:
    """判断市场前缀：920/4/8 开头→北 bj，6/9 开头→沪 sh，0/3 开头→深 sz。
    注意: 920xxx 是北交所新股代码(2021 年起), 不能用「9→沪」笼统覆盖, 否则会被当成 sh920xxx 去拉行情而取不到数。"""
    if code.startswith("920"):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return "bj"


def _secid(code: str) -> str:
    """东财 secid：沪 1.xxxxxx，深/北 0.xxxxxx。北交所(920/8/4 开头)用 0. 前缀。"""
    if code.startswith("920"):
        return "0." + code
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _as_float_or_none(v):
    """严格解析：不可解析（None / '' / '-' / 非数字）时返回 **None** 而非 0.0。

    v174 新增。用途：资金流等场景必须区分「数值真的是 0」与「接口没给数据」——
    东财在无数据时把字段置为 "-"，旧逻辑 `_as_float('-') -> 0.0` 会让
    `main_net is None` 永远为假，于是「无数据」被当成「净流入 0」落库并显示为 0.00%，
    同时掩盖了取数失败。此函数用于所有需要判空的地方。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
# 东财熔断（2026-09-03）：东财不可达时，避免"每只票每次都白等一次连接超时"。
# 连续失败 3 次 → 熔断 5 分钟，期间直接跳过东财请求（amount 用 volume×close×100 估算兜底）。
_EM_CIRCUIT: dict[str, float] = {"until": 0.0, "fails": 0}
_EM_CIRCUIT_FAILS = 3
_EM_CIRCUIT_TTL = 300.0


def _em_available() -> bool:
    """东财当前是否可用（熔断中则 False）。"""
    if time.time() < _EM_CIRCUIT["until"]:
        return False
    return True


def _em_note_fail() -> None:
    _EM_CIRCUIT["fails"] += 1
    if _EM_CIRCUIT["fails"] >= _EM_CIRCUIT_FAILS:
        _EM_CIRCUIT["until"] = time.time() + _EM_CIRCUIT_TTL


def _em_note_ok() -> None:
    _EM_CIRCUIT["fails"] = 0
    _EM_CIRCUIT["until"] = 0.0


def _fetch_daily_tencent(code: str, days: int) -> list[Quote] | None:
    """腾讯日线（前复权）。返回 [{date, open, close, high, low, volume}] 升序。
    注意(2026-09-04 实测):必须带 web 前缀——ifzq.gtimg.cn(裸域名)会被腾讯 WAF 501 拦截，
    web.ifzq.gtimg.cn 正常返回 200。与 _market_return 用同一域名。"""
    mkt = _market_of(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt}{code},day,,,{days},qfq"
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


def _fetch_daily_sina(code: str, days: int) -> list[Quote] | None:
    """新浪日线——北交所兜底。实测(2026-09-08):腾讯对 920xxx 一律只回最新 1 根,
    东财在本机常被网络掐断,akshare 亦依赖东财;新浪 CN_MarketDataService 支持
    bj920xxx 全史(datalen 约可到 500)。注意该接口为不复权日线,仅用于北交所,
    与历史行同一口径自洽,不影响 MA/箱体等相对指标。"""
    mkt = _market_of(code)
    try:
        url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_/"
               "CN_MarketDataService.getKLineData"
               f"?symbol={mkt}{code}&scale=240&ma=no&datalen={min(max(days, 60), 500)}")
        txt = _http_get(url)
        if not txt:
            return None
        import json
        i = txt.find("(")
        if i < 0 or txt.rfind(")") <= i:
            return None
        arr = json.loads(txt[i + 1: txt.rfind(")")])
        out: list[Quote] = []
        prev = None
        for r in arr:
            o = _as_float(r.get("open")); c = _as_float(r.get("close"))
            h = _as_float(r.get("high")); l = _as_float(r.get("low"))
            v = _as_float(r.get("volume"))
            out.append(Quote(code=code, date=str(r.get("day")), open=o, high=h, low=l, close=c,
                             volume=v, amount=0.0, turnover=0.0,
                             pre_close=prev if prev is not None else c))
            prev = c
        return out[-days:] if out else None
    except Exception:
        return None


def _fetch_daily_eastmoney(code: str, days: int) -> list[Quote] | None:
    """东财日线（前复权），腾讯失败时兜底。

    带熔断(2026-09-03)：腾讯日线本身不含成交额(amount 恒为 0)，所以 ensure_quotes 每次
    都会再试一次东财去补 amount。一旦东财在本机不可达（代理/网络被掐），就会变成
    「每只票每次都白等一次连接超时」——单只约 0.5~1s，一页 15 只累计很可观。
    连续失败 3 次后熔断 5 分钟不再请求（此时 amount 用 volume×close×100 估算，不影响指标）。
    """
    if not _em_available():
        return None
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt=101&fqt=1&end=20500101&lmt={days}")
    txt = _http_get(url)
    if not txt:
        _em_note_fail()
        return None
    try:
        import json
        data = json.loads(txt).get("data")
        klines = (data or {}).get("klines") or []
        if not klines:
            _em_note_fail()
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
        _em_note_ok()
        return out[-days:]
    except Exception:
        _em_note_fail()
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


def ensure_quotes(code: str, days: int = 180, _prov: list | None = None) -> list[Quote]:
    """取日线：新鲜度缓存 → HTTP 直连（腾讯→东财）→ DB 缓存 → akshare → 演示数据。
    直连成功会写回缓存（覆盖旧演示数据）；直连失败才回退缓存/演示。

    _prov(v137)：可选传出参，用于把本条日线的真实来源告诉调用方（写 _prov[0]）：
      "live"  = 本次 HTTP 直连真实源成功（腾讯/东财/akshare），可放心展示
      "fresh" = 命中内存新鲜缓存（内容源自前次 live/cache 结果，需调用方与实时盘口交叉验证）
      "cache" = 读本地库缓存（可能是历史真实数据；也可能是历史故障期遗留的演示行，需交叉验证）
      "demo"  = 所有真实源不可用时的确定性演示数据（仅供离线演示，绝不能当真实行情展示）

    性能说明(2026-09-03)：第 0 步的新鲜度缓存是关键。原实现每只票每次都走 HTTP +
    DELETE/INSERT 整表，单只约 2.4s；可投池 138 只票、一页 15 只，冷启动要 7 秒左右，
    而纯 DB 读只要 0.018s。日线只服务于 MA/箱体/20日均量这类慢变量，盘中 60s
    新鲜度完全够用（现价走实时盘口，不受本缓存影响）。
    """
    # 0. 新鲜度短路（演示数据不进缓存，见 _daily_fresh_put 注释）
    fresh = _daily_fresh_get(code, days)
    if fresh is not None:
        if _prov is not None:
            _prov[:] = ["fresh"]
        return fresh

    db = SessionLocal()
    try:
        # 1. HTTP 直连真实日线（盘间保证最新，且覆盖演示缓存）
        # 腾讯 kline 不含成交额,若日线需要金额类指标(如日均成交额),优先尝试东财接口。
        quotes = _fetch_daily_tencent(code, days)
        # 源只回零星几根时不能当完整序列(实测:腾讯对北交所 920xxx 一律只回最新 1 根,
        # 若当 live 整表覆盖,会把几十上百根真实缓存删成 1 根 → 下次直接跌入 demo 数据异常)。
        if quotes and len(quotes) < 10:
            quotes = None
        if quotes and all(q.amount == 0 for q in quotes):
            east = _fetch_daily_eastmoney(code, days)
            if east:
                quotes = east
        if not quotes:
            quotes = _fetch_daily_eastmoney(code, days)
        if not quotes and _market_of(code) == "bj":
            # 北交所日线兜底:腾讯仅回最新1根/东财常被掐,新浪支持 bj920xxx 全史
            quotes = _fetch_daily_sina(code, days)
            if quotes and len(quotes) < 10:
                quotes = None
        if quotes:
            # 写回缓存（先清旧，避免唯一约束重复 + 覆盖旧演示数据）
            db.query(DailyQuote).filter(DailyQuote.code == code).delete()
            for q in quotes:
                db.add(DailyQuote(code=q.code, date=q.date, open=q.open, high=q.high, low=q.low,
                                  close=q.close, volume=q.volume, amount=q.amount,
                                  turnover=q.turnover, pre_close=q.pre_close))
            db.commit()
            if _prov is not None:
                _prov[:] = ["live"]
            _daily_fresh_put(code, days, quotes)
            return quotes[-days:]

        # 2. 直连失败 → DB 缓存（可能是历史真实数据）
        #    阈值放宽(2026-09-08):演示数据已不落库,缓存里只可能是真实行;次新/停牌股
        #    真实根数偏少(如上市 3 周仅 ~16 根),过严的 max(20, days//2) 会把它们逼进 demo
        #    → 线上永久「数据异常」。MA20/箱体在根数不足时用现有根数折算,仍可展示。
        rows = (db.query(DailyQuote).filter(DailyQuote.code == code)
                .order_by(DailyQuote.date).all())
        if rows and len(rows) >= max(8, days // 4):
            cached = [Quote(code=r.code, date=r.date, open=r.open, high=r.high, low=r.low,
                            close=r.close, volume=r.volume, amount=r.amount,
                            turnover=r.turnover, pre_close=r.pre_close) for r in rows[-days:]]
            if _prov is not None:
                _prov[:] = ["cache"]
            _daily_fresh_put(code, days, cached)
            return cached

        # 3. akshare 兜底
        quotes = _fetch_akshare(code, days)
        if quotes:
            db.query(DailyQuote).filter(DailyQuote.code == code).delete()
            for q in quotes:
                db.add(DailyQuote(code=q.code, date=q.date, open=q.open, high=q.high, low=q.low,
                                  close=q.close, volume=q.volume, amount=q.amount,
                                  turnover=q.turnover, pre_close=q.pre_close))
            db.commit()
            if _prov is not None:
                _prov[:] = ["live"]
            _daily_fresh_put(code, days, quotes)
            return quotes[-days:]

        # 4. 演示数据（所有真实源都不可用）
        #    只返回内存演示序列，绝不写库。历史教训(2026-09-08):此分支曾 DELETE 真实日线缓存
        #    再写入 8~45 随机假价,网络抖动一次就永久污染 daily_quotes——后续 cache 分支读到假价,
        #    与实时盘口价差>50% 交叉验证失败 → 该股永久 qbad=0「数据异常」,且无自愈途径。
        #    与第 0 步新鲜度缓存同一原则:演示数据不进缓存,真实源恢复后自动覆盖。
        quotes = generate_demo_quotes(code, days)
        if _prov is not None:
            _prov[:] = ["demo"]
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


# ===== v184: 行业字段运行时补全 + 板块当日涨跌(真实值) =====
# 背景: stocks.industry 5549 只里只有 44 只有值, 板块共振长期取不到数;
# 且 get_sector_trend() 只返回 20 日 slope, 没有 change_pct —— 旧评分取 sector.get("change_pct", 0)
# 恒为 0, 导致板块共振永远判不出「强」。这里补两个能力:
#   1) _fetch_industry_em: 东财 push2 单只 f127 行业(域名池回退 + 短超时 + 失败负缓存)
#   2) _sector_day_change: 用本地 daily_quotes 缓存算同行业样本股「最近一日平均涨跌幅」
_IND_EM_CACHE: dict[str, tuple[float, str]] = {}      # code -> (ts, industry)
_IND_EM_NEG: dict[str, float] = {}                    # code -> 失败时刻(负缓存)
_IND_NEG_TTL = 6 * 3600.0                             # 失败后 6h 内不再重试,避免拖慢评分


def _secucode(code: str) -> str:
    """6 位代码 → 东财 datacenter 的 SECUCODE(如 300377.SZ / 600519.SH / 920008.BJ)。"""
    c = str(code or "").strip()
    if not c:
        return ""
    if c[0] in ("6", "9"):
        return f"{c}.SH"
    if c[:2] in ("00", "30", "20"):
        return f"{c}.SZ"
    if c[0] in ("8", "4") or c[:2] == "92":
        return f"{c}.BJ"
    return f"{c}.SZ"


def _norm_industry(em2016: str = "", csrc1: str = "") -> str:
    """东财三级行业 '金融-银行-股份制与城商行' → 取二级('银行')。

    二级粒度最适合做板块共振:一级太粗(样本过大、区分度低),三级太细(同板块样本常不足 3 只)。
    只有两段时取末段;取不到则回退证监会行业 INDUSTRYCSRC1 的末段。
    """
    s = str(em2016 or "").strip()
    if s:
        parts = [x.strip() for x in s.split("-") if x.strip()]
        if len(parts) >= 2:
            return parts[1]
        if parts:
            return parts[0]
    s = str(csrc1 or "").strip()
    if s:
        parts = [x.strip() for x in s.split("-") if x.strip()]
        if parts:
            return parts[-1]
    return ""


def _fetch_industry_em(code: str) -> str:
    """单只股票行业。取不到返回 ''。

    数据源优先级:
      1) 东财 datacenter RPT_F10_BASIC_ORGINFO(EM2016 东财行业三级) —— 最稳,实测可用,取二级
      2) 东财 push2 f127(域名池回退) —— 备用
    仅用于「DB 里 industry 为空」的兜底:取到后由调用方写回 stocks 表,下次直接用。
    失败进负缓存,防止每次评分都打网络。
    """
    import time as _t
    now = _t.time()
    hit = _IND_EM_CACHE.get(code)
    if hit and (now - hit[0]) < 86400:
        return hit[1]
    if (now - _IND_EM_NEG.get(code, 0)) < _IND_NEG_TTL:
        return ""
    sc = _secucode(code)
    # 源 1: datacenter(3s 超时,单次)
    if sc:
        try:
            url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
                   "?reportName=RPT_F10_BASIC_ORGINFO&columns=SECUCODE,EM2016,INDUSTRYCSRC1"
                   f"&filter=(SECUCODE%3D%22{sc}%22)&pageSize=1")
            with httpx.Client(headers={"User-Agent": _DEFAULT_UA,
                                       "Referer": "https://data.eastmoney.com/"},
                              timeout=3.0, follow_redirects=True) as cli:
                r = cli.get(url)
            if r.status_code == 200:
                rows = (((r.json() or {}).get("result") or {}).get("data")) or []
                if rows:
                    ind = _norm_industry(rows[0].get("EM2016"), rows[0].get("INDUSTRYCSRC1"))
                    if ind:
                        _IND_EM_CACHE[code] = (now, ind)
                        return ind
        except Exception:
            pass
    # 源 2: push2 f127(2.5s 超时,只试前 2 个域名)
    for host in _EM_PUSH_HOSTS[:2]:
        try:
            with httpx.Client(headers={"User-Agent": _DEFAULT_UA, "Referer": "https://quote.eastmoney.com/"},
                              timeout=2.5, follow_redirects=True) as cli:
                r = cli.get(f"https://{host}/api/qt/stock/get?secid={_secid(code)}&fields=f57,f127")
            if r.status_code != 200:
                continue
            ind = str(((r.json() or {}).get("data") or {}).get("f127") or "").strip()
            if ind and ind not in ("-", "nan"):
                _IND_EM_CACHE[code] = (now, ind)
                return ind
        except Exception:
            continue
    _IND_EM_NEG[code] = now
    return ""


def _sector_day_change(industry: str, db, min_samples: int = 3) -> float | None:
    """同行业样本股「最近一日平均涨跌幅(%)」。

    口径: 取该行业样本(最多 15 只)最后一根日线的 (close - pre_close)/pre_close 均值。
    只读本地 daily_quotes 缓存,不触发网络。样本不足 min_samples 只 → 返回 None
    (1~2 只算出的"板块涨跌"其实是单只个股,不能当板块用,宁可不计分)。
    """
    if not industry:
        return None
    try:
        from app.models import Stock
        codes = [s.code for s in db.query(Stock.code).filter(Stock.industry == industry).limit(15).all()]
    except Exception:
        return None
    if len(codes) < min_samples:
        return None
    pcts: list[float] = []
    for c in codes:
        try:
            qs = _get_cached_quotes(c, 5)
        except Exception:
            continue
        if not qs:
            continue
        last = qs[-1]
        if last.close and last.pre_close:
            pcts.append((last.close - last.pre_close) / last.pre_close * 100)
    if len(pcts) < min_samples:
        return None
    return round(sum(pcts) / len(pcts), 2)


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
        # f[6]/f[36] 实测单位 = 手（2026-09-03 交叉验证：
        #   600519 现价 1297.58、成交额 f[37]=101270 万 → 66106 手×100×1297.58 ≈ 成交额，吻合；
        #   且腾讯日线 09-02 volume=20308 手 ↔ 新浪K线 2013445 股 → 口径一致）。
        # 旧注释误写为「股」并按 /1e6 换算，导致当天实时成交量被缩小 100 倍。
        volume = _as_float(f[36]) if len(f) > 36 else _as_float(f[6])   # 单位: 手
        volume_wan = volume / 1e4 if volume else 0.0                    # 万手 (1手=100股)
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
        vol_raw = _as_float(d.get("f47"))                          # 单位: 手(东财 push2 f47 约定为手)
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
        # 新浪 hq 的 volume 单位是「股」（与新浪K线一致，f[9] 成交额(元) 可反推校验），
        # 股 → 万手 需 /100(/手) /1e4(万手) = /1e6。旧代码 /1e4 少除了 100。
        volume_wan = volume / 1e6 if volume else 0.0
        return {
            "name": name, "price": price, "pre_close": pre_close, "open": open0,
            "change": round(price - pre_close, 2),
            "change_pct": round((price - pre_close) / pre_close * 100, 2) if pre_close else 0,
            "high": high, "low": low, "volume": volume / 100.0, "volume_wan": volume_wan,
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


def _norm_intraday_time(t: str) -> str:
    """把各数据源的分时时间统一成 HH:MM。

    腾讯是 "0930"（无分隔符，且不带日期）；东财是 "2026-09-03 09:30"（带日期）。
    前端 X 轴直接显示，必须口径一致，否则会出现 "0930" 和 "09:30" 混排。
    """
    t = (t or "").strip()
    if not t:
        return ""
    if " " in t:                 # 东财：日期 + 时间，只留时间部分
        t = t.split(" ")[-1]
    if len(t) >= 5 and t[2] == ":":   # 已是 HH:MM(:SS)
        return t[:5]
    if len(t) >= 4:              # 腾讯：HHMM
        return f"{t[:2]}:{t[2:4]}"
    return t


def _fetch_intraday_tencent(code: str):
    """腾讯分时。返回 (prices, vols, avg_price, times)。
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
        prices, vols, times = [], [], []
        for line in node:
            parts = line.split()
            if len(parts) >= 3:
                times.append(_norm_intraday_time(parts[0]))
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
        return prices, vols, avg_price, times
    except Exception:
        return None


def _fetch_intraday_eastmoney(code: str, ndays: int = 1):
    """东财分时（trends2），腾讯失败时兜底。返回 (prices, vols, avg_price, times, amounts)。

    注意：这里是 5 元组，与腾讯源的 4 元组不同，
    **不要**直接把本函数的返回值透传给按 3/4 元组解包的调用方。"""
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
                times.append(_norm_intraday_time(parts[0]))
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

# 日线新鲜度缓存（2026-09-03 新增，性能）：
# ensure_quotes() 原本「先 HTTP 直连、失败才回 DB」，等于每次调用都要拉一次日线 +
# DELETE/INSERT 180 行。可投池 138 只票、一页 15 只，冷启动串行起来要 7 秒左右，
# 而纯 DB 读只要 0.018s。日线用于 MA5/MA20/箱体/20日均量，盘中不需要秒级新鲜度，
# 故加一层「新鲜度短路」：60s 内（收盘后 30min）直接复用上次结果。
# key = f"{code}:{days}"，LRU 上限 300 条，防止 screener 遍历全市场时撑爆内存。
_DAILY_FRESH: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_DAILY_FRESH_MAX = 300
_DAILY_TTL_LIVE = 60.0      # 盘中：60 秒
_DAILY_TTL_CLOSED = 1800.0  # 收盘/周末：30 分钟（日线已定格）


def _daily_ttl() -> float:
    """日线新鲜度窗口：收盘后/周末放宽到 30 分钟，其余按 60 秒。"""
    n = dt.datetime.now()
    if n.weekday() >= 5:
        return _DAILY_TTL_CLOSED
    return _DAILY_TTL_CLOSED if n.hour * 60 + n.minute >= 15 * 60 else _DAILY_TTL_LIVE


def _daily_fresh_get(code: str, days: int) -> list | None:
    """命中新鲜缓存则返回副本（浅拷贝 list，避免调用方改动内部缓存）。"""
    hit = _DAILY_FRESH.get(f"{code}:{days}")
    if not hit:
        return None
    if (time.time() - hit[0]) >= _daily_ttl():
        return None
    _DAILY_FRESH.move_to_end(f"{code}:{days}")
    return list(hit[1])


def _daily_fresh_put(code: str, days: int, quotes: list) -> None:
    """写入新鲜缓存。注意：演示数据不要调用，避免把假数据钉住一个刷新周期。"""
    key = f"{code}:{days}"
    _DAILY_FRESH[key] = (time.time(), list(quotes))
    _DAILY_FRESH.move_to_end(key)
    while len(_DAILY_FRESH) > _DAILY_FRESH_MAX:
        _DAILY_FRESH.popitem(last=False)


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


def _fetch_sina_minute_range_once(code: str, datalen: int) -> list[tuple[str, int, float, float]] | None:
    """拉一次新浪分钟 K 线（datalen 由调用方给定，上限 1800）。"""
    mkt = _market_of(code)  # sh/sz/bj, 新浪也是同样前缀
    sym = f"{mkt}{code}"
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
        # 新浪超上限时返回 "=(null);" -> arr 为 None
        if not isinstance(arr, list) or not arr:
            return None
        out: list[tuple[str, int, float, float]] = []  # (date, hm, cum_amount_yuan, cum_volume_hand)
        cum_amt = 0.0
        cum_vol = 0.0
        last_date = None
        for a in arr:
            date_str = str(a.get("day", ""))  # "2026-09-02 09:45:00"
            if not date_str or len(date_str) < 16:
                continue
            d = date_str[:10]
            if d != last_date:
                # 换交易日 -> 累计归零，保证 cum_* 语义 = 当日累计（见 docstring 坑 2）
                cum_amt = 0.0
                cum_vol = 0.0
                last_date = d
            amt = _as_float(a.get("amount", 0))
            # 新浪K线 volume 实测单位 = 股（2026-09-03 交叉验证：
            #   000001 T-1 volume_total=89224752，成交额 10.63 亿、均价 ~11.91
            #   → 89224752股×11.91 ≈ 10.63 亿，完全吻合；若按手算会大 100 倍；
            #   且 /100 = 892248 手 与腾讯日线 09-02 volume=892248 完全一致）。
            # 这里统一折算成「手」，与腾讯日线 / 盘口口径对齐，避免下游再踩单位坑。
            vol = _as_float(a.get("volume", 0)) / 100.0
            cum_amt += amt
            cum_vol += vol
            # hm_int: HHMM 整数, 例 09:45 -> 945
            hm = int(date_str[11:13]) * 100 + int(date_str[14:16])
            out.append((d, hm, cum_amt, cum_vol))
        return out if out else None
    except Exception:
        return None


def _fetch_sina_minute_range(code: str, days_back: int = 10) -> list[tuple[str, int, float, float]] | None:
    """新浪分钟级 K 线。返回 [(date_str, hm_int, cum_amount_yuan, cum_volume_hand), ...] 或 None。

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

    三个已踩过的坑(2026-09-03 修复):
      1) datalen 有硬上限: 实测 1800 正常、>=2000 直接返回 "=(null);" (57 字节),
         而 days_back=10 会算出 2430 -> 整条链路静默返回 None。上限钳到 1800。
      2) 累计量必须「按日重置」: 单根 amount/volume 是当分钟的增量,
         跨天后若不归零, cum 会变成「自窗口起点以来的总量」,
         导致 T-1 全天成交量被放大若干倍(且随 datalen 增大而变)。
         cum_* 的语义固定为「当日累计」。
      3) 单根 volume 单位是「股」不是「手」, 已在 _once() 内折算成手统一口径。

    取数策略(性能):
      要 T-1 只需 2 个交易日, 但周末/假期要往前跳, 所以「小窗口优先、不够再扩」:
      先用 800 根(≈3.3 个交易日, 覆盖周末) 拉, 若已含 >=2 个不同日期就直接返回;
      只有遇到长假(窗口里凑不出 2 个交易日)才回退到 1800 根重试。
      实测 1800 根响应体约 400KB、单只 3.4s; 800 根约 210KB, 一页 15 只的首次加载明显更快。

    缓存：_INTRADAY_CACHE（30s, 同其它分钟接口, 避免连续刷新打爆新浪）。
    """
    small = min(max(days_back * 240 + 30, 800), 1800)
    for datalen in ((800,) if small <= 800 else (800, 1800)):
        rows = _fetch_sina_minute_range_once(code, datalen)
        if not rows:
            continue
        # 够用判定：窗口里至少要有 2 个不同交易日，否则说明被假期掏空，扩窗重试
        if len({d for d, _, _, _ in rows}) >= 2 or datalen == 1800:
            return rows
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
    """分时多源互备：腾讯 → 东财。

    **统一返回 4 元组 (prices, vols, avg_price, times)**。

    两个源的原始结构不同：腾讯 4 元组、东财 5 元组（多 amounts）。
    若直接 `return tencent() or eastmoney()` 透传，一旦腾讯失败、东财接管，
    调用方 `prices, vols, avg = intra` 会 ValueError（东财 5 元组解不进 3 个变量），
    且该处不在 try 内 → 整个做T分析接口 500。故在此归一。
    times 不可用时为空列表，调用方需按长度判断是否可用。
    """
    raw = _fetch_intraday_tencent(code) or _fetch_intraday_eastmoney(code)
    if not raw:
        return None
    prices, vols, avg_price = raw[0], raw[1], raw[2]
    times = raw[3] if len(raw) > 3 else []
    return prices, vols, avg_price, times


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
            _, _, avg_price, _ = intra
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
      4. C. 资金与板块(25 分):量比 10/主力资金净流入 8/板块共振 4/跳空缺口 3。
         v184: 板块共振由 5 档 7 分改为强/弱二档 4 分(强=板块涨且个股同步),新增跳空缺口 3 分,
         C 类总分仍为 25 分。
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
    # v176: avg_volume_5 近5日均量(万手),供"历史活跃度(5日均量)"评分用。短期均值更贴近近期活跃度。
    avg_volume_5 = (sum(volumes[-5:]) / min(5, len(volumes))) / 1e4 if volumes else 0.0
    out["avg_volume_5"] = round(avg_volume_5, 2)
    # 流动性阈值改为「日均成交量(万手)」口径(见下方否决项与 A 档评分),
    # 不再用成交额(元)。注意: 万手口径与价格无关, 高价股(如茅台)成交量(万手)偏小,
    # 用万手阈值判流动性会低估其真实流动性 —— 这是切换口径的固有代价(用户2026-09-03确认)。
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

    # 1) 日均成交量(20日)<5万手
    if avg_volume_20 < 5.0:
        veto_reasons.append("日均成交量<5万手")
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
    # v179: A 类 5 指标每个默认满分 8 分,权重均摊各 20%(iw 自动 even=100/5=20)
    # 阈值沿用 v176 规则;score 按各原 bands 等比缩放到 max=8:
    #   5日均 / 当日vs20日均:原 5/4/3/1/0 × 8/5 → 8/6/5/2/0
    #   日内振幅:            原 15/12/9/5/0 × 8/15 → 8/6/5/3/0
    #   换手率:              原 10/8/7/4/3 × 8/10 → 8/6/6/3/2
    # 新增的 20 日均量打分规则与 5 日均量完全一致(阈值+score 都不变)。

    # 1a) 历史活跃度(5日均量,万手) 8分 —— v176 改用 5 日均量贴近近期活跃度
    if avg_volume_5 >= 40.0:
        score_a += 8
        detail.append("历史活跃度(5日均)≥40万手(+8)")
    elif avg_volume_5 >= 20.0:
        score_a += 6
        detail.append("历史活跃度(5日均)20~40万手(+6)")
    elif avg_volume_5 >= 10.0:
        score_a += 5
        detail.append("历史活跃度(5日均)10~20万手(+5)")
    elif avg_volume_5 >= 5.0:
        score_a += 2
        detail.append("历史活跃度(5日均)5~10万手(+2)")
    else:
        detail.append("历史活跃度(5日均)<5万手(否决)")

    # 1a-long) v179 新增:历史活跃度(20日均量,万手) 8分 —— 规则与 5 日均量完全一致
    if avg_volume_20 >= 40.0:
        score_a += 8
        detail.append("历史活跃度(20日均)≥40万手(+8)")
    elif avg_volume_20 >= 20.0:
        score_a += 6
        detail.append("历史活跃度(20日均)20~40万手(+6)")
    elif avg_volume_20 >= 10.0:
        score_a += 5
        detail.append("历史活跃度(20日均)10~20万手(+5)")
    elif avg_volume_20 >= 5.0:
        score_a += 2
        detail.append("历史活跃度(20日均)5~10万手(+2)")
    else:
        detail.append("历史活跃度(20日均)<5万手(否决)")

    # 1b) + 1c) 合并 —— v176: "当日/前日成交量 vs 20日均成交量" 8分
    #     痛点: 盘前/盘中开盘初期 volumes[-1] 极小或为 0,"当日 vs 20日均" 的值是噪声,统计无意义。
    #     解法: 15:00 之前用"前日 vs 20日均"打分(避开当日 0 量), 15:00 之后(收盘)切回"当日 vs 20日均"。
    #     两个原始量都写到 out,前端按当前时间直接展示对应值即可,逻辑在数据层做完。
    day_vs_ma20_pct: float | None = None
    vol_ratio_prev_vs_ma20: float | None = None
    avg_vol_hands = avg_volume_20 * 1e4
    if avg_vol_hands > 0 and volumes:
        day_vs_ma20_pct = round((volumes[-1] - avg_vol_hands) / avg_vol_hands * 100, 2)
        if len(volumes) >= 2 and volumes[-2] and volumes[-2] > 0:
            vol_ratio_prev_vs_ma20 = round((volumes[-2] - avg_vol_hands) / avg_vol_hands * 100, 2)
    out["vol_ratio_vs_ma20"] = day_vs_ma20_pct            # 当日 vs 20日均(%)
    out["vol_ratio_prev_vs_ma20"] = vol_ratio_prev_vs_ma20  # 前日 vs 20日均(%)
    # 评分按当前服务器时间切:>=15:00 视为"当日数据已定型",用当日;否则用前日避免 0 量污染
    is_after_close = dt.datetime.now().time() >= dt.time(15, 0)
    used_field = "当日" if is_after_close else "前日"
    pct_for_score = day_vs_ma20_pct if is_after_close else vol_ratio_prev_vs_ma20
    if pct_for_score is not None:
        if pct_for_score >= 50.0:
            score_a += 8; detail.append(f"当日/前日vs20日均量({used_field})+{pct_for_score:.1f}%(+8)")
        elif pct_for_score >= 20.0:
            score_a += 6; detail.append(f"当日/前日vs20日均量({used_field})+{pct_for_score:.1f}%(+6)")
        elif pct_for_score >= 0.0:
            score_a += 5; detail.append(f"当日/前日vs20日均量({used_field})+{pct_for_score:.1f}%(+5)")
        elif pct_for_score >= -20.0:
            score_a += 2; detail.append(f"当日/前日vs20日均量({used_field}){pct_for_score:.1f}%(+2)")
        else:
            detail.append(f"当日/前日vs20日均量({used_field}){pct_for_score:.1f}%(+0)")
    else:
        detail.append("当日/前日vs20日均量无数据(+0)")

    # 2) 日内振幅(近5日平均) 8分 (原 15 分,v179 按 8/15 缩放)
    if avg_amplitude_5 >= 6.0:
        score_a += 8
        detail.append("振幅≥6%(+8)")
    elif avg_amplitude_5 >= 4.0:
        score_a += 6
        detail.append("振幅4~6%(+6)")
    elif avg_amplitude_5 >= 3.0:
        score_a += 5
        detail.append("振幅3~4%(+5)")
    elif avg_amplitude_5 >= 2.0:
        score_a += 3
        detail.append("振幅2~3%(+3)")
    else:
        detail.append("振幅<2%(否决)")

    # 3) 换手率 8分 (原 10 分,v179 按 8/10 缩放)
    if 5.0 <= turnover <= 10.0:
        score_a += 8
        detail.append("换手率5~10%(+8)")
    elif 3.0 <= turnover < 5.0:
        score_a += 6
        detail.append("换手率3~5%(+6)")
    elif 10.0 < turnover <= 15.0:
        score_a += 6
        detail.append("换手率10~15%(+6)")
    elif 1.0 <= turnover < 3.0:
        score_a += 3
        detail.append("换手率1~3%(+3)")
    elif turnover > 15.0:
        score_a += 2
        detail.append("换手率>15%过热(+2)")
    else:
        detail.append("换手率<1%(+0)")

    # ===== B. 趋势与位置(35分) =====
    # 1) MA5/MA20 位置 15分
    # v185: 由旧的「当日双上15 / 仅MA5八分 / 双下0」改为用户定义的 3 档:
    #   档1 连续3日双上: 最近 3 个交易日收盘「同时」站上 MA5 与 MA20 → 15 分(趋势确立,最强)
    #   档2 仅MA20站上:  连续3日双上不成立,但当日收盘站上 MA20 → 8 分(中期不坏,短期待确认)
    #   档3 连续3日下跌: 最近 3 日收盘价逐日走低(closes[-1]<closes[-2]<closes[-3]) → 0 分
    #   其余中间态(如跌破MA20但没连跌)→ 4 分中性偏低,既不误杀也不白给
    # 判定期望顺序:先看最强(连3双上),再看最弱(连3下跌),最后看仅MA20
    def _ma_series(src: list[float], n: int) -> list[float | None]:
        """滑动均线序列,前 n-1 位为 None(不足以成线)。"""
        return [None if i < n - 1 else (sum(src[i - n + 1:i + 1]) / n) for i in range(len(src))]

    if len(closes) >= 22:   # 至少 20(MA20) + 3(连3判定) - 1
        ma5s, ma20s = _ma_series(closes, 5), _ma_series(closes, 20)
        idxs = (-3, -2, -1)
        above3 = all(ma5s[i] is not None and ma20s[i] is not None
                     and closes[i] > ma5s[i] and closes[i] > ma20s[i] for i in idxs)
        fall3 = closes[-1] < closes[-2] < closes[-3]
        ma20_only = (ma20s[-1] is not None) and (closes[-1] > ma20s[-1])
        if above3:
            score_b += 15
            out["ma_position"] = "above3"
            detail.append("连续3日站上MA5与MA20(+15)")
        elif fall3:
            out["ma_position"] = "fall3"
            detail.append("连续3日下跌(+0)")
        elif ma20_only:
            score_b += 8
            out["ma_position"] = "ma20_only"
            detail.append("仅站上MA20(+8)")
        else:
            score_b += 4
            out["ma_position"] = "mid"
            detail.append("MA5/MA20中间态(+4)")
    else:
        # 日线不足 22 根(新票/停牌) → 退化到单日口径,避免「数据不足恒 0」误杀
        if above_ma5 is True and above_ma20 is True:
            score_b += 8
            out["ma_position"] = "above3_short"
            detail.append("站上MA5且MA20(日线不足22根,降级+8)")
        elif above_ma20 is True:
            score_b += 4
            out["ma_position"] = "ma20_only_short"
            detail.append("仅站上MA20(日线不足22根,降级+4)")
        else:
            out["ma_position"] = "unknown"
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
    # v184 fix: 旧代码只把 MACD 状态写进 detail 文案,从未写入 track 字段,
    #   前端评分模型 macd 指标 source=t.macd_signal 永远取不到值 → B 类 MACD 恒显示「无数据」/ 0 分。
    #   这里统一产出 macd_signal(与前端 valid 枚举一致)+ macd 明细,供评分与展示复用。
    if dif and dea and hist and len(hist) >= 2:
        dif_last = dif[-1]
        hist_last, hist_prev = hist[-1], hist[-2]
        above_zero = dif_last > 0
        red_expand = hist_last > hist_prev > 0
        red_shrink = hist_prev > hist_last > 0
        green_shrink = 0 > hist_prev > hist_last
        green_expand = 0 > hist_last > hist_prev
        out["macd"] = {"dif": round(dif_last, 4), "dea": round(dea[-1], 4),
                       "hist": round(hist_last, 4), "above_zero": bool(above_zero)}
        if above_zero and red_expand:
            score_b += 10
            out["macd_signal"] = "red_expand"
            detail.append("MACD零轴上方红柱放大(+10)")
        elif above_zero and red_shrink:
            score_b += 7
            out["macd_signal"] = "red_shrink"
            detail.append("MACD零轴上方红柱缩短(+7)")
        elif not above_zero and green_shrink:
            score_b += 4
            out["macd_signal"] = "green_shrink"
            detail.append("MACD零轴下方绿柱缩短(+4)")
        elif not above_zero and green_expand:
            out["macd_signal"] = "green_expand"
            detail.append("MACD零轴下方绿柱放大(+0/否决)")
        else:
            out["macd_signal"] = "neutral"
            detail.append("MACD状态中性(+0)")
    else:
        out["macd_signal"] = "missing"   # 前端 valid 不含 missing → 不计分,展示「数据不足」
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

    # 3) 板块共振 4分(v184: 原 5 档 7 分 → 强/弱二档 4 分, C 类总 25 分不变: 量比10+主力8+板块4+缺口3)
    #    强 = 板块上涨(>1%)且个股同步上涨/领涨 → 4 分; 其余(板块平/跌、板块涨但个股跌) → 弱 = 0 分。
    #    行业信息缺失/取数异常 → missing: 不计分(前端 valid 排除后权重自动转移), 不误杀也不白给分。
    try:
        from app.models import Stock
        stock = db.get(Stock, code)
        industry = (stock.industry or "").strip() if stock else ""
        if not industry:
            # v184: 库里行业缺失(5549 只仅 44 只有值) → 现场用东财 push2 f127 补一次并落库
            industry = _fetch_industry_em(code)
            if industry and stock is not None:
                try:
                    stock.industry = industry
                    db.commit()
                except Exception:
                    db.rollback()
        if industry:
            # v184: 旧代码取 get_sector_trend()["change_pct"],但该字典根本没有该字段 → 恒 0,
            #   板块共振永远判不出「强」。改用同行业样本股最近一日平均涨跌幅(真实值)。
            sector_pct = _sector_day_change(industry, db)
            stock_pct = out.get("change_pct", 0)
            out["sector_industry"] = industry        # v184: 行业名(前端可展示/核对)
            if sector_pct is not None:
                out["sector_pct"] = sector_pct      # v185: 强/弱都输出,避免详情里缺板块涨跌幅
            if sector_pct is None:
                out["sector_resonance"] = "missing"
                detail.append(f"板块样本不足,共振不计分(行业{industry},+0/4)")
            elif sector_pct > 1.0 and stock_pct > 0:
                out["sector_resonance"] = "strong"
                score_c += 4
                detail.append(f"板块涨{sector_pct}%且个股上涨(+4)")
            else:
                out["sector_resonance"] = "weak"
                detail.append(f"板块共振弱(板块{sector_pct}%/个股{stock_pct}%)(+0)")
        else:
            out["sector_resonance"] = "missing"
            detail.append("行业信息缺失,板块共振不计分(+0/4)")
    except Exception:
        out["sector_resonance"] = "missing"
        detail.append("板块共振数据未接入,不计分(+0/4)")

    # 4) 跳空缺口 3分(v184 新增): 向上跳空=强势信号 3 分; 无缺口/向下缺口 0 分(向下本就是风险项)
    #    口径与 get_pool_track 的 out["gap"] 完全一致: 今日开盘 vs 昨日最高/最低价
    try:
        if len(quotes) >= 2 and quotes[-1].open and quotes[-2].close:
            _o = quotes[-1].open
            _yh, _yl = quotes[-2].high, quotes[-2].low
            if _yh and _o > _yh:
                out["gap_signal"] = "up"
                score_c += 3
                detail.append("向上跳空缺口(+3)")
            elif _yl and _o < _yl:
                out["gap_signal"] = "down"
                detail.append("向下跳空缺口(+0)")
            else:
                out["gap_signal"] = "flat"
                detail.append("无跳空缺口(+0)")
        else:
            out["gap_signal"] = "missing"
            detail.append("跳空缺口数据不足,不计分(+0/3)")
    except Exception:
        out["gap_signal"] = "missing"

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


# ============ 可投池扩展指标：多源真实数据抓取 ============
# 说明：资金流等"当日"类指标仅在开盘后到收盘前/定格时有数据，源端盘前
# 通常为空；此时函数返回 None，前端统一显示 "-"（带 tooltip 说明），不编造数据。
# 注：原「北向资金(个股当日净买入)」列已删除——2024-08-19 起沪深港通调整披露
# 机制，不再公布个股实时净买入（仅收市后成交总额/前十大活跃/季度末持股）。
_VALUATION_CACHE: dict = {}
_UNLOCK_CACHE: dict = {}
_FIN_CACHE: dict = {}
_NEWS_CACHE: dict = {}


def _em_json(url: str):
    """拉取并解析东方财富类 JSON 接口；失败返回 None。"""
    txt = _http_get(url)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _fetch_capital_flow(code: str) -> dict | None:
    """主力/大单资金流（东方财富数据中心【日级】资金流报表 RPT_DMSK_TS_STOCKNEW）。

    ⚠️ 重要口径（v116 修正）：该报表是「日级、收盘后落地」的，盘中查不到当日行——
    实测 2026-09-04 盘中查询最新行仍为 2026-09-03。因此调用方必须用 trade_date
    与「当日」比对后才能使用，否则会出现「昨天的资金流 ÷ 今天的成交额」的错值
    （历史上曾因此把跌停股显示成"主力流入 8.59%"）。

    返回字段(均为元): main_net=主力净流入净额, xl_net=超大单净额, l_net=大单净额,
    trade_date=该行所属交易日(YYYY-MM-DD)。"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_TS_STOCKNEW"
           f"&columns=ALL&filter=(SECURITY_CODE%3D%22{code}%22)&pageSize=2"
           "&sortColumns=TRADE_DATE&sortTypes=-1")
    j = _em_json(url)
    if not j or not isinstance(j.get("result"), dict):
        return None
    rows = j["result"].get("data") or []
    if not rows:
        return None
    r = rows[0]
    s_in = _as_float(r.get("SUPERDEAL_INFLOW")) or 0.0
    s_out = _as_float(r.get("SUPERDEAL_OUTFLOW")) or 0.0
    b_in = _as_float(r.get("BIGDEAL_INFLOW")) or 0.0
    b_out = _as_float(r.get("BIGDEAL_OUTFLOW")) or 0.0
    return {
        "main_net": _as_float_or_none(r.get("PRIME_INFLOW")),   # 主力净流入净额(元)；无数据=None
        "xl_net": s_in - s_out,                          # 超大单净流入净额(元)
        "l_net": b_in - b_out,                           # 大单净流入净额(元)
        "trade_date": (r.get("TRADE_DATE") or "")[:10],
    }


# 盘中资金流缓存：code -> (ts, rec)，TTL 120s（避免刷新页面反复打 push2）
_FUND_INTRADAY_TTL = 120.0
_FUND_INTRADAY_CACHE: dict[str, tuple[float, dict]] = {}

# v174：东财 push2 域名池。云端部署沙箱常拦截 push2 主域名（v123 已证实），
# 而 push2his / 数字 CDN 节点可能仍可达；取数时按序回退，任一返回有效数值即采用。
_EM_PUSH_HOSTS = ["push2.eastmoney.com", "push2his.eastmoney.com", "82.push2.eastmoney.com"]


def _fund_from_fflow(code: str, today: str) -> dict | None:
    """单只「当日累计资金流」：东财 push2 `stock/fflow/kline/get`（klt=1 分钟级）。

    返回 {"main_net", "xl_net", "l_net"}（单位元，带符号）或 None。
    只取 klines 末条，并校验其日期 == today：
      休市日/源滞后时末条是上一交易日，绝不能拿昨日冒充今日（v137 原则）。
    """
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
           "?lmt=0&klt=1&secid=" + _secid(code)
           + "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56")
    j = _em_json(url)
    kl = (((j or {}).get("data") or {}).get("klines")) or []
    if not kl:
        return None
    parts = str(kl[-1]).split(",")
    if len(parts) < 6:
        return None
    if not parts[0].strip().startswith(today):
        return None   # 非当日（休市/源滞后）→ 视为无数据
    main_net = _as_float_or_none(parts[1])
    if main_net is None:
        return None
    return {
        "main_net": main_net,                    # 主力净额 = 大单 + 超大单
        "xl_net": _as_float_or_none(parts[5]),   # 超大单净额
        "l_net": _as_float_or_none(parts[4]),    # 大单净额
        "main_pct": None,                        # 该源不返回占比，由调用方按实时成交额计算
    }


def _fetch_intraday_fund(codes: list[str]) -> dict[str, dict]:
    """【当日盘中】资金流。

    v175 主源切换：东财 push2 的批量接口 `ulist.np`（f62/f184）在本沙箱与云端均被
    连接级拒绝（RemoteProtocolError，实测本地 0/5、云端同样取不到），而同域名的
    `stock/fflow/kline/get` 路径实测 HTTP 200 可用，返回**当日累计**资金流：
        data.klines[-1] = "时间,主力净额,小单,中单,大单,超大单"（元，带符号）
        实测 f52(主力) == f55(大单) + f56(超大单)，且随时间单调递增 → 末条即当日累计值。

    该源不返回占比，main_pct 由调用方按「当日实时成交额」计算；
    只接受末条日期 == 今日，避免休市日/源滞后时拿昨日冒充今日（v137 原则）。
    fflow 不可达时回落旧 ulist.np 多域名兜底。
    """
    codes = [c for c in dict.fromkeys(codes or []) if c]
    if not codes:
        return {}
    now = time.time()
    out: dict[str, dict] = {}
    todo: list[str] = []
    for c in codes:
        hit = _FUND_INTRADAY_CACHE.get(c)
        if hit and (now - hit[0]) < _FUND_INTRADAY_TTL:
            out[c] = hit[1]
        else:
            todo.append(c)
    # v175：主源 = fflow/kline（单只请求，实测可达），逐只取当日累计资金流
    _today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    missing: list[str] = []
    for c in todo:
        rec = _fund_from_fflow(c, _today)
        if rec:
            _FUND_INTRADAY_CACHE[c] = (now, rec)
            out[c] = rec
        else:
            missing.append(c)
    # 兜底：fflow 路径不可达时，回落旧批量接口 ulist.np（多域名逐个尝试）
    if missing:
        for host in _EM_PUSH_HOSTS:
            secids = ",".join(_secid(c) for c in missing)
            url = ("https://" + host + "/api/qt/ulist.np/get?secids=" + secids
                   + "&fields=f12,f62,f184,f66,f72&invt=2&fltt=2")
            j = _em_json(url)
            diff = (((j or {}).get("data") or {}).get("diff")) or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            got = 0
            for it in diff:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("f12") or "")
                # v174：必须严格判空——东财无数据时字段为 "-"，旧 _as_float 会返回 0.0，
                # 使「无数据」被误判为「净流入 0」并落库显示 0.00%
                main_net = _as_float_or_none(it.get("f62"))
                if not code or main_net is None:
                    continue
                rec = {
                    "main_net": main_net,                            # 主力(超大单+大单)净额
                    "main_pct": _as_float_or_none(it.get("f184")),   # 主力净流入占比(%)
                    "xl_net": _as_float(it.get("f66")),      # 超大单净额
                    "l_net": _as_float(it.get("f72")),       # 大单净额(不含超大单)
                }
                _FUND_INTRADAY_CACHE[code] = (now, rec)
                out[code] = rec
                got += 1
            if got:
                break   # 该域名拿到了有效数据，不再尝试其余域名
    return out


def _pct_of_amount(net, quotes) -> float | None:
    """净额(元) / 当日成交额 * 100。成交额取日线最新一根(即当日)，缺失时按量*价兜底。"""
    if net is None or not quotes:
        return None
    amount = (quotes[-1].amount if quotes else 0) or 0.0
    if amount <= 0:
        amount = (quotes[-1].volume or 0) * 100 * (quotes[-1].close or 0)
    if amount <= 0:
        return None
    return round(net / amount * 100, 2)


def _fetch_valuation(code: str) -> dict | None:
    """当前 PE(TTM)/PB + 近3年 PE 历史分位。

    数据源兜底（v123, 2026-09-05）：云端部署沙箱仅能连通腾讯(qt.gtimg.cn /
    web.ifzq.gtimg.cn / push2his)，东财 push2 / emweb / datacenter-web 均被拦截或
    接口已废弃(reportName 全部返回「报表配置不存在」)。因此：
      - pe_ttm: 优先腾讯 qt.gtimg.cn（云端可达），兜底东财 push2（仅本地可达）。
      - pb:     腾讯 qt.gtimg.cn 当前响应不含市净率字段，仅东财 push2 兜底（本地可达）。
      - pe_pct_3y: 东财 emweb 估值历史（仅本地可达，云端留空）。
    """
    market = "SH" if _market_of(code) == "sh" else ("SZ" if _market_of(code) == "sz" else "BJ")
    secid = _secid(code)
    cached = _VALUATION_CACHE.get(code)
    if cached and (time.time() - cached.get("_t", 0)) < 3600:
        return cached
    out: dict = {}
    # 当前 PE(TTM)/PB —— 优先腾讯(云端可达)，兜底东财 push2(本地)
    # 1) 腾讯 qt.gtimg.cn: f[39]=市盈率TTM, f[40]=市净率(部分品种缺失)
    try:
        mkt = _market_of(code)
        txt = _http_get(f"https://qt.gtimg.cn/q={mkt}{code}",
                        headers={"Referer": "https://gu.qq.com/"})
        if txt and "=" in txt:
            f = txt.split("=", 1)[1].strip().strip('";\n').split("~")
            pe = _as_float(f[39]) if len(f) > 39 else None
            pb = _as_float(f[40]) if len(f) > 40 else None
            if pe:
                out["pe_ttm"] = pe
            if pb:
                out["pb"] = pb
    except Exception:
        pass
    # 2) 东财 push2 兜底(本地可达；云端 push2 被拦截时跳过)
    if "pe_ttm" not in out or "pb" not in out:
        try:
            url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
                   f"&fields=f162,f167&invt=2&ut=fa5fd194d4d9e83cfbfd4f3f3b5f1c5")
            j = _em_json(url)
            if j and isinstance(j.get("data"), dict):
                if "pe_ttm" not in out:
                    out["pe_ttm"] = _as_float(j["data"].get("f162"))
                if "pb" not in out:
                    out["pb"] = _as_float(j["data"].get("f167"))
        except Exception:
            pass
    # 近3年 PE 历史分位(东财 emweb, 仅本地可达；云端 emweb 被拦截→留空)
    try:
        vurl = (f"https://emweb.securities.eastmoney.com/PC_HSF10/ValuationAnalysis/"
                f"PageAjax?code={market}{code}&type=0&isforettm=0")
        jv = _em_json(vurl)
        if jv and isinstance(jv.get("result"), dict):
            rows = jv["result"].get("data") or []
            pes = [_as_float(r.get("PE_TTM")) for r in rows if _as_float(r.get("PE_TTM")) > 0]
            if pes:
                out["pe_history_count"] = len(pes)
                if out.get("pe_ttm"):
                    below = sum(1 for v in pes if v <= out["pe_ttm"])
                    out["pe_pct_3y"] = round(below / len(pes) * 100, 1)
    except Exception:
        pass
    out["_t"] = time.time()
    _VALUATION_CACHE[code] = out
    return out


def _fetch_unlock(code: str) -> dict | None:
    """未来解禁：最近一次解禁日与数量（真实数据，东方财富解禁报表）。"""
    cached = _UNLOCK_CACHE.get(code)
    if cached and (time.time() - cached.get("_t", 0)) < 86400:
        return cached
    out: dict = {"_t": time.time()}
    try:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_FA_LIFT_LOCK_MAIN"
               "&columns=ALL&filter=(SECURITY_CODE%3D%22" + code + "%22)&pageSize=8"
               "&sortColumns=LISTING_DATE&sortTypes=-1")
        j = _em_json(url)
        if j and isinstance(j.get("result"), dict) and j["result"].get("data"):
            rows = j["result"]["data"]
            today = dt.date.today()
            future = [r for r in rows if r.get("LISTING_DATE") and
                      dt.datetime.strptime(str(r["LISTING_DATE"])[:10], "%Y-%m-%d").date() >= today]
            if future:
                nx = future[0]
                out["next_date"] = str(nx.get("LISTING_DATE"))[:10]
                out["next_amount"] = _as_float(nx.get("LIFT_LOCK_AMOUNT"))    # 解禁数量(股)
                out["next_ratio"] = _as_float(nx.get("LIFT_LOCK_RATIO"))      # 解禁占比(%)
    except Exception:
        pass
    _UNLOCK_CACHE[code] = out
    return out


def _fetch_financials(code: str) -> dict | None:
    """最新财报关键指标（真实数据，东方财富业绩报表）。

    字段映射（v135 → v136 数据源切换）：
    - report_date     ← RPT_LICO_FN_CPD.REPORTDATE     (新源，可达)
    - net_profit      ← RPT_LICO_FN_CPD.PARENT_NETPROFIT (新源)
    - net_profit_yoy  ← RPT_LICO_FN_CPD.SJLTZ           (新源，净利润同比%)
    - revenue_yoy     ← RPT_LICO_FN_CPD.YSTZ            (新源，营收同比%)
    - bps             ← RPT_LICO_FN_CPD.BPS             (新源，每股净资产)
    - equity          ← 暂无可达源，留 None
    - goodwill        ← RPT_FCI_BUSINESSASSET.GOOD_WILL (旧源，2024 起 9501 留 fallback)
    """
    cached = _FIN_CACHE.get(code)
    if cached and (time.time() - cached.get("_t", 0)) < 86400:
        return cached
    out: dict = {"_t": time.time()}
    # 1) 新版业绩报表（datacenter.eastmoney.com/securities 域）
    try:
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
               "?reportName=RPT_LICO_FN_CPD"
               "&columns=ALL"
               "&filter=(SECURITY_CODE%3D%22" + code + "%22)"
               "&pageSize=1&sortColumns=REPORTDATE&sortTypes=-1")
        j = _em_json(url)
        if j and isinstance(j.get("result"), dict) and j["result"].get("data"):
            r = j["result"]["data"][0]
            rd = str(r.get("REPORTDATE") or "")[:10]
            if rd:
                out["report_date"] = rd
            ystz = _as_float(r.get("YSTZ"))          # 营业总收入同比(%)
            sjltz = _as_float(r.get("SJLTZ"))        # 归属母公司净利润同比(%)
            if ystz is not None:
                out["revenue_yoy"] = ystz
            if sjltz is not None:
                out["net_profit_yoy"] = sjltz
            np_ = _as_float(r.get("PARENT_NETPROFIT"))  # 归母净利润(元)
            if np_ is not None:
                out["net_profit"] = np_
            bps = _as_float(r.get("BPS"))            # 每股净资产
            if bps is not None:
                out["bps"] = bps
    except Exception:
        pass
    # 2) 商誉（资产负债表）：旧源 RPT_FCI_BUSINESSASSET 2024 起 9501 留 fallback
    try:
        gurl = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_FCI_BUSINESSASSET"
                "&columns=ALL&filter=(SECURITY_CODE%3D%22" + code + "%22)&pageSize=2"
                "&sortColumns=REPORT_DATE&sortTypes=-1")
        jg = _em_json(gurl)
        if jg and isinstance(jg.get("result"), dict) and jg["result"].get("data"):
            g = jg["result"]["data"][0]
            gw = _as_float(g.get("GOOD_WILL"))             # 商誉(元)
            if gw is not None:
                out["goodwill"] = gw
                if g.get("REPORT_DATE"):
                    out["goodwill_date"] = str(g["REPORT_DATE"])[:10]
    except Exception:
        pass
    _FIN_CACHE[code] = out
    return out


def _classify_sentiment(text: str) -> str:
    """关键词启发式判定新闻情绪：利好/利空/中性。明确为启发式，非权威分类。"""
    if not text:
        return "neutral"
    bull = ["利好", "增持", "回购", "中标", "签约", "增长", "扭亏", "预增", "获批", "大涨",
            "突破", "合作", "订单", "扩产", "分红", "高送转", "业绩预增", "上调", "机构调研", "新高"]
    bear = ["减持", "亏损", "预亏", "下滑", "下降", "退市", "ST", "问询", "处罚", "诉讼",
            "商誉减值", "暴雷", "跌停", "解禁", "警示", "暂停", "立案", "质押", "冻结",
            "下调", "投诉", "监管", "风险", "下调评级"]
    t = text
    b = sum(1 for k in bear if k in t)
    u = sum(1 for k in bull if k in t)
    if b > u:
        return "bear"
    if u > b:
        return "bull"
    return "neutral"


def _fetch_news(code: str, days: int = 5) -> list | None:
    """近 N 日个股相关大事/新闻（真实数据，东方财富 F10 大事提醒）。返回 [{title,time,sentiment}]。"""
    market = "SH" if _market_of(code) == "sh" else ("SZ" if _market_of(code) == "sz" else "BJ")
    cached = _NEWS_CACHE.get(code)
    if cached and (time.time() - cached.get("_t", 0)) < 1800:
        return cached.get("list")
    out: dict = {"_t": time.time(), "list": []}
    try:
        url = f"https://emweb.securities.eastmoney.com/PC_HSF10/OperationsRequired/PageAjax?code={market}{code}"
        j = _em_json(url)
        items = []
        if isinstance(j, dict):
            # 大事提醒在 dstx.data（数组）; 每条含 EVENT_TYPE / NOTICE_DATE / LEVEL1_CONTENT 等
            dstx = j.get("dstx") or {}
            rows = dstx.get("data") if isinstance(dstx, dict) else None
            if not rows:
                rows = j.get("data") or []
            # dstx.data 可能是 [[{...}],[{...}]]（按类别分组的嵌套数组），展平
            flat = []
            for it in (rows or []):
                if isinstance(it, list):
                    flat.extend(it)
                else:
                    flat.append(it)
            for it in flat:
                if not isinstance(it, dict):
                    continue
                title = (it.get("LEVEL1_CONTENT") or it.get("LEVEL2_CONTENT")
                         or it.get("EVENT_TYPE") or it.get("TITLE") or "")
                tms = it.get("NOTICE_DATE") or it.get("DATE") or it.get("EVENT_DATE") or ""
                if title:
                    items.append({"title": str(title), "time": str(tms)[:10],
                                  "sentiment": _classify_sentiment(str(title))})
        # 仅保留近 N 日
        cutoff = dt.datetime.now() - dt.timedelta(days=days)
        recent = []
        for it in items:
            try:
                tt = dt.datetime.strptime(it["time"][:10], "%Y-%m-%d")
                if tt >= cutoff:
                    recent.append(it)
            except Exception:
                recent.append(it)
        out["list"] = recent[:20]
    except Exception:
        pass
    _NEWS_CACHE[code] = out
    return out["list"]


# 大盘(沪深300)近 N 日收益率缓存，供板块强度对比
_MARKET_RETURN_CACHE: dict = {}


def _market_return(days: int = 20) -> float | None:
    """上证指数(sh000001)近 days 日收益率(%)，带缓存。

    数据源（v117 加固）：腾讯指数日线优先 → 东财指数日线兜底。原实现只走腾讯
    web.ifzq.gtimg.cn，云端部署环境偶发该域名不可达/超时，导致板块强度整表为空；
    且旧实现把失败结果(None)也缓存 1 小时，一次抖动会污染后续全部请求。现在：
    - 失败不写缓存，下次调用自动重试；
    - 腾讯失败立即用东财 push2his 指数日线兜底。
    """
    cached = _MARKET_RETURN_CACHE.get(days)
    if cached and (time.time() - cached[0]) < 3600:
        return cached[1]
    ret = None
    # 源1：腾讯指数日线（已验证可用）：day 行 = [date, open, close, high, low, volume]
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,{days+1},qfq"
        j = _em_json(url)
        node = (j or {}).get("data", {}).get("sh000001", {})
        rows = node.get("day") or node.get("qfqday") or []
        closes = [_as_float(r[2]) for r in rows if len(r) >= 3]
        if len(closes) >= 2:
            ret = (closes[-1] - closes[0]) / closes[0] * 100
    except Exception:
        pass
    # 源2：东财指数日线兜底（klines 行 = "date,open,close,high,low,...,volume,..."，取第2列收盘）
    if ret is None:
        try:
            url2 = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
                    f"&klt=101&fqt=0&lmt={days+1}&end=20500101&fields1=f1,f2,f3&fields2=f51,f53")
            j2 = _em_json(url2)
            rows2 = ((j2 or {}).get("data") or {}).get("klines") or []
            closes2 = [_as_float(str(r).split(",")[1]) for r in rows2 if "," in str(r)]
            if len(closes2) >= 2:
                ret = (closes2[-1] - closes2[0]) / closes2[0] * 100
        except Exception:
            pass
    if ret is not None:  # 仅成功结果进缓存；失败不缓存，避免抖动污染板块强度整表 1 小时
        _MARKET_RETURN_CACHE[days] = (time.time(), ret)
    return ret


def _stock_return(quotes: list, days: int = 20) -> float | None:
    """个股近 days 日收益率(%)，用日线前复权收盘。"""
    try:
        closes = [q.close for q in quotes if q.close]
        if len(closes) < 2:
            return None
        n = min(days, len(closes) - 1)
        return (closes[-1] - closes[-1 - n]) / closes[-1 - n] * 100
    except Exception:
        return None


# ===== get_pool_track 可选扩展阶段的 deadline 门控（v118） =====
# 单只冷启动全量约 8s（沙箱外源每请求有 0.3~1s 地板成本），而列表页网关约 10s 超时。
# 给「非核心」的扩展阶段(资金流/估值/板块/事件标签/财报风险)加 deadline：到点即抛内部
# 异常终止，返回已算好的核心字段(现价/MA/箱体/打分/建议)，保证整页在预算内返回。
class _DeadlineHit(Exception):
    """内部信号：单只跟踪的可选阶段已超出 deadline，立即返回已聚合部分。"""


def _over_deadline(deadline_ts: float | None) -> bool:
    """是否已超过 deadline 时间戳（None=不设限）。"""
    return deadline_ts is not None and time.monotonic() >= deadline_ts


def get_pool_track(code: str, db, position: dict | None = None, deadline: float | None = None) -> dict:
    """单只标的的「每日跟踪」数据,供短线可投池表格使用。

    返回字段:
      - price/change_pct/vol_ratio/turnover: 实时(腾讯/东财/新浪)
      - box_high/box_low/box_pos: 近 20 日箱体上下沿及当前价相对位置(0~1,1=触上沿)
      - ma5/ma20: 均线;above_ma5/above_ma20: 当前价是否站上
      - operation_advice: 基于当前盘口生成的操作建议对象
      - src: 数据源标识(tencent/eastmoney/sina/demo),失败时为空
      - ts: 拉取时间戳(秒),供前端展示「X秒前」
      - mkt(v137): 行情可信度标记 {q:日线0/1, s:实时盘口0/1, f:当日资金流0/1}, 0=读取异常。
        约定：q=0 时由日线推导的价/均线/箱体/5日/缺口/振幅/评分/操作建议一律不产出
        (只可能用真实源实时价兜底 price/change_pct), 前端对异常列显示【数据异常】,
        绝不允许把演示/过期/0 等错误数值当真实行情展示。

    deadline(v118): 可选秒数预算。超过后跳过剩余「可选扩展阶段」(资金流/估值百分位/
    板块强度/事件风险标签/财报风险摘要)，只返回已算好的核心字段。列表页传 ~7s 让
    整页稳定在网关 10s 内；详情/导出可不传(全量)。

    失败策略:任意一步失败就返回 {},不抛异常,保证 list_pool 不被单只拖死。
    """
    now = dt.datetime.now().timestamp()
    cached = _POOL_TRACK_CACHE.get(code)
    if cached and (now - cached[0]) < _POOL_TRACK_TTL:
        return cached[1]

    out: dict = {}
    # deadline 换算为单调时钟绝对时刻（None=不设限）
    _dl = (time.monotonic() + deadline) if deadline else None
    # ===== v136: 财务/解禁字段 优先预取 + 同步赋值（外层 try 之前） =====
    # 源稳定（东财 RPT_LICO_FN_CPD 新版）但耗时 0.3~1s，且块 9/10 的 deadline 守卫 raise _DeadlineHit
    # 会跳到外层 except，导致原块 11 永远不执行——本块独立于外层 try 之前，财务日期/同比%
    # 是用户决策关键字段，必须必跑（缓存 86400s 后无重发成本）。`is not None` 语义保留。
    try:
        v = _fetch_valuation(code) or {}
        if v.get("pe_ttm") is not None:
            out["pe_ttm"] = v["pe_ttm"]
        if v.get("pb") is not None:
            out["pb"] = v["pb"]
        if v.get("pe_pct_3y") is not None:
            out["pe_pct_3y"] = v["pe_pct_3y"]
        fin = _fetch_financials(code) or {}
        if fin.get("net_profit") is not None:
            out["net_profit"] = fin["net_profit"]            # v153 补:前端财报明细弹窗需要绝对值判断盈亏
        if fin.get("net_profit_yoy") is not None:
            out["net_profit_yoy"] = fin["net_profit_yoy"]
        if fin.get("revenue_yoy") is not None:
            out["revenue_yoy"] = fin["revenue_yoy"]
        if fin.get("report_date"):
            out["report_date"] = fin["report_date"]
        if fin.get("eps") is not None:
            out["eps"] = fin["eps"]
        if fin.get("goodwill") is not None:
            out["goodwill"] = fin["goodwill"]
        unl = _fetch_unlock(code) or {}
        if unl.get("next_date"):
            out["next_date"] = unl["next_date"]
        if unl.get("next_ratio") is not None:
            out["next_ratio"] = unl["next_ratio"]
    except Exception:
        pass
    try:
        # ts 提前置位：deadline 中断跳过后端赋值时前端仍能显示「X秒前」
        out["ts"] = int(now)
        # ===== 数据源口径说明 =====
        # 实时盘口(腾讯 qt.gtimg.cn):价格是「不复权」原始价
        # 日线接口(腾讯 ifzq.gtimg.cn):价格是「前复权」价
        # 当股票发生除权除息/送股/大比例拆分时,两个接口会给出完全不同的价格。
        # 本表「现价」必须和 MA5/MA20 同源(都用日线前复权),否则会因为复权差异数学上"必错"。
        # 实时盘口仅用于「量比/换手」这种"日内活跃度"指标(与除权无关)。
        spot = _fetch_spot_direct(code)
        spot_ok = bool(spot and (spot.get("price") or 0) > 0)
        if spot:
            # 实时指标(日内活跃度,与除权无关)：字段缺失(空串解析成 None)时宁可不写，
            # 一不能把 None 交给 round()（会炸掉整行行情链路），二不能把 0 当真实值展示
            _vr, _tr = spot.get("vol_ratio"), spot.get("turnover")
            if _vr is not None:
                out["vol_ratio"] = round(_vr, 2)
            if _tr is not None:
                out["turnover"] = round(_tr, 2)
            # 当天实时成交量(万手) 的填充移到「严格按交易时段」块(见下方 3)，
            # 盘前/非交易日一律留空(避免 spot 残留昨日量被误认为当日实时量)
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

        # 日线只取近 60 天足够(算 MA20/箱体)。_prov 记录本条日线的真实来源(live/fresh/cache/demo)
        _prov: list = []
        quotes = ensure_quotes(code, days=60, _prov=_prov)
        _qsrc = _prov[0] if _prov else ""
        closes = [q.close for q in quotes if q.close]
        last_q = quotes[-1] if quotes else None
        # 行情日线可信度 qbad(v137)：
        #   live(本次直连真实源成功) → 可信
        #   demo → 演示数字，绝不可信
        #   fresh/cache(未直连) → 仅当实时盘口(真实源)能佐证最新收盘价(差异<50%)才可信；
        #    否则宁判异常——避免把历史故障期遗留的演示价/过期价当现价展示
        if _qsrc == "live":
            qbad = False
        elif _qsrc == "demo":
            qbad = True
        else:
            _agree = bool(last_q and last_q.close and spot_ok
                          and abs(spot["price"] - last_q.close) / last_q.close < 0.5)
            qbad = not (_agree and len(closes) >= 2)
        if last_q is None or not last_q.close:
            qbad = True
        out.setdefault("mkt", {})["q"] = 0 if qbad else 1   # 1=可信 0=异常
        out["mkt"]["s"] = 1 if spot_ok else 0               # 1=实时盘口可用 0=异常
        if qbad:
            # 日线行情异常：整条由日线推导的展示链路(价/均线/箱体/5日涨幅/振幅/评分/建议)
            # 一律不产出，避免把演示/过期数字当真实数据展示。
            # 若实时盘口(真实源)可用，仅用它的现价/涨跌幅兜底并打上提示。
            if spot_ok:
                out["price"] = round(spot["price"], 3)
                out["change_pct"] = round(spot.get("change_pct") or 0.0, 2)
                out["price_note"] = "行情日线读取异常，暂用实时价"
        else:
            # 现价/涨跌幅以「日线最新一根」(前复权)为准,跟 MA 系列同源同口径
            out["price"] = round(last_q.close, 3)
            _pc = last_q.pre_close
            if _pc:
                out["change_pct"] = round((last_q.close - _pc) / _pc * 100, 2)
                # 昨收缺失的异常 K 线不产出 change_pct(以前会 fallback 0.0,展示假"平盘")
            out["price_date"] = last_q.date
            out["ma5"] = round(sum(closes[-5:]) / min(5, len(closes)), 3)
            out["ma20"] = round(sum(closes[-20:]) / min(20, len(closes)), 3)
            recent = closes[-20:] if len(closes) >= 20 else closes
            out["box_high"] = round(max(recent), 3)
            out["box_low"] = round(min(recent), 3)
            if out.get("price") and out["box_high"] != out["box_low"]:
                pos = (out["price"] - out["box_low"]) / (out["box_high"] - out["box_low"])
                out["box_pos"] = round(max(0.0, min(1.0, pos)), 2)
            # 实时(未复权)价 与 日线(前复权)价交叉验证，防除权 / 源异常
            if spot_ok and last_q.close > 0:
                diff_ratio = abs(spot["price"] - last_q.close) / last_q.close
                if diff_ratio >= 0.5 and spot.get("src") not in ("demo",):
                    # 差异过大:实时盘口更可信(仍来自真实源),用它覆盖「现价」；
                    # 但 MA/箱体仍基于日线,同时打 price_note 提示口径不一致
                    out["price"] = round(spot["price"], 3)
                    out["change_pct"] = round(spot.get("change_pct") or 0.0, 2)
                    out["price_note"] = "实时价(日线数据源异常 fallback)"
        if out.get("price") is not None and out.get("ma5") is not None:
            out["above_ma5"] = out["price"] > out["ma5"]
        if out.get("price") is not None and out.get("ma20") is not None:
            out["above_ma20"] = out["price"] > out["ma20"]
        # 短线可投池三类达标项打分 + 操作建议：仅日线可信时产出——打分/MACD/振幅/建议
        # 全部依赖日线，演示/过期日线会算出假的分数与建议（v137 用户强调不可展示错误数据）
        if quotes and not qbad:
            _calc_pass_scores(code, out, quotes, spot, db)
            # 基于 track 数据生成操作建议(结构同 t_analysis.section6)
            out["operation_advice"] = _operation_advice_for_pool(out, position)

        # ===== 可投池扩展指标（真实数据 + 可计算） =====
        # 1) 5日涨跌幅(%) —— qbad 时日线不可信，跳过（演示/过期收盘价算出的涨幅是假数据）
        try:
            if len(closes) >= 2 and not qbad:
                n = min(6, len(closes))
                base = closes[-n]
                if base:
                    out["pct_5d"] = round((closes[-1] - base) / base * 100, 2)
        except Exception:
            pass
        # 2) 跳空缺口（今日开盘 vs 昨日高低）—— 同上，qbad 时跳过
        try:
            if len(quotes) >= 2 and not qbad and quotes[-1].open and quotes[-2].close:
                today_open = quotes[-1].open
                y_high = quotes[-2].high
                y_low = quotes[-2].low
                y_close = quotes[-2].close
                if y_close:
                    if today_open > y_high and y_high:
                        amp = (today_open - y_high) / y_close * 100
                        out["gap"] = {"dir": "up", "amp": round(amp, 2),
                                      "text": f"向上缺口 +{round(amp, 2)}%"}
                    elif today_open < y_low and y_low:
                        amp = (today_open - y_low) / y_close * 100
                        out["gap"] = {"dir": "down", "amp": round(amp, 2),
                                      "text": f"向下缺口 {round(amp, 2)}%"}
                    else:
                        out["gap"] = {"dir": "flat", "amp": 0.0, "text": "无缺口"}
        except Exception:
            pass
        # 3) 主力资金净流入占比(%) + 4) 主力资金简易信号 + 5) 大单流向(万元)
        #    口径(v117 用户确认)：按「当前交易日交易时段」取——盘前/非交易日留空，
        #    盘中取 push2 实时，收盘后取当日最后静态数据(优先 push2 定格值；
        #    push2 不可用时回落日级报表 RPT_DMSK_TS_STOCKNEW，且必须 trade_date==今日)。
        #    绝不回落到昨日数据——历史教训：昨日资金流 ÷ 今日成交额 = 方向完全反了。
        if _over_deadline(_dl): raise _DeadlineHit
        try:
            from datetime import time as _tt
            _bj_now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
            _today = _bj_now.strftime("%Y-%m-%d")
            _last_date = str((quotes[-1].date if quotes else "") or "")[:10]
            # 今天有日线 => 今天是交易日；日线缺失时退化为工作日判断
            # v175：原判断严格要求「当日日线已落地」才算交易日，但开盘初期日线常未更新
            # （实测 09:32 线上 price_date 仍为 09-08），导致整个交易日都不进入资金流
            # 取数分支、三列全空。改为：当日日线已落地 或 实时盘口可用(spot_ok) 即视为交易中；
            # 休市日由 _fund_from_fflow 的「末条日期 == 今日」校验兜底，不会拿昨日冒充今日。
            _is_trading_today = (_last_date == _today) or bool(spot_ok)
            # 未到开盘(北京时间 < 09:30)视为"当日资金流尚未产生"，三列一律留空
            _opened = _bj_now.time() >= _tt(9, 30)
            if _is_trading_today and _opened:
                # 当天实时成交量(万手)：仅在已开盘且为交易日的盘中/盘后取数；
                # 盘前/非交易日留空(前端显示 -)，避免 spot 残留的昨日量被误当作当日实时量
                if spot:
                    out["realtime_volume"] = round(spot.get("volume_wan", 0), 2)
                _fund = _fetch_intraday_fund([code]).get(code)
                _main_pct = _big_net = None
                if _fund and _fund.get("main_net") is not None:
                    _main_pct = _fund.get("main_pct")
                    _big_net = _fund.get("main_net")   # 对齐同花顺"大单流向"：超大单+大单
                    # 占比兜底：v175 优先用「当日实时成交额」作分母——早盘用昨日全天成交额
                    # 会把占比算小一个量级（如净流入 100 万 ÷ 昨日全天 1 亿 = 1%，
                    # 而当日实时成交额可能仅 1000 万，真实占比约 10%）。
                    if _main_pct is None:
                        _amt_now = float((spot or {}).get("amount") or 0)
                        if _amt_now > 0 and _big_net is not None:
                            _main_pct = round(_big_net / _amt_now * 100, 2)
                        elif not qbad:
                            # 日线成交额兜底——qbad(日线不可信)时禁用，否则演示/过期
                            # 成交额 ÷ 真实净流入 = 量级全错的百分比（v137）
                            _main_pct = _pct_of_amount(_fund.get("main_net"), quotes)
                else:
                    # 盘后(push2 不可达/已停更时)回落日级报表：仅当该行是今日才可用
                    _cf = _fetch_capital_flow(code)
                    if _cf and _cf.get("trade_date") == _today and _cf.get("main_net") is not None:
                        _big_net = _cf.get("main_net")
                        if not qbad:
                            _main_pct = _pct_of_amount(_cf.get("main_net"), quotes)
                # v137：交易时段内两路资金流源都取不到 → 标记 f=0，前端对三列显示【数据异常】
                out.setdefault("mkt", {})["f"] = 0 if _main_pct is None else 1
                if _main_pct is not None:
                    out["main_net_pct"] = round(_main_pct, 2)
                    out["big_order_net"] = round(_big_net / 1e4, 1) if _big_net is not None else None
                    _pctv = out["main_net_pct"] or 0.0
                    _bnv = _big_net or 0.0
                    if _pctv > 3 and _bnv > 0:
                        out["main_signal"] = {"level": "strong_bull", "text": "主力大幅流入"}
                    elif _pctv > 0:
                        out["main_signal"] = {"level": "bull", "text": "主力流入"}
                    elif _pctv < -3 and _bnv < 0:
                        out["main_signal"] = {"level": "strong_bear", "text": "主力大幅流出"}
                    elif _pctv < 0:
                        out["main_signal"] = {"level": "bear", "text": "主力流出"}
                    else:
                        out["main_signal"] = {"level": "flat", "text": "主力均衡"}
        except Exception:
            pass
        # 6) 估值百分位（近3年）：优先真实 PE 历史分位；不可用时以近3年价格分位近似
        if _over_deadline(_dl): raise _DeadlineHit
        try:
            val = _fetch_valuation(code)
            if val and val.get("pe_pct_3y") is not None:
                # v118: PE 历史可用时不拉 750 天日线（省 ~1s/只，长日线仅为价格分位兜底用）
                out["valuation_pct_3y"] = val["pe_pct_3y"]
                out["valuation_basis"] = "pe"
            elif not qbad:
                # qbad 时日线不可信，"近3年价格分位"同样会算出假分位 → 不产出(v137)
                quotes_long = ensure_quotes(code, days=750)
                closes_long = [q.close for q in quotes_long if q.close]
                price_pct = None
                if len(closes_long) >= 20 and out.get("price"):
                    below = sum(1 for c in closes_long if c <= out["price"])
                    price_pct = round(below / len(closes_long) * 100, 1)
                if price_pct is not None:
                    out["valuation_pct_3y"] = price_pct
                    out["valuation_basis"] = "price"   # 价格分位近似（PE 历史接口暂不可用）
            out["valuation_pe"] = (val or {}).get("pe_ttm")
            out["valuation_pb"] = (val or {}).get("pb")
        except Exception:
            pass
        # 7) 板块强度（个股20日收益 vs 大盘）—— 个股收益依赖日线,qbad 时不产出(v137)
        if _over_deadline(_dl): raise _DeadlineHit
        try:
            if not qbad:
                stock_ret = _stock_return(quotes, 20)
                mkt_ret = _market_return(20)
                if stock_ret is not None and mkt_ret is not None:
                    diff = stock_ret - mkt_ret
                    out["sector_strength"] = {
                        "diff": round(diff, 2),
                        "level": "strong" if diff > 3 else ("weak" if diff < -3 else "sync"),
                        "text": "强于大盘" if diff > 3 else ("弱于大盘" if diff < -3 else "同步"),
                    }
        except Exception:
            pass
        # 9) 事件风险标签（真实源/启发式）
        #    v117: value 由 True 改为「详情数组」，每一项 {text/time/sentiment}：
        #    - 新闻命中的标签(大额减持/股东增持/股价异动公告/监管问询/股东质押高) => 匹配的新闻条目
        #    - 财务/解禁启发式标签(业绩亏损/预亏/商誉高/解禁临近/ST风险) => 一条 {text: 触发原因说明}
        #    前端点击标签可弹窗查看详情。
        #    v118: 块9/块10 原本各自重复拉 financials+news,现改为调用内复用(省 ~0.5-0.9s/只)。
        _memo_extra: dict = {}

        def _fin1():
            if "fin" not in _memo_extra:
                try:
                    _memo_extra["fin"] = _fetch_financials(code) or None
                except Exception:
                    _memo_extra["fin"] = None
            return _memo_extra["fin"]

        def _news1():
            if "news" not in _memo_extra:
                try:
                    _memo_extra["news"] = _fetch_news(code, 5) or []
                except Exception:
                    _memo_extra["news"] = []
            return _memo_extra["news"]

        if _over_deadline(_dl): raise _DeadlineHit
        try:
            tags: dict = {}
            fin = _fin1()
            if fin:
                if fin.get("net_profit") is not None and fin["net_profit"] < 0:
                    tags["业绩亏损"] = [{"text": "最新报告期净利润为负", "time": (fin.get("report_date") or "")[:10]}]
                if fin.get("net_profit_yoy") is not None and fin["net_profit_yoy"] < 0:
                    tags["预亏"] = [{"text": "最新报告期净利润同比下滑", "time": (fin.get("report_date") or "")[:10]}]
                if fin.get("goodwill") and fin.get("equity") and fin["equity"] > 0:
                    if fin["goodwill"] / fin["equity"] > 0.3:
                        ratio = round(fin["goodwill"] / fin["equity"] * 100, 1)
                        tags["商誉高"] = [{"text": f"商誉占净资产 {ratio}% (>30%)", "time": (fin.get("report_date") or "")[:10]}]
            unl = _fetch_unlock(code)
            if unl and unl.get("next_date"):
                try:
                    nd = dt.datetime.strptime(unl["next_date"], "%Y-%m-%d").date()
                    if (nd - dt.date.today()).days <= 30:
                        tags["解禁临近"] = [{"text": f"最近解禁日 {unl['next_date']}"
                                            + (f"，解禁 {unl.get('unlock_num_desc') or ''}" if unl.get("unlock_num_desc") else "")
                                            + f"，距今 {(nd - dt.date.today()).days} 天"}]
                except Exception:
                    pass
            # 名称含 ST / 退市
            name = (position or {}).get("name") or ""
            if "ST" in name or "退" in name:
                tags["ST风险"] = [{"text": f"证券名称含 {'ST' if 'ST' in name else '退'} 标记，注意退市风险"}]
            # 新闻关键词扫描：命中 tag 时带上触发的新闻详情
            news = _news1()
            def _news_detail(kws: list[str]) -> list[dict]:
                hits = []
                for n in news:
                    t = str(n.get("title", ""))
                    if any(k in t for k in kws):
                        hits.append({"text": t, "time": str(n.get("time", ""))[:10],
                                     "sentiment": n.get("sentiment", "neu")})
                return hits
            d = _news_detail(["减持", "大额减持"])
            if d:
                tags["大额减持"] = d
            d = _news_detail(["增持"])
            if d:
                tags["股东增持"] = d
            d = _news_detail(["异动", "波动", "异常波动"])
            if d:
                tags["股价异动公告"] = d
            d = _news_detail(["问询", "监管", "立案", "警示函"])
            if d:
                tags["监管问询"] = d
            d = _news_detail(["质押"])
            if d:
                tags["股东质押高"] = d
            d = _news_detail(["解禁", "限售股"])
            if d:
                tags["解禁临近"] = d
            if tags:
                out["event_risk_tags"] = tags
        except Exception:
            pass
        # 10) 财报风险快速标记（3-6字摘要 + 触发详情 + 按风险原因过滤的近5日新闻）
        #    v144 改造：原版直接把 5 日全部新闻塞进 news，导致「盈利为负」summary 却展示
        #    融资余额/股东户数等无关新闻，对不上账。改为：
        #      - triggers: 每条 reason 携带具体触发值（净利润金额/同比/营收同比/商誉比等）,
        #                 解决"为什么是利空"的明细诉求；
        #      - news_keywords: 与触发原因对应的中文关键词集合；
        #      - news: 优先用关键词从近 5 日新闻里筛出与本次风险相关的条目；
        #              若过滤为空但确实有触发原因(说明近期东财未收录相关报道)，
        #              降级展示近 5 日全部新闻 + 由前端提示"未匹配关键词"，
        #              避免出现"完全对不上"的用户体验。
        if _over_deadline(_dl): raise _DeadlineHit
        try:
            fin = _fin1()
            summary = "无明显风险"
            triggers: list[dict] = []
            keyword_pool: list[str] = []
            if fin:
                rd = (fin.get("report_date") or "")[:10]
                # 1) 盈利为负（最严重）
                if fin.get("net_profit") is not None and fin["net_profit"] < 0:
                    np_ = fin["net_profit"]
                    if abs(np_) >= 1e8:
                        v_text = f"净利润 {np_/1e8:+.2f} 亿"
                    else:
                        v_text = f"净利润 {np_/1e4:+.0f} 万"
                    triggers.append({"reason": "盈利为负", "text": v_text, "time": rd})
                    keyword_pool += ["盈利", "净利", "净亏", "业绩", "预亏", "预减",
                                     "减亏", "扭亏", "中报", "年报", "季报", "财报", "亏损"]
                # 2) 盈利下滑（净利润同比为负，但本期可能仍为正）
                elif fin.get("net_profit_yoy") is not None and fin["net_profit_yoy"] < 0:
                    triggers.append({
                        "reason": "盈利下滑",
                        "text": f"净利润同比 {fin['net_profit_yoy']:+.1f}%",
                        "time": rd,
                    })
                    keyword_pool += ["盈利", "净利", "净亏", "业绩", "同比",
                                     "中报", "年报", "季报", "下滑", "下降"]
                # 3) 营收下滑
                if fin.get("revenue_yoy") is not None and fin["revenue_yoy"] < 0:
                    triggers.append({
                        "reason": "营收下滑",
                        "text": f"营收同比 {fin['revenue_yoy']:+.1f}%",
                        "time": rd,
                    })
                    keyword_pool += ["营收", "营业", "收入", "同比",
                                     "中报", "年报", "季报", "下滑", "下降"]
                # 4) 商誉偏高（占净资产 > 30%）
                if fin.get("goodwill") and fin.get("equity") and fin["equity"] > 0:
                    if fin["goodwill"] / fin["equity"] > 0.3:
                        ratio = round(fin["goodwill"] / fin["equity"] * 100, 1)
                        triggers.append({
                            "reason": "商誉偏高",
                            "text": f"商誉占净资产 {ratio}% (>30%)",
                            "time": rd,
                        })
                        keyword_pool += ["商誉", "减值"]
            if triggers:
                # summary 仍按用户原约定 3-6 字摘要
                summary = "、".join(t["reason"] for t in triggers)[:6]
            keywords = sorted(set(keyword_pool))
            # 按关键词过滤近 5 日新闻
            raw_news = _news1()
            news_filtered: list[dict] = []
            seen: set = set()
            for n in raw_news:
                t = str(n.get("title", ""))
                if not t:
                    continue
                if any(k in t for k in keywords):
                    key = (t, str(n.get("time", ""))[:10])
                    if key in seen:
                        continue
                    seen.add(key)
                    news_filtered.append({
                        "title": t,
                        "time": str(n.get("time", ""))[:10],
                        "sentiment": n.get("sentiment", "neu"),
                    })
            # 兜底：若过滤为空但确实有触发原因 → 降级显示近 5 日全部新闻
            # 前端根据 news_keywords 列表提示"未匹配关键词"以保持对账
            news_for_modal = news_filtered if (news_filtered or not triggers) else raw_news
            out["finance_risk"] = {
                "summary": summary,
                "report_date": (fin or {}).get("report_date"),
                "triggers": triggers,
                "news_keywords": keywords,
                "news_matched": len(news_filtered),
                "news": news_for_modal,
            }
        except Exception:
            pass

        # 11) v136 已前移到外层 try 之前（_over_deadline 触发 _DeadlineHit 时本块必须必跑）。
        #     保留空 try 占位以避免破坏既有「块 9 / 10 / 11」编号惯例；不在此重复赋值。
        try:
            pass
        except Exception:
            pass

        out["ts"] = int(now)
    except Exception:
        # 任意异常都吞掉,返回当前已聚合的部分(可能为空)
        pass
    _POOL_TRACK_CACHE[code] = (now, out)
    return out
