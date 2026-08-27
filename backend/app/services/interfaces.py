"""模块接口契约（Protocol）。应用层只依赖这些接口，不直接依赖实现。
同一接口可有多个实现（如 Notifier: serverchan/pushplus/console），运行时按配置切换。
新增方案/通道/引擎 = 实现接口 + 注册，无需改动编排层代码。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Quote:
    code: str
    date: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    turnover: float = 0.0
    pre_close: float = 0.0


@dataclass
class IndicatorSnapshot:
    """指标快照，供信号引擎与 UI 引用。"""
    code: str
    date: str
    close: float = 0.0
    change_pct: float = 0.0
    ma: dict[str, float] = field(default_factory=dict)          # {"5":..,"10":..,"20":..,"60":..}
    vol_ratio: float = 0.0
    turnover: float = 0.0
    macd: dict[str, float] = field(default_factory=dict)        # {dif,dea,hist}
    boll: dict[str, float] = field(default_factory=dict)        # {upper,mid,lower}
    kdj: dict[str, float] = field(default_factory=dict)         # {k,d,j}
    rsi: dict[str, float] = field(default_factory=dict)         # {rsi6,rsi12,rsi24}
    amplitude: float = 0.0
    box: dict[str, float] = field(default_factory=dict)         # {support,pressure,slope}
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    code: str
    name: str = ""
    industry: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalResult:
    code: str
    signal_type: str       # add / reduce / alert / hold
    action: str
    reason: str
    risk_level: str        # 低 / 中 / 高
    risk_advice: str       # 风控前置（软引导）
    confidence: int = 0     # 共振置信度 0-100
    metrics: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Screener(Protocol):
    """选股模块接口。实现：RuleScreener；预留：MLScreener。"""
    def run(self, config: dict[str, Any], db: Any) -> list[Candidate]: ...


@runtime_checkable
class SignalEngine(Protocol):
    """信号模块接口。实现：RuleSignalEngine。"""
    def evaluate(self, pool: list[str], rules: list[dict[str, Any]],
                 db: Any) -> list[SignalResult]: ...


@runtime_checkable
class RiskAdvisor(Protocol):
    """风控模块接口（软引导）。实现：SoftRiskAdvisor。"""
    def assess(self, signal: SignalResult, risk_config: dict[str, Any]) -> SignalResult: ...


@runtime_checkable
class Notifier(Protocol):
    """通知模块接口。实现：ServerChanNotifier / PushPlusNotifier / ConsoleNotifier。"""
    def send(self, title: str, content: str, config: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class AIProvider(Protocol):
    """AI 模块接口。实现：DeepSeekProvider；预留：通义/智谱/本地 Ollama。"""
    def analyze(self, prompt: str, context: dict[str, Any]) -> str: ...
