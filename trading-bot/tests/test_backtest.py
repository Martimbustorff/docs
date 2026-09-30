"""Backtest engine, metrics and regimes. Tiny scripted strategies make every fill exact."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    bar_open_ts,
    position_size,
    run_backtest,
)
from bot.backtest.metrics import compute_metrics
from bot.backtest.regimes import STRESS_WINDOWS, label_regimes, regime_breakdown
from bot.models import PositionState, Signal, SignalKind, Trade
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts

CAPITAL = 10_000.0
SLIP = 10 / 10_000
FEE = 20 / 10_000


def make_cfg(**overrides: float) -> BacktestConfig:
    values = {
        "capital_usd": CAPITAL,
        "risk_per_trade_pct": 1.0,
        "max_position_pct": 50.0,
        "max_position_usd": 1e9,
        "slippage_bps": 10.0,
        "fee_bps": 20.0,
        "approval_threshold_usd": 1_000.0,
    }
    return BacktestConfig(**{**values, **overrides})


def flat_bars(n: int = 20, price: float = 100.0, start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="D", name="date")
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1e6}, index=idx
    )


def set_bar(bars: pd.DataFrame, i: int, **values: float) -> None:
    for column, value in values.items():
        bars.iloc[i, bars.columns.get_loc(column)] = value


def day(bars: pd.DataFrame, i: int) -> date:
    return bars.index[i].date()


class Scripted(Strategy):
    """Enters on the bars in `entries` ({bar: (stop, take_profit)}) or, with `stop_frac`, on every
    flat bar; exits on the bars in `exits`; proposes the stops in `trails` ({bar: stop})."""

    name = "scripted"
    title = "Scripted test strategy"
    default_params: dict[str, Any] = {"entries": {}, "exits": (), "trails": {}, "warm": 1, "stop_frac": None}
    param_grid: list[dict[str, Any]] = []

    def __init__(self, symbol: str = "SPY", **params: Any) -> None:
        super().__init__(symbol, **params)
        self.prepare_calls: list[int] = []
        self.seen: list[tuple[int, int, float, float]] = []  # (bar, bars_held, highest_close, stop)

    @property
    def warmup(self) -> int:
        return self.params["warm"]

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        self.prepare_calls.append(len(bars))
        return bars.copy()

    def _signal(self, df: pd.DataFrame, i: int, kind: SignalKind, stop=None, tp=None) -> Signal:
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=kind,
            reason="scripted",
            price=float(df["close"].iat[i]),
            stop_price=stop,
            take_profit=tp,
        )

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        if i in self.params["entries"]:
            stop, tp = self.params["entries"][i]
            return self._signal(df, i, SignalKind.ENTRY, stop, tp)
        if self.params["stop_frac"] is not None:
            close = float(df["close"].iat[i])
            return self._signal(df, i, SignalKind.ENTRY, close * (1 - self.params["stop_frac"]))
        return None

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        self.seen.append((i, position.bars_held, position.highest_close, position.stop_price))
        position.stop_price = 0.0  # the engine must hand out a copy
        return self._signal(df, i, SignalKind.EXIT) if i in self.params["exits"] else None

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        return self.params["trails"].get(i)

    def describe(self) -> dict[str, str]:
        return {
            "entry": "scripted",
            "exit": "scripted",
            "stop_loss": "",
            "take_profit": "",
            "timeframe": "1d",
        }


class SmaCross(Strategy):
    """Causal: buy when the close crosses above SMA(n); sell below it; trail at highest close."""

    name = "smacross"
    title = "SMA cross"
    default_params: dict[str, Any] = {"n": 5, "stop_frac": 0.03, "tp_frac": 0.08}
    param_grid: list[dict[str, Any]] = []

    @property
    def warmup(self) -> int:
        return self.params["n"] + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        df["sma"] = df["close"].rolling(self.params["n"]).mean()
        return df

    def _signal(self, df, i, kind, stop=None, tp=None) -> Signal:
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=kind,
            reason="cross",
            price=float(df["close"].iat[i]),
            stop_price=stop,
            take_profit=tp,
            features={"sma": float(df["sma"].iat[i])},
        )

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        close, sma = df["close"].iat[i], df["sma"].iat[i]
        if close > sma and df["close"].iat[i - 1] <= df["sma"].iat[i - 1]:
            return self._signal(
                df,
                i,
                SignalKind.ENTRY,
                close * (1 - self.params["stop_frac"]),
                close * (1 + self.params["tp_frac"]),
            )
        return None

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        return self._signal(df, i, SignalKind.EXIT) if df["close"].iat[i] < df["sma"].iat[i] else None

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        return position.highest_close * (1 - self.params["stop_frac"])

    def describe(self) -> dict[str, str]:
        return {"entry": "", "exit": "", "stop_loss": "", "take_profit": "", "timeframe": "1d"}


def random_walk(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    open_ = np.concatenate([[100.0], close[:-1]]) * np.exp(rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    idx = pd.bdate_range("2019-01-01", periods=n, name="date")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx)


def only_trade(result: BacktestResult) -> Trade:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


# --------------------------------------------------------------------------- fills and costs


def test_entry_and_exit_fill_at_next_open_with_slippage_and_fees():
    bars = flat_bars()
    set_bar(bars, 6, open=102.0, high=103.0)
    set_bar(bars, 11, open=110.0, high=111.0, low=109.0, close=110.0)
    strategy = Scripted(entries={5: (95.0, None)}, exits={10})

    result = run_backtest(bars, strategy, make_cfg())

    trade = only_trade(result)
    entry, exit_ = 102.0 * (1 + SLIP), 110.0 * (1 - SLIP)
    qty = CAPITAL * 0.01 / (100.0 - 95.0)  # risk-limited: 20
    fees = qty * entry * FEE + qty * exit_ * FEE
    pnl = qty * (exit_ - entry) - fees
    assert trade.qty == pytest.approx(20.0)
    assert trade.entry_price == pytest.approx(102.102)
    assert trade.exit_price == pytest.approx(109.89)
    assert trade.fees == pytest.approx(fees)
    assert trade.pnl == pytest.approx(147.28032)
    assert trade.pnl == pytest.approx(pnl)
    assert trade.pnl_pct == pytest.approx(pnl / (entry * qty))
    assert trade.exit_reason == "signal"
    assert trade.entry_ts == bar_open_ts("SPY", day(bars, 6))
    assert trade.entry_ts == datetime(2024, 1, 7, 14, 30, tzinfo=timezone.utc)
    assert trade.exit_ts == bar_open_ts("SPY", day(bars, 11))
    assert trade.meta == {
        "signal_bar": "2024-01-06",
        "entry_bar": "2024-01-07",
        "exit_bar": "2024-01-12",
        "exit_fill": "open",
        "initial_stop": 95.0,
        "final_stop": 95.0,
        "bars_held": 5,
    }
    assert [s.kind for s in result.signals] == [SignalKind.ENTRY, SignalKind.EXIT]
    assert result.entry_orders == 1
    assert result.orders_needing_approval == 1  # notional 20 * 100 = 2000 > 1000

    equity = result.equity
    assert list(equity.index) == list(bars.index)
    assert equity.iloc[5] == pytest.approx(CAPITAL)
    assert equity.iloc[6] == pytest.approx(CAPITAL - qty * entry - qty * entry * FEE + qty * 100.0)
    assert equity.iloc[11] == pytest.approx(CAPITAL + pnl)
    assert equity.iloc[-1] == pytest.approx(CAPITAL + pnl)
    assert result.exposure.iloc[5] == 0 and result.exposure.iloc[6] == pytest.approx(2000.0)


@pytest.mark.parametrize(
    ("bar8", "tp", "price", "reason", "fill"),
    [
        ({"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0}, None, 90.0, "stop", "open"),
        ({"low": 94.0}, None, 95.0, "stop", "intrabar"),
        ({"high": 106.0, "low": 94.0}, 105.0, 95.0, "stop", "intrabar"),  # both touched: stop first
        ({"high": 106.0}, 105.0, 105.0, "take_profit", "intrabar"),
        ({"open": 107.0, "high": 108.0, "low": 106.0, "close": 107.0}, 105.0, 107.0, "take_profit", "open"),
        (
            {"open": 107.0, "high": 108.0, "low": 90.0},
            105.0,
            107.0,
            "take_profit",
            "open",
        ),  # open comes first
        ({"open": 90.0, "high": 108.0, "low": 89.0}, 105.0, 90.0, "stop", "open"),
    ],
)
def test_stop_and_take_profit_levels(bar8, tp, price, reason, fill):
    bars = flat_bars()
    set_bar(bars, 8, **bar8)
    result = run_backtest(bars, Scripted(entries={5: (95.0, tp)}), make_cfg())

    trade = only_trade(result)
    assert trade.exit_reason == reason
    assert trade.exit_price == pytest.approx(price * (1 - SLIP))
    assert trade.meta["exit_bar"] == "2024-01-09"
    assert trade.meta["exit_fill"] == fill
    expected_ts = bar_open_ts("SPY", day(bars, 8)) if fill == "open" else bar_close_ts("SPY", day(bars, 8))
    assert trade.exit_ts == expected_ts


@pytest.mark.parametrize(
    ("bar6", "exit_level", "fill"),
    [
        ({"low": 94.0}, 95.0, "intrabar"),
        ({"open": 94.0, "high": 95.0, "low": 93.0, "close": 94.0}, 94.0, "open"),
    ],
)
def test_stops_are_checked_on_the_fill_bar(bar6, exit_level, fill):
    bars = flat_bars()
    set_bar(bars, 6, **bar6)
    result = run_backtest(bars, Scripted(entries={5: (95.0, None)}), make_cfg())

    trade = only_trade(result)
    assert trade.meta["entry_bar"] == trade.meta["exit_bar"] == "2024-01-07"
    assert trade.meta["bars_held"] == 0
    assert trade.entry_price == pytest.approx(bars["open"].iat[6] * (1 + SLIP))
    assert trade.exit_price == pytest.approx(exit_level * (1 - SLIP))
    assert trade.meta["exit_fill"] == fill


def test_trailing_stop_applies_from_the_next_bar_and_only_ratchets_up():
    bars = flat_bars()
    set_bar(bars, 7, low=97.5)  # below the stop raised on bar 7's close: must not exit on bar 7
    set_bar(bars, 9, low=97.9)  # below 98 (kept) but above 96 (the rejected lower proposal)
    strategy = Scripted(entries={5: (95.0, None)}, trails={6: math.nan, 7: 98.0, 8: 96.0})

    trade = only_trade(run_backtest(bars, strategy, make_cfg()))

    assert trade.meta["exit_bar"] == "2024-01-10"
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(98.0 * (1 - SLIP))
    assert trade.meta["initial_stop"] == 95.0 and trade.meta["final_stop"] == 98.0


def test_bars_held_and_highest_close_as_seen_by_the_strategy():
    bars = flat_bars()
    for i, close in {6: 100.0, 7: 103.0, 8: 101.0, 9: 105.0}.items():
        set_bar(bars, i, close=close, high=close + 1)
    set_bar(bars, 10, low=89.0)  # hits the real stop, even though the strategy zeroed its copy
    strategy = Scripted(entries={5: (90.0, None)})

    trade = only_trade(run_backtest(bars, strategy, make_cfg()))

    fill = 100.0 * (1 + SLIP)
    assert strategy.seen == [
        (6, 1, pytest.approx(fill), 90.0),
        (7, 2, 103.0, 90.0),
        (8, 3, 103.0, 90.0),
        (9, 4, 105.0, 90.0),
    ]
    assert trade.exit_reason == "stop" and trade.meta["exit_bar"] == "2024-01-11"
    assert trade.meta["bars_held"] == 4


# --------------------------------------------------------------------------- order flow


def test_no_new_entry_on_a_bar_where_an_exit_filled():
    bars = flat_bars()
    set_bar(bars, 13, low=90.0)  # stop (95) hit intrabar on bar 13
    strategy = Scripted(stop_frac=0.05, exits={8})  # wants to enter on every flat bar

    result = run_backtest(bars, strategy, make_cfg())

    entries = [
        (t.meta["signal_bar"], t.meta["entry_bar"], t.meta["exit_bar"], t.exit_reason) for t in result.trades
    ]
    assert entries == [
        ("2024-01-01", "2024-01-02", "2024-01-10", "signal"),  # exit signal on bar 8, filled bar 9
        ("2024-01-11", "2024-01-12", "2024-01-14", "stop"),  # no entry signal on bar 9
        ("2024-01-15", "2024-01-16", "2024-01-20", "end_of_data"),  # none on bar 13 either
    ]
    signal_days = [(s.kind.value, s.ts.date()) for s in result.signals]
    assert ("entry", date(2024, 1, 10)) not in signal_days
    assert ("entry", date(2024, 1, 14)) not in signal_days
    assert result.entry_orders == len(result.trades) == 3


def test_end_of_data_closes_at_the_window_close_and_ignores_later_bars():
    bars = flat_bars()
    set_bar(bars, 15, close=104.0, high=105.0)
    for i in range(16, 20):
        set_bar(bars, i, open=50.0, high=51.0, low=49.0, close=50.0)
    strategy = Scripted(entries={5: (95.0, None)})

    result = run_backtest(bars, strategy, make_cfg(), end="2024-01-16")

    trade = only_trade(result)
    assert trade.exit_reason == "end_of_data"
    assert trade.exit_price == pytest.approx(104.0 * (1 - SLIP))
    assert trade.exit_ts == bar_close_ts("SPY", date(2024, 1, 16))
    assert trade.meta["exit_fill"] == "close"
    assert result.equity.index[-1] == pd.Timestamp("2024-01-16")
    assert result.equity.iloc[-1] == pytest.approx(CAPITAL + trade.pnl)
    assert result.exposure.iloc[-1] == pytest.approx(trade.qty * 104.0)  # held through the last bar


def test_signals_on_the_last_bar_are_recorded_but_place_no_order():
    bars = flat_bars()
    entry_last = run_backtest(bars, Scripted(entries={19: (95.0, None)}), make_cfg())
    assert [s.kind for s in entry_last.signals] == [SignalKind.ENTRY]
    assert entry_last.trades == [] and entry_last.entry_orders == 0

    exit_last = run_backtest(bars, Scripted(entries={5: (95.0, None)}, exits={19}), make_cfg())
    assert [s.kind for s in exit_last.signals] == [SignalKind.ENTRY, SignalKind.EXIT]
    trade = only_trade(exit_last)
    assert trade.exit_reason == "end_of_data" and trade.exit_price == pytest.approx(100.0 * (1 - SLIP))


def test_prepare_runs_once_on_full_history_and_only_the_window_trades():
    bars = flat_bars()
    strategy = Scripted(entries={2: (95.0, None), 12: (95.0, None)})

    result = run_backtest(bars, strategy, make_cfg(), start="2024-01-06", end="2024-01-16")

    assert strategy.prepare_calls == [20]
    assert result.equity.index[0] == pd.Timestamp("2024-01-06")
    assert result.equity.index[-1] == pd.Timestamp("2024-01-16")
    assert [s.ts.date() for s in result.signals] == [date(2024, 1, 13)]  # bar 2 is before the window
    trade = only_trade(result)
    assert (trade.meta["entry_bar"], trade.exit_reason) == ("2024-01-14", "end_of_data")


def test_warmup_counts_bars_including_the_current_one():
    bars = flat_bars()
    assert run_backtest(bars, Scripted(entries={8: (95.0, None)}, warm=10), make_cfg()).signals == []
    assert len(run_backtest(bars, Scripted(entries={9: (95.0, None)}, warm=10), make_cfg()).signals) == 1


# --------------------------------------------------------------------------- sizing


@pytest.mark.parametrize(
    ("stop", "overrides", "qty"),
    [
        (95.0, {}, 20.0),  # risk: 100 / 5
        (99.9, {}, 50.0),  # max_position_pct: 5000 / 100
        (95.0, {"max_position_usd": 1_500.0}, 15.0),  # max_position_usd: 1500 / 100
    ],
)
def test_sizing_takes_the_smallest_cap(stop, overrides, qty):
    result = run_backtest(flat_bars(), Scripted(entries={5: (stop, None)}), make_cfg(**overrides))
    assert only_trade(result).qty == pytest.approx(qty)


@pytest.mark.parametrize("stop", [100.0, 101.0, None, math.nan])
def test_entry_is_skipped_when_the_stop_is_not_below_the_close(stop):
    result = run_backtest(flat_bars(), Scripted(entries={5: (stop, None)}), make_cfg())
    assert len(result.signals) == 1  # recorded pre-risk
    assert result.trades == [] and result.entry_orders == 0 and result.orders_needing_approval == 0
    assert (result.equity == CAPITAL).all()


def test_sizing_uses_mark_to_market_equity_at_the_signal_close():
    bars = flat_bars()
    set_bar(bars, 5, low=94.0)  # first trade (filled bar 3) stops out
    strategy = Scripted(entries={2: (95.0, None), 8: (95.0, None)})

    first, second = run_backtest(bars, strategy, make_cfg()).trades

    assert first.exit_reason == "stop" and first.pnl < 0
    assert second.qty == pytest.approx((CAPITAL + first.pnl) * 0.01 / 5.0)


def test_approval_count_uses_signal_close_notional_strictly_above_threshold():
    strategy = Scripted(entries={5: (95.0, None)})
    at_threshold = run_backtest(flat_bars(), strategy, make_cfg(max_position_usd=1_000.0))
    assert at_threshold.entry_orders == 1 and at_threshold.orders_needing_approval == 0
    above = run_backtest(
        flat_bars(), strategy, make_cfg(max_position_usd=1_000.0, approval_threshold_usd=999.99)
    )
    assert above.orders_needing_approval == 1


def test_position_size_rejects_invalid_inputs():
    cfg = make_cfg()
    assert position_size(10_000.0, 100.0, 95.0, cfg) == pytest.approx(20.0)
    assert position_size(0.0, 100.0, 95.0, cfg) == 0.0
    assert position_size(-5.0, 100.0, 95.0, cfg) == 0.0
    assert position_size(10_000.0, 100.0, math.inf, cfg) == 0.0
    assert position_size(10_000.0, 0.0, -1.0, cfg) == 0.0


# --------------------------------------------------------------------------- look-ahead and invariants


def test_changing_future_bars_never_changes_the_past():
    bars = random_walk()
    k = 250
    rng = np.random.default_rng(99)
    changed = bars.copy()
    factors = rng.uniform(0.6, 1.4, len(bars) - k - 1)
    for column in ["open", "high", "low", "close"]:
        changed.iloc[k + 1 :, changed.columns.get_loc(column)] *= factors
    cut = bars.index[k]

    base = run_backtest(bars, SmaCross("SPY"), make_cfg())
    other = run_backtest(changed, SmaCross("SPY"), make_cfg())
    truncated = run_backtest(bars.iloc[: k + 1], SmaCross("SPY"), make_cfg())

    def closed_by(result: BacktestResult, last: pd.Timestamp) -> list[Trade]:
        return [t for t in result.trades if pd.Timestamp(t.meta["exit_bar"]) <= last]

    def signals_by(result: BacktestResult, last: pd.Timestamp) -> list[Signal]:
        return [s for s in result.signals if s.ts <= bar_close_ts("SPY", last.date())]

    assert len(closed_by(base, cut)) >= 5  # the test must exercise real trades
    assert closed_by(other, cut) == closed_by(base, cut)
    assert signals_by(other, cut) == signals_by(base, cut)
    pd.testing.assert_series_equal(other.equity.loc[:cut], base.equity.loc[:cut])
    before_cut = bars.index[k - 1]
    assert closed_by(truncated, before_cut) == closed_by(base, before_cut)
    assert signals_by(truncated, cut) == signals_by(base, cut)
    pd.testing.assert_series_equal(truncated.equity.iloc[:-1], base.equity.loc[:before_cut])


def test_accounting_invariants_on_a_random_walk():
    bars = random_walk(seed=3)
    result = run_backtest(bars, SmaCross("SPY"), make_cfg())

    assert len(result.trades) >= 5
    assert result.entry_orders == len(result.trades)
    assert result.equity.iloc[0] == pytest.approx(CAPITAL)
    assert result.equity.iloc[-1] == pytest.approx(CAPITAL + sum(t.pnl for t in result.trades))
    assert result.equity.notna().all()
    for t in result.trades:
        assert t.meta["signal_bar"] < t.meta["entry_bar"] <= t.meta["exit_bar"]
        assert t.fees == pytest.approx(t.qty * (t.entry_price + t.exit_price) * FEE)
        assert t.qty * bars["close"].loc[t.meta["signal_bar"]] <= CAPITAL * 2  # sized on equity, not leverage
    reasons = {t.exit_reason for t in result.trades}
    assert reasons <= {"signal", "stop", "take_profit", "end_of_data"}


# --------------------------------------------------------------------------- input validation


def test_rejects_bad_bars_and_bad_strategies():
    cfg = make_cfg()
    with_nan = flat_bars()
    set_bar(with_nan, 3, open=math.nan)
    with pytest.raises(ValueError, match="NaN"):
        run_backtest(with_nan, Scripted(), cfg)
    with pytest.raises(ValueError, match="sorted"):
        run_backtest(flat_bars().iloc[::-1], Scripted(), cfg)
    with pytest.raises(ValueError, match="missing"):
        run_backtest(flat_bars().drop(columns="low"), Scripted(), cfg)

    class DropsRows(Scripted):
        def prepare(self, bars):
            return bars.iloc[1:].copy()

    with pytest.raises(ValueError, match="index"):
        run_backtest(flat_bars(), DropsRows(), cfg)

    class WrongKind(Scripted):
        def entry_signal(self, df, i):
            return self._signal(df, i, SignalKind.EXIT)

    with pytest.raises(ValueError, match="exit signal"):
        run_backtest(flat_bars(), WrongKind(), cfg)


def test_empty_window_returns_an_empty_result():
    strategy = Scripted(entries={5: (95.0, None)})
    result = run_backtest(flat_bars(), strategy, make_cfg(), start="2025-01-01")
    assert result.trades == [] and result.signals == [] and result.equity.empty
    assert strategy.prepare_calls == []
    metrics = compute_metrics(result, 252)
    assert metrics["n_trades"] == 0 and metrics["total_return_pct"] == 0.0 and metrics["sharpe"] == 0.0


def test_config_from_strategy_md(strategy_cfg):
    spy = BacktestConfig.from_strategy(strategy_cfg, "SPY")
    btc = BacktestConfig.from_strategy(strategy_cfg, "BTC/USD")
    assert (spy.slippage_bps, spy.fee_bps) == (5, 0)
    assert (btc.slippage_bps, btc.fee_bps) == (10, 25)
    assert spy.capital_usd == 10_000 and spy.risk_per_trade_pct == 1.0
    assert spy.max_position_pct == 33.0 and spy.max_position_usd == 3_500
    assert spy.approval_threshold_usd == 1_000


# --------------------------------------------------------------------------- metrics

METRIC_KEYS = {
    "total_return_pct", "cagr_pct", "max_drawdown_pct", "win_rate", "profit_factor", "n_trades",
    "avg_win_pct", "avg_loss_pct", "expectancy_pct", "sharpe", "sortino", "exposure_pct",
    "largest_loss_usd", "largest_loss_pct", "max_dd_duration_days", "buy_hold_return_pct",
    "buy_hold_max_dd_pct",
}  # fmt: skip


def make_trade(pnl: float, pnl_pct: float, entry: str = "2024-01-02") -> Trade:
    ts = datetime.fromisoformat(entry).replace(hour=14, minute=30, tzinfo=timezone.utc)
    return Trade("SPY", "scripted", ts, 100.0, ts, 100.0, 1.0, pnl, pnl_pct, "signal")


def hand_result(equity: list[float], trades: list[Trade], closes=None, exposure=None) -> BacktestResult:
    idx = pd.date_range("2024-01-01", periods=len(equity), freq="D", name="date")
    kwargs = {}
    if closes is not None:
        kwargs["closes"] = pd.Series(closes, index=idx, dtype=float)
    if exposure is not None:
        kwargs["exposure"] = pd.Series(exposure, index=idx, dtype=float)
    return BacktestResult(
        "SPY",
        "scripted()",
        {},
        trades,
        pd.Series(equity, index=idx, dtype=float),
        [],
        0,
        len(trades),
        **kwargs,
    )


def test_metrics_known_values():
    equity = [100.0, 110.0, 99.0, 105.0, 121.0, 110.0]
    trades = [make_trade(10, 0.10), make_trade(-5, -0.05), make_trade(20, 0.20), make_trade(0, 0.0)]
    result = hand_result(equity, trades, closes=[50, 60, 45, 55, 70, 65], exposure=[0, 500, 500, 0, 0, 0])

    m = compute_metrics(result, 252)

    assert set(m) == METRIC_KEYS
    returns = pd.Series(equity).pct_change().dropna().to_numpy()
    downside = np.sqrt(np.mean(np.minimum(returns, 0) ** 2))
    assert m["total_return_pct"] == pytest.approx(10.0)
    assert m["max_drawdown_pct"] == pytest.approx(10.0)  # 110 -> 99
    assert m["max_dd_duration_days"] == 3  # peak Jan 2, regained Jan 5
    assert m["win_rate"] == pytest.approx(0.5)  # a zero-P&L trade is not a win
    assert m["profit_factor"] == pytest.approx(30 / 5)
    assert m["n_trades"] == 4
    assert m["avg_win_pct"] == pytest.approx(15.0)
    assert m["avg_loss_pct"] == pytest.approx(-5.0)
    assert m["expectancy_pct"] == pytest.approx(6.25)
    assert m["sharpe"] == pytest.approx(returns.mean() / returns.std(ddof=1) * math.sqrt(252))
    assert m["sortino"] == pytest.approx(returns.mean() / downside * math.sqrt(252))
    assert m["exposure_pct"] == pytest.approx(100 * 2 / 6)
    assert m["largest_loss_usd"] == pytest.approx(-5.0)
    assert m["largest_loss_pct"] == pytest.approx(-5.0)
    assert m["buy_hold_return_pct"] == pytest.approx(30.0)
    assert m["buy_hold_max_dd_pct"] == pytest.approx(25.0)  # 60 -> 45
    assert m["cagr_pct"] == pytest.approx((1.1 ** (365.25 / 5) - 1) * 100)


def test_cagr_and_unrecovered_drawdown_duration():
    idx = pd.DatetimeIndex(["2020-01-01", "2021-01-01", "2022-01-01"], name="date")
    result = BacktestResult("SPY", "x", {}, [], pd.Series([100.0, 250.0, 200.0], index=idx), [], 0, 0)
    m = compute_metrics(result, 252)
    assert m["cagr_pct"] == pytest.approx((2.0 ** (365.25 / 731) - 1) * 100)
    assert m["max_drawdown_pct"] == pytest.approx(20.0)
    assert m["max_dd_duration_days"] == 365  # never regained: measured to the end of the data
    assert math.isnan(m["buy_hold_return_pct"]) and math.isnan(m["exposure_pct"])  # hand-built result


def test_profit_factor_edge_cases():
    only_wins = compute_metrics(hand_result([100.0, 101.0], [make_trade(1, 0.01)]), 252)
    assert only_wins["profit_factor"] == math.inf
    assert only_wins["largest_loss_usd"] == 0.0 and only_wins["avg_loss_pct"] == 0.0
    no_trades = compute_metrics(hand_result([100.0, 100.0], []), 252)
    assert no_trades["profit_factor"] == 0.0 and no_trades["win_rate"] == 0.0
    assert (
        no_trades["sharpe"] == 0.0 and no_trades["sortino"] == 0.0 and no_trades["max_dd_duration_days"] == 0
    )


def test_metrics_on_an_engine_run_match_its_trades():
    bars = random_walk(seed=5)
    result = run_backtest(bars, SmaCross("SPY"), make_cfg())
    m = compute_metrics(result, 252)
    assert m["n_trades"] == len(result.trades)
    assert m["total_return_pct"] == pytest.approx(sum(t.pnl for t in result.trades) / CAPITAL * 100)
    assert m["buy_hold_return_pct"] == pytest.approx((bars["close"].iat[-1] / bars["close"].iat[0] - 1) * 100)
    assert 0 < m["exposure_pct"] < 100
    assert all(math.isfinite(v) for k, v in m.items() if k != "profit_factor")


# --------------------------------------------------------------------------- regimes


def series_bars(closes: np.ndarray, start: str = "2015-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="D", name="date")
    return pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes, "volume": 1.0},
        index=idx,
    )


def test_stress_windows_match_the_contract():
    assert set(STRESS_WINDOWS.values()) == {
        ("2018-01-06", "2018-12-15"),
        ("2018-09-20", "2018-12-24"),
        ("2020-02-19", "2020-03-23"),
        ("2022-01-03", "2022-10-12"),
        ("2021-11-10", "2022-11-21"),
        ("2025-02-19", "2025-04-08"),
    }


def test_label_regimes_trend_and_warmup():
    up = label_regimes(series_bars(np.linspace(100, 200, 400)))
    down = label_regimes(series_bars(np.linspace(200, 100, 400)))
    assert list(up.columns) == ["trend", "vol"]
    assert (up["trend"].iloc[:219] == "sideways").all()  # SMA200 and its 20-bar slope warming up
    assert (up["trend"].iloc[219:] == "bull").all()
    assert (down["trend"].iloc[219:] == "bear").all()


def test_label_regimes_vol():
    rng = np.random.default_rng(1)
    calm = rng.normal(0, 0.002, 300)
    wild = rng.normal(0, 0.03, 60)
    labels = label_regimes(series_bars(100 * np.exp(np.cumsum(np.concatenate([calm, wild])))))
    assert (labels["vol"].iloc[:271] == "low").all()  # no 252-bar median yet
    assert (labels["vol"].iloc[-40:] == "high").all()


def test_regime_breakdown_compounds_returns_by_label():
    rng = np.random.default_rng(4)
    bars = series_bars(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 700))))
    window = bars.index[300:]
    equity = pd.Series(10_000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(window)))), index=window)
    trades = [make_trade(1.0, 0.01, str(window[i].date())) for i in (10, 50, 200)]
    result = BacktestResult("SPY", "x", {}, trades, equity, [], 0, len(trades))

    breakdown = regime_breakdown(result, bars)

    labels = label_regimes(bars)
    growth = 1.0
    for kind, names in (("trend", ("bull", "bear", "sideways")), ("vol", ("high", "low"))):
        total, n_bars, n_trades = 1.0, 0, 0
        for name in names:
            expected = 1.0
            for t in range(1, len(equity)):
                if labels[kind].loc[equity.index[t]] == name:
                    expected *= equity.iloc[t] / equity.iloc[t - 1]
            got = breakdown[kind][name]
            if got["n_bars"]:
                assert got["return_pct"] == pytest.approx((expected - 1) * 100)
                total *= 1 + got["return_pct"] / 100
            n_bars += got["n_bars"]
            n_trades += got["n_trades"]
        growth = equity.iloc[-1] / equity.iloc[0]
        assert total == pytest.approx(growth)
        assert n_bars == len(equity) - 1 and n_trades == len(trades)


def test_regime_breakdown_stress_window():
    idx = pd.date_range("2020-02-10", "2020-03-31", freq="D", name="date")
    bars = series_bars(np.full(len(idx), 100.0), start="2020-02-10")
    values = np.full(len(idx), 100.0)
    pos = {d: i for i, d in enumerate(idx.strftime("%Y-%m-%d"))}
    values[pos["2020-02-19"] :] = 110.0  # +10% on the window's first bar (from the Feb 18 close)
    values[pos["2020-03-01"] :] = 88.0  # -20% from the in-window peak
    values[pos["2020-03-20"] :] = 99.0
    values[pos["2020-03-25"] :] = 150.0  # after the window: ignored
    equity = pd.Series(values, index=idx)
    trades = [
        make_trade(1, 0.01, "2020-02-20"),
        make_trade(1, 0.01, "2020-02-12"),
        make_trade(1, 0.01, "2020-03-23"),
    ]
    result = BacktestResult("SPY", "x", {}, trades, equity, [], 0, 3)

    covid = regime_breakdown(result, bars)["stress"]["covid_crash_2020"]

    assert covid["start"] == "2020-02-19" and covid["end"] == "2020-03-23"
    assert covid["return_pct"] == pytest.approx(-1.0)  # 99 / 100 - 1
    assert covid["max_dd_pct"] == pytest.approx(20.0)
    assert covid["n_trades"] == 2
    assert covid["n_bars"] == 34
    other = regime_breakdown(result, bars)["stress"]["bear_market_2022"]
    assert other["return_pct"] is None and other["max_dd_pct"] is None and other["n_bars"] == 0


def test_regime_breakdown_of_an_engine_run_is_json_safe():
    import json

    bars = random_walk(n=600, seed=11)
    result = run_backtest(bars, SmaCross("SPY"), make_cfg(), start=str(bars.index[300].date()))
    breakdown = regime_breakdown(result, bars)
    json.dumps(breakdown, allow_nan=False)
    assert sum(v["n_trades"] for v in breakdown["trend"].values()) == len(result.trades)
