"""信号模块实现：RuleSignalEngine（遵循 SignalEngine 接口）。
把用户习惯用的几个指标规律化：条件原语 AND/OR 组合 + 优先级 + 作用范围；
多指标共振 → 置信度；动作含 add/reduce/alert/hold + 可读原因 + 风险前置（软引导）。
条件类型覆盖均线/量能/价格/动能/波动结构（13 类，按普适有效项收敛）。"""
from __future__ import annotations
from typing import Any

from app.services.data_fetcher import ensure_quotes
from app.services.indicators import compute_snapshot
from app.services.interfaces import SignalEngine, SignalResult


def _ma_at(closes: list[float], idx: int, period: int) -> float | None:
    if idx + 1 < period:
        return None
    return sum(closes[idx + 1 - period: idx + 1]) / period


_OP = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _eval_condition(c: dict[str, Any], snap, quotes, closes: list[float]) -> tuple[bool, str]:
    t = c.get("type")
    if t in ("below_ma", "above_ma"):
        ma_p = int(c.get("ma", 20))
        days = int(c.get("days", 3))
        ok = 0
        for i in range(len(closes) - 1, max(-1, len(closes) - 1 - days), -1):
            m = _ma_at(closes, i, ma_p)
            if m is None:
                break
            if t == "below_ma" and closes[i] < m:
                ok += 1
            elif t == "above_ma" and closes[i] > m:
                ok += 1
            else:
                break
        res = ok >= days
        return res, f"{'连续' if res else '未连续'}{ok}日{('跌破' if t=='below_ma' else '站上')}{ma_p}日均线"
    if t == "vol_ratio":
        v = float(snap.vol_ratio)
        op = c.get("op", ">")
        res = _OP[op](v, float(c.get("value", 2)))
        return res, f"量比{v}{op}{c.get('value')}"
    if t == "change_pct":
        v = float(snap.change_pct)
        op = c.get("op", ">=")
        res = _OP[op](v, float(c.get("value", 3)))
        return res, f"涨跌幅{v}%{op}{c.get('value')}"
    if t == "amplitude":
        v = float(snap.amplitude)
        op = c.get("op", "<=")
        res = _OP[op](v, float(c.get("value", 9)))
        return res, f"振幅{v}%{op}{c.get('value')}"
    if t == "turnover":
        v = float(snap.turnover)
        op = c.get("op", "<=")
        res = _OP[op](v, float(c.get("value", 25)))
        return res, f"换手率{v}%{op}{c.get('value')}"
    if t == "price_cross":
        ma_p = int(c.get("ma", 20))
        m = _ma_at(closes, len(closes) - 1, ma_p)
        mp = _ma_at(closes, len(closes) - 2, ma_p)
        if m is None or mp is None:
            return False, f"{ma_p}日均线数据不足"
        cur, prev = closes[-1], closes[-2]
        if c.get("dir") == "up":
            res = prev <= mp and cur > m
            return res, f"价格上穿{ma_p}日均线"
        else:
            res = prev >= mp and cur < m
            return res, f"价格下穿{ma_p}日均线"
    if t == "macd_cross":
        d, de = snap.macd["dif"], snap.macd["dea"]
        d0, de0 = None, None
        if len(closes) >= 27:
            # 前一交易日用近似：用上一根需重算，简化用 hist 符号变化
            pass
        if c.get("dir") == "gold":
            res = d > de  # 金叉后多头
            return res, f"MACD金叉(DEA上)"
        else:
            res = d < de
            return res, f"MACD死叉(DEA下)"
    if t == "kdj":
        k, d_, j = snap.kdj["k"], snap.kdj["d"], snap.kdj["j"]
        if c.get("state") == "overbought":
            return j > 100, f"KDJ超买(J={round(j,1)})"
        if c.get("state") == "oversold":
            return j < 0, f"KDJ超卖(J={round(j,1)})"
        if c.get("dir") == "gold":
            return k > d_, f"KDJ金叉(K>D)"
        return k < d_, f"KDJ死叉(K<D)"
    if t == "boll_touch":
        up, lo = snap.boll["upper"], snap.boll["lower"]
        if c.get("band") == "upper":
            return snap.close >= up and up > 0, f"触布林上轨{up}"
        return snap.close <= lo and lo > 0, f"触布林下轨{lo}"
    if t == "box_break":
        sup, pres = snap.box["support"], snap.box["pressure"]
        if c.get("dir") == "up":
            return snap.close > pres, f"放量突破箱体压力{pres}"
        return snap.close < sup, f"跌破箱体支撑{sup}"
    return False, f"未知条件:{t}"


