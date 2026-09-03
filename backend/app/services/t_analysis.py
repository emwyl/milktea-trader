"""个股箱体做T分析：6大分区 + 3选1操作建议。
设计原则（来自需求）：
- 箱体震荡做T辅助，仅作人工决策参考；不预测涨跌、不自动交易。
- 极简：只保留高频有用的指标，拒绝堆砌复杂衍生指标。
- 所有判断阈值与计算公式均写中文注释，方便逐条验证模型有效性。
- 风控软引导：风险文字红色高亮，但最终由用户决策。
"""
from __future__ import annotations
import datetime as dt

from app.models import Stock, StockTConfig, TrackedPool
from app.services.data_fetcher import ensure_quotes, get_market_status, get_t_realtime, get_sector_trend, _fetch_intraday
from app.services.indicators import _sma, compute_snapshot  # 复用简单移动平均工具 + 指标快照(三维投票)


# ============ 基础工具 ============
def _box_20(daily_quotes):
    """近20个交易日箱体：上沿=最高价最大值(压力)，下沿=最低价最小值(支撑)。"""
    if not daily_quotes:
        return 0.0, 0.0
    win = daily_quotes[-20:]
    support = min(q.low for q in win)      # 箱体下沿（支撑位）
    pressure = max(q.high for q in win)    # 箱体上沿（压力位）
    return support, pressure


def _ma5(daily_quotes):
    """5日线 = 最近5个交易日收盘价简单平均。"""
    if len(daily_quotes) < 5:
        return 0.0
    return _sma([q.close for q in daily_quotes], 5)


# ============ 分区2：量能解读 ============
def _vol_ratio_text(vr: float) -> str:
    """量比大白话解读（通用经验阈值）。"""
    if vr < 0.5:
        return "极度缩量，波动很小，不适合做T"
    if vr < 1.0:
        return "成交清淡，谨慎观察"
    if vr < 1.5:
        return "正常成交，可以观察机会"
    return "放量，资金有动作，重点跟踪"


def _amount_compare_text(today_v: float, prev_v: float) -> str:
    """今日实时成交量 VS 前一交易日全天成交量:标注放量/持平/缩量。
    使用 volume(手)而非 amount(元)——腾讯日线接口不返回 amount 字段,
    强用会得到「vs 昨日 0」的错误结论;volume 字段每日都有真实数据。"""
    if prev_v <= 0:
        return "对比基准缺失"
    ratio = today_v / prev_v
    if ratio > 1.2:
        return "放量"
    if ratio < 0.8:
        return "缩量"
    return "持平"


def _prev_trade_volume(daily_quotes, today_date: str) -> tuple[float, str | None]:
    """返回「前一个完整交易日」的成交量(手),以及该日日期。

    关键:daily[-1] 在盘中/盘后都是今日,即使盘后数据已完整(成交量 15.97 万手)，
    也不应与今日盘中累计(16 万手)对比——含义不同(今日实时累计 vs 昨日全天)。
    所以严格按「日期 < 今日」往前找第一根 volume > 0 的 K 线(自然跳过
    今日 + 周末/节假日),找到的是「上一个交易日」。
    """
    if not daily_quotes:
        return 0.0, None
    for q in reversed(daily_quotes):
        if q.date == today_date:
            continue  # 跳过今日
        if (q.volume or 0) > 0:
            return q.volume, q.date
    return 0.0, None


def _turnover_text(t: float) -> str:
    """换手率活跃度说明（箱体做T优选区间 3%~10%）。"""
    if t < 3:
        return "流动性偏弱"
    if t <= 10:
        return "流动性合适（做T优选区间）"
    return "换手偏高，注意追高风险"


# ============ 分区3：箱体价位 ============
def _box_position_text(price: float, support: float, pressure: float) -> str:
    """现价在箱体中的位置提示。靠近上沿=压力适合高抛；靠近下沿=支撑企稳才可低吸。"""
    if pressure <= support:
        return "箱体区间异常，建议观望"
    rng = pressure - support
    # 距上沿/下沿 15% 范围内视为“靠近”
    if price >= pressure - rng * 0.15:
        return "靠近箱体上沿：压力区间，适合T仓高抛"
    if price <= support + rng * 0.15:
        return "靠近箱体下沿：支撑区间，企稳后才可小仓低吸做T"
    return "箱体中间区域：无明确点位，建议观望"


# ============ 分区4：短线信号 ============
def _ma5_signal(price: float, ma5: float) -> str:
    """现价与5日线对比：站上=短线偏强；跌破=短线偏弱。"""
    if ma5 <= 0:
        return "数据不足"
    return "短线偏强（站上5日线）" if price > ma5 else "短线偏弱（跌破5日线）"


def _volume_price_match(vol_ratio: float, change_pct: float) -> dict:
    """量价匹配 4 种结果的自动判断。放量上涨/缩量回调=健康；缩量拉升/放量下跌=风险。"""
    if vol_ratio > 1.2 and change_pct > 0:
        return {"level": "ok", "text": "健康：放量上涨（有资金推动）"}
    if vol_ratio < 0.8 and change_pct < 0:
        return {"level": "ok", "text": "健康：缩量回调（抛压轻）"}
    if vol_ratio < 0.8 and change_pct > 0:
        return {"level": "risk", "text": "风险：缩量拉升（无量虚涨，警惕回落）"}
    if vol_ratio > 1.2 and change_pct < 0:
        return {"level": "risk", "text": "风险：放量下跌（资金出逃）"}
    return {"level": "neutral", "text": "量价中性，无明显信号"}


