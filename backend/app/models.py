"""ORM 模型。所有用户数据本地存储，隐私不出本机。"""
from __future__ import annotations
import datetime as dt

from sqlalchemy import (
    Integer, String, Text, Float, Boolean, DateTime, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    salt: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin / user
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    last_login_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(48), nullable=True)  # 最近一次登录 IP
    must_change_pw: Mapped[bool] = mapped_column(Boolean, default=False)  # 强制首登改密


class Stock(Base):
    """A 股基础信息（首次初始化填充，演示数据亦可）。"""
    __tablename__ = "stocks"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    industry: Mapped[str] = mapped_column(String(64), default="")
    market: Mapped[str] = mapped_column(String(8), default="")
    list_date: Mapped[str] = mapped_column(String(16), default="")


class DailyQuote(Base):
    """日线行情缓存。技术指标运行时计算，不落表。"""
    __tablename__ = "daily_quotes"
    __table_args__ = (UniqueConstraint("code", "date", name="uq_quote"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    date: Mapped[str] = mapped_column(String(16), index=True)
    open: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    close: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    turnover: Mapped[float] = mapped_column(Float, default=0.0)  # 换手率 %
    pre_close: Mapped[float] = mapped_column(Float, default=0.0)


class Screen(Base):
    """选股模型定义（Screener 接口的参数化实例）。"""
    __tablename__ = "screens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")  # 选股参数
    scheme_type: Mapped[str] = mapped_column(String(32), default="custom")  # 关联方案类型
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class ScreenResult(Base):
    __tablename__ = "screen_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey("screens.id"), index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    matched_at: Mapped[str] = mapped_column(String(32), default=_now)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")


class TrackedPool(Base):
    """短线可投池（用户自持 + 选股推送）。"""
    __tablename__ = "tracked_pool"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    added_at: Mapped[str] = mapped_column(String(32), default=_now)
    note: Mapped[str] = mapped_column(Text, default="")
    cost_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_qty: Mapped[float | None] = mapped_column(Float, nullable=True)  # 持仓股数（做T页算总浮盈浮亏用）
    position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 仓位占比 %
    scheme_type: Mapped[str] = mapped_column(String(32), default="custom")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archive


class PositionRule(Base):
    """加减仓规则（SignalEngine 接口的参数化实例）。"""
    __tablename__ = "position_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(32), default="all")  # all 或 JSON 数组
    conditions_json: Mapped[str] = mapped_column(Text, default="{}")
    action: Mapped[str] = mapped_column(String(16), default="alert")  # add/reduce/alert/hold
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scheme_type: Mapped[str] = mapped_column(String(32), default="custom")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Signal(Base):
    """规则引擎产出的信号/建议（软引导）。"""
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_type: Mapped[str] = mapped_column(String(16))  # add/reduce/alert
    action: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    risk_level: Mapped[str] = mapped_column(String(16), default="中")  # 低/中/高
    risk_advice: Mapped[str] = mapped_column(Text, default="")  # 风控前置建议
    confidence: Mapped[int] = mapped_column(Integer, default=0)  # 共振置信度 0-100
    generated_at: Mapped[str] = mapped_column(String(32), default=_now)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/done/ignored


class SchemeType(Base):
    """分析模型方案类型：选股+信号+风控三件套的 JSON 参数（可套用/自建）。"""
    __tablename__ = "scheme_types"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(String(16), default="中")
    screener_json: Mapped[str] = mapped_column(Text, default="{}")
    signal_json: Mapped[str] = mapped_column(Text, default="{}")
    risk_json: Mapped[str] = mapped_column(Text, default="{}")  # 软引导风控参数
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class UserProfile(Base):
    """投资偏好画像（问卷评分结果）。"""
    __tablename__ = "user_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True, unique=True)
    answers_json: Mapped[str] = mapped_column(Text, default="{}")  # 问卷答案
    scores_json: Mapped[str] = mapped_column(Text, default="{}")  # 维度评分
    archetype: Mapped[str] = mapped_column(String(32), default="")  # 画像标签
    focus_indicators_json: Mapped[str] = mapped_column(Text, default="[]")  # 关注指标
    ai_advice: Mapped[str] = mapped_column(Text, default="")  # LLM 建议
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class NotifyConfig(Base):
    __tablename__ = "notify_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True, unique=True)
    channel: Mapped[str] = mapped_column(String(32), default="console")  # serverchan/pushplus/email/wecom/console
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}")  # token/url 本地加密存储
    daily_review_cron: Mapped[str] = mapped_column(String(32), default="30 15 * * 1-5")


class NotifyLog(Base):
    __tablename__ = "notify_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[str] = mapped_column(String(32), default=_now)
    status: Mapped[str] = mapped_column(String(16), default="success")


class AppSetting(Base):
    """通用键值配置（系统级默认，不随用户隔离）。"""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")


class UserSetting(Base):
    """用户级键值配置（AI Key 等按账号隔离）。"""
    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_setting"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[str] = mapped_column(Text, default="{}")


class StockTConfig(Base):
    """个股做T分析页的本地配置：自定义支撑/压力、特殊风控备注。
    存于本机数据库（仍不出本机），跨设备/浏览器都能读，满足长期多端使用。"""
    __tablename__ = "stock_tconfig"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    custom_support: Mapped[float | None] = mapped_column(Float, nullable=True)  # 自定义支撑位
    custom_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)  # 自定义压力位
    risk_note: Mapped[str] = mapped_column(Text, default="")  # 标的特殊风控备注（如：年底清仓禁加仓）
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)
