"""Pydantic 请求/响应模型。"""
from __future__ import annotations
from typing import Any, Optional

from pydantic import BaseModel


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    user: dict


class Msg(BaseModel):
    ok: bool = True
    msg: str = ""


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class PasswordResetIn(BaseModel):
    new_password: str


class SchemeTypeOut(BaseModel):
    key: str
    name: str
    risk_level: str
    screener_json: dict
    signal_json: dict
    risk_json: dict
    builtin: bool


class ScreenIn(BaseModel):
    name: str
    description: str = ""
    config: dict = {}
    scheme_type: str = "custom"


class ScreenOut(BaseModel):
    id: int
    name: str
    description: str
    is_active: bool
    config: dict
    scheme_type: str
    created_at: str
    updated_at: str


class CandidateOut(BaseModel):
    code: str
    name: str
    industry: str
    metrics: dict


class PoolIn(BaseModel):
    code: str
    note: str = ""
    cost_price: Optional[float] = None
    position_qty: Optional[float] = None
    position_pct: Optional[float] = None
    scheme_type: str = "custom"
    tag_ids: list[int] = []  # 可选标签(支持多选)


class PoolBatchDeleteIn(BaseModel):
    codes: list[str]


class PoolBatchTagsIn(BaseModel):
    codes: list[str]
    tag_ids: list[int] = []