# ============ 分区5：风控 ============
def _risk_warnings(realtime: dict, risk_note: str, change_pct: float, vol_ratio: float) -> list[str]:
    """动态风险提醒（全部红字高亮）。
    1) 特殊风控备注优先（如“年底清仓禁加仓”）
    2) 通用：量比<0.5 交投冷清；缩量阴跌 禁止开新T仓"""
    warns = []
    if risk_note and risk_note.strip():
        warns.append(f"⚠️ 标的特殊风控：{risk_note.strip()}")  # 用户自定义红字警告，置顶
    if vol_ratio < 0.5:
        warns.append("⚠️ 量比<0.5：交投冷清，差价很难覆盖手续费，不建议做T")
    if change_pct < 0 and vol_ratio < 0.8:
        warns.append("⚠️ 现价持续缩量阴跌：无企稳信号，禁止开新T仓")
    return warns


def _forbid_new_t(warns: list[str]) -> bool:
    """是否禁止开新T仓：出现“禁止”或“不建议”类提示即禁止。"""
    return any(("禁止" in w or "不建议" in w) for w in warns)


# ============ 分区5b：持仓风控（止损/止盈/套牢决策）============
# 设计原则：先控风险再谈收益。所有阈值来自主流交易共识，写中文注释便于验证。
# 参数预留扩展：第一期用合理默认，后续可做成「方案类型/设置」可调参数。
_STOP_LOSS_PCT = 0.08      # 止损线：浮亏达 8% 触发风控（短线通用单笔止损阈值）
_WARN_PCT = 0.05           # 预警线：浮亏达 5% 进入风控观察区（先于止损提醒）
_DEEP_LOSS_PCT = 0.15      # 深套线：浮亏超 15% 视为深套，止损意义下降 → 改反弹减仓策略
_TAKE_PROFIT_PCT = 0.10    # 止盈目标：盈利达 10% 提示分批止盈（A股短线常见目标）
_RECOVER_LIMIT_PCT = 0.30  # 回本难度线：需涨超 30% 才回本 → 摊薄无意义
_MOVE_STOP_PCT = 0.06      # 移动止盈：已盈利持仓回撤 6% 提示落袋（保护浮盈）


def position_risk_plan(position: dict, price: float, support: float, pressure: float,
                       forbid_new: bool) -> dict:
    """持仓风控计算：止损位、止盈位、摊薄/止损决策、风险等级、三级预警线。
    仅在用户填了持仓（股数+成本价）时才有意义。
    返回：{has, risk_level, risk_label, warn_line, warn_line_text,
           stop_loss, stop_loss_text, take_profit, take_profit_text,
           dilution, plan, note}"""
    if not position.get("has"):
        return {"has": False}
    cost = position["cost_price"]
    if cost <= 0:
        return {"has": False}
    # —— 核心计算 ——
    loss_rate = (cost - price) / cost          # 浮亏率（0 为平价，正数=亏损）
    profit_rate = (price - cost) / cost        # 浮盈率
    warn_line = round(cost * (1 - _WARN_PCT), 2)          # 预警线 = 成本×(1-5%)
    stop_loss = round(cost * (1 - _STOP_LOSS_PCT), 2)     # 止损位 = 成本×(1-8%)
    take_profit = round(min(pressure, cost * (1 + _TAKE_PROFIT_PCT)), 2)  # 止盈位 = min(压力位, 成本×1.1)
    recover_needed = (cost - price) / price if price > 0 else 0.0  # 回本所需涨幅

    risk_level, risk_label = "green", "正常"
    plan, note, dilution = "", "", "谨慎摊薄"
    stop_loss_text, take_profit_text = "", ""

    # —— 深套（浮亏>15%）：止损太晚，改反弹减仓策略 ——
    if loss_rate > _DEEP_LOSS_PCT:
        risk_level, risk_label = "red", "高风险"
        stop_loss = None  # 深套股止损位已无意义（现价远低于成本-8%），置空避免误导
        stop_loss_text = f"已深套 {loss_rate*100:.1f}%，跌破止损位已无意义，不建议此时割肉地板"
        take_profit_text = f"反弹到压力位 {pressure} 可分批减仓，降低风险敞口"
        if recover_needed > _RECOVER_LIMIT_PCT:
            dilution = "摊薄无意义"
            plan = (f"深套 {loss_rate*100:.1f}%，回本需涨 {recover_needed*100:.0f}%，"
                    f"盲目摊薄回本概率低。建议：反弹到 {pressure} 分批减仓，"
                    f"把仓位降下来，避免深套扩大。")
        else:
            dilution = "谨慎摊薄"
            plan = (f"深套 {loss_rate*100:.1f}%。若箱体下沿 {support} 有企稳放量信号，"
                    f"可极小仓位试T降成本；否则反弹到 {pressure} 分批减仓。")
        note = "深套状态：先控风险，反弹减仓优于继续加仓。"

    # —— 中度亏损（8%~15%）：执行止损纪律 ——
    elif loss_rate >= _STOP_LOSS_PCT:
        if price <= stop_loss:
            risk_level, risk_label = "red", "高风险"
            stop_loss_text = f"已跌破止损位 {stop_loss}，止损纪律失效，反弹无力时应减仓"
            plan = (f"已跌破止损位 {stop_loss}（成本 {cost} 的 -{_STOP_LOSS_PCT*100:.0f}%）。"
                    f"若反弹不能快速收复，建议分批减仓控制损失，不宜补仓摊薄。")
            dilution = "不建议摊薄"
        else:
            risk_level, risk_label = "yellow", "警惕"
            stop_loss_text = f"止损位 {stop_loss}（成本 -{_STOP_LOSS_PCT*100:.0f}%），触及即执行风控减仓"
            plan = (f"浮亏 {loss_rate*100:.1f}%，接近止损位 {stop_loss}。"
                    f"纪律优先：触及止损位必须减仓，不加仓摊薄。")
            dilution = "不建议摊薄"
        note = "接近/触及止损线：执行纪律，先降风险。"

    # —— 轻套或盈利（浮亏<8%）：给止损 + 止盈，正常跟踪 ——
    else:
        stop_loss_text = f"止损位 {stop_loss}（成本 -{_STOP_LOSS_PCT*100:.0f}%）"
        if profit_rate > 0:
            take_profit_text = f"止盈位 {take_profit}（目标 +{_TAKE_PROFIT_PCT*100:.0f}% 或箱体压力位）"
            if price >= take_profit:
                risk_level, risk_label = "yellow", "注意止盈"
                plan = (f"已达到/超过止盈位 {take_profit}，建议分批止盈锁定利润"
                        f"（先减一部分，剩余按移动止盈 -{_MOVE_STOP_PCT*100:.0f}% 保护）。")
            elif price >= take_profit * 0.9:
                risk_level, risk_label = "yellow", "注意止盈"
                plan = (f"接近止盈位 {take_profit}，建议分批止盈锁定利润"
                        f"（先减一部分，剩余按移动止盈 -{_MOVE_STOP_PCT*100:.0f}% 保护）。")
            else:
                plan = (f"浮盈 {profit_rate*100:.1f}%，止损位 {stop_loss} 保护持仓。"
                        f"目标止盈 {take_profit}，到压力位 {pressure} 附近分批减仓。")
            dilution = "不建议摊薄（盈利持仓）"
        else:
            plan = (f"浮亏 {loss_rate*100:.1f}%（<{_STOP_LOSS_PCT*100:.0f}%），仍在风控容忍内。"
                    f"止损位 {stop_loss}，跌破即执行；反弹到压力位 {pressure} 可考虑减仓。")
            if price <= support * 1.03 and not forbid_new:
                dilution = "可在支撑位小额摊薄"
                plan += f"现价贴近箱体下沿 {support}，可小额摊薄降成本（控制总仓位）。"
        note = "轻套/盈利：止损保护 + 止盈目标双线跟踪。"

    # 预警线文本：给一条“现在跌到哪条线”的白话提示
    if loss_rate >= _WARN_PCT:
        warn_line_text = f"已跌破预警线 {warn_line}（成本 -{_WARN_PCT*100:.0f}%），进入风控观察区"
    else:
        warn_line_text = f"预警线 {warn_line}（成本 -{_WARN_PCT*100:.0f}%），跌破即重点跟踪"

    return {
        "has": True,
        "risk_level": risk_level, "risk_label": risk_label,
        "warn_line": warn_line, "warn_line_text": warn_line_text,
        "stop_loss": stop_loss, "stop_loss_text": stop_loss_text,
        "take_profit": take_profit, "take_profit_text": take_profit_text,
        "dilution": dilution, "plan": plan, "note": note,
    }


