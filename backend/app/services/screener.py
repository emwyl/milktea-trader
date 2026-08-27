"""选股模块实现：RuleScreener（遵循 Screener 接口）。
配置驱动筛选：股价上限 / 周期平均振幅 / 箱体稳定 / 行业联动 / 流动性。全部参数化、可自定义。
预留扩展点：MLScreener 等新实现只需实现 Screener 接口并注册。"""
from __future__ import annotations
from typing import Any

from sqlalchemy import select, func

from app.models import Stock
from app.services.data_fetcher import ensure_quotes, get_sector_trend
from app.services.interfaces import Screener, Candidate


class RuleScreener:
    def run(self, config: dict[str, Any], db) -> list[Candidate]:
        cfg = config or {}
        price_max = float(cfg.get("price_max", 30))
        amp_win = int(cfg.get("amplitude_window", 20))
        amp_min = float(cfg.get("amplitude_avg_min", 3))
        amp_max = float(cfg.get("amplitude_avg_max", 5))
        box = cfg.get("box_stability", {}) or {}
        box_on = bool(box.get("enabled", True))
        box_win = int(box.get("window", 60))
        max_dd = float(box.get("max_drawdown_pct", 15))
        min_slope = float(box.get("min_trend_slope", 0.02))
        sector = cfg.get("sector_trend", {}) or {}
        sector_on = bool(sector.get("enabled", True))
        liq = cfg.get("liquidity", {}) or {}
        min_amount = float(liq.get("min_avg_amount", 1e8))
        industries_filter = cfg.get("industries")  # 可选行业白名单

        # 全 A 股场景:不再对每只 ensure_quotes(5500+ × 几秒 = 阻塞)
        # 改为:只对「daily_quotes 已有 ≥ 30 根缓存」的股票评估,无缓存跳过(可后续预热)
        from app.models import DailyQuote
        min_klines = max(10, amp_win, 30)  # 至少 30 根日线才评估(避免早期数据噪音)
        # 子查询:取「有 ≥ min_klines 根日线」的股票 code
        # 简化:用 join 拿有日线的股票,客户端过滤(不严谨但实用)
        from sqlalchemy import func
        sub = (db.query(DailyQuote.code, func.count(DailyQuote.id).label('cnt'))
               .group_by(DailyQuote.code).having(func.count(DailyQuote.id) >= min_klines).subquery())
        stocks = db.query(Stock).join(sub, Stock.code == sub.c.code).all()
        # 演示宇宙若空则 bypass（main 启动时已 seed）
        candidates: list[Candidate] = []
        for s in stocks:
            if industries_filter and s.industry not in industries_filter:
                continue
            try:
                # 仅取缓存,不再实时拉(让预热任务负责)
                from app.services.data_fetcher import _get_cached_quotes
                quotes = _get_cached_quotes(s.code, max(box_win, amp_win) + 30)
            except Exception:
                continue
            if len(quotes) < max(10, amp_win):
                continue
            last = quotes[-1]
            # ===== 条件评估(每只记录"通过/未通过"用于推荐原因) =====
            reasons_pass: list[str] = []  # 通过的条件
            reasons_fail: list[str] = []  # 未通过(用于排查/前端提示)
            if last.close > price_max:
                reasons_fail.append(f"价格 {last.close:.2f} > 上限 {price_max:.0f}")
                continue
            reasons_pass.append(f"价格 {last.close:.2f} ≤ {price_max:.0f}")

            # 流动性(腾讯日线 amount=0, 用 close×volume×100 估算;1 手=100 股)
            def _est_amount(q):
                return q.amount if (q.amount or 0) > 0 else (q.close or 0) * (q.volume or 0) * 100
            avg_amount = sum(_est_amount(q) for q in quotes[-amp_win:]) / amp_win
            if avg_amount < min_amount:
                reasons_fail.append(f"日均成交额 {avg_amount/1e4:.0f}万 < 门槛 {min_amount/1e4:.0f}万")
                continue
            reasons_pass.append(f"日均成交 {avg_amount/1e4:.0f}万 ≥ 门槛")

            # 平均振幅
            amps = [(q.high - q.low) / q.pre_close * 100 for q in quotes[-amp_win:] if q.pre_close]
            avg_amp = sum(amps) / len(amps) if amps else 0
            if not (amp_min <= avg_amp <= amp_max):
                reasons_fail.append(f"振幅 {avg_amp:.2f}% 不在 [{amp_min},{amp_max}]")
                continue
            reasons_pass.append(f"振幅 {avg_amp:.2f}% ∈ [{amp_min},{amp_max}]")

            # 箱体稳定(可选)
            if box_on:
                win = [q.close for q in quotes[-box_win:]]
                peak = win[0]
                mdd = 0.0
                for p in win:
                    peak = max(peak, p)
                    mdd = max(mdd, (peak - p) / peak)
                slope = (win[-1] - win[0]) / win[0] if win[0] else 0
                if mdd * 100 > max_dd or slope < min_slope:
                    reasons_fail.append(f"箱体不稳:回撤 {mdd*100:.1f}% > 阈值 / 斜率 {slope:.3f} 不足")
                    continue
                reasons_pass.append(f"箱体稳:回撤 {mdd*100:.1f}% ≤ {max_dd}% + 斜率 {slope:.3f} ≥ {min_slope}")

            # 行业联动(可选)
            sector_trend = None
            if sector_on:
                st = get_sector_trend(s.industry, db)
                sector_trend = st
                mode = sector.get("mode", "up")
                if mode == "up" and st.get("trend") != "up":
                    reasons_fail.append(f"行业 {s.industry or '未分类'} 趋势非上行")
                    continue
                if s.industry:
                    reasons_pass.append(f"行业 {s.industry} 趋势 {st.get('trend', 'n/a')}")

            # ===== 通过,收集完整原因 =====
            candidates.append(Candidate(
                code=s.code, name=s.name, industry=s.industry,
                metrics={
                    "price": round(last.close, 2),
                    "avg_amplitude": round(avg_amp, 2),
                    "avg_amount": round(avg_amount, 0),
                    "industry": s.industry,
                    "sector_trend": sector_trend,
                    # 推荐原因:前端直接展示
                    "reason_pass": reasons_pass,
                    "reason_fail_unmet": [],  # 全通过,没未满足项
                },
            ))
        return candidates


def get_screener(kind: str = "rule") -> Screener:
    """工厂：按 key 返回 Screener 实现（可插拔）。"""
    return RuleScreener()
