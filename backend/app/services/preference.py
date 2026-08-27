"""投资偏好分析：问卷 → 评分 → 画像 → 关注指标采集 → (LLM 建议在 router 中调用)。
所有评分透明、可解释，不黑箱。"""
from __future__ import annotations
from typing import Any

# 问卷：每题选项带维度得分（risk 风险承受 / agg 进攻性 / freq 交易频率 / stop 止损纪律）
QUESTIONNAIRE: list[dict[str, Any]] = [
    {"id": "risk_tolerance", "q": "你能接受单笔最大亏损是多少？",
     "options": [
        {"label": "≤3%", "score": {"risk": 20, "agg": 10}},
        {"label": "3%–6%", "score": {"risk": 50, "agg": 40}},
        {"label": "6%–10%", "score": {"risk": 75, "agg": 70}},
        {"label": ">10%", "score": {"risk": 95, "agg": 95}},
     ]},
    {"id": "holding_period", "q": "你习惯的持仓周期？",
     "options": [
        {"label": "日内/隔日", "score": {"agg": 90, "freq": 90}},
        {"label": "几天~两周（短线波段）", "score": {"agg": 60, "freq": 60}},
        {"label": "数周~数月", "score": {"agg": 30, "freq": 20}},
        {"label": "半年以上", "score": {"agg": 10, "freq": 5}},
     ]},
    {"id": "capital", "q": "可投入股市的资金占闲钱比例？",
     "options": [
        {"label": "≤20%", "score": {"risk": 20}},
        {"label": "20%–50%", "score": {"risk": 50}},
        {"label": "50%–80%", "score": {"risk": 75}},
        {"label": ">80%", "score": {"risk": 95}},
     ]},
    {"id": "stop_discipline", "q": "触及止损位你的做法？",
     "options": [
        {"label": "严格止损、不补仓摊成本", "score": {"stop": 90, "risk": 30}},
        {"label": "多数执行，偶尔犹豫", "score": {"stop": 60, "risk": 55}},
        {"label": "常扛单等回本", "score": {"stop": 20, "risk": 85}},
     ]},
    {"id": "style", "q": "你更偏好哪种走势？",
     "options": [
        {"label": "低波动、箱体稳定缓慢增长", "score": {"vol": 10}},
        {"label": "题材强势突破、波动大", "score": {"vol": 90, "agg": 70}},
        {"label": "均值回归、高抛低吸", "score": {"vol": 50}},
     ]},
    {"id": "industry_care", "q": "你关注行业的程度？",
     "options": [
        {"label": "严格看行业/板块趋势联动", "score": {"sector": 80}},
        {"label": "偶尔看", "score": {"sector": 40}},
        {"label": "不看，只看个股", "score": {"sector": 10}},
     ]},
]

# 可关注的指标（供用户勾选+排序）
INDICATOR_CHOICES = [
    {"key": "ma", "name": "均线 MA（5/10/20/60）", "desc": "趋势方向与价格位置"},
    {"key": "volume", "name": "量能（量比/换手）", "desc": "活跃度与趋势真伪验证"},
    {"key": "macd", "name": "MACD", "desc": "趋势动能与转折"},
    {"key": "boll", "name": "布林带 BOLL", "desc": "波动边界、超买超卖"},
    {"key": "kdj", "name": "KDJ", "desc": "短线超买超卖极值"},
    {"key": "rsi", "name": "RSI", "desc": "相对强弱"},
    {"key": "amplitude", "name": "振幅", "desc": "日内波动幅度"},
    {"key": "box", "name": "箱体（支撑/压力）", "desc": "高抛低吸与突破结构"},
]


def score(answers: dict[str, int]) -> dict[str, Any]:
    """answers: {question_id: option_index}。返回维度评分与画像。"""
    dims = {"risk": 0, "agg": 0, "freq": 0, "stop": 0, "vol": 40, "sector": 30}
    for q in QUESTIONNAIRE:
        idx = answers.get(q["id"])
        if idx is None:
            continue
        opt = q["options"][idx]
        for k, v in opt.get("score", {}).items():
            dims[k] = max(dims.get(k, 0), v)
    # 画像判定（风险优先）
    risk = dims["risk"]
    if risk <= 40:
        archetype = "稳健型"
    elif risk >= 70:
        archetype = "激进型"
    elif dims.get("vol", 40) <= 25:
        archetype = "低波动偏好"
    else:
        archetype = "短线波段"
    return {"scores": dims, "archetype": archetype,
            "summary": f"风险承受{risk}/100，进攻性{dims['agg']}，止损纪律{dims['stop']}"}


def match_scheme(archetype: str) -> str:
    mapping = {"稳健型": "steady", "激进型": "aggressive", "短线波段": "swing", "低波动偏好": "lowvol"}
    return mapping.get(archetype, "custom")
