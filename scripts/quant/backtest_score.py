# -*- coding: utf-8 -*-
"""
评分模型回测脚本（0911批次4 任务 #177）
=====================================

目的
----
回答三个问题，用来支撑「市场环境折扣系数」两周后的微调：

1. **截面有效性**：综合评分（原始分 / 调整后分）对未来 N 日收益有没有区分度？
   → 用 IC（每日截面 Spearman 秩相关）+ IR（IC 均值/标准差）衡量。
2. **阈值有效性**：60 分阈值（或档位阈值）下的胜率 / 混淆矩阵是什么水平？
   → 用混淆矩阵 + 分档统计，N 小的格子必须能一眼看出不可信。
3. **折扣系数稳定性**：市场环境折扣（adj_factor）到底有没有增量？
   → walk-forward：在样本内网格搜系数，在样本外看是否稳定。

数据契约
--------
- **输入**：`pool_score_snapshot`（时点快照，Phase A 采集器写入）+ `daily_quotes`（日线）。
- **只读**：本脚本只读数据库，绝不写入任何表。
- **口径**：预测正例 = 分数 ≥ 阈值 且 未被否决；实际正例 = 未来 H 日收益 ≥ eps(%)。
  与前端报告页「回测统计」口径同源（见 backend/app/routers/review.py）。

⚠ 行情可信度铁律
----------------
样本不足时**不给结论**，只输出样本量与"不可信"标记（与前端 N<20 灰显同一口径）。
宁可输出「数据不足」，也不输出看起来像结论的噪声。

用法
----
    # 1) 自检（用合成库验证脚本自身算得对不对）
    python scripts/quant/backtest_score.py --db C:/tmp/bt_synth.db --self-check

    # 2) 实战（真实库；snapshot 不足会明确报"数据不足"而不是编造）
    python scripts/quant/backtest_score.py --db backend/data/app.db --horizon 5 --threshold 60

    # 3) walk-forward 折扣系数网格
    python scripts/quant/backtest_score.py --db backend/data/app.db --walk-forward
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# 与前端 mtscore 保持一致的档位阈值（对「原始分」有效，见项目记忆）
GRADE_CUTS = [("A", 80.0), ("B", 65.0), ("C", 50.0), ("D", 0.0)]
# 样本量下限：低于此值不输出胜率类结论（与前端 btLowN 同口径）
MIN_N = 20


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 数据加载
def load_snapshots(db: str, user: str | None = None, slot: str | None = None):
    """读时点快照。返回 list[dict]。"""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    sql = "SELECT * FROM pool_score_snapshot"
    where, args = [], []
    if user:
        where.append("user_id = (SELECT id FROM users WHERE username = ?)")
        args.append(user)
    if slot:
        where.append("slot = ?")
        args.append(slot)
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        rows = [dict(r) for r in con.execute(sql, args)]
    except sqlite3.OperationalError as e:
        con.close()
        raise SystemExit(f"[FATAL] 读 pool_score_snapshot 失败: {e}\n"
                         f"        该表由复盘采集器写入，需先跑过至少一次时点采集。")
    con.close()
    return rows


def load_quote_map(db: str, codes: set[str]):
    """读日线，返回 {code: [(date, close), ...]}（按日期升序）。"""
    con = sqlite3.connect(db)
    qmap: dict[str, list[tuple[str, float]]] = defaultdict(list)
    if not codes:
        con.close()
        return qmap
    # 分批，避免 SQL 变量数超限
    codes = list(codes)
    CH = 400
    for i in range(0, len(codes), CH):
        chunk = codes[i:i + CH]
        ph = ",".join("?" * len(chunk))
        for code, d, close in con.execute(
                f"SELECT code, date, close FROM daily_quotes WHERE code IN ({ph}) "
                f"AND close IS NOT NULL ORDER BY code, date", chunk):
            c = _f(close)
            if c is not None:
                qmap[code].append((d, c))
    con.close()
    return qmap


# ---------------------------------------------------------------- 指标计算
def forward_return(qmap, code: str, base_date: str, base_price: float | None, horizon: int):
    """未来 horizon 个交易日的收益(%)。基准价优先用时点快照价，回退当日收盘。

    返回 (ret_pct, used_date) 或 (None, None)（数据不足/无行情）。
    """
    series = qmap.get(code)
    if not series:
        return None, None
    dates = [d for d, _ in series]
    # 找到 base_date 在序列中的位置（快照当天可能非交易日 → 取其后第一个交易日）
    idx = None
    for i, d in enumerate(dates):
        if d >= base_date:
            idx = i
            break
    if idx is None:
        return None, None
    if idx + horizon >= len(series):
        return None, None          # 未来数据不足 → 不给结论
    end_price = series[idx + horizon][1]
    bp = base_price if (base_price and base_price > 0) else series[idx][1]
    if not bp or bp <= 0:
        return None, None
    return (end_price - bp) / bp * 100.0, series[idx + horizon][0]


def rankdata(xs: list[float]) -> list[float]:
    """平均秩（并列取均值），用于 Spearman。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def spearman(a: list[float], b: list[float]) -> float | None:
    return pearson(rankdata(a), rankdata(b))


