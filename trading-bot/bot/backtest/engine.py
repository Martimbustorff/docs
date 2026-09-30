"""Single-symbol daily backtester that follows the timing model in ARCHITECTURE.md.

Per bar i in the traded window, in this order:
1. At the open: fill orders queued on bar i-1's close (an exit first, then an entry) at the open
   with slippage against you and `fee_bps` on the fill's notional.
2. Intrabar, from the fill bar onward: a bar that opens through the stop (or above the
   take-profit) exits at the open; otherwise a bar whose low reaches the stop exits at the stop,
   and one whose high reaches the take-profit exits there. A bar that touches both exits at the
   stop.
3. At the close, while long: `bars_held` += 1 and `highest_close` is updated, then the strategy's
   trailing stop is applied (upward only, so it first protects bar i+1), then its exit signal is
   queued for bar i+1's open.
4. At the close, while flat and only if no exit filled on this bar: the entry signal is sized
   from the mark-to-market equity at this close and queued for bar i+1's open.
Signals on the window's last bar are recorded but place no order, and a position still open at
the last close is closed there as `end_of_data` (with slippage and fees).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Literal

import numpy as np
import pandas as pd

from bot.config import StrategyConfig
from bot.models import AssetClass, PositionState, Signal, SignalKind, Trade, asset_class
from bot.strategies.base import Strategy
from bot.timeutil import NY, UTC, bar_close_ts

log = logging.getLogger(__name__)

OHLC = ["open", "high", "low", "close"]
STOCK_OPEN = time(9, 30)


def _empty_series(name: str) -> pd.Series:
    return pd.Series(dtype=float, index=pd.DatetimeIndex([], name="date"), name=name)


@dataclass(frozen=True)
class BacktestConfig:
    capital_usd: float
    risk_per_trade_pct: float  # percent, e.g. 1.0 = 1%
    max_position_pct: float  # percent of equity
    max_position_usd: float
    slippage_bps: float
    fee_bps: float
    approval_threshold_usd: float

    @classmethod
    def from_strategy(cls, cfg: StrategyConfig, symbol: str) -> BacktestConfig:
        klass: Literal["stock", "crypto"] = "crypto" if asset_class(symbol) is AssetClass.CRYPTO else "stock"
        costs = {"slippage_bps": cfg.execution.slippage_bps, "fee_bps": cfg.execution.fee_bps}
        for key, table in costs.items():
            if klass not in table:
                raise ValueError(f"execution.{key} has no {klass!r} entry; refusing to assume zero cost")
        return cls(
            capital_usd=cfg.risk.capital_usd,
            risk_per_trade_pct=cfg.risk.risk_per_trade_pct,
            max_position_pct=cfg.risk.max_position_pct,
            max_position_usd=cfg.risk.max_position_usd,
            slippage_bps=cfg.execution.slippage_bps[klass],
            fee_bps=cfg.execution.fee_bps[klass],
            approval_threshold_usd=cfg.risk.approval_threshold_usd,
        )


@dataclass
class BacktestResult:
    symbol: str
    strategy_label: str
    params: dict
    trades: list[Trade]
    equity: pd.Series  # daily mark-to-market equity at each close of the traded window
    signals: list[Signal]  # every ENTRY/EXIT signal generated (pre-risk)
    orders_needing_approval: int
    entry_orders: int
    # Additions to the contract, used by metrics (buy & hold, exposure). Indexed like `equity`.
    closes: pd.Series = field(default_factory=lambda: _empty_series("close"))
    exposure: pd.Series = field(default_factory=lambda: _empty_series("exposure_usd"))  # USD held at close


def position_size(equity: float, entry_est: float, stop: float | None, cfg: BacktestConfig) -> float:
    """The contract's sizing rule; 0.0 when the entry must be skipped."""
    if stop is None or not (math.isfinite(stop) and math.isfinite(entry_est)):
        return 0.0
    if entry_est <= 0 or entry_est <= stop:
        return 0.0
    qty = min(
        equity * cfg.risk_per_trade_pct / 100.0 / (entry_est - stop),
        equity * cfg.max_position_pct / 100.0 / entry_est,
        cfg.max_position_usd / entry_est,
    )
    return qty if math.isfinite(qty) and qty > 0 else 0.0


def bar_open_ts(symbol: str, day: date) -> datetime:
    """Open time of the daily bar dated `day`: 09:30 New York for stocks, 00:00 UTC for crypto."""
    if asset_class(symbol) is AssetClass.CRYPTO:
        return datetime.combine(day, time(0, 0), tzinfo=UTC)
    return datetime.combine(day, STOCK_OPEN, tzinfo=NY).astimezone(UTC)