class PoolImportItem(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None


class PoolImportIn(BaseModel):
    items: list[PoolImportItem] = []


class TagIn(BaseModel):
    name: str
    color: str = "#3b82f6"


class TagOut(BaseModel):
    id: int
    name: str
    color: str
    created_at: str


class PoolOut(BaseModel):
    id: int
    code: str
    name: Optional[str] = None
    industry: Optional[str] = None  # 行业(便于前端展示/筛选/看板分组)
    note: str
    cost_price: Optional[float] = None
    position_qty: Optional[float] = None
    position_pct: Optional[float] = None
    scheme_type: str
    status: str
    added_at: str
    tag_ids: list[int] = []
    tags: list[dict] = []  # [{id,name,color}, ...]
    day_view: str = ""  # v135 旧字段,保留兼容,不再写新值
    # v143：日初判断历史(从 day_view_log 派生)
    day_view_today: str = ""  # 当日最后一条 trend(看涨/看跌/风险/-),空=今日无 log
    day_view_log_count: int = 0  # 该 code 的总历史条数(便于判定「盯盘日志」按钮显示)


class DayViewIn(BaseModel):
    """日初判断更新。允许置空。"""
    day_view: str = ""


class DayViewLogIn(BaseModel):
    """v143：日初判断修改记录。trend 看涨/看跌/风险/-；target_price 可空；target_note ≤20字。
    trade_date 由前端传（YYYY-MM-DD），避免后端时区错位。
    v178：录入时可一并提交"录入当时"的综合评分快照 + 预判参考信息文本 + 结构化指标 JSON，
          用于盯盘日志表格的「综合评分 / 预判参考信息」两列展示。这三个字段非必填，留空表示
          该股为旧录入或当时未生成参考信息（旧数据保持向后兼容）。
    """
    trade_date: str  # YYYY-MM-DD,前端传入
    trend: str  # 看涨/看跌/风险/-
    target_price: Optional[float] = None
    target_note: str = ""  # ≤20字
    # v178: 录入时的快照(用于盯盘日志表格展示)
    composite_score: Optional[float] = None  # 综合评分(0-100),None 表示不入快照
    reference_text: str = ""  # 预判参考信息文本(如 "量比:1.0、换手:3.01%、…")
    reference_metrics_json: str = ""  # 结构化指标 JSON(后端写库时再验,默认空)


class DayViewLogOut(BaseModel):
    """v143：日初判断修改记录返回。v178 加 3 个快照字段。"""
    id: int
    code: str
    trade_date: str
    trend: str
    target_price: Optional[float] = None
    target_note: str = ""
    operator: str = ""
    operated_at: str = ""
    # v178
    composite_score: Optional[float] = None
    reference_text: str = ""
    reference_metrics_json: str = ""


class MetricsSummaryOut(BaseModel):
    """v178：录入日初判断前的预判参考信息快照——「综合评分 + 预判参考信息」合并返回。
    前端在打开日初判断录入弹窗时 GET 一次该端点，把 composite_score / reference_text
    同步到弹窗的两个只读预览字段；保存时这两个字段会随 POST 一并入库（成为盯盘日志列的快照值）。
    """
    code: str
    composite_score: Optional[float] = None  # 当前综合评分(None=缺数据)
    official_score: Optional[float] = None  # 后端官方分(给前端对照)
    reference_text: str = ""  # 组装好的预判参考信息(完整中文短句,用于预览)
    metrics: dict = {}  # 结构化指标 {量比,换手,日内振幅,MA5,MA20,箱体位置,…},便于前端兜底格式化


class WatchLogItem(BaseModel):
    """v143:盯盘日志单日条目。已聚合当日最后一条 + 计算偏离度/原因;
    v146 扩展:recap_user_text/recap_user/recap_updated_at——用户编辑的偏离原因复盘文本,优先展示。
    v178 扩展:composite_score / reference_text / reference_metrics_json——录入时刻的综合评分
    快照与预判参考信息，用于盯盘日志表格的「综合评分 / 预判参考信息」两列展示。
    """
    trade_date: str
    open: Optional[float] = None
    close: Optional[float] = None
    intraday_avg: Optional[float] = None  # 分时均价=amount/(volume*100),单位 元/股
    trend: str = ""  # 当日最后一条 trend
    target_price: Optional[float] = None
    target_note: str = ""
    deviation: Optional[float] = None  # (target - close) / close
    deviation_pct: Optional[float] = None  # 百分比
    deviation_reason: str = ""  # 默认模板话术(按 trend × deviation 方向)
    # v146: 用户编辑的偏离原因复盘
    recap: str = ""  # 用户编辑的文本;空=沿用模板话术
    recap_user: str = ""  # 最后一次编辑者用户名
    recap_updated_at: str = ""  # 最后一次编辑时间 ISO8601 (UTC, 毫秒)
    # v178: 录入时刻的快照
    composite_score: Optional[float] = None
    reference_text: str = ""
    reference_metrics_json: str = ""


class RecapIn(BaseModel):
    """v146: 保存单条偏离原因复盘 upsert 入参(按 (user,code,trade_date) 唯一)。
    trade_date 走路径参数,body 仅含 recap。空字符串=清除(降级为模板话术)。
    """
    recap: str = ""


class WatchLogOut(BaseModel):
    """v143:盯盘日志聚合。"""
    code: str
    name: Optional[str] = None
    items: list[WatchLogItem] = []  # 按 trade_date desc


class TConfigIn(BaseModel):
    custom_support: Optional[float] = None
    custom_pressure: Optional[float] = None
    risk_note: str = ""


class TConfigOut(BaseModel):
    code: str
    custom_support: Optional[float] = None
    custom_pressure: Optional[float] = None
    risk_note: str = ""


class PositionIn(BaseModel):
    position_qty: Optional[float] = None
    cost_price: Optional[float] = None


class RuleIn(BaseModel):
    name: str
    scope: str = "all"
    conditions: dict
    action: str = "alert"
    priority: int = 0
    scheme_type: str = "custom"
    enabled: bool = True
    risk_notice: str = ""  # v173：规则级风控提示


class RuleOut(BaseModel):
    id: int
    name: str
    scope: str
    conditions: dict
    action: str
    priority: int
    enabled: bool
    scheme_type: str
    risk_notice: str = ""  # v173：规则级风控提示


class SignalOut(BaseModel):
    id: int
    code: str
    name: Optional[str] = None
    signal_type: str
    action: str
    reason: str
    risk_level: str
    risk_advice: str
    confidence: int
    metrics: dict
    generated_at: str
    status: str
    # v173：信号反查触发它的规则，用于在「最新信号」表展示用户自定义的等级/名称/风控提示
    rule_name: Optional[str] = None        # 风险规则名称
    rule_detail: Optional[str] = None      # 命中的条件描述（reason 去掉 [规则名] 前缀后的部分）
    rule_risk_level: Optional[str] = None  # 风险等级：紧急 / 重要 / 关注
    rule_notice: Optional[str] = None      # 该规则自带的风控提示


class DashboardOut(BaseModel):
    pool_count: int
    pending_signals: int
    risk_alerts: int
    add_signals: int
    reduce_signals: int
    industry_dist: dict
    recent_signals: list
    market_env: dict


class PreferenceIn(BaseModel):
    answers: dict
    focus_indicators: list = []


class PreferenceOut(BaseModel):
    scores: dict
    archetype: str
    summary: str
    focus_indicators: list
    ai_advice: str
    matched_scheme: str


class NotifyConfigIn(BaseModel):
    channel: str = "console"
    enabled: bool = False
    config: dict = {}
    daily_review_cron: str = "30 15 * * 1-5"


class NotifyConfigOut(BaseModel):
    channel: str
    enabled: bool
    config: dict
    daily_review_cron: str


class HistoryPoint(BaseModel):
    """K 线历史序列的单点契约。

    前后端数据契约：history_series() 必须构造本模型再导出 dict，
    这样一旦漏字段（如 v97 漏了 pre_close 导致前端 K 线全绿）
    会在**构造时**立刻报错，而不是静默返回缺字段的裸 dict。
    均线在前 n 根不足时为 None，前端需自行跳过。
    """
    date: str
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: float
    turnover: float
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None


class IntradayProfile(BaseModel):
    """分时走势数据契约。

    由 _intraday_analysis() 构造，前端走势分析卡的分时视图直接消费。
    字段包括价格序列、VWAP、时间轴、均价、斜率、偏离度、量价背离、
    早盘方向、解读文案和标签。契约化后可防止新增前端字段时后端漏返
    （如 v100 加入的 times 如果以后被前端强依赖，缺失会直接报错）。
    """
    ok: bool = False
    source: str = "none"
    prices: list[float] = []
    vwap: list[float] = []
    times: list[str] = []
    avg_price: float = 0.0
    vwap_slope: float = 0.0
    deviation: float = 0.0
    divergence: str = "unknown"
    early_dir: str = "unknown"
    summary: str = ""
    tags: list[str] = []
