"""ORM 模型。所有用户数据本地存储，隐私不出本机。"""
from __future__ import annotations
import datetime as dt

from sqlalchemy import (
    Integer, String, Text, Float, Boolean, DateTime, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> str:
    # v143: 改毫秒精度,确保同日多次写入的 operated_at 可区分排序(v143 day_view_log 同秒会乱序)
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


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
    # v189 主力资金「今日起落库」：系统只有当日实时资金流接口，没有历史源。
    #   每次取到当日主力净流入就写进当日日线行，盘前预判即可取 T-1 的静态值（全天不变）。
    #   今天之前的历史行为 NULL → 前端显示 "-"，用上几天后自动补全。
    main_net_pct: Mapped[float | None] = mapped_column(Float, nullable=True)   # 主力净流入占成交额 %
    main_signal: Mapped[str] = mapped_column(String(64), default="")           # 主力信号文本，如「主力流入」


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
    day_view: Mapped[str] = mapped_column(String(16), default="")  # 日初判断：看涨/看跌/不动/风险/空
    # v189 监控链路：盘前预判的股票点「加入监控」后才进入盘间监控/复盘管理。
    #   monitored: 1=已加入监控(盘间监控+复盘管理可见)；0=只在盘前预判可见。
    #   source:    新增来源 pretrade/pool/review —— 决定该行在哪些页面可见。
    #     盘前预判可见 = source!='pool'（盘间监控自行新增的只在盘间监控显示）
    #     盘间监控可见 = (source=='pretrade' and monitored==1) or source=='pool'
    #     复盘管理可见 = (source=='pretrade' and monitored==1) or source=='review'
    #   存量数据迁移时两列分别填 1 / 'pretrade'，保证老数据三页都照常可见、不丢。
    monitored: Mapped[int | None] = mapped_column(Integer, default=1, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="pretrade")
    tags: Mapped[list["PoolTag"]] = relationship("PoolTag", secondary="tracked_pool_tags", back_populates="pools")


class PoolTag(Base):
    """短线可投池自定义标签（名称 + 填充颜色）。"""
    __tablename__ = "pool_tags"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    color: Mapped[str] = mapped_column(String(16), default="#3b82f6")  # 默认蓝色
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    pools: Mapped[list["TrackedPool"]] = relationship("TrackedPool", secondary="tracked_pool_tags", back_populates="tags")


class TrackedPoolTag(Base):
    """可投池与标签的多对多关联表。增加 user_id 便于按账号级联清理。
    新建/修改标签关联时由后端统一写入 pool 所属 user_id。"""
    __tablename__ = "tracked_pool_tags"
    pool_id: Mapped[int] = mapped_column(Integer, ForeignKey("tracked_pool.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("pool_tags.id"), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)


class DayViewLog(Base):
    """日初判断修改记录（v143）：每次修改都追加一条；同一天允许多条；盯盘日志取当日最后一条。
    v178: 新增 composite_score(录入时的综合评分快照)、reference_text(录入时组装好的预判参考信息)、
    reference_metrics_json(结构化指标,JSON),用于盯盘日志"综合评分/预判参考信息"两列展示。
    """
    __tablename__ = "day_view_log"
    __table_args__ = (
        # 复合索引:按 (user, code, trade_date, operated_at) 高频查询
        # 单字段索引 user_id/code 也建,便于按账号清理与按股票查全量历史
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    trade_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD,由前端传入(避免后端时区)
    trend: Mapped[str] = mapped_column(String(4), default="-")  # 看涨/看跌/风险/-
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # 目标价位(v185 起由「目标买入/目标卖出」取代,保留兼容旧数据)
    # v185: 目标价位拆分为买入/卖出两端(用户原意:日初预判一个想买入的价位、一个想卖出的价位)
    target_buy: Mapped[float | None] = mapped_column(Float, nullable=True)    # 目标买入价
    target_sell: Mapped[float | None] = mapped_column(Float, nullable=True)   # 目标卖出价
    target_note: Mapped[str] = mapped_column(String(40), default="")  # 目标依据,前端校验 ≤20字,DB 给冗余
    operator: Mapped[str] = mapped_column(String(64), default="")  # 操作用户名(冗余便于历史回看)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operated_at: Mapped[str] = mapped_column(String(32), default=_now)  # ISO8601 UTC,带时区
    # v178: 录入时的快照（用于盯盘日志表格展示"综合评分/预判参考信息"两列）
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 综合评分(0-100),None=未录入/旧数据
    reference_text: Mapped[str] = mapped_column(Text, default="")  # 预判参考信息(默认组装文本,如"量比:1.0、换手:3.01%…")
    reference_metrics_json: Mapped[str] = mapped_column(Text, default="")  # 同一时刻的结构化指标(JSON),便于后续做准确率聚合


class DayViewRecap(Base):
    """v146: 偏离原因复盘(盯盘日志复盘文本,可编辑,按 (user,code,trade_date) 一行存最新)。
    设计:每次保存都覆盖 update_at,仅保留最新一份;后续按 (user,trade_date) 聚合即可做月度复盘准确率统计。
    """
    __tablename__ = "day_view_recap"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    trade_date: Mapped[str] = mapped_column(String(10), index=True)
    recap: Mapped[str] = mapped_column(Text, default="")  # 用户编辑的复盘文本(可空,空=沿用模板话术)
    operator: Mapped[str] = mapped_column(String(64), default="")
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)  # 最后一次复盘更新时间


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
    risk_notice: Mapped[str] = mapped_column(Text, default="")  # v173：规则级风控提示（用户自定义，展示在信号表）
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


class SystemSetting(Base):
    """系统级（全局）键值配置（不按账号隔离）。
    仅 admin 可写；任意已登录或未登录用户可读（如登录页判断是否显示游客入口）。"""
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class StockTConfig(Base):
    """个股做T分析页的本地配置：自定义支撑/压力、特殊风控备注。
    存于本机数据库（仍不出本机），按 user_id 隔离，跨设备/浏览器都能读。"""
    __tablename__ = "stock_tconfig"
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_stock_tconfig_user_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    custom_support: Mapped[float | None] = mapped_column(Float, nullable=True)  # 自定义支撑位
    custom_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)  # 自定义压力位
    risk_note: Mapped[str] = mapped_column(Text, default="")  # 标的特殊风控备注（如：年底清仓禁加仓）
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)



class AccessLog(Base):
    """网站访问日志：记录进入网站/登录事件，含游客（is_guest=True）。
    增加 user_id 字段，便于按账号追溯与清理游客数据。"""
    __tablename__ = "access_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(32), default=_now, nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ua: Mapped[str | None] = mapped_column(String(512), nullable=True)
    path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # page/login/guest/register
