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


class RuleOut(BaseModel):
    id: int
    name: str
    scope: str
    conditions: dict
    action: str
    priority: int
    enabled: bool
    scheme_type: str


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
