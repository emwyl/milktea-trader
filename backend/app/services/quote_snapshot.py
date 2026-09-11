"""行情快照缓存层（Phase 0 · 低风险）。

解决的问题：原来 /api/pool 每次请求都会对每一只票调用 get_pool_track 实时拉取上游行情
（腾讯/东财/新浪）+ 计算 MACD/换手率等，导致：
  1) 上游限流/抖动时数值跳变、页面"跳动看不清"；
  2) 5 秒轮询直接打爆免费源，延迟与失败率飙升。

本模块把"采集"与"请求"解耦：
  - 后台采集线程按固定节律（盘中日 3s、盘后 60s）批量拉取所有可投池标的的实时盘口，
    写入进程内单例快照 SNAPSHOT（每标的一份最新值 + 时间戳）。
  - /api/pool/quotes 只读快照返回，绝不现拉上游；前端 5s 轮询只更新变化的单元格。
  - 采集失败则保留上一份快照（不清空、不报错），页面不会因此跳动。

这是专业行情系统（东财 EMQ / 同花顺 / 万得）"采集→内存快照→增量推送"架构的最小落地版，
后续可平滑升级为 Redis 快照 + WebSocket 增量推送。
"""
from __future__ import annotations
import datetime as dt
import threading
import time

from sqlalchemy import distinct

from app.db import SessionLocal
from app.models import TrackedPool
from app.services.data_fetcher import _http_get

# ============ 快照存储 ============
# code -> { price, pre_close, open, change, change_pct, today_high, today_low,
#           realtime_volume(万手), vol_ratio, turnover, amount, avg_price, ts, src, mkt, fetched_at(epoch) }
SNAPSHOT: dict[str, dict] = {}
SNAPSHOT_TS: dict[str, float] = {}        # code -> 最近一次成功写入的 epoch
_LOCK = threading.Lock()

_COLLECT_INTERVAL_OPEN = 3.0              # 交易时段刷新间隔（秒）
_COLLECT_INTERVAL_CLOSED = 60.0           # 非交易时段
_STALE_SECONDS = 15.0                     # 超过该时长视为过期（前端可据此提示）
_BATCH = 55                              # 每次批量拉取的标的数（URL 长度与免费源限流考虑）

_thread: threading.Thread | None = None
_running = False
_stop_ev = threading.Event()


def _market_prefix(code: str) -> str:
    """腾讯行情市场前缀：920/4/8→bj，6/9→sh，0/3→sz。"""
    if code.startswith("920"):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return "bj"


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _cn_now() -> dt.datetime:
    """中国时区当前时间（UTC+8，无夏令时）。不依赖 tzdata，避免 Windows 缺 IANA 库。"""
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def market_open(now: dt.datetime | None = None) -> bool:
    """是否处于 A 股交易时段：周一~周五 9:30-11:30 或 13:00-15:00。"""
    now = now or _cn_now()
    if now.weekday() >= 5:  # 6=周六 5=周日
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60)


