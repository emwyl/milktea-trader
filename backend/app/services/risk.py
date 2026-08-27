"""风控模块实现：SoftRiskAdvisor（遵循 RiskAdvisor 接口，软引导）。
不强制拦截，只给出风险前置建议（仓位上限、止损位），由用户最终决策。
超阈值强预警：信号风险等级提升并在 reason 中提示。"""
from __future__ import annotations
from typing import Any

from app.services.interfaces import RiskAdvisor, SignalResult


class SoftRiskAdvisor:
    def assess(self, signal: SignalResult, risk_config: dict[str, Any]) -> SignalResult:
        rc = risk_config or {}
        max_pos = float(rc.get("max_position_pct", 20))
        stop = float(rc.get("stop_loss_pct", 5))
        max_total = float(rc.get("max_total_pct", 50))
        close = float(signal.metrics.get("close", 0) or 0)
        stop_price = round(close * (1 - stop / 100), 2) if close else 0.0
        change = float(signal.metrics.get("change_pct", 0) or 0)

        advice = (f"建议单只仓位上限{max_pos}%、总仓上限{max_total}%；"
                  f"参考止损位≈{stop_price}（按止损{stop}%测算）。"
                  f"请结合自身风险偏好确认，勿盲目跟随。")
        # 超阈值强预警（软引导：提升风险等级并提示，不拦截）
        flags = []
        if abs(change) >= 7:
            flags.append(f"单日涨跌{change}%偏大")
        if float(signal.metrics.get("vol_ratio", 0) or 0) >= 3:
            flags.append("量比骤增疑似异动")
        if signal.signal_type == "add" and signal.risk_level == "高":
            flags.append("加仓信号但风险偏高，建议审慎")
        if flags:
            signal.risk_level = "高"
            advice = "⚠️ " + "；".join(flags) + "。" + advice
        signal.risk_advice = advice
        return signal


def get_risk_advisor(kind: str = "soft") -> RiskAdvisor:
    return SoftRiskAdvisor()