# ============ 分区6：操作建议 ============
def _advice(realtime: dict, support: float, pressure: float, vol_ratio: float,
            change_pct: float, avg_price: float, forbid_new: bool, risk_note: str,
            position_risk: dict | None = None, has_position: bool = False,
            position: dict | None = None,
            box_text: str = "", ma5_signal: str = "", vp_text: str = "",
            vote: dict | None = None, sector: dict | None = None,
            intraday_summary: str = "", ps_eval_text: str = "") -> dict:
    """根据全部指标联动，输出结构化操作建议。

    输出包含：
    - key/title：操作主结论标识与标题
    - summary：一句话综合结论
    - points：按维度拆解的 1/2/3 分点说明，便于用户理解差异原因
    - text：兼容旧版的完整文本（保留）

    优先级：持仓止盈减仓 > 风控硬拦 > 高抛 > 低吸 > 中间震荡可做T > 观望。
    """
    price = realtime["price"]
    high = realtime.get("high", price)
    low = realtime.get("low", price)
    turnover = realtime.get("turnover", 0)
    amplitude = (high - low) / price * 100 if price > 0 else 0.0
    near_up = pressure > support and price >= pressure - (pressure - support) * 0.15
    near_low = pressure > support and price <= support + (pressure - support) * 0.15
    in_middle = pressure > support and not near_up and not near_low

    vote = vote or {"overall": "数据不足", "bull": 0, "bear": 0, "neutral": 0}
    sector = sector or {"has": False, "text": "无板块数据"}

    # ---- 先确定主结论（保持原有优先级） ----
    main_key, main_title, main_summary, main_text = "wait", "【观望，不操作】", "", ""

    # 1) 持仓感知：已有盈利持仓触及止盈位 → 优先建议减仓落袋
    if has_position and position_risk and position_risk.get("has"):
        if (position_risk.get("risk_label") == "注意止盈"
                and price >= position_risk.get("take_profit", 0)):
            main_key, main_title = "reduce", "【建议分批止盈/减仓】"
            main_summary = ("持仓已达止盈位，建议先分批减仓锁定利润；"
                            "日内若冲高至压力位可高抛T仓，但不再净加仓。")
            main_text = (f"持仓已达止盈位 {position_risk['take_profit']}（成本 +10% 或箱体压力位），"
                         f"建议分批减仓锁定利润：先减一部分，剩余按移动止盈 -{_MOVE_STOP_PCT*100:.0f}% 保护；"
                         f"日内可在压力位高抛、回踩均价低吸做T 摊薄，但不净加仓。")

    # 2) 风控硬拦
    if not main_text and forbid_new:
        main_key, main_title = "wait", "【观望，不操作】"
        main_summary = "当前存在风险信号，不建议开新T仓，等待企稳后再评估。"
        main_text = "当前存在风险信号（缩量阴跌/量比过低/特殊风控），不开T仓，等待企稳。"

    # 3) 等待高抛（卖出已有T仓）
    if not main_text and near_up and change_pct > 0 and vol_ratio < 0.8:
        main_key, main_title = "sell", "【等待高抛机会（卖出T仓）】"
        main_summary = "价格接近箱体上沿且上涨无量，持有T仓可分批卖出止盈。"
        main_text = "到达压力区间，上涨无量，持有T仓可分批卖出止盈。"

    # 4) 等待低吸（小仓T）
    if not main_text and near_low and vol_ratio < 1.2 and change_pct >= -0.5:
        main_key, main_title = "buy", "【等待低吸机会（小仓T）】"
        main_summary = "价格接近箱体下沿，等待放量站稳后可极小仓位试做T。"
        main_text = "到达支撑区间，等待放量站稳分时均价，极小仓位试做T，提前设置止损。"

    # 5) 箱体中间震荡可做T
    if not main_text and (in_middle and 2.5 <= amplitude <= 8.0 and 0.5 <= vol_ratio <= 2.5
                          and 2.0 <= turnover <= 10.0 and -1.0 <= change_pct <= 3.0):
        main_key, main_title = "t", "【可日内高抛低吸】"
        main_summary = "箱体中间区域，日内波动适中，可在分时均价附近小仓位做T。"
        main_text = "箱体中间区域，日内波动适中、量能配合，可在分时均价附近小仓位做T，严格止损。"

    # 6) 日内弱势+缩量
    if not main_text and price < avg_price and vol_ratio < 0.8:
        main_key, main_title = "wait", "【观望，不操作】"
        main_summary = "日内弱势且缩量无承接，建议等待企稳信号。"
        main_text = "日内弱势，缩量无承接，等待企稳信号，不开T仓。"

    # 7) 默认观望
    if not main_text:
        main_key, main_title = "wait", "【观望，不操作】"
        main_summary = "无明显做T点位，建议观望，等待箱体上下沿信号。"
        main_text = "无明显做T点位，建议观望，等待箱体上下沿信号。"

    # ---- 按维度生成分点说明，解释差异原因 ----
    points = []

    # 持仓维度
    position = position or {"has": False}
    if has_position and position_risk and position_risk.get("has"):
        cost = position.get("cost_price") or 0.0
        pnl_pct = (price - cost) / cost * 100 if cost else 0.0
        if position_risk.get("risk_label") == "注意止盈":
            points.append({
                "dim": "持仓风控",
                "text": (f"当前价 {price} 已达到止盈位 {position_risk.get('take_profit')}（成本 {cost:.2f}，"
                         f"浮盈约 {pnl_pct:.1f}%）。盈利持仓优先落袋，分批止盈后再用移动止盈保护剩余仓位。")
            })
        elif position_risk.get("risk_label") in ("高风险", "警惕"):
            points.append({
                "dim": "持仓风控",
                "text": (f"当前浮亏/风控状态：{position_risk.get('risk_label')}。{position_risk.get('plan', '')}"
                         f"{position_risk.get('note', '')}")
            })
        else:
            points.append({
                "dim": "持仓风控",
                "text": (f"持有成本 {cost:.2f}，当前价 {price}，浮盈/亏约 {pnl_pct:.1f}%。"
                         f"止损位 {position_risk.get('stop_loss')}，止盈位 {position_risk.get('take_profit')}，双线跟踪。")
            })
    else:
        points.append({"dim": "持仓风控", "text": "未填写持仓成本/股数，操作建议仅基于盘面指标，不含个人盈亏约束。"})

    # 箱体维度
    points.append({
        "dim": "箱体位置",
        "text": box_text or (f"箱体上沿 {pressure} / 下沿 {support}，当前价 {price}，"
                             f"{'靠近上沿' if near_up else ('靠近下沿' if near_low else '位于中间')}。")
    })

    # 量能维度
    points.append({
        "dim": "量能信号",
        "text": (f"量比 {vol_ratio:.2f}（{'成交清淡' if vol_ratio < 1 else '正常/放量'}），"
                 f"换手率 {turnover:.2f}%（{'流动性合适，适合做T' if 3 <= turnover <= 10 else '流动性偏弱/偏高'}）。"
                 f"当前量能不支持追涨，更适合高抛或观望。")
    })

    # 短线维度
    points.append({
        "dim": "短线信号",
        "text": f"{ma5_signal}；{vp_text}。短期趋势{'偏强' if '偏强' in ma5_signal else ('偏弱' if '偏弱' in ma5_signal else '震荡')}，但尚未形成明确单边信号。"
    })

    # 分时维度
    if intraday_summary:
        points.append({"dim": "分时走势", "text": intraday_summary})

    # 抛压/承接评估
    if ps_eval_text:
        points.append({"dim": "抛压/承接", "text": ps_eval_text})

    # 技术投票维度
    points.append({
        "dim": "技术投票",
        "text": (f"MACD/KDJ/布林综合结论：{vote.get('overall', '数据不足')}"
                 f"（看多 {vote.get('bull', 0)} / 看空 {vote.get('bear', 0)} / 中性 {vote.get('neutral', 0)}）。"
                 f"日线趋势{'向好' if vote.get('overall') == '偏多' else ('向淡' if vote.get('overall') == '偏空' else '不明')}，但需服从短期止盈/风控纪律。")
    })

    # 板块维度
    if sector.get("has"):
        points.append({"dim": "板块强度", "text": sector.get("text", "")})

    return {
        "key": main_key,
        "title": main_title,
        "summary": main_summary,
        "text": main_text,
        "points": points,
    }


