"""时点评分快照采集器（定期复盘报告 Phase A · 0911批次4）。

## 为什么必须存在

定期复盘报告要回答两个问题：
  1) 某交易日某时点（如 09:45）每只票的**静态综合评分**是多少；
  2) 按「评分 > 60 看涨、否则看跌」的规则，那次判断后来到底对不对。

但原系统的行情层（`quote_snapshot.py`）只在**内存**里保留每只票的**最新**盘口，
一刷新就覆盖，没有任何分钟级历史 → **历史时点评分无法事后重建**。
所以只有两种选择：要么在时点上主动落库，要么永远没有复盘数据。

## 本模块的做法（复用既有架构，不引入新依赖）

1) 复用 `quote_snapshot` 的后台线程生命周期与批量拉取能力；
2) 每 30s tick，判断「**刚越过**」哪个采集时点（见 `SLOTS`）。只采集 ≤ `_TOL_MIN` 分钟内
   越过的时点 —— 服务重启/停机后**绝不用错误时间的数据补写别的时点**（宁可缺，不可错）；
3) 到点先用**批量接口冻结全池盘口**（约 3 个请求覆盖全池，得到同一时刻的价向量），
   再逐只跑 `get_pool_track` 算分。这样「慢的算分过程」不会让记录的**价格**出现分钟级错位
   —— 这是本模块最关键的设计取舍；
4) 结果 `INSERT OR REPLACE` 落 `pool_score_snapshot`（`UNIQUE(user_id,code,trade_date,slot)`），
   天然幂等，重启重跑安全；
5) 上游失败只跳过、下个 tick 重试；不阻塞主链路，**绝不写假数据**。

## 铁律遵守

- 与 A/B/C 评分公式**完全解耦**（只调用 `get_pool_track`，不修改任何评分逻辑）。
- 行情异常（`mkt.q == 0`）时 `score_raw` 写 **NULL 而不是 0** —— 复盘只统计真实采集到的
  有效样本，绝不让「数据异常」被当成「0 分」污染回测（用户强调的行情可信度契约）。
- 原始分与最终分分离：`score_raw` 存未乘环境系数的真实计算分，`score` 存调节后最终分。
  注意后端 `total_score` 在「否决 > 3 条」时已被强制归零，因此 `score_raw` 由
  `score_a + score_b + score_c - penalty` 反算还原（对应前端 v192 的 rawScore 口径）。
"""
from __future__ import annotations
import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text

from app.db import SessionLocal
from app.models import Stock, TrackedPool
from app.services import quote_snapshot as qs
from app.services.data_fetcher import get_pool_track

# ============ 配置 ============

# 采集时点（10 个基准时点）。09:25 = 集合竞价结果（用户 2026-09-11 决策，替代原 09:00）。
SLOTS = ("0925", "0930", "0945", "1100", "1130", "1315", "1330", "1400", "1445", "1500")
# 回测监控时点默认值；**必须 ⊆ 采集时点** → 自动并入采集清单（13:45 因此被加入）。
MONITOR_SLOTS = ("0945", "1345")

_TICK = 30.0          # tick 间隔（秒）
_TOL_MIN = 6          # 只采集「刚越过 ≤6 分钟」的时点；超出即放弃（不补错误时点的数据）
_DEADLINE = 20.0      # 单只 get_pool_track 的秒数预算（保护整轮采集时长）
_MAX_WORKERS = 8      # 算分并发（119 只 ≈ 25s/轮，实测）
_PASS_WARN = 300.0    # 单轮耗时超过此值只记日志，不影响正确性

_DONE: set[tuple[str, str]] = set()   # 进程内已采集 (trade_date, slot)，避免重复跑
_RUN_LOCK = threading.Lock()

_thread: threading.Thread | None = None
_running = False
_stop_ev = threading.Event()

STATUS: dict = {                     # 诊断用（可在 /api/review/report/status 查看）
    "last_tick": "", "last_slot": "", "last_date": "",
    "last_rows": 0, "last_elapsed": 0.0, "last_error": "", "skipped": "",
}


