"""Shared-capital backtest of several symbols traded together (the tournament's winners).

Each symbol is replayed by the single-symbol engine's own simulation, so fills, slippage, fees,
intrabar stops, trailing stops, exit signals and end-of-data closes are exactly the engine's.
What changes is that every symbol draws on one cash balance and every entry sees the whole book:

- Dates are the union of the symbols' bars (stocks on NYSE days, crypto every day). On each date
  the opens are processed in time order (crypto at 00:00 UTC, then stocks at 09:30 New York),
  then the closes (stocks at 16:00 New York, then crypto at 00:00 UTC the next day). Bars that
  close at the same moment are all marked before any of them decides, and their entries are
  decided in the order `strategies_by_symbol` lists them. A symbol with no bar on a date stays
  marked at its latest close.
- An entry is sized like the engine and the live bot: `engine.position_size` on the portfolio's
  mark-to-market equity at the signal close. It is then shrunk to fit `max_total_exposure_pct`
  (open positions at their latest close plus entries queued but not filled yet), and dropped
  when less than `risk.MIN_ORDER_USD` is left.
- Daily loss limit: once equity at a close is down `daily_loss_limit_pct` or more from the
  equity at the end of the previous date, entries signalled for the rest of that date are
  blocked. Every close of the bar dated D happens on New York day D (crypto's 00:00 UTC close is
  the New York evening), so a bar date is the New York day of its signals.
- Kill switch: each close where the drawdown from peak equity reaches `max_drawdown_kill_pct` is
  recorded, and the peak is re-based to the current equity, as `RiskManager.check_drawdown`
  does. The simulation keeps trading afterwards; the records say when the live bot would have
  stopped and waited for a manual resume.

Equity is only observed at closes, so an intraday dip that recovers by the close trips neither
limit here even though the live bot, which polls last prices, might see it. Approvals are
assumed granted; `max_orders_per_day` cannot bind with one entry per symbol per bar.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from bot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    _empty_series,
    _Simulation,
    _validate_bars,
    _window,
    bar_open_ts,
    position_size,
)
from bot.backtest.regimes import regime_breakdown
from bot.config import RiskConfig, StrategyConfig
from bot.models import Signal, SignalKind, Trade
from bot.risk import MIN_ORDER_USD
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts, periods_per_year

log = logging.getLogger(__name__)


@dataclass
class PortfolioResult:
    symbols: list[str]
    labels: dict[str, str]  # symbol -> strategy label
    trades: list[Trade]  # every symbol's closed trades, in exit order
    equity: pd.Series  # mark-to-market equity at the end of each date
    exposure: pd.Series  # USD held at each date's closes
    signals: list[Signal]
    entry_orders: int
    orders_needing_approval: int
    kill_switch_would_fire: list[dict[str, str]] = field(default_factory=list)  # {"date","reason"}
    daily_loss_limit_days: list[str] = field(default_factory=list)  # ISO dates
    blocked_entries: list[dict[str, str]] = field(default_factory=list)  # {"date","symbol","reason"}
    shrunk_entries: int = 0  # entries cut down by the total exposure cap

    @property
    def daily_loss_limit_hits(self) -> int:
        return len(self.daily_loss_limit_days)

    @property
    def periods_per_year(self) -> int:
        return max((periods_per_year(s) for s in self.symbols), default=252)

    def as_backtest_result(self) -> BacktestResult:
        """The portfolio in the engine's result shape, for `compute_metrics` and
        `regime_breakdown`. There is no single asset to buy and hold, so `closes` stays empty."""
        return BacktestResult(
            symbol="PORTFOLIO",
            strategy_label=" + ".join(self.labels[s] for s in self.symbols),
            params={s: self.labels[s] for s in self.symbols},
            trades=list(self.trades),
            equity=self.equity,
            signals=list(self.signals),
            orders_needing_approval=self.orders_needing_approval,
            entry_orders=self.entry_orders,
            exposure=self.exposure,
        )


@dataclass
class _Book:
    """One symbol: the engine's simulation plus its place in the shared calendar."""

    symbol: str
    sim: _Simulation
    cfg: BacktestConfig
    rows: dict[pd.Timestamp, int]  # bar date -> positional index, traded window only
    last: int
    latest_close: float = math.nan
    close_value: float = 0.0  # position value at today's close, before an end-of-data exit

    @property
    def market_value(self) -> float:
        pos = self.sim.position
        if pos is None:
            return 0.0
        price = self.latest_close if math.isfinite(self.latest_close) else pos.entry_price
        return pos.qty * price

    @property
    def queued_notional(self) -> float:
        if self.sim.pending_entry is None:
            return 0.0
        signal, qty, _ = self.sim.pending_entry
        return qty * signal.price