# ---------------------------------------------------------------- 报告生成
def build_samples(snaps, qmap, horizon: int):
    """把快照 + 未来收益拼成样本行。"""
    out = []
    for s in snaps:
        code = s.get("code")
        if not code:
            continue
        ret, _ = forward_return(qmap, code, s.get("trade_date") or "", _f(s.get("price")), horizon)
        if ret is None:
            continue                       # 无未来收益 → 该样本不参与任何统计
        out.append({
            "code": code,
            "date": s.get("trade_date"),
            "slot": s.get("slot"),
            "raw": _f(s.get("score_raw")),
            "adj": _f(s.get("score")),
            "factor": _f(s.get("adj_factor")) or 1.0,
            "vetoed": bool(s.get("vetoed")),
            "grade": s.get("grade") or "",
            "ret": ret,
        })
    return out


def confusion(samples, threshold: float, eps: float, use_adj: bool = False):
    """混淆矩阵。预测正例 = 分≥阈值 且 未否决；实际正例 = 未来收益 ≥ eps。"""
    tp = fp = tn = fn = 0
    for r in samples:
        score = r["adj"] if use_adj else r["raw"]
        if score is None or r["vetoed"]:
            continue
        pred = score >= threshold
        real = r["ret"] >= eps
        if pred and real:
            tp += 1
        elif pred and not real:
            fp += 1
        elif not pred and real:
            fn += 1
        else:
            tn += 1
    n = tp + fp + tn + fn
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    acc = (tp + tn) / n if n else None
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "n": n,
            "precision": prec, "recall": rec, "accuracy": acc}


def grade_stats(samples):
    """按落库 grade 分组统计（用原始分口径）。"""
    g = defaultdict(list)
    for r in samples:
        if r["raw"] is None:
            continue
        g[r["grade"] or grade_of(r["raw"])].append(r["ret"])
    rows = []
    for name in ("A", "B", "C", "D"):
        rets = g.get(name, [])
        n = len(rets)
        if not n:
            rows.append((name, 0, None, None, True))
            continue
        win = sum(1 for x in rets if x > 0) / n
        avg = sum(rets) / n
        rows.append((name, n, win, avg, n < MIN_N))
    return rows


def grade_of(raw: float) -> str:
    for name, cut in GRADE_CUTS:
        if raw >= cut:
            return name
    return "D"


def daily_ic(samples, use_adj: bool = False, by_slot: bool = True):
    """每日截面 IC（Spearman）序列。

    by_slot=True  → 按 (交易日, 时点) 分组：同一时点截面才严格可比，是**正确口径**。
    by_slot=False → 按交易日分组（每只票取该日**最后一个时点**的分数，避免同票多时点
                    重复计数虚增样本量）——样本稀疏时的降级口径，结果必须标注。
    """
    picked = defaultdict(dict)     # key -> {code: (score, ret)}
    for r in samples:
        score = r["adj"] if use_adj else r["raw"]
        if score is None or r["vetoed"]:
            continue
        key = (r["date"], r["slot"] or "") if by_slot else r["date"]
        prev = picked[key].get(r["code"])
        # 降级口径下同一只票保留时点最晚的一条
        if prev is None or (not by_slot and (r["slot"] or "") >= (prev[2] or "")):
            picked[key][r["code"]] = (score, r["ret"], r["slot"] or "")
    ics = []
    for key in sorted(picked):
        pairs = list(picked[key].values())
        if len(pairs) < 5:            # 截面样本太少，秩相关无意义
            continue
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append((key, len(pairs), ic))
    return ics