# ============ 做T增强：置信空间 / 分时 / 次日 / 三维投票 / 板块 ============
def _atr(daily_quotes, n: int = 14) -> float:
    """平均真实波幅(14):做T置信价位与止损位的核心输入。

    真实波幅 TR = max(当日高-当日低, |当日高-昨收|, |当日低-昨收|)。
    ATR 用 Wilder 平滑(首值=前 n 日 TR 均值,其后递推),比简单均值更稳定。"""
    if len(daily_quotes) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(daily_quotes)):
        q, prev = daily_quotes[i], daily_quotes[i - 1]
        h, l, pc = q.high, q.low, prev.close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return round(sum(trs) / len(trs), 3) if trs else 0.0
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return round(atr, 3)


def _is_trading_time() -> bool:
    """是否处于 A 股交易时段(周一~周五 9:30-11:30 / 13:00-15:00)。
    用于区分次日预案是「盘中预演」还是「正式预案」。"""
    now = dt.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dt.time(9, 30) <= t <= dt.time(11, 30)) or (dt.time(13, 0) <= t <= dt.time(15, 0))


def _confidence_space(P: float, S: float, R: float, atr: float) -> list[dict]:
    """盘间动态价位 · 三档置信空间(做T 高抛/低吸参考价)。

    口径说明(务必提示用户):这里的"概率"是「策略激进度标签」而非真实统计概率——
    P0 最保守(价位远离现价,难触发但成功空间大),P2 最激进(价位贴现价,易触发但易被震)。

    公式:H(c)=min(R, P + c×ATR) 高抛参考价;L(c)=max(S, P − c×ATR) 低吸参考价。
    高抛价封顶到压力位、低吸价托底到支撑位,避免给出箱体外的无效价位。"""
    specs = [("P0", "90% 保守", 1.5), ("P1", "80% 平衡", 1.0), ("P2", "70% 激进", 0.5)]
    tiers = []
    for level, conf_label, c in specs:
        H = round(min(R, P + c * atr), 2)
        L = round(max(S, P - c * atr), 2)
        h_dist = round((H - P) / P * 100, 2) if P else 0.0
        l_dist = round((P - L) / P * 100, 2) if P else 0.0
        # 触发状态:现价已到/超过高抛价→可高抛;已到/低于低吸价→可低吸
        sell_trig = P >= H - 1e-9
        buy_trig = P <= L + 1e-9
        tiers.append({
            "level": level, "conf_label": conf_label, "c": c,
            "sell": H, "buy": L, "sell_dist": h_dist, "buy_dist": l_dist,
            "sell_trig": sell_trig, "buy_trig": buy_trig,
        })
    return tiers