class _Portfolio:
    def __init__(self, books: list[_Book], risk: RiskConfig) -> None:
        self.books = books
        self.risk = risk
        self.capital = float(risk.capital_usd)
        # Each simulation's cash is its own net cash flow; the account holds their sum.
        for book in books:
            book.sim.cash = 0.0
        self.peak = self.capital
        self.day_start = self.capital
        self.blocked_day: date | None = None
        self.kill_events: list[dict[str, str]] = []
        self.loss_days: list[str] = []
        self.blocked: list[dict[str, str]] = []
        self.shrunk = 0

    def equity(self) -> float:
        return self.capital + sum(b.sim.cash + b.market_value for b in self.books)

    def open_exposure(self) -> float:
        return sum(b.market_value + b.queued_notional for b in self.books)

    def step(self, ts: pd.Timestamp) -> tuple[float, float]:
        """Process every bar dated `ts`; return (equity, exposure) after the day's closes."""
        day = ts.date()
        today = [b for b in self.books if ts in b.rows]
        exited: dict[str, bool] = {}
        for book in sorted(today, key=lambda b: bar_open_ts(b.symbol, day)):
            i = book.rows[ts]
            exited[book.symbol] = book.sim._fill_queued(i)
            if book.sim.position is not None:
                exited[book.symbol] = book.sim._check_levels(i) or exited[book.symbol]
        closes: dict[datetime, list[_Book]] = {}
        for book in sorted(today, key=lambda b: bar_close_ts(b.symbol, day)):
            closes.setdefault(bar_close_ts(book.symbol, day), []).append(book)
        for group in closes.values():
            # Bars that close at the same moment are all known before any of them decides.
            for book in group:
                book.latest_close = float(book.sim.close[book.rows[ts]])
            self._observe(day, self.equity())
            for book in group:
                self._close(book, book.rows[ts], day, exited[book.symbol])
        equity = self.equity()
        self._observe(day, equity)
        self.day_start = equity
        exposure = sum(b.close_value if b in today else b.market_value for b in self.books)
        return equity, exposure

    def _close(self, book: _Book, i: int, day: date, exited: bool) -> None:
        """The engine's close step for one symbol, with portfolio-level entry decisions."""
        sim = book.sim
        if sim.position is not None:
            sim._on_close_long(i, can_order=i < book.last)
        elif not exited:
            self._on_close_flat(book, i, day)
        book.close_value = book.market_value
        if i == book.last and sim.position is not None:
            sim._exit(i, sim.close[i], "end_of_data", "close")

    def _on_close_flat(self, book: _Book, i: int, day: date) -> None:
        sim = book.sim
        if i + 1 < sim.strategy.warmup:
            return
        signal = sim.strategy.entry_signal(sim.df, i)
        if signal is None:
            return
        sim._expect(signal, SignalKind.ENTRY, "entry_signal")
        sim.signals.append(signal)
        if i >= book.last:
            return
        entry_est = float(sim.close[i])
        equity = self.equity()
        qty = position_size(equity, entry_est, signal.stop_price, book.cfg)
        if qty <= 0:
            return
        if self.blocked_day == day:
            self._block(day, book.symbol, "daily loss limit hit earlier this New York day")
            return
        room = equity * self.risk.max_total_exposure_pct / 100.0 - self.open_exposure()
        if qty * entry_est > room:
            qty = max(room, 0.0) / entry_est
            if qty * entry_est < MIN_ORDER_USD:
                self._block(day, book.symbol, f"total exposure cap leaves ${max(room, 0.0):,.2f}")
                return
            self.shrunk += 1
        sim.entry_orders += 1
        if qty * entry_est > book.cfg.approval_threshold_usd:
            sim.orders_needing_approval += 1
        sim.pending_entry = (signal, qty, i)

    def _observe(self, day: date, equity: float) -> None:
        """Apply the daily loss limit and the drawdown kill to one equity observation."""
        start = self.day_start
        if self.blocked_day != day and start > 0:
            if start - equity >= start * self.risk.daily_loss_limit_pct / 100.0:
                self.blocked_day = day
                self.loss_days.append(day.isoformat())
        self.peak = max(self.peak, equity)
        if self.peak <= 0:
            return
        drawdown_pct = (self.peak - equity) / self.peak * 100.0
        if drawdown_pct >= self.risk.max_drawdown_kill_pct:
            reason = (
                f"drawdown {drawdown_pct:.2f}% from peak ${self.peak:,.2f} "
                f">= {self.risk.max_drawdown_kill_pct:g}%"
            )
            self.kill_events.append({"date": day.isoformat(), "reason": reason})
            self.peak = equity

    def _block(self, day: date, symbol: str, reason: str) -> None:
        self.blocked.append({"date": day.isoformat(), "symbol": symbol, "reason": reason})


