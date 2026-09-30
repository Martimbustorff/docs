"""Shared data types. Every module imports these; none of them import each other's internals.

All timestamps are timezone-aware UTC `datetime`s. Symbols use the canonical Alpaca form:
"SPY", "QQQ", "BTC/USD".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


def asset_class(symbol: str) -> AssetClass:
    """Crypto pairs contain a slash ("BTC/USD"); everything else is a US stock or ETF."""
    return AssetClass.CRYPTO if "/" in symbol else AssetClass.STOCK


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalKind(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"


class OrderPurpose(str, Enum):
    """Why an order exists. Only ENTRY increases risk; every other purpose reduces it."""

    ENTRY = "entry"
    EXIT = "exit"  # strategy exit signal
    STOP = "stop"  # stop loss hit
    TAKE_PROFIT = "take_profit"
    KILL = "kill"  # kill-switch flatten

    @property
    def increases_risk(self) -> bool:
        return self is OrderPurpose.ENTRY


@dataclass(frozen=True)
class Signal:
    """A strategy's decision, computed on a completed bar's close. Long-only system."""

    ts: datetime  # close time (UTC) of the bar the signal was computed on
    symbol: str
    strategy: str  # registry name, e.g. "trend"
    kind: SignalKind
    reason: str  # human-readable, e.g. "close crossed above 200d SMA"
    price: float  # the bar's close, used as the reference price
    stop_price: float | None = None  # ENTRY only: initial stop loss
    take_profit: float | None = None  # ENTRY only: optional take-profit level
    features: dict[str, float] = field(default_factory=dict)  # indicator snapshot for logs and Jev state


@dataclass
class PositionState:
    """The bot's view of one open long position (stored in SQLite and in the backtester)."""

    symbol: str
    strategy: str
    qty: float
    entry_price: float
    entry_ts: datetime
    stop_price: float
    take_profit: float | None = None
    bars_held: int = 0
    highest_close: float = 0.0  # for trailing stops; starts at the entry fill price


@dataclass(frozen=True)
class JevAnswer:
    """One answered Jev question, normalised across noul/choice/score."""

    name: str  # question key from strategy.md
    type: str  # "noul" | "choice" | "score"
    probabilities: dict[str, float]  # noul -> {"yes": p, "no": 1-p}; choice -> label->p; score -> "0".."n"->p
    top: str  # noul -> "yes"/"no"; choice -> label; score -> str(round(score))
    gate_value: float  # the probability the gate compares against its threshold
    passed: bool
    rule: str  # e.g. "P(yes) >= 0.55" or "P(bearish) <= 0.40"


@dataclass(frozen=True)
class JevDecision:
    """Result of asking Jev about one ENTRY signal. `passed` is False on any error (fail-closed)."""

    passed: bool
    answers: tuple[JevAnswer, ...]
    model: str | None
    latency_ms: float
    input_tokens: int
    cost_usd: float
    error: str | None = None  # set when the call failed; passed is then False
    shadow: bool = False  # True when Jev was consulted but its verdict did not gate the trade

    @property
    def reason(self) -> str:
        if self.error:
            return f"jev error: {self.error}"
        failed = [a.rule for a in self.answers if not a.passed]
        return "all Jev thresholds cleared" if not failed else "failed: " + "; ".join(failed)


@dataclass(frozen=True)
class OrderIntent:
    """What the bot wants the broker to do. Built by the runner, checked by RiskManager,
    submitted only through OrderGateway."""

    symbol: str
    side: Side
    qty: float
    ref_price: float  # price used to estimate notional (last close or last trade)
    purpose: OrderPurpose
    reason: str
    client_order_id: str  # deterministic; makes retries and restarts idempotent
    signal_id: int | None = None

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.ref_price


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    broker_order_id: str | None
    status: str  # "new" | "accepted" | "filled" | "partially_filled" | "rejected" | "canceled" | "refused"
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    message: str = ""


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float
    is_paper: bool
    trading_blocked: bool = False
    status: str = "ACTIVE"


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


class RiskAction(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    BLOCK = "block"


@dataclass(frozen=True)
class RiskVerdict:
    action: RiskAction
    reason: str
    adjusted_qty: float | None = None  # set when the risk manager shrinks an order to fit a cap


@dataclass(frozen=True)
class Trade:
    """A closed round trip, used by both the backtester and the live store."""

    symbol: str
    strategy: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    qty: float
    pnl: float  # after fees and slippage
    pnl_pct: float  # pnl / (entry_price * qty)
    exit_reason: str  # "signal" | "stop" | "take_profit" | "kill" | "end_of_data"
    fees: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
