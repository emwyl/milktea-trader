"""全 A 股日线预热:后台线程并发拉(50 worker),进度可查询。

设计:
- 只对「无 daily_quotes 缓存」的股票拉(有缓存的跳过,增量预热)。
- ensure_quotes 内部已有腾讯/东财/akshare 多源兜底,成功写库。
- 模块级 _STATE 存进度,GET /api/stocks/warmup/status 可查。
- 单只失败不中断(计数 errors)。
"""
from __future__ import annotations
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import SessionLocal
from app.models import DailyQuote, Stock
from app.services.data_fetcher import ensure_quotes

_WARMUP_STATE = {
    "running": False, "done": 0, "total": 0, "errors": 0, "started_at": None, "finished_at": None, "msg": "未启动",
}
_LOCK = threading.Lock()


def _worker(code: str) -> bool:
    try:
        ensure_quotes(code, 120)  # 拉 120 根日线并写缓存
        return True
    except Exception:
        return False


def start_warmup() -> dict:
    """启动(或拒绝)全 A 预热。返回 {ok, msg, total}。

    拉取范围 = 「无日线缓存」+「缓存最后日期 < 今天」的股票(保证每天 16:00 跑后数据更新到当天)。
    """
    with _LOCK:
        if _WARMUP_STATE["running"]:
            return {"ok": False, "msg": "预热已在进行中,请稍候", "total": _WARMUP_STATE["total"]}
        db = SessionLocal()
        try:
            from sqlalchemy import func
            today = dt.date.today().isoformat()
            rows = (db.query(DailyQuote.code, func.max(DailyQuote.date))
                    .group_by(DailyQuote.code).all())
            last_date = {code: d for code, d in rows}
            # 需要拉:无缓存 或 缓存最后日期 < 今天(需要补今天的数据)
            codes = [s.code for s in db.query(Stock).all()
                     if last_date.get(s.code, "") != today]
        finally:
            db.close()
        if not codes:
            _WARMUP_STATE.update(running=False, done=0, total=0, errors=0,
                                 started_at=None, finished_at=None, msg="全部股票日线已是最新(今天),无需预热")
            return {"ok": True, "msg": "全部股票日线已是最新(今天),无需预热", "total": 0}
        _WARMUP_STATE.update(running=True, done=0, total=len(codes), errors=0,
                             started_at=dt.datetime.now().isoformat(timespec="seconds"),
                             finished_at=None, msg=f"预热进行中:0/{len(codes)}")

    def _run(codes: list[str]) -> None:
        with ThreadPoolExecutor(max_workers=50) as ex:
            futs = {ex.submit(_worker, c): c for c in codes}
            for f in as_completed(futs):
                ok = f.result()
                with _LOCK:
                    if not ok:
                        _WARMUP_STATE["errors"] += 1
                    _WARMUP_STATE["done"] += 1
                    if _WARMUP_STATE["done"] % 100 == 0 or _WARMUP_STATE["done"] == _WARMUP_STATE["total"]:
                        _WARMUP_STATE["msg"] = f"预热进行中:{_WARMUP_STATE['done']}/{_WARMUP_STATE['total']}"
        with _LOCK:
            _WARMUP_STATE["running"] = False
            _WARMUP_STATE["finished_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            _WARMUP_STATE["msg"] = (f"预热完成:{_WARMUP_STATE['done']}/{_WARMUP_STATE['total']}"
                                    f"(失败 {_WARMUP_STATE['errors']})")

    threading.Thread(target=_run, args=(codes,), daemon=True).start()
    return {"ok": True, "msg": f"预热已启动,共 {len(codes)} 只,后台进行中(可切走做别的)", "total": len(codes)}


def warmup_status() -> dict:
    with _LOCK:
        return dict(_WARMUP_STATE)