def _book(
    symbol: str, bars: pd.DataFrame, strategy: Strategy, cfg: StrategyConfig, start: str | None, end: str | None
) -> _Book | None:
    if strategy.symbol != symbol:
        raise ValueError(f"the strategy for {symbol} was built for {strategy.symbol}")
    _validate_bars(bars)
    first, last = _window(bars.index, start, end)
    if first > last:
        log.warning("%s: no bars in [%s, %s]; left out of the portfolio", symbol, start, end)
        return None
    df = strategy.prepare(bars)  # full history, once, exactly like run_backtest
    if len(df) != len(bars) or not df.index.equals(bars.index):
        raise ValueError(f"{strategy.name}.prepare must keep the bars' index unchanged")
    bt_cfg = BacktestConfig.from_strategy(cfg, symbol)
    rows = {bars.index[i]: i for i in range(first, last + 1)}
    return _Book(symbol, _Simulation(bars, df, strategy, bt_cfg), bt_cfg, rows, last)


def run_portfolio(
    bars_by_symbol: Mapping[str, pd.DataFrame],
    strategies_by_symbol: Mapping[str, Strategy],
    cfg: StrategyConfig,
    start: str | None = None,
    end: str | None = None,
) -> PortfolioResult:
    """Backtest every symbol's strategy on one shared account, trading bars dated in [start, end]."""
    missing = sorted(set(strategies_by_symbol) - set(bars_by_symbol))
    if missing:
        raise ValueError(f"no bars for {missing}")
    books = [
        book
        for symbol, strategy in strategies_by_symbol.items()
        if (book := _book(symbol, bars_by_symbol[symbol], strategy, cfg, start, end))
    ]
    labels = {b.symbol: b.sim.strategy.label() for b in books}
    result = PortfolioResult(
        symbols=[b.symbol for b in books],
        labels=labels,
        trades=[],
        equity=_empty_series("equity"),
        exposure=_empty_series("exposure_usd"),
        signals=[],
        entry_orders=0,
        orders_needing_approval=0,
    )
    if not books:
        return result

    dates = pd.DatetimeIndex(sorted(set().union(*(b.rows for b in books))), name="date")
    portfolio = _Portfolio(books, cfg.risk)
    equity, exposure = np.empty(len(dates)), np.empty(len(dates))
    for k, ts in enumerate(dates):
        equity[k], exposure[k] = portfolio.step(ts)

    result.equity = pd.Series(equity, index=dates, name="equity")
    result.exposure = pd.Series(exposure, index=dates, name="exposure_usd")
    result.trades = sorted((t for b in books for t in b.sim.trades), key=lambda t: (t.exit_ts, t.symbol))
    result.signals = sorted((s for b in books for s in b.sim.signals), key=lambda s: (s.ts, s.symbol))
    result.entry_orders = sum(b.sim.entry_orders for b in books)
    result.orders_needing_approval = sum(b.sim.orders_needing_approval for b in books)
    result.kill_switch_would_fire = portfolio.kill_events
    result.daily_loss_limit_days = portfolio.loss_days
    result.blocked_entries = portfolio.blocked
    result.shrunk_entries = portfolio.shrunk
    return result


def regimes_on_proxy(result: PortfolioResult, proxy_bars: pd.DataFrame) -> dict:
    """`regime_breakdown` of the portfolio, with regimes labelled on `proxy_bars` (the full
    history of a market proxy such as SPY). Equity is sampled on the proxy's dates, so a weekend's
    crypto P&L lands on the next trading day instead of being dropped; trades entered on a date
    the proxy did not trade count only in the stress windows."""
    equity = result.equity
    if equity.empty:
        return regime_breakdown(result.as_backtest_result(), proxy_bars)
    idx = proxy_bars.index[(proxy_bars.index >= equity.index[0]) & (proxy_bars.index <= equity.index[-1])]
    sampled = equity.reindex(equity.index.union(idx)).ffill().reindex(idx)
    proxy_view = result.as_backtest_result()
    proxy_view.equity = sampled.rename("equity")
    return regime_breakdown(proxy_view, proxy_bars)