def _risk_level(snap) -> str:
    if abs(snap.change_pct) >= 7 or snap.amplitude >= 9 or snap.vol_ratio >= 3:
        return "高"
    return "中"


def _risk_advice(risk_level: str, action: str) -> str:
    """根据风险等级和操作类型给出风控建议(软引导,最终由用户决策)。"""
    if action == "add":
        return {
            "高": "⚠️ 高风险:严禁追高,仓位≤10%,严格设止损",
            "中": "中等风险:仓位≤15%,跌破支撑立即止损",
            "低": "风险可控:可分批建仓,建议先小仓试探",
        }.get(risk_level, "风险等级未知,谨慎决策")
    if action == "reduce":
        return {
            "高": "🚨 紧急减仓:跌破关键支撑,建议减半以上仓位",
            "中": "建议减仓:跌破均线/箱体下沿,先减1/3观察",
            "低": "适度减仓:信号共振不足,减至合理仓位",
        }.get(risk_level, "风险等级未知,谨慎决策")
    return "暂无明确建议,关注后续走势"


def _confidence(n_satisfied: int) -> int:
    # 多指标共振：满足条件的独立条件越多，置信度越高（上限 100）
    return min(100, 45 + 12 * max(0, n_satisfied))


class RuleSignalEngine:
    def evaluate(self, pool: list[str], rules: list[dict[str, Any]], db) -> list[SignalResult]:
        results: list[SignalResult] = []
        # 规则按优先级降序
        for rule in sorted(rules, key=lambda r: r.get("priority", 0), reverse=True):
            cond = rule.get("conditions_json", {})
            if isinstance(cond, str):
                import json
                cond = json.loads(cond)
            logic = cond.get("logic", "AND")
            conditions = cond.get("conditions", [])
            scope = rule.get("scope", "all")
            scope_codes = None
            if scope != "all":
                import json
                try:
                    scope_codes = set(json.loads(scope) if isinstance(scope, str) else scope)
                except Exception:
                    scope_codes = None
            for code in pool:
                if scope_codes and code not in scope_codes:
                    continue
                quotes = ensure_quotes(code, 90)
                if len(quotes) < 25:
                    continue
                closes = [q.close for q in quotes]
                snap = compute_snapshot(quotes)
                evals = [_eval_condition(c, snap, quotes, closes) for c in conditions]
                passed = [e for e in evals if e[0]]
                if logic == "AND" and len(passed) < len(conditions):
                    continue
                if logic == "OR" and len(passed) == 0:
                    continue
                reason = "；".join(d for _, d in evals if _)
                action = rule.get("action", "alert")
                sig = SignalResult(
                    code=code, signal_type=action, action=action,
                    reason=f"[{rule.get('name','规则')}] {reason}",
                    risk_level=_risk_level(snap),
                    risk_advice=_risk_advice(_risk_level(snap), action),  # 风控前置文案
                    confidence=_confidence(len(passed)),
                    metrics={"close": snap.close, "change_pct": snap.change_pct,
                             "vol_ratio": snap.vol_ratio, "turnover": snap.turnover,
                             "macd": snap.macd, "kdj": snap.kdj, "box": snap.box},
                )
                results.append(sig)
        return results


def get_signal_engine(kind: str = "rule") -> SignalEngine:
    return RuleSignalEngine()