def _pressure_support_eval(price: float, support: float, pressure: float,
                           vol_ratio: float, change_pct: float) -> dict:
    """抛压/承接评估:结合价位相对箱体位置 + 量能。

    抛压:价格贴近压力位且放量(量比>1.5)→ 高抛信号增强;缩量靠近→ 可能突破,不宜盲抛。
    承接:价格贴近支撑位且缩量止跌→ 低吸信号增强;放量跌破→ 支撑失效,不宜低吸。"""
    if pressure <= support:
        return {"text": "箱体区间异常,无法评估抛压/承接", "sell_force": "unknown", "buy_force": "unknown"}
    rng = pressure - support
    near_up = price >= pressure - rng * 0.15
    near_low = price <= support + rng * 0.15
    sell_force, buy_force = "neutral", "neutral"
    parts = []
    if near_up and vol_ratio > 1.5:
        sell_force = "strong"; parts.append("价格贴近压力位且放量(量比>1.5),抛压显现,高抛信号增强")
    elif near_up and vol_ratio < 0.8:
        parts.append("价格贴近压力位但缩量,可能尝试突破,不宜盲目高抛")
    elif near_up:
        parts.append("价格贴近压力位,关注放量确认抛压")
    if near_low and vol_ratio < 0.8 and change_pct < 0:
        buy_force = "strong"; parts.append("价格贴近支撑位且缩量止跌,承接显现,低吸信号增强")
    elif near_low and vol_ratio > 1.5 and change_pct < 0:
        buy_force = "weak"; parts.append("价格放量跌破支撑位,支撑失效,不宜低吸")
    elif near_low:
        parts.append("价格贴近支撑位,关注缩量企稳信号")
    if not parts:
        parts.append("价格处于箱体中部,抛压/承接均不明显,等待上下沿信号")
    return {"text": "; ".join(parts), "sell_force": sell_force, "buy_force": buy_force}