def _validate_bars(bars: pd.DataFrame) -> None:
    missing = [c for c in OHLC if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars must have a DatetimeIndex")
    if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
        raise ValueError("bars must be sorted by date with unique dates")
    if bars[OHLC].isna().to_numpy().any():
        raise ValueError("bars contain NaN prices; load them with bot.data.load_daily")


def _window(index: pd.DatetimeIndex, start: str | None, end: str | None) -> tuple[int, int]:
    """Inclusive positional bounds of [start, end]; first > last when the window is empty."""
    first = 0 if start is None else int(index.searchsorted(pd.Timestamp(start), side="left"))
    last = len(index) - 1 if end is None else int(index.searchsorted(pd.Timestamp(end), side="right")) - 1
    return first, last


@dataclass
class _OpenTrade:
    """Engine-side bookkeeping for the open position that PositionState doesn't carry."""

    entry_fee: float
    signal_bar: date
    entry_bar: date
    initial_stop: float


class _Simulation:
    def __init__(self, bars: pd.DataFrame, df: pd.DataFrame, strategy: Strategy, cfg: BacktestConfig) -> None:
        self.df = df
        self.strategy = strategy
        self.cfg = cfg
        self.symbol = strategy.symbol
        self.days = [ts.date() for ts in bars.index]
        self.open, self.high, self.low, self.close = (bars[c].to_numpy(dtype=float) for c in OHLC)
        self.slip = cfg.slippage_bps / 10_000.0
        self.fee_rate = cfg.fee_bps / 10_000.0
        self.cash = float(cfg.capital_usd)
        self.position: PositionState | None = None
        self.book: _OpenTrade | None = None
        self.pending_entry: tuple[Signal, float, int] | None = None  # signal, qty, signal bar
        self.pending_exit: Signal | None = None
        self.trades: list[Trade] = []
        self.signals: list[Signal] = []
        self.entry_orders = 0
        self.orders_needing_approval = 0

    # -- per-bar steps ------------------------------------------------------------------------

    def step(self, i: int, last: int) -> tuple[float, float]:
        """Process bar i; return (equity, position value) at its close."""
        exited = self._fill_queued(i)
        if self.position is not None:
            exited = self._check_levels(i) or exited
        if self.position is not None:
            self._on_close_long(i, can_order=i < last)
        elif not exited:
            self._on_close_flat(i, can_order=i < last)
        value = self.position.qty * self.close[i] if self.position is not None else 0.0
        if i == last and self.position is not None:
            self._exit(i, self.close[i], "end_of_data", "close")
        return self._equity(i), value

    def _fill_queued(self, i: int) -> bool:
        exited = False
        if self.pending_exit is not None:
            self.pending_exit = None
            self._exit(i, self.open[i], "signal", "open")
            exited = True
        if self.pending_entry is not None:
            signal, qty, signal_bar = self.pending_entry
            self.pending_entry = None
            self._enter(i, signal, qty, signal_bar)
        return exited

    def _check_levels(self, i: int) -> bool:
        pos = self.position
        assert pos is not None
        stop, tp = pos.stop_price, pos.take_profit
        # The open is the bar's first price, so a gap beyond either level fills there. Only when
        # both levels are touched intrabar is the order unknown, and then the stop is assumed.
        if self.open[i] <= stop:
            self._exit(i, self.open[i], "stop", "open")
        elif tp is not None and self.open[i] >= tp:
            self._exit(i, self.open[i], "take_profit", "open")
        elif self.low[i] <= stop:
            self._exit(i, stop, "stop", "intrabar")
        elif tp is not None and self.high[i] >= tp:
            self._exit(i, tp, "take_profit", "intrabar")
        else:
            return False
        return True

    def _on_close_long(self, i: int, can_order: bool) -> None:
        pos = self.position
        assert pos is not None
        pos.bars_held += 1
        pos.highest_close = max(pos.highest_close, float(self.close[i]))
        new_stop = self.strategy.trailing_stop(self.df, i, replace(pos))
        if new_stop is not None and math.isfinite(new_stop) and new_stop > pos.stop_price:
            pos.stop_price = float(new_stop)
        signal = self.strategy.exit_signal(self.df, i, replace(pos))
        if signal is None:
            return
        self._expect(signal, SignalKind.EXIT, "exit_signal")
        self.signals.append(signal)
        if can_order:
            self.pending_exit = signal

    def _on_close_flat(self, i: int, can_order: bool) -> None:
        if i + 1 < self.strategy.warmup:
            return
        signal = self.strategy.entry_signal(self.df, i)
        if signal is None:
            return
        self._expect(signal, SignalKind.ENTRY, "entry_signal")
        self.signals.append(signal)
        if not can_order:
            return
        entry_est = float(self.close[i])
        qty = position_size(self._equity(i), entry_est, signal.stop_price, self.cfg)
        if qty <= 0:
            log.debug("%s %s: entry skipped (stop %s)", self.symbol, self.days[i], signal.stop_price)
            return
        self.entry_orders += 1
        if qty * entry_est > self.cfg.approval_threshold_usd:
            self.orders_needing_approval += 1
        self.pending_entry = (signal, qty, i)

    # -- fills --------------------------------------------------------------------------------

    def _enter(self, i: int, signal: Signal, qty: float, signal_bar: int) -> None:
        assert signal.stop_price is not None  # position_size() returned > 0
        price = self.open[i] * (1.0 + self.slip)
        fee = qty * price * self.fee_rate
        self.cash -= qty * price + fee
        self.position = PositionState(
            symbol=self.symbol,
            strategy=self.strategy.name,
            qty=qty,
            entry_price=price,
            entry_ts=bar_open_ts(self.symbol, self.days[i]),
            stop_price=float(signal.stop_price),
            take_profit=signal.take_profit,
            bars_held=0,
            highest_close=price,
        )
        self.book = _OpenTrade(fee, self.days[signal_bar], self.days[i], float(signal.stop_price))

    def _exit(self, i: int, level: float, reason: str, fill: str) -> None:
        pos, book = self.position, self.book
        assert pos is not None and book is not None
        price = level * (1.0 - self.slip)
        fee = pos.qty * price * self.fee_rate
        self.cash += pos.qty * price - fee
        pnl = pos.qty * (price - pos.entry_price) - book.entry_fee - fee
        day = self.days[i]
        exit_ts = bar_open_ts(self.symbol, day) if fill == "open" else bar_close_ts(self.symbol, day)
        self.trades.append(
            Trade(
                symbol=self.symbol,
                strategy=pos.strategy,
                entry_ts=pos.entry_ts,
                entry_price=pos.entry_price,
                exit_ts=exit_ts,
                exit_price=price,
                qty=pos.qty,
                pnl=pnl,
                pnl_pct=pnl / (pos.entry_price * pos.qty),
                exit_reason=reason,
                fees=book.entry_fee + fee,
                meta={
                    "signal_bar": book.signal_bar.isoformat(),
                    "entry_bar": book.entry_bar.isoformat(),
                    "exit_bar": self.days[i].isoformat(),
                    "exit_fill": fill,  # "open" | "intrabar" | "close"
                    "initial_stop": book.initial_stop,
                    "final_stop": pos.stop_price,
                    "bars_held": pos.bars_held,
                },
            )
        )
        self.position, self.book = None, None

    # -- helpers ------------------------------------------------------------------------------

    def _equity(self, i: int) -> float:
        held = self.position.qty * self.close[i] if self.position is not None else 0.0
        return self.cash + held

    def _expect(self, signal: Signal, kind: SignalKind, method: str) -> None:
        if signal.kind is not kind:
            raise ValueError(f"{self.strategy.name}.{method} returned a {signal.kind.value} signal")


def run_backtest(
    bars: pd.DataFrame,
    strategy: Strategy,
    cfg: BacktestConfig,
    start: str | None = None,
    end: str | None = None,
) -> BacktestResult:
    """Backtest `strategy` on `bars`, trading only bars dated within [start, end]."""
    _validate_bars(bars)
    first, last = _window(bars.index, start, end)
    result = BacktestResult(
        symbol=strategy.symbol,
        strategy_label=strategy.label(),
        params=dict(strategy.params),
        trades=[],
        equity=_empty_series("equity"),
        signals=[],
        orders_needing_approval=0,
        entry_orders=0,
    )
    if first > last:
        log.warning("%s: no bars in [%s, %s]; nothing to backtest", strategy.symbol, start, end)
        return result

    df = strategy.prepare(bars)  # full history, once: warm at `start` if enough bars precede it
    if len(df) != len(bars) or not df.index.equals(bars.index):
        raise ValueError(f"{strategy.name}.prepare must keep the bars' index unchanged")

    sim = _Simulation(bars, df, strategy, cfg)
    n = last - first + 1
    equity, exposure = np.empty(n), np.empty(n)
    for k, i in enumerate(range(first, last + 1)):
        equity[k], exposure[k] = sim.step(i, last)

    index = bars.index[first : last + 1]
    result.trades = sim.trades
    result.signals = sim.signals
    result.entry_orders = sim.entry_orders
    result.orders_needing_approval = sim.orders_needing_approval
    result.equity = pd.Series(equity, index=index, name="equity")
    result.exposure = pd.Series(exposure, index=index, name="exposure_usd")
    result.closes = bars["close"].iloc[first : last + 1].astype(float).rename("close")
    return result