def _parse_qt_batch(txt: str) -> dict[str, dict]:
    """解析腾讯 qt.gtimg.cn 批量响应，提取核心实时盘口字段。
    字段索引与 data_fetcher._fetch_spot_tencent 保持一致。"""
    out: dict[str, dict] = {}
    if not txt or "=" not in txt:
        return out
    for line in txt.split(";"):
        if "=" not in line:
            continue
        try:
            payload = line.split("=", 1)[1].strip().strip('";\n')
            f = payload.split("~")
            if len(f) < 50:
                continue
            code = str(f[2]).strip()
            if not code or not code[0].isdigit():
                continue
            price = _as_float(f[3])
            pre_close = _as_float(f[4])
            open0 = _as_float(f[5])
            change = _as_float(f[31]) if len(f) > 31 else round(price - pre_close, 2)
            change_pct = _as_float(f[32]) if len(f) > 32 else (round((price - pre_close) / pre_close * 100, 2) if pre_close else 0.0)
            high = _as_float(f[33]) if len(f) > 33 else price
            low = _as_float(f[34]) if len(f) > 34 else price
            amount_wan = _as_float(f[37]) if len(f) > 37 else 0.0     # 成交额(万)
            turnover = _as_float(f[38]) if len(f) > 38 else 0.0       # 换手率(%)
            vol_ratio = _as_float(f[49]) if len(f) > 49 else 0.0
            avg_price = _as_float(f[51]) if len(f) > 51 else price
            volume = _as_float(f[36]) if len(f) > 36 else _as_float(f[6])  # 手
            volume_wan = volume / 1e4 if volume else 0.0              # 万手
            out[code] = {
                "price": price, "pre_close": pre_close, "open": open0,
                "change": change, "change_pct": change_pct,
                "today_high": high, "today_low": low,
                "realtime_volume": round(volume_wan, 2),
                "vol_ratio": vol_ratio, "turnover": turnover,
                "amount": amount_wan * 1e4, "avg_price": avg_price,
                "ts": f[30] if len(f) > 30 else "",
                "src": "tencent",
                "mkt": {"q": 1, "s": 1},   # 快照来源实时盘口可信；f(资金流)未在此拉取，保持调用方原值
            }
        except Exception:
            continue
    return out


def _fetch_batch(codes: list[str]) -> dict[str, dict]:
    """批量拉腾讯实时盘口，返回 code->snapshot（仅成功的）。"""
    if not codes:
        return {}
    q = ",".join(f"{_market_prefix(c)}{c}" for c in codes)
    txt = _http_get(f"https://qt.gtimg.cn/q={q}", headers={"Referer": "https://gu.qq.com/"})
    return _parse_qt_batch(txt) if txt else {}


def _pool_codes() -> list[str]:
    """当前可投池去重后的全部标的代码（含所有用户，个人版规模很小）。"""
    try:
        db = SessionLocal()
        rows = db.query(distinct(TrackedPool.code)).all()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass


def refresh_all() -> int:
    """刷新全部可投池标的快照。返回成功更新的标的数。失败的项保留旧值。"""
    codes = _pool_codes()
    if not codes:
        return 0
    now = time.time()
    merged: dict[str, dict] = {}
    for i in range(0, len(codes), _BATCH):
        batch = codes[i:i + _BATCH]
        try:
            got = _fetch_batch(batch)
            for c, snap in got.items():
                snap["fetched_at"] = now
                merged[c] = snap
        except Exception:
            # 单批失败：跳过，保留旧快照
            continue
    if not merged:
        return 0
    with _LOCK:
        for c, snap in merged.items():
            SNAPSHOT[c] = snap
            SNAPSHOT_TS[c] = now
    return len(merged)


def get_quotes(codes: list[str]) -> dict:
    """供 /api/pool/quotes 调用：从快照读取，不触发任何上游请求。
    返回 { code: {...snapshot, stale: bool} }，缺失的 code 不返回（前端保留原值）。"""
    now = time.time()
    out: dict[str, dict] = {}
    with _LOCK:
        for c in codes:
            snap = SNAPSHOT.get(c)
            if not snap:
                continue
            item = dict(snap)
            item["stale"] = (now - (SNAPSHOT_TS.get(c, 0) or 0)) > _STALE_SECONDS
            out[c] = item
    return out


def _loop():
    """后台采集线程主循环。"""
    # 启动即先拉一次，避免首屏空白
    try:
        refresh_all()
    except Exception:
        pass
    while _running and not _stop_ev.is_set():
        interval = _COLLECT_INTERVAL_OPEN if market_open() else _COLLECT_INTERVAL_CLOSED
        if _stop_ev.wait(interval):
            break
        try:
            refresh_all()
        except Exception:
            # 任何异常都不应终止采集线程
            continue


def start_collector():
    """在应用启动时调用（lifespan）。幂等：重复调用安全。"""
    global _thread, _running
    if _running:
        return
    _running = True
    _stop_ev.clear()
    _thread = threading.Thread(target=_loop, name="quote-snapshot", daemon=True)
    _thread.start()


def stop_collector():
    """在应用关闭时调用。"""
    global _running
    _running = False
    _stop_ev.set()
