"""通用技术指标计算（curated，仅普适有效项）。
输入：按日期升序的行情序列；输出：最新指标快照 + 历史序列（用于画图）。
参考 Section 2 专业建议基础；仅实现被长期验证、散户可理解的指标。
"""
from __future__ import annotations
from typing import Any

from app.services.interfaces import Quote, IndicatorSnapshot


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n or n <= 0:
        return 0.0
    return sum(vals[-n:]) / n


def _ema(vals: list[float], n: int) -> float:
    if not vals:
        return 0.0
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5


def macd_series(closes: list[float], fast=12, slow=26, signal=9):
    if len(closes) < slow:
        return 0.0, 0.0, 0.0
    ema_fast = [_ema(closes[: i + 1], fast) for i in range(len(closes))]
    ema_slow = [_ema(closes[: i + 1], slow) for i in range(len(closes))]
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = [_ema(dif[: i + 1], signal) for i in range(len(dif))]
    hist = [(d - e) for d, e in zip(dif, dea)]
    return dif[-1], dea[-1], hist[-1] * 2


def kdj_series(highs: list[float], lows: list[float], closes: list[float], n=9):
    if len(closes) < n:
        return 50.0, 50.0, 50.0
    rsvs = []
    for i in range(n - 1, len(closes)):
        window_h = max(highs[i - n + 1 : i + 1])
        window_l = min(lows[i - n + 1 : i + 1])
        if window_h == window_l:
            rsv = 50.0
        else:
            rsv = (closes[i] - window_l) / (window_h - window_l) * 100
        rsvs.append(rsv)
    k, d, j = 50.0, 50.0, 50.0
    for rsv in rsvs:
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        j = 3 * k - 2 * d
    return k, d, j


def rsi_series(closes: list[float], n=14):
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_g = sum(gains[-n:]) / n
    avg_l = sum(losses[-n:]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def compute_snapshot(quotes: list[Quote]) -> IndicatorSnapshot:
    """由升序行情序列计算最新指标快照。"""
    if not quotes:
        return IndicatorSnapshot(code="", date="")
    closes = [q.close for q in quotes]
    highs = [q.high for q in quotes]
    lows = [q.low for q in quotes]
    vols = [q.volume for q in quotes]
    last = quotes[-1]
    prev = quotes[-2] if len(quotes) >= 2 else last

    change_pct = round((last.close - last.pre_close) / last.pre_close * 100, 2) if last.pre_close else 0.0
    ma = {str(p): round(_sma(closes, p), 3) for p in (5, 10, 20, 60) if len(closes) >= p}
    # 量比：当日量 / 近5日均量（不含当日）
    base = vols[-6:-1] if len(vols) >= 6 else vols[:-1]
    avg_vol = sum(base) / len(base) if base else (vols[-1] or 1)
    vol_ratio = round(vols[-1] / avg_vol, 2) if avg_vol else 0.0
    dif, dea, hist = macd_series(closes)
    upper = lower = mid = 0.0
    if len(closes) >= 20:
        mid = round(_sma(closes, 20), 3)
        sd = _std(closes[-20:])
        upper = round(mid + 2 * sd, 3)
        lower = round(mid - 2 * sd, 3)
    k, d, j = kdj_series(highs, lows, closes)
    rsi = rsi_series(closes, 14)
    amplitude = round((last.high - last.low) / last.pre_close * 100, 2) if last.pre_close else 0.0

    # 箱体：近 60 日支撑(最低)/压力(最高) + 斜率(线性趋势)
    win = closes[-60:] if len(closes) >= 60 else closes
    support = round(min(win), 3)
    pressure = round(max(win), 3)
    slope = round((win[-1] - win[0]) / len(win), 4) if len(win) > 1 else 0.0

    return IndicatorSnapshot(
        code=last.code, date=last.date, close=round(last.close, 3),
        change_pct=change_pct, ma=ma, vol_ratio=vol_ratio, turnover=round(last.turnover, 2),
        macd={"dif": round(dif, 3), "dea": round(dea, 3), "hist": round(hist, 3)},
        boll={"upper": upper, "mid": mid, "lower": lower},
        kdj={"k": round(k, 2), "d": round(d, 2), "j": round(j, 2)},
        rsi={"rsi14": round(rsi, 2)}, amplitude=amplitude,
        box={"support": support, "pressure": pressure, "slope": slope},
    )


def history_series(quotes: list[Quote], window: int = 120) -> list[dict[str, Any]]:
    """返回用于前端画 K 线 + 均线的历史序列。"""
    out = []
    closes = [q.close for q in quotes]
    for i, q in enumerate(quotes):
        # pre_close 在 data_fetcher 四个分支(腾讯/东财/akshare/演示)都已填上；含它才能让前端 K 线按「红涨绿跌」正确配色
        out.append({
            "date": q.date, "open": q.open, "high": q.high, "low": q.low, "close": q.close,
            "pre_close": q.pre_close,
            "volume": q.volume, "turnover": q.turnover,
            "ma5": round(_sma(closes[: i + 1], 5), 3) if i + 1 >= 5 else None,
            "ma10": round(_sma(closes[: i + 1], 10), 3) if i + 1 >= 10 else None,
            "ma20": round(_sma(closes[: i + 1], 20), 3) if i + 1 >= 20 else None,
        })
    return out[-window:]