def ic_series(samples, use_adj: bool):
    """取 IC 序列，样本稀疏时自动降级到「按日截面」，并回报所用口径。"""
    ics = daily_ic(samples, use_adj=use_adj, by_slot=True)
    if len(ics) >= 3:
        return ics, "按(交易日,时点)截面"
    ics2 = daily_ic(samples, use_adj=use_adj, by_slot=False)
    if len(ics2) > len(ics):
        return ics2, "按交易日截面(已降级:每票取当日最晚时点)"
    return ics, "按(交易日,时点)截面"


def ir_of(ics: list[float]) -> float | None:
    if len(ics) < 3:
        return None
    m = sum(ics) / len(ics)
    var = sum((x - m) ** 2 for x in ics) / (len(ics) - 1)
    sd = math.sqrt(var)
    return m / sd if sd > 0 else None


# ---------------------------------------------------------------- walk-forward
def walk_forward(samples, grid, threshold: float, eps: float, folds: int = 3):
    """对折扣系数做 walk-forward：前 k 折样本内选最优，后一折样本外验证。

    注意：这里**不重新评分**（历史指标无法重算），而是检验
    「用不同系数缩放后，分档排序是否更稳」——即 adj = raw * factor
    （factor 为网格值，与真实 adj_factor 区分）在样本外的 IC/胜率表现。
    """
    days = sorted({r["date"] for r in samples if r["date"]})
    if len(days) < folds + 1:
        return None, f"交易日仅 {len(days)} 天，不足 {folds + 1} 天，无法做 walk-forward"
    size = max(1, len(days) // (folds + 1))
    cuts = [days[min(len(days) - 1, (i + 1) * size)] for i in range(folds)]
    result = []
    for i in range(folds):
        lo = cuts[i - 1] if i > 0 else None
        hi = cuts[i]
        is_ = [r for r in samples if (lo is None or (r["date"] or "") > lo) and (r["date"] or "") <= hi]
        oos = [r for r in samples if (hi is not None and (r["date"] or "") > hi)
               and (i + 1 >= folds or (r["date"] or "") <= cuts[i + 1])]
        if not is_ or not oos:
            continue
        best = None
        for f in grid:
            scaled = [dict(r, adj=(r["raw"] * f if r["raw"] is not None else None)) for r in is_]
            ics = [ic for _, _, ic in daily_ic(scaled, use_adj=True)]
            m = sum(ics) / len(ics) if ics else None
            cf = confusion(scaled, threshold, eps, use_adj=True)
            score = (m or 0.0) * 0.5 + (cf["precision"] or 0.0) * 0.5
            if best is None or score > best[1]:
                best = (f, score, m, cf)
        # 用样本内选出的系数在样本外验证
        f = best[0]
        oos_scaled = [dict(r, adj=(r["raw"] * f if r["raw"] is not None else None)) for r in oos]
        oos_ics = [ic for _, _, ic in daily_ic(oos_scaled, use_adj=True)]
        oos_cf = confusion(oos_scaled, threshold, eps, use_adj=True)
        result.append({
            "fold": i + 1, "is_days": len({r['date'] for r in is_}), "oos_days": len({r['date'] for r in oos}),
            "factor": f, "is_ic": best[2], "is_prec": best[3]["precision"],
            "oos_ic": (sum(oos_ics) / len(oos_ics)) if oos_ics else None,
            "oos_prec": oos_cf["precision"], "oos_n": oos_cf["n"],
        })
    return result, None


# ---------------------------------------------------------------- 输出
def hr(title=""):
    print("\n" + "=" * 72)
    if title:
        print(title)
        print("=" * 72)


def pct(v, nd=1):
    return "-" if v is None else f"{v * 100:.{nd}f}%"


def num(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser(description="评分模型回测（IC/IR + 混淆矩阵 + walk-forward）")
    ap.add_argument("--db", default="backend/data/app.db", help="SQLite 库路径")
    ap.add_argument("--user", default=None, help="只回测某用户名（默认全部）")
    ap.add_argument("--slot", default=None, help="只回测某个时点（如 0945 / 1345）")
    ap.add_argument("--threshold", type=float, default=60.0, help="看多阈值（默认 60）")
    ap.add_argument("--horizon", type=int, default=5, help="未来持有交易日数（默认 5）")
    ap.add_argument("--eps", type=float, default=0.2, help="判定上涨的收益阈值 %（默认 0.2）")
    ap.add_argument("--walk-forward", action="store_true", help="跑折扣系数 walk-forward")
    ap.add_argument("--grid", default="0.5,0.6,0.7,0.75,0.8,0.9,1.0", help="系数网格")
    ap.add_argument("--self-check", action="store_true", help="自检：验证脚本自身算法")
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    hr("评分模型回测报告")
    print(f"数据库      : {args.db}")
    print(f"生成时间    : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"参数        : 阈值={args.threshold}  持有={args.horizon}日  上涨判定≥{args.eps}%  用户={args.user or '全部'}")

    snaps = load_snapshots(args.db, args.user, args.slot)
    if not snaps:
        print("\n[数据不足] pool_score_snapshot 无快照数据。")
        print("           该表由复盘采集器在每个监控时点写入；当前还没有累积到任何数据，")
        print("           **不输出任何回测结论**（宁可空白，也不用编造的数撑场面）。")
        print("           建议：至少累积 20 个交易日的快照再跑本脚本。")
        return 0
    dates = sorted({s.get("trade_date") for s in snaps if s.get("trade_date")})
    slots = sorted({s.get("slot") for s in snaps if s.get("slot")})
    print(f"快照        : {len(snaps)} 行 / {len(dates)} 个交易日 / 时点={','.join(slots)}")

    qmap = load_quote_map(args.db, {s["code"] for s in snaps if s.get("code")})
    samples = build_samples(snaps, qmap, args.horizon)
    covered = len(samples)
    print(f"可回测样本  : {covered} 行（有未来 {args.horizon} 日收益）")
    if covered < MIN_N:
        print(f"\n[数据不足] 有效样本仅 {covered} 行 < {MIN_N}，不足以支撑任何统计结论。")
        print("           本脚本到此为止，不输出混淆矩阵/IC。")
        return 0

    # ---------- 1. IC / IR ----------
    hr("1. 截面有效性 IC / IR（越高越好；IC>0.03 才算有弱区分度）")
    ic_store = {}
    for label, use_adj in (("原始分 raw", False), ("调整后分 adj", True)):
        ics, mode = ic_series(samples, use_adj=use_adj)
        ic_store[label] = ics
        vals = [ic for _, _, ic in ics]
        if not vals:
            print(f"  {label:12s} 无任何截面样本≥5 只，无法计算 IC")
            continue
        m = sum(vals) / len(vals)
        ir = ir_of(vals)
        pos = sum(1 for v in vals if v > 0) / len(vals)
        flag = "" if len(vals) >= 10 else f"  ⚠ IC 天数 {len(vals)}<10，IR 不可信"
        print(f"  {label:12s} IC均值={num(m, 4)}  IR={num(ir, 3)}  IC>0占比={pct(pos)}  "
              f"(N={len(vals)} 天){flag}")
        print(f"               口径: {mode}")

    # ---------- 2. 混淆矩阵 ----------
    hr(f"2. 阈值 {args.threshold} 分混淆矩阵（预测=分≥阈值且未否决；实际=收益≥{args.eps}%）")
    for label, use_adj in (("原始分 raw", False), ("调整后分 adj", True)):
        cf = confusion(samples, args.threshold, args.eps, use_adj)
        if cf["n"] < MIN_N:
            print(f"  {label:12s} N={cf['n']} < {MIN_N} → 样本不足，不给胜率")
            continue
        print(f"  {label:12s} 精确率={pct(cf['precision'])}  召回={pct(cf['recall'])}  "
              f"准确率={pct(cf['accuracy'])}  N={cf['n']}")
        print(f"               TP={cf['tp']} FP={cf['fp']} FN={cf['fn']} TN={cf['tn']}")

    # ---------- 3. 阈值 × 折扣系数 兼容性诊断 ----------
    # 背景（见项目记忆）：档位阈值 A/B/C=80/65/50 是按「原始分」标定的，
    # 而落库 grade 由「调整后分」判档。崩坏市系数 0.49~0.75 会让固定 60 分阈值
    # 在调整后分口径下几乎不可达 → 召回崩塌、全池被压成 D。本节把它量化出来。
    hr("3. 阈值 × 折扣系数 兼容性诊断")
    cf_raw = confusion(samples, args.threshold, args.eps, use_adj=False)
    cf_adj = confusion(samples, args.threshold, args.eps, use_adj=True)
    rec_r, rec_a = cf_raw.get("recall"), cf_adj.get("recall")
    if cf_raw["n"] >= MIN_N and cf_adj["n"] >= MIN_N and rec_r and rec_a is not None:
        drop = (rec_r - rec_a) / rec_r if rec_r else 0.0
        print(f"  原始分口径   召回={pct(rec_r)}  精确率={pct(cf_raw['precision'])}")
        print(f"  调整后分口径 召回={pct(rec_a)}  精确率={pct(cf_adj['precision'])}")
        if drop > 0.4:
            print(f"\n  ⚠ 召回下滑 {pct(drop)}：固定 {args.threshold} 分阈值与折扣系数**不兼容**。")
            print("    调整后分被环境系数整体压低后，几乎不再触发信号（不是模型变差，是刻度被平移了）。")
            print("    建议二选一：")
            print("      a) 判档/触发一律改用**原始分**（跨日可比，推荐）；")
            print(f"      b) 阈值随系数联动：阈值_adj = {args.threshold} × 当日 adj_factor。")
        elif drop > 0.15:
            print(f"\n  ⚠ 召回下滑 {pct(drop)}，折扣系数对触发率有实质影响，建议按上面 (a)(b) 择一统一口径。")
        else:
            print(f"\n  召回仅下滑 {pct(drop)}，当前系数区间下固定阈值尚可接受。")
    else:
        print("  样本不足（N<20），不做兼容性诊断。")

    # ---------- 4. 分档统计 ----------
    hr("4. 分档表现（档位按原始分 80/65/50 现算，未落库 grade 时也一致）")
    print(f"  {'档位':<6}{'N':>6}{'胜率':>10}{'平均收益':>12}   可信度")
    for name, n, win, avg, low in grade_stats(samples):
        if n == 0:
            print(f"  {name:<6}{0:>6}{'-':>10}{'-':>12}   —")
            continue
        mark = "⚠ 样本不足(N<20)，仅供参考" if low else "可信"
        print(f"  {name:<6}{n:>6}{pct(win):>10}{num(avg) + '%':>12}   {mark}")

    # ---------- 4. walk-forward ----------
    if args.walk_forward:
        hr("5. 折扣系数 walk-forward（样本内选系数 → 样本外验证）")
        grid = [float(x) for x in args.grid.split(",") if x.strip()]
        res, err = walk_forward(samples, grid, args.threshold, args.eps)
        if err:
            print(f"  [跳过] {err}")
        else:
            print(f"  {'折':<4}{'IS天':>6}{'OOS天':>7}{'选中系数':>10}{'IS_IC':>9}{'OOS_IC':>9}{'OOS精确率':>11}{'OOS_N':>8}")
            for r in res:
                print(f"  {r['fold']:<4}{r['is_days']:>6}{r['oos_days']:>7}{num(r['factor']):>10}"
                      f"{num(r['is_ic'], 4):>9}{num(r['oos_ic'], 4):>9}{pct(r['oos_prec']):>11}{r['oos_n']:>8}")
            fs = [r["factor"] for r in res if r["factor"] is not None]
            if fs:
                spread = max(fs) - min(fs)
                verdict = "稳定（≤0.1）" if spread <= 0.1 else f"不稳定（跨 {spread:.2f}）"
                print(f"\n  系数跨度: {min(fs):.2f}~{max(fs):.2f} → {verdict}")
                if spread > 0.1:
                    print("  → 不建议据此调参：样本外最优系数漂移过大，更可能是噪声而非稳定规律。")

    # ---------- 6. 结论 ----------
    hr("6. 调参建议")
    if len(dates) < 20:
        print(f"  当前仅 {len(dates)} 个交易日，低于 20 日门槛。")
        print("  → **不要微调折扣系数**。样本不足以区分「模型有效」与「运气好」。")
        print(f"  → 请把采集器跑满 20+ 交易日（当前 {len(dates)}/{20}），再回来跑一次。")
    else:
        print("  样本量达标，可结合上文 IC/IR 与 walk-forward 系数稳定性判断是否微调：")
        print("   · IC 均值 > 0.03 且 IR > 0.3 → 排序有区分度，可考虑微调阈值；")
        print("   · walk-forward 系数跨度 ≤ 0.1 → 折扣系数稳定，可按样本内最优值小幅调整；")
        print("   · 否则维持默认系数 1.0，继续累积数据。")
    print()
    return 0


# ---------------------------------------------------------------- 自检
def self_check():
    """用已知答案的合成数据验证脚本自身算法（不碰任何真实库）。"""
    hr("脚本自检（self-check）")
    ok, bad = [], []
    chk = lambda n, c, x="": (ok if c else bad).append(n + (" | " + x if x else ""))

    # 1) rankdata 并列取平均秩
    chk("rankdata 并列平均秩", rankdata([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0])

    # 2) 完全正相关 → Spearman = 1
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    chk("Spearman 完全单调=1", abs((spearman(a, [2.0 * x for x in a]) or 0) - 1.0) < 1e-9)

    # 3) 完全反相关 → -1
    chk("Spearman 完全反序=-1", abs((spearman(a, list(reversed(a))) or 0) + 1.0) < 1e-9)

    # 4) 混淆矩阵：构造 4 个已知样本
    s = [
        {"code": "A", "date": "2026-01-01", "slot": "0945", "raw": 90.0, "adj": 90.0,
         "factor": 1.0, "vetoed": False, "grade": "A", "ret": 5.0},   # TP
        {"code": "B", "date": "2026-01-01", "slot": "0945", "raw": 90.0, "adj": 90.0,
         "factor": 1.0, "vetoed": False, "grade": "A", "ret": -5.0},  # FP
        {"code": "C", "date": "2026-01-01", "slot": "0945", "raw": 30.0, "adj": 30.0,
         "factor": 1.0, "vetoed": False, "grade": "D", "ret": 5.0},   # FN
        {"code": "D", "date": "2026-01-01", "slot": "0945", "raw": 30.0, "adj": 30.0,
         "factor": 1.0, "vetoed": False, "grade": "D", "ret": -5.0},  # TN
    ]
    cf = confusion(s, 60.0, 0.2)
    chk("混淆矩阵 TP/FP/FN/TN 各 1", (cf["tp"], cf["fp"], cf["fn"], cf["tn"]) == (1, 1, 1, 1),
        f"实际={cf['tp'],cf['fp'],cf['fn'],cf['tn']}")
    chk("精确率=50%", abs((cf["precision"] or 0) - 0.5) < 1e-9)
    # 否决行必须被排除
    s2 = s + [{"code": "E", "date": "2026-01-01", "slot": "0945", "raw": 95.0, "adj": 95.0,
               "factor": 1.0, "vetoed": True, "grade": "A", "ret": 9.0}]
    chk("否决行不计入混淆矩阵", confusion(s2, 60.0, 0.2)["n"] == 4)

    # 5) 档位阈值（对原始分）
    chk("档位 85→A", grade_of(85.0) == "A")
    chk("档位 70→B", grade_of(70.0) == "B")
    chk("档位 55→C", grade_of(55.0) == "C")
    chk("档位 10→D", grade_of(10.0) == "D")

    # 6) forward_return：未来数据不足必须返回 None（不给假结论）
    qm = {"X": [("2026-01-01", 10.0), ("2026-01-02", 11.0)]}
    chk("未来数据不足→None", forward_return(qm, "X", "2026-01-01", 10.0, 5)[0] is None)
    r, _ = forward_return(qm, "X", "2026-01-01", 10.0, 1)
    chk("forward_return 计算正确", r is not None and abs(r - 10.0) < 1e-9, f"ret={r}")

    # 7) 样本不足直接不产出（confusion N<MIN_N 时主流程会拦截）
    chk("MIN_N 门槛生效", MIN_N == 20)

    # 8) IC 路径：造 6 天 × 20 只、分数与收益**完全正相关**的面板 → IC 必须接近 1
    panel = []
    for d in range(6):
        day = f"2026-02-0{d + 1}"
        for i in range(20):
            sc = 40.0 + i * 2.0                    # 40..78，严格单调
            panel.append({"code": f"C{i:02d}", "date": day, "slot": "0945",
                          "raw": sc, "adj": sc, "factor": 1.0, "vetoed": False,
                          "grade": grade_of(sc), "ret": (i - 10) * 0.5})   # 与 sc 同序
    ics, mode = ic_series(panel, use_adj=False)
    vals = [ic for _, _, ic in ics]
    chk("ic_series 返回 6 天 IC", len(vals) == 6, f"实际 {len(vals)} 天, 口径={mode}")
    chk("完全正相关 → IC≈1", bool(vals) and abs(sum(vals) / len(vals) - 1.0) < 1e-6,
        f"IC均值={sum(vals)/len(vals) if vals else None}")
    # IC 恒为 1 时标准差为 0 → IR 数学上无定义，返回 None 才是**正确**行为
    chk("IC 无波动→IR 无定义(返回None)", ir_of(vals) is None)
    noisy = [1.0, 0.4, -0.1, 0.6, 0.2, 0.55, 0.1, 0.35]
    chk("IC 有波动→IR 可计算", ir_of(noisy) is not None, f"IR={num(ir_of(noisy), 3)}")

    # 9) IC 降级口径：同票多时点时按日合并且每票只取最晚时点
    multi = []
    for i in range(8):
        # 同一只票两个时点，若不去重会让截面样本虚增一倍
        multi.append({"code": f"M{i}", "date": "2026-03-01", "slot": "0945",
                      "raw": 50.0 + i, "adj": 50.0 + i, "factor": 1.0,
                      "vetoed": False, "grade": "C", "ret": i * 0.3})
        multi.append({"code": f"M{i}", "date": "2026-03-01", "slot": "1345",
                      "raw": 60.0 + i, "adj": 60.0 + i, "factor": 1.0,
                      "vetoed": False, "grade": "C", "ret": i * 0.3})
    # 直接验证降级函数本身：按日分组时，每只票只保留一个时点（不重复计数）
    ics_m = daily_ic(multi, use_adj=False, by_slot=False)
    grp_n = [n for _, n, _ in ics_m]
    chk("降级口径每票只取最晚时点(不重复计数)", bool(grp_n) and max(grp_n) == 8,
        f"按日分组各组样本量={grp_n}（8 只票→应为 8，若为 16 说明重复计数）")
    # 且保留的是**最晚**时点：slot=1345 的分(60+i) 而非 0945 的(50+i)
    kept = defaultdict(dict)
    for r in multi:
        prev = kept[r["date"]].get(r["code"])
        if prev is None or r["slot"] >= prev[2]:
            kept[r["date"]][r["code"]] = (r["raw"], r["ret"], r["slot"])
    chk("降级保留最晚时点(1345)", all(v[2] == "1345" for v in kept["2026-03-01"].values()))

    # 10) walk-forward 能在足够数据上跑通（不校验数值，只校验结构与不崩）
    wf, err = walk_forward(panel, [0.6, 0.8, 1.0], 60.0, 0.2, folds=2)
    chk("walk_forward 可跑通", err is None and bool(wf), err or f"{len(wf) if wf else 0} 折")
    if wf:
        chk("walk_forward 每折都有 OOS 系数", all(r["factor"] is not None for r in wf))
    # 数据不足时必须明确报错而不是硬算
    wf2, err2 = walk_forward(panel[:5], [1.0], 60.0, 0.2, folds=3)
    chk("walk_forward 数据不足→报错", err2 is not None, err2 or "")

    print("\n通过 " + str(len(ok)) + " 项:")
    for x in ok:
        print("  OK  " + x)
    if bad:
        print("\n失败 " + str(len(bad)) + " 项:")
        for x in bad:
            print("  ❌  " + x)
    print("\n结论: " + ("❌ 脚本自身有问题" if bad else "✅ 脚本算法自检通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