def _intraday_analysis(code: str, daily_quotes: list) -> dict:
    """分时走势解读:价格 + 运行均价(VWAP) 折线、均价斜率、量价背离、早盘方向。

    返回 {ok, source, prices[], vwap[], avg_price, vwap_slope, deviation,
          divergence, early_dir, summary, tags[]},供前端 Canvas 画图 + 文案展示。
    prices/vwap 为分钟级序列;沙箱/网络失败时 ok=False 并给降级文案。"""
    out = {"ok": False, "source": "none", "prices": [], "vwap": [], "avg_price": 0.0,
           "vwap_slope": 0.0, "deviation": 0.0, "divergence": "unknown",
           "early_dir": "unknown", "summary": "", "tags": []}
    try:
        intra = _fetch_intraday(code)
    except Exception:
        intra = None
    if not intra:
        out["summary"] = "分时数据获取失败(演示/沙箱环境下腾讯分时接口不可达),分时解读暂不可用"
        return out
    prices, vols, avg_price, times = intra
    if not prices or len(prices) < 2:
        out["summary"] = "分时数据为空,分时解读暂不可用"
        return out
    # 运行 VWAP(量加权均价):cumsum(price×vol)/cumsum(vol)
    cum_pv, cum_v = 0.0, 0.0
    vwap = []
    for p, v in zip(prices, vols):
        cum_pv += p * v; cum_v += v
        vwap.append(round(cum_pv / cum_v, 2) if cum_v else p)
    n = len(prices)
    # 均价斜率:末 30 点相对首点的线性趋势(百分化)
    window = prices[-30:] if n >= 30 else prices
    vwap_slope = (window[-1] - window[0]) / window[0] * 100 if len(window) >= 2 and window[0] else 0.0
    last_p = prices[-1]
    deviation = (last_p - vwap[-1]) / vwap[-1] * 100 if vwap[-1] else 0.0
    # 量价背离:现价接近全日高位但量未同步放大 → 顶背离;接近低位但量未放大 → 底背离
    ph, pl = max(prices), min(prices)
    vmax, vmin = (max(vols), min(vols)) if vols else (0, 0)
    divergence = "unknown"
    if last_p >= ph * 0.995 and vols[-1] < vmax * 0.6:
        divergence = "top"
    elif last_p <= pl * 1.005 and vols[-1] < vmin * 1.5:
        divergence = "bottom"
    # 早盘方向:前 30 分钟涨跌幅
    early = prices[:30] if n >= 30 else prices[:max(1, n // 4)]
    early_dir = "unknown"
    if len(early) >= 2 and early[0]:
        ed = (early[-1] - early[0]) / early[0] * 100
        early_dir = "up" if ed > 0.3 else ("down" if ed < -0.3 else "flat")
    # 标签 + 结论句
    tags = []
    if vwap_slope > 0.2: tags.append("均价趋势↑"); bias = "日内偏强"
    elif vwap_slope < -0.2: tags.append("均价趋势↓"); bias = "日内偏弱"
    else: tags.append("均价趋势→"); bias = "日内震荡"
    if deviation > 1: tags.append(f"偏离均价+{deviation:.1f}%")
    elif deviation < -1: tags.append(f"偏离均价{deviation:.1f}%")
    else: tags.append("贴合均价")
    if divergence == "top": tags.append("量价顶背离")
    elif divergence == "bottom": tags.append("量价底背离")
    else: tags.append("量价配合")
    if early_dir == "up": tags.append("早盘强势")
    elif early_dir == "down": tags.append("早盘偏弱")
    if vwap_slope > 0.2 and deviation > 1:
        summary = f"日内偏强,现价偏离均价 {deviation:.1f}% 已偏贵,回踩均价且量能配合可低吸;冲高至压力区分批高抛"
    elif vwap_slope < -0.2:
        summary = "日内偏弱,反弹至均价附近宜高抛;跌破支撑且放量则放弃做T"
    elif divergence == "top":
        summary = "出现量价顶背离,追涨动能不足,接近压力位分批高抛"
    elif divergence == "bottom":
        summary = "出现量价底背离,杀跌动能减弱,接近支撑位可试低吸"
    else:
        summary = "日内震荡,量价配合中性,等待箱体上下沿信号再做T"
    out.update({"ok": True, "source": "live", "prices": prices, "vwap": vwap,
                "times": times, "avg_price": round(avg_price, 2),
                "vwap_slope": round(vwap_slope, 2), "deviation": round(deviation, 2),
                "divergence": divergence, "early_dir": early_dir,
                "summary": summary, "tags": tags})
    return out


def _nextday_plan(P_close: float, S: float, R: float, atr: float, is_intraday: bool) -> dict:
    """次日短线预案:基于昨日收盘价的 3 档高抛/低吸,含触发/失效条件。

    is_intraday=True → 标注「盘中预演」(基于当前价推演);收盘后刷新为正式。"""
    specs = [
        ("P0", "90% 保守", 1.5, "开盘不大幅跳空(>3% 反向)", "开盘跳空>3% 且反向运行 / 放量跌破箱体下沿"),
        ("P1", "80% 平衡", 1.0, "维持当前箱体结构", "放量跌破箱体下沿 / 大盘转弱"),
        ("P2", "70% 激进", 0.5, "早盘方向延续", "缩量横盘 <30 分钟 / 冲高无量"),
    ]
    tiers = []
    for level, conf_label, c, sell_cond, fail_cond in specs:
        H = round(min(R, P_close + c * atr), 2)
        L = round(max(S, P_close - c * atr), 2)
        tiers.append({"level": level, "conf_label": conf_label, "c": c,
                      "sell": H, "buy": L, "sell_cond": sell_cond, "fail_cond": fail_cond})
    note = "基于当前价的「盘中预演」,正式预案于收盘后按昨收刷新" if is_intraday else "正式次日预案(基于昨日收盘)"
    return {"tiers": tiers, "is_preview": is_intraday, "note": note}


def _three_dim_vote(daily_quotes: list) -> dict:
    """三维投票:MACD / KDJ / 布林带 各投多/空/中性一票,聚合为整体信号。

    MACD:DIF>DEA 且柱>0 看多;反之看空;交叉区中性。
    KDJ:J>100 超买看空(偏反转减仓);J<0 超卖看多;K>D 看多 否则看空。
    布林:收≥上轨超买看空;收≤下轨超卖看多;收>中轨看多 否则看空。"""
    snap = compute_snapshot(daily_quotes)
    macd = snap.macd; dif, dea, hist = macd["dif"], macd["dea"], macd["hist"]
    macd_vote = "bull" if (dif > dea and hist > 0) else ("bear" if (dif < dea and hist < 0) else "neutral")
    kdj = snap.kdj; k, d, j = kdj["k"], kdj["d"], kdj["j"]
    if j > 100: kdj_vote = "bear"
    elif j < 0: kdj_vote = "bull"
    elif k > d: kdj_vote = "bull"
    else: kdj_vote = "bear"
    boll = snap.boll; up, mid, low = boll["upper"], boll["mid"], boll["lower"]
    close = snap.close
    if close >= up: boll_vote = "bear"
    elif close <= low: boll_vote = "bull"
    elif close > mid: boll_vote = "bull"
    else: boll_vote = "bear"
    votes = [
        ("MACD", macd_vote, f"DIF{dif}/DEA{dea}/柱{hist}"),
        ("KDJ", kdj_vote, f"K{k}/D{d}/J{j}"),
        ("布林", boll_vote, f"上{up}/中{mid}/下{low} 收{close}"),
    ]
    bull = sum(1 for _, v, _ in votes if v == "bull")
    bear = sum(1 for _, v, _ in votes if v == "bear")
    neutral = 3 - bull - bear
    overall = "偏多" if bull > bear else ("偏空" if bear > bull else "中性")
    return {"votes": [{"name": n, "vote": v, "detail": d} for n, v, d in votes],
            "bull": bull, "bear": bear, "neutral": neutral, "overall": overall, "strength": bull - bear}


def _sector_strength(industry: str) -> dict:
    """板块强度:用所属行业近 20 日样本股平均斜率近似(只读本地缓存,不触发网络)。"""
    if not industry:
        return {"has": False, "text": "无行业信息,跳过板块强度评估"}
    st = get_sector_trend(industry)
    trend, slope = st.get("trend"), st.get("slope", 0.0)
    if trend == "up":
        text = f"所属行业「{industry}」近 20 日走强(斜率 {slope*100:.1f}%),板块资金偏暖,做T 成功率高"
    elif trend == "down":
        text = f"所属行业「{industry}」近 20 日走弱(斜率 {slope*100:.1f}%),板块承压,跟风股成功率低,谨慎"
    else:
        text = f"所属行业「{industry}」近 20 日横盘(斜率 {slope*100:.1f}%),板块无明显方向"
    return {"has": True, "industry": industry, "trend": trend, "slope": slope, "text": text}


# ============ 主入口 ============
def analyze_t(code: str, db) -> dict:
    """聚合做T分析全部输出。db 为数据库会话。"""
    daily = ensure_quotes(code, 120)
    rt = get_t_realtime(code, daily)
    st = db.query(Stock).filter(Stock.code == code).first()
    pool = db.query(TrackedPool).filter(TrackedPool.code == code, TrackedPool.status == "active").first()
    tcfg = db.query(StockTConfig).filter(StockTConfig.code == code).first()

    name = (rt.get("name") or (st.name if st else "")) or code
    industry = st.industry if st else ""

    # —— 持仓打通（可选）——
    position = {"has": False, "qty": None, "cost_price": None, "pnl": None}
    if pool and pool.position_qty and pool.cost_price:
        pnl = round(pool.position_qty * (rt["price"] - pool.cost_price), 2)
        position = {"has": True, "qty": pool.position_qty,
                    "cost_price": pool.cost_price, "pnl": pnl}

    # —— 分区3：箱体 ——
    support, pressure = _box_20(daily)
    # 自定义支撑/压力（用户本地记忆，覆盖自动计算）
    custom_support = tcfg.custom_support if tcfg else None
    custom_pressure = tcfg.custom_pressure if tcfg else None
    box_text = _box_position_text(rt["price"],
                                  custom_support if custom_support else support,
                                  custom_pressure if custom_pressure else pressure)

    # —— 分区2:量能 ——
    # 用「前一个完整交易日」(非「昨日」)。今日是周一,昨日=周日,数据应对比上周五;
    # 严格按「日期 < 今日」从尾部往前找,跳过今日和周末/节假日,得到真实的上一交易日。
    # 改用 volume 而非 amount:腾讯日线接口不返回 amount 字段,强用会得到「vs 昨日 0」错误。
    today_date = dt.date.today().isoformat()
    prev_vol, prev_date = _prev_trade_volume(daily, today_date)
    today_vol = rt.get("volume", 0)  # 实时累计成交量(手)
    amount_cmp = _amount_compare_text(today_vol, prev_vol)

    # —— 分区4：短线信号 ——
    ma5 = _ma5(daily)
    vp = _volume_price_match(rt["vol_ratio"], rt["change_pct"])

    # —— 分区5：风控 ——
    risk_note = tcfg.risk_note if tcfg else ""
    warns = _risk_warnings(rt, risk_note, rt["change_pct"], rt["vol_ratio"])
    forbid = _forbid_new_t(warns)

    # —— 大盘环境过滤：大盘大跌时全局提示（先控风险）——
    market = get_market_status()
    if market.get("level") == "red":
        warns.append(f"⚠️ {market['name']} {market['change_pct']:.2f}%，大盘大跌，系统性风险升高，短线做T成功率下降，建议降低仓位观望")
    elif market.get("level") == "yellow":
        warns.append(f"⚠️ {market['name']} {market['change_pct']:.2f}%，大盘偏弱，追高需谨慎")

    # —— 分区5b：持仓风控（止损/止盈/套牢决策）——
    position_risk = position_risk_plan(position, rt["price"],
                                       custom_support if custom_support else support,
                                       custom_pressure if custom_pressure else pressure,
                                       forbid)

    # —— 分区1：状态标签 ——
    intraday_strong = rt["price"] > rt["avg_price"]  # 现价>分时均价=偏强

    # —— 分区7：做T增强（P0/P1/P2 置信空间 + 分时解读 + 次日预案 + 三维投票 + 板块强度）——
    eff_support = custom_support if custom_support else support
    eff_pressure = custom_pressure if custom_pressure else pressure
    atr = _atr(daily)
    # ATR 兜底：数据不足时用箱体宽度的 1/8 近似日均波幅，保证三档价位不塌缩成同一个数
    if atr <= 0:
        atr = round(max((eff_pressure - eff_support) / 8.0, rt["price"] * 0.01), 3)
    is_intraday = _is_trading_time()
    conf_tiers = _confidence_space(rt["price"], eff_support, eff_pressure, atr)
    ps_eval = _pressure_support_eval(rt["price"], eff_support, eff_pressure,
                                     rt["vol_ratio"], rt["change_pct"])
    intraday = _intraday_analysis(code, daily)
    # 次日预案基准价：盘中用现价推演，盘后用最近收盘价
    base_close = daily[-1].close if daily else rt["price"]
    nextday = _nextday_plan(rt["price"] if is_intraday else base_close,
                            eff_support, eff_pressure, atr, is_intraday)
    try:
        vote = _three_dim_vote(daily)
    except Exception:
        vote = {"votes": [], "bull": 0, "bear": 0, "neutral": 0,
                "overall": "数据不足", "strength": 0}
    sector = _sector_strength(industry)

    # —— 分区6：建议 ——
    # 把分区2/3/4/7 的关键文本传入，让建议能按维度拆解说明差异原因
    advice = _advice(
        rt,
        custom_support if custom_support else support,
        custom_pressure if custom_pressure else pressure,
        rt["vol_ratio"], rt["change_pct"], rt["avg_price"], forbid, risk_note,
        position_risk=position_risk, has_position=position["has"], position=position,
        box_text=box_text,
        ma5_signal=_ma5_signal(rt["price"], ma5),
        vp_text=vp["text"],
        vote=vote,
        sector=sector,
        intraday_summary=intraday.get("summary", ""),
        ps_eval_text=ps_eval.get("text", ""),
    )
    enhance = {
        "atr": atr,
        "atr_pct": round(atr / rt["price"] * 100, 2) if rt["price"] else 0.0,
        "is_intraday": is_intraday,
        "confidence": {
            "tiers": conf_tiers,
            "note": "P0/P1/P2 为「策略激进度」标签，非真实统计概率：P0 最保守（价位远离现价，难触发但空间大），P2 最激进（价位贴近现价，易触发但易被震）。价位=现价±系数×ATR，并以箱体压力/支撑封顶托底。",
        },
        "pressure_eval": ps_eval,
        "intraday": intraday,
        "nextday": nextday,
        "vote": vote,
        "sector": sector,
    }

    return {
        "code": code, "name": name, "industry": industry,
        "source": rt["source"],  # akshare / demo，前端提示数据来源
        "realtime": rt,
        "position": position,
        "section1": {
            "name": name, "code": code, "industry": industry,
            "price": rt["price"], "change": rt["change"], "change_pct": rt["change_pct"],
            "today_high": rt["today_high"], "today_low": rt["today_low"],
            "avg_price": rt["avg_price"],
            "intraday_tag": "日内偏强（现价>分时均价）" if intraday_strong else "日内弱势（现价<分时均价）",
            "intraday_strong": intraday_strong,
            "realtime_confidence": rt.get("realtime_confidence", "ok"),
            "realtime_diff_pct": rt.get("realtime_diff_pct", 0.0),
        },
        "section2": {
            "vol_ratio": rt["vol_ratio"], "vol_ratio_text": _vol_ratio_text(rt["vol_ratio"]),
            "today_volume": today_vol, "prev_volume": prev_vol, "prev_date": prev_date, "amount_cmp": amount_cmp,
            "turnover": round(rt["turnover"], 2), "turnover_text": _turnover_text(rt["turnover"]),
        },
        "section3": {
            "support": support, "pressure": pressure,
            "custom_support": custom_support, "custom_pressure": custom_pressure,
            "box_text": box_text,
        },
        "section4": {
            "ma5": round(ma5, 3),
            "ma5_signal": _ma5_signal(rt["price"], ma5),
            "vp_level": vp["level"], "vp_text": vp["text"],
        },
        "section5": {"warnings": warns, "forbid_new_t": forbid,
                     "position_risk": position_risk},
        "section6": advice,
        "enhance": enhance,
        "risk_note": risk_note,
    }