# ============ 工具 ============

def _slot_minutes(slot: str) -> int:
    """'0945' -> 585（当日分钟数）。"""
    try:
        return int(slot[:2]) * 60 + int(slot[2:])
    except Exception:
        return 24 * 60 + 1


def effective_slots() -> list[str]:
    """采集时点 = 基准时点 ∪ 监控时点（监控时点必须可回测，因此强制并入）。"""
    monitor = _setting_list("review_monitor_slots", MONITOR_SLOTS)
    base = set(SLOTS) | set(monitor)
    return sorted(base, key=_slot_minutes)


def _setting_list(key: str, default) -> tuple[str, ...]:
    """从 system_settings 读一个字符串列表；任何异常都回落默认值。"""
    try:
        db = SessionLocal()
        from app.models import SystemSetting
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        db.close()
        if row and row.value_json:
            val = json.loads(row.value_json)
            if isinstance(val, list):
                out = tuple(str(v).strip() for v in val if str(v).strip())
                if out:
                    return out
    except Exception:
        pass
    return tuple(default)


def _num(v):
    """安全转 float；None/空串/非法 → None（**不**回落 0，避免把缺失当 0 分）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pos(v):
    """正数才有效（价格为 0/负 视为取数失败）。"""
    n = _num(v)
    return n if (n is not None and n > 0) else None


def _pick(*vals):
    """取第一个有效数值。"""
    for v in vals:
        n = _num(v)
        if n is not None:
            return n
    return None


# ============ 盘口冻结 ============

def _freeze_quotes(codes: list[str]) -> dict[str, dict]:
    """一次性批量冻结全池盘口 —— 同一时刻的价向量。

    这是本模块的核心取舍：算分要 2 分钟，但「价格」必须在时点上一次性定格，
    否则同一时点内先算的票与后算的票会差出 1~2 分钟的行情，回测口径就不可比了。
    """
    out: dict[str, dict] = {}
    if not codes:
        return out
    batch = int(getattr(qs, "_BATCH", 55) or 55)
    for i in range(0, len(codes), batch):
        try:
            got = qs._fetch_batch(codes[i:i + batch])
        except Exception:
            got = None
        if got:
            out.update(got)
    return out


def _batch_trade_date(frozen: dict) -> str:
    """从腾讯批量返回的 ts(f[30]，形如 20260911150000) 解析出交易日。

    用途：**节假日自动兜底**。非交易日上游返回的是上一交易日的 ts，日期不匹配即跳过，
    不需要维护交易日历，也不会往库里写假数据。取众数以避免个别票停牌/异常 ts 干扰。
    """
    counts: dict[str, int] = {}
    for snap in frozen.values():
        ts = str((snap or {}).get("ts") or "")
        if len(ts) >= 8 and ts[:8].isdigit():
            d = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ============ 采集 ============

def _pool_rows(db) -> list[tuple[int | None, str, int]]:
    """全池（含所有账号，个人版规模小）：[(user_id, code, in_monitor)]。

    注意：`tracked_pool` 里存在同一 (user_id, code) 多行的情况（用户重复加入监控的历史数据，
    实测有 6 组）。快照表按 (user_id, code, trade_date, slot) 唯一，因此这里必须先按
    (user_id, code) 归并，否则「哪一行先写」会随机决定 in_monitor —— 归并规则取
    **只要有一行在监控池就记为在监控池**（max），语义明确且与写入顺序无关。
    """
    rows = db.query(TrackedPool.user_id, TrackedPool.code, TrackedPool.monitored).all()
    merged: dict[tuple, int] = {}
    for uid, code, mon in rows:
        if not code:
            continue
        flag = 1 if (mon is None or int(mon) == 1) else 0
        key = (uid, str(code))
        merged[key] = max(merged.get(key, 0), flag)
    return [(uid, code, flag) for (uid, code), flag in merged.items()]


def _industries(db, codes: list[str]) -> dict[str, str]:
    """批量取行业，供 market_regime 计算板块 S（避免逐只查库 + 逐只打东财）。"""
    try:
        rows = db.query(Stock.code, Stock.industry).filter(Stock.code.in_(codes)).all()
        return {str(c): (i or "") for c, i in rows}
    except Exception:
        return {}


def _build_record(uid, code, trade_date, slot, in_monitor, out: dict, snap: dict) -> dict:
    """把 track 结果 + 冻结盘口组装成一行快照记录。"""
    out = out or {}
    snap = snap or {}

    # —— 盘口：优先用冻结快照（全池同一时刻），缺项才回落 track ——
    price = _pos(snap.get("price")) or _pos(out.get("price"))
    change_pct = _pick(snap.get("change_pct"), out.get("change_pct"))
    pre_close = _pos(snap.get("pre_close"))
    if pre_close is None and price and change_pct is not None:
        # 兜底反算：昨收 = 现价 / (1 + 涨跌幅)
        try:
            pre_close = round(price / (1 + change_pct / 100.0), 3) if (1 + change_pct / 100.0) else None
        except Exception:
            pre_close = None

    # —— 评分：行情异常(mkt.q=0)一律不产出分数，绝不写 0 分假数据 ——
    q_ok = ((out.get("mkt") or {}).get("q") != 0)

    adj = out.get("regime_adj") or {}
    factor = _num(adj.get("adj_factor"))
    factor = factor if factor is not None else 1.0
    thr = _num(adj.get("threshold"))
    thr = thr if thr is not None else 60.0

    veto_reasons = out.get("veto_reasons") or []
    vetoed = 1 if len(veto_reasons) > 3 else 0

    score_raw = score = None
    grade = semantic = ""
    if q_ok and out.get("total_score") is not None:
        # 后端 total_score 在「否决 > 3 条」时已被强制归零 → 用 A/B/C - 扣分 反算真实原始分
        sa, sb, sc = _num(out.get("score_a")), _num(out.get("score_b")), _num(out.get("score_c"))
        pen = _num(out.get("penalty"))
        if None not in (sa, sb, sc):
            score_raw = round(sa + sb + sc - (pen or 0.0), 1)
        else:
            score_raw = round(_num(out.get("total_score")), 1)
        score = 0.0 if vetoed else max(0.0, min(100.0, round(score_raw * factor, 1)))
        grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D"
        # 语义口径与前端 mtscore.compute 严格一致：
        #   崩坏市(block_new_long) / 弱势题材逆势涨(pulse_rebound) → 观望
        #   否则按档位动态阈值判 看涨 / 看跌
        if adj.get("block_new_long") or adj.get("pulse_rebound"):
            semantic = "观望"
        elif score >= thr:
            semantic = "看涨"
        else:
            semantic = "看跌"

    mreg = out.get("market_regime") or {}
    sreg = out.get("sector_regime") or {}
    metrics = {
        "score_a": _num(out.get("score_a")), "score_b": _num(out.get("score_b")),
        "score_c": _num(out.get("score_c")), "penalty": _num(out.get("penalty")),
        "veto_reasons": veto_reasons, "veto_count": len(veto_reasons),
        "veto_checks": out.get("veto_checks") or [],
        "box_pos": _num(out.get("box_pos")), "box_high": _num(out.get("box_high")),
        "box_low": _num(out.get("box_low")), "ma5": _num(out.get("ma5")), "ma20": _num(out.get("ma20")),
        "above_ma5": out.get("above_ma5"), "above_ma20": out.get("above_ma20"),
        "tech_signals": out.get("tech_signals") or {},
        "intra_amplitude": _num(out.get("intra_amplitude")),
        "avg_volume_5": _num(out.get("avg_volume_5")), "avg_volume_20": _num(out.get("avg_volume_20")),
        "pct_5d": _num(out.get("pct_5d")), "gap": out.get("gap") or {},
        "main_net_pct": _num(out.get("main_net_pct")), "main_signal": out.get("main_signal") or "",
        "sector_resonance": out.get("sector_resonance") or "",
        "src": out.get("src") or "", "mkt": out.get("mkt") or {},
        # 环境层原始值一并留档，供复盘「分环境/分风格」透视与系数复算
        "market_regime": mreg, "sector_regime": sreg, "regime_adj": adj,
    }

    return {
        "user_id": uid, "code": code, "trade_date": trade_date, "slot": slot,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "in_monitor": in_monitor,
        "price": price, "pre_close": pre_close,
        "open": _pos(snap.get("open")), "high": _pos(snap.get("today_high")),
        "low": _pos(snap.get("today_low")),
        "change_pct": change_pct, "amplitude_pct": _num(out.get("intra_amplitude")),
        "turnover": _pick(snap.get("turnover"), out.get("turnover")),
        "vol_ratio": _pick(snap.get("vol_ratio"), out.get("vol_ratio")),
        "amount": _pick(snap.get("amount")), "volume": _pick(snap.get("realtime_volume")),
        "score": score, "score_raw": score_raw, "grade": grade,
        "vetoed": vetoed, "semantic": semantic,
        "market_regime": str(mreg.get("tier") or ""), "sector_regime": str(sreg.get("tier") or ""),
        "style_tag": str(out.get("style_tag") or ""), "mv_tier": str(out.get("mv_tier") or ""),
        "beta": _num(out.get("beta")), "rs": _num(out.get("relative_strength")),
        "adj_factor": factor,
        "metrics_json": json.dumps(metrics, ensure_ascii=False, default=str),
    }


_SQL_UPSERT = text("""
INSERT OR REPLACE INTO pool_score_snapshot
 (user_id, code, trade_date, slot, captured_at, in_monitor,
  price, pre_close, "open", high, low, change_pct, amplitude_pct, turnover, vol_ratio, amount, volume,
  score, score_raw, grade, vetoed, semantic,
  market_regime, sector_regime, style_tag, mv_tier, beta, rs, adj_factor, metrics_json)
