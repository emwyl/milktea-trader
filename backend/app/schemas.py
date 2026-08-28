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
    tag_id: Optional[int] = None  # 可选标签


class PoolBatchDeleteIn(BaseModel):
    codes: list[str]


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
    tag_id: Optional[int] = None
    tag: Optional[dict] = None  # {id,name,color}


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