VALUES
 (:user_id, :code, :trade_date, :slot, :captured_at, :in_monitor,
  :price, :pre_close, :open, :high, :low, :change_pct, :amplitude_pct, :turnover, :vol_ratio, :amount, :volume,
  :score, :score_raw, :grade, :vetoed, :semantic,
  :market_regime, :sector_regime, :style_tag, :mv_tier, :beta, :rs, :adj_factor, :metrics_json)
""")


def collect_slot(trade_date: str, slot: str) -> dict:
    """采集一个时点：冻结全池盘口 → 逐只算分 → 落库。返回统计。"""
    t0 = time.monotonic()
    stat = {"trade_date": trade_date, "slot": slot, "quotes": 0, "rows": 0,
            "scored": 0, "elapsed": 0.0, "note": ""}
    db = SessionLocal()
    try:
        rows = _pool_rows(db)
        if not rows:
            stat["note"] = "空池"
            return stat
        codes = sorted({c for _, c, _ in rows})

        # 1) 冻结全池盘口（同一时刻的价向量）
        frozen = _freeze_quotes(codes)
        stat["quotes"] = len(frozen)
        if not frozen:
            stat["note"] = "上游无有效盘口"
            return stat

        # 2) 节假日兜底：上游 ts 的日期必须是今天，否则不写（宁可缺，不可错）
        bdate = _batch_trade_date(frozen)
        if bdate and bdate != trade_date:
            stat["note"] = f"上游交易日{bdate}≠今日{trade_date}(疑似非交易日)"
            return stat

        # 3) 逐只算分（复用 get_pool_track，与页面显示同一套口径）
        industries = _industries(db, codes)
        scored: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {ex.submit(get_pool_track, c, db,
                              ({"industry": industries[c]} if industries.get(c) else None),
                              _DEADLINE): c for c in codes}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    scored[c] = f.result() or {}
                except Exception:
                    scored[c] = {}

        # 4) 组装 + 落库（单事务，写在本线程，避免多线程写 SQLite）
        records = [_build_record(uid, c, trade_date, slot, mon, scored.get(c) or {}, frozen.get(c) or {})
                   for uid, c, mon in rows]
        ok = [r for r in records if r["score_raw"] is not None]
        if ok:
            db.execute(_SQL_UPSERT, ok)
            db.commit()
        stat["rows"] = len(ok)
        stat["scored"] = len(ok)
        if len(ok) < len(records):
            stat["note"] = (stat["note"] + "; " if stat["note"] else "") + \
                           f"{len(records) - len(ok)} 行无有效评分(行情异常/数据缺失)，已跳过"
        return stat
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        stat["note"] = f"异常: {e!r}"
        return stat
    finally:
        stat["elapsed"] = round(time.monotonic() - t0, 1)
        try:
            db.close()
        except Exception:
            pass


# ============ 调度 ============

def _due_slot(now: dt.datetime) -> str | None:
    """返回「当前刚越过、且尚未采集」的最近时点；没有则 None。"""
    cur = now.hour * 60 + now.minute
    best = None
    for s in effective_slots():
        if (now.strftime("%Y-%m-%d"), s) in _DONE:
            continue
        d = cur - _slot_minutes(s)
        if 0 <= d <= _TOL_MIN:
            if best is None or _slot_minutes(s) > _slot_minutes(best):
                best = s
    return best


def _loop():
    """后台主循环：每 _TICK 秒判断一次是否到采集时点。"""
    while _running and not _stop_ev.is_set():
        if _stop_ev.wait(_TICK):
            break
        try:
            now = qs._cn_now()
            STATUS["last_tick"] = now.strftime("%Y-%m-%d %H:%M:%S")
            # 周末不采（节假日由上游 ts 日期兜底）
            if now.weekday() >= 5:
                STATUS["skipped"] = "周末"
                continue
            slot = _due_slot(now)
            if not slot:
                continue
            trade_date = now.strftime("%Y-%m-%d")
            if not _RUN_LOCK.acquire(blocking=False):
                STATUS["skipped"] = f"{trade_date} {slot} 上一轮采集仍在进行"
                continue
            try:
                stat = collect_slot(trade_date, slot)
                _DONE.add((trade_date, slot))
                STATUS.update({
                    "last_slot": slot, "last_date": trade_date,
                    "last_rows": stat.get("rows", 0), "last_elapsed": stat.get("elapsed", 0.0),
                    "last_error": "" if stat.get("rows") else (stat.get("note") or ""),
                    "skipped": "",
                })
                if stat.get("elapsed", 0) > _PASS_WARN:
                    STATUS["skipped"] = f"{slot} 单轮耗时 {stat['elapsed']}s，超过建议值"
            finally:
                _RUN_LOCK.release()
        except Exception as e:
            STATUS["last_error"] = repr(e)
            continue


def start_collector():
    """应用启动时调用（lifespan）。幂等：重复调用安全。"""
    global _thread, _running
    if _running:
        return
    _running = True
    _stop_ev.clear()
    _thread = threading.Thread(target=_loop, name="score-snapshot", daemon=True)
    _thread.start()


def stop_collector():
    """应用关闭时调用。"""
    global _running
    _running = False
    _stop_ev.set()


def status() -> dict:
    """采集器诊断信息（供 API / 排障使用）。"""
    return {
        "running": _running,
        "slots": effective_slots(),
        "monitor_slots": list(_setting_list("review_monitor_slots", MONITOR_SLOTS)),
        "done_today": sorted(s for d, s in _DONE if d == qs._cn_now().strftime("%Y-%m-%d")),
        "tick_seconds": _TICK,
        "tolerance_minutes": _TOL_MIN,
        **STATUS,
    }
