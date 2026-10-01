import functools
import math
from datetime import datetime, timedelta, timezone
from itertools import islice, product
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.models import PositionState, Signal, SignalKind
from bot.strategies import REGISTRY, TrendStrategy, build
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
REAL_FILES = {"SPY": "SPY_1d.csv", "BTC/USD": "BTC_USD_1d.csv"}
NAMES = ("trend", "breakout", "meanrev", "momentum")
EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)

# The grids from the ARCHITECTURE.md strategy table.
TABLE_GRIDS = {
    "trend": {"fast": (20, 50), "slow": (100, 200), "atr_mult": (3, 4)},
    "breakout": {"entry_n": (20, 55), "exit_n": (10, 20), "atr_mult": (2, 3)},
    "meanrev": {"entry_rsi": (5, 10), "atr_mult": (2, 3), "max_hold": (5, 10)},
    "momentum": {"lookback": (60, 120, 252), "atr_mult": (3, 5)},
}


@functools.cache
def _cached_bars(symbol: str) -> pd.DataFrame | None:
    path = CACHE / REAL_FILES[symbol]
    if not path.exists():
        return None
    return pd.read_csv(path, index_col="date", parse_dates=True).astype(float).sort_index()


def real_bars(symbol: str) -> pd.DataFrame:
    bars = _cached_bars(symbol)
    if bars is None:
        pytest.skip(f"{REAL_FILES[symbol]} not cached; run `python -m bot fetch-data`")
    return bars


def _position(strategy: Strategy, **overrides) -> PositionState:
    fields = dict(
        symbol=strategy.symbol,
        strategy=strategy.name,
        qty=1.0,
        entry_price=100.0,
        entry_ts=EPOCH,
        stop_price=90.0,
        bars_held=1,
        highest_close=100.0,
    )
    fields.update(overrides)
    return PositionState(**fields)


def _simulate(strategy: Strategy, df: pd.DataFrame) -> tuple[list[Signal], list[Signal], int]:
    """Engine-like loop: signal on bar i's close, fill at bar i+1's open, stops checked intrabar,
    bars_held counts completed bars in the trade. Returns (entries, exits, stop-outs)."""
    entries: list[Signal] = []
    exits: list[Signal] = []
    stops = 0
    position: PositionState | None = None
    for i in range(len(df)):
        if position is not None:
            position.bars_held += 1
            if float(df["low"].iat[i]) <= position.stop_price:
                stops, position = stops + 1, None
            else:
                position.highest_close = max(position.highest_close, float(df["close"].iat[i]))
                exit_ = strategy.exit_signal(df, i, position)
                if exit_ is not None:
                    exits.append(exit_)
                    position = None
                    continue
                new_stop = strategy.trailing_stop(df, i, position)
                if new_stop is not None:
                    position.stop_price = max(position.stop_price, new_stop)
                continue
        entry = strategy.entry_signal(df, i)
        if entry is not None and i + 1 < len(df):
            entries.append(entry)
            fill = float(df["open"].iat[i + 1])
            position = _position(strategy, entry_price=fill, stop_price=entry.stop_price, bars_held=0, highest_close=fill)
    return entries, exits, stops


# --------------------------------------------------------------------------- look-ahead


def _probe_position(strategy: Strategy, bars: pd.DataFrame, i: int) -> PositionState:
    """A position built only from raw bars before i, so it is identical for any truncation >= i."""
    prev_close = float(bars["close"].iat[max(i - 1, 0)])
    return _position(strategy, stop_price=0.9 * prev_close, bars_held=i % 12, highest_close=1.05 * prev_close)


def _decisions(strategy: Strategy, df: pd.DataFrame, bars: pd.DataFrame, i: int) -> tuple:
    position = _probe_position(strategy, bars, i)
    return strategy.entry_signal(df, i), strategy.exit_signal(df, i, position), strategy.trailing_stop(df, i, position)


def _lookahead_mismatches(strategy: Strategy, bars: pd.DataFrame, n_cuts: int = 30, tail: int = 5) -> list[str]:
    """Truncate-and-compare. For each cut k, prepare df[:k+1] and check that its rows equal the
    first k+1 prepared rows of the full history and that the decisions on bars <= k match. Two
    cuts compare every bar [0..k]; the others compare the last `tail` bars, where a leak shows."""
    n = len(bars)
    full = strategy.prepare(bars)
    expected: dict[int, tuple] = {}

    def want(i: int) -> tuple:
        if i not in expected:
            expected[i] = _decisions(strategy, full, bars, i)
        return expected[i]

    entry_bars = [i for i in range(n) if strategy.entry_signal(full, i) is not None]
    whole_ks = {min(strategy.warmup + 50, n - 2), n // 3}
    tail_ks = set(np.linspace(0, n - 2, n_cuts).astype(int).tolist())
    tail_ks |= {k for k in range(strategy.warmup - 3, strategy.warmup + 3) if 0 <= k < n - 1}
    tail_ks |= set(entry_bars[:: max(1, len(entry_bars) // 15)])

    mismatches: list[str] = []
    for k in sorted(whole_ks | tail_ks):
        truncated = strategy.prepare(bars.iloc[: k + 1])
        if not truncated.equals(full.iloc[: k + 1]):
            mismatches.append(f"prepared rows differ for cut k={k}")
        start = 0 if k in whole_ks else max(0, k - tail + 1)
        mismatches += [
            f"decision on bar {i} differs for cut k={k}"
            for i in range(start, k + 1)
            if _decisions(strategy, truncated, bars, i) != want(i)
        ]
    return mismatches


# Every strategy on both assets with default params, plus another grid point on SPY.
LOOKAHEAD_CASES = [(name, symbol, "default") for name in NAMES for symbol in REAL_FILES]
LOOKAHEAD_CASES += [(name, "SPY", "grid0") for name in NAMES]


@pytest.mark.parametrize(("name", "symbol", "which"), LOOKAHEAD_CASES)
def test_no_lookahead_truncate_and_compare(name, symbol, which):
    params = {} if which == "default" else REGISTRY[name].param_grid[0]
    strategy = build(name, symbol, params)
    assert _lookahead_mismatches(strategy, real_bars(symbol)) == []


class _PeekingTrend(TrendStrategy):
    """Deliberately broken: fires one bar before the uptrend actually turns on."""

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = super().prepare(bars)
        df["turned_on"] = df["turned_on"].shift(-1, fill_value=False)
        return df


def test_lookahead_check_catches_a_leaky_strategy():
    bars = real_bars("SPY")
    assert _lookahead_mismatches(_PeekingTrend("SPY"), bars, n_cuts=10)


# --------------------------------------------------------------------------- registry and params


def test_registry_names_and_build():
    assert set(REGISTRY) == set(NAMES)
    for name, cls in REGISTRY.items():
        assert issubclass(cls, Strategy) and cls.name == name and cls.title
        strategy = build(name, "SPY", {})
        assert isinstance(strategy, cls)
        assert strategy.symbol == "SPY"
        assert strategy.params == cls.default_params


def test_build_rejects_unknown_strategy_and_params():
    with pytest.raises(ValueError, match="unknown strategy"):
        build("martingale", "SPY", {})
    with pytest.raises(ValueError, match="unknown params"):
        build("trend", "SPY", {"slowest": 5})


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("trend", {"fast": 200, "slow": 100}),
        ("trend", {"fast": 50, "slow": 50}),
        ("trend", {"atr_mult": 0}),
        ("trend", {"fast": 20.5}),
        ("breakout", {"entry_n": 0}),
        ("breakout", {"atr_mult": -1}),
        ("meanrev", {"entry_rsi": 0}),
        ("meanrev", {"entry_rsi": 150}),
        ("meanrev", {"max_hold": 0}),
        ("momentum", {"lookback": 2.5}),
        ("momentum", {"atr_mult": float("nan")}),
    ],
)
def test_build_rejects_invalid_param_values(name, params):
    with pytest.raises(ValueError):
        build(name, "SPY", params)


def test_integral_float_windows_give_the_same_label():
    assert build("trend", "SPY", {"fast": 50.0, "slow": 200.0}).label() == build("trend", "SPY", {}).label()
    assert build("trend", "SPY", {}).label() == "trend(atr_mult=3,fast=50,slow=200)"


@pytest.mark.parametrize("name", NAMES)
def test_param_grid_is_the_full_cartesian_grid_from_the_table(name):
    cls = REGISTRY[name]
    table = TABLE_GRIDS[name]
    expected = sorted(product(*table.values()))
    got = sorted(tuple(point[k] for k in table) for point in cls.param_grid)
    assert got == expected
    for point in cls.param_grid:
        assert set(point) == set(cls.default_params)
        assert build(name, "SPY", point).params == point


@pytest.mark.parametrize(
    ("name", "params", "warmup"),
    [
        ("trend", {}, 201),
        ("trend", {"fast": 20, "slow": 100}, 101),
        ("breakout", {}, 21),
        ("breakout", {"entry_n": 55, "exit_n": 20}, 56),
        ("meanrev", {}, 200),
        ("momentum", {}, 272),
        ("momentum", {"lookback": 60}, 272),
    ],
)
def test_warmup_matches_hand_count(name, params, warmup):
    assert build(name, "SPY", params).warmup == warmup


@pytest.mark.parametrize("name", NAMES)
def test_indicators_are_valid_by_warmup(name):
    bars = real_bars("SPY")
    for params in ({}, REGISTRY[name].param_grid[0]):
        strategy = build(name, "SPY", params)
        df = strategy.prepare(bars)
        added = [c for c in df.columns if c not in bars.columns and df[c].dtype != bool]
        row = df.iloc[strategy.warmup - 1]
        assert all(math.isfinite(row[c]) for c in added), (strategy.label(), row[added].to_dict())


# --------------------------------------------------------------------------- prepare edge cases


@pytest.mark.parametrize("name", NAMES)
def test_prepare_returns_a_copy_with_the_original_columns(name):
    bars = real_bars("SPY").iloc[:400]
    before = bars.copy()
    df = build(name, "SPY", {}).prepare(bars)
    pd.testing.assert_frame_equal(bars, before)
    pd.testing.assert_frame_equal(df[list(bars.columns)], bars)


@pytest.mark.parametrize("name", NAMES)
def test_short_and_empty_history_gives_no_signals(name):
    strategy = build(name, "SPY", {})
    bars = real_bars("SPY")
    assert strategy.prepare(bars.iloc[:0]).empty
    short = strategy.prepare(bars.iloc[: strategy.warmup - 1])
    assert all(strategy.entry_signal(short, i) is None for i in range(len(short)))


# --------------------------------------------------------------------------- real-data behaviour


@pytest.mark.parametrize(("name", "symbol"), [(n, s) for n in NAMES for s in REAL_FILES])
def test_signals_on_real_data_are_well_formed(name, symbol):
    strategy = build(name, symbol, {})
    bars = real_bars(symbol)
    df = strategy.prepare(bars)
    entries = {i: s for i in range(len(df)) if (s := strategy.entry_signal(df, i)) is not None}
    assert entries
    for i, signal in entries.items():
        assert signal.kind is SignalKind.ENTRY
        assert (signal.symbol, signal.strategy) == (symbol, name)
        assert signal.price == float(bars["close"].iat[i])
        assert 0 < signal.stop_price < signal.price
        assert signal.take_profit is None
        assert signal.ts == bar_close_ts(symbol, df.index[i].date())
        assert signal.features and all(type(v) is float and math.isfinite(v) for v in signal.features.values())
    _, exits, _ = _simulate(strategy, df)
    assert exits
    for signal in exits:
        assert signal.kind is SignalKind.EXIT and signal.stop_price is None
        assert signal.features and all(type(v) is float and math.isfinite(v) for v in signal.features.values())


def test_signal_ts_is_the_bar_close_in_utc():
    for symbol, hour_utc in (("SPY", {20, 21}), ("BTC/USD", {0})):
        strategy = build("breakout", symbol, {})
        df = strategy.prepare(real_bars(symbol))
        signal = next(s for i in range(len(df)) if (s := strategy.entry_signal(df, i)) is not None)
        assert signal.ts.tzinfo is not None and signal.ts.utcoffset() == timedelta(0)
        assert signal.ts.hour in hour_utc


@pytest.mark.parametrize(("name", "symbol"), [(n, s) for n in NAMES for s in REAL_FILES])
def test_reasonable_number_of_trades_on_real_data(name, symbol):
    strategy = build(name, symbol, {})
    bars = real_bars(symbol)
    entries, exits, stops = _simulate(strategy, strategy.prepare(bars))
    years = (bars.index[-1] - bars.index[0]).days / 365.25
    # About 12 years of history: expect a handful to a couple of dozen round trips a year.
    assert 15 <= len(entries) <= 25 * years, (strategy.label(), len(entries))
    assert len(exits) + stops >= len(entries) - 1  # every trade but the last one closed


@pytest.mark.parametrize("name", NAMES)
def test_every_grid_point_trades_on_real_data(name):
    bars = real_bars("SPY")
    for params in REGISTRY[name].param_grid:
        strategy = build(name, "SPY", params)
        df = strategy.prepare(bars)
        hits = (i for i in range(len(df)) if strategy.entry_signal(df, i) is not None)
        assert len(list(islice(hits, 10))) == 10, strategy.label()


@pytest.mark.parametrize("name", NAMES)
def test_no_entry_when_atr_is_nan_or_zero(name):
    strategy = build(name, "SPY", {})
    df = strategy.prepare(real_bars("SPY"))
    i = next(i for i in range(len(df)) if strategy.entry_signal(df, i) is not None)
    for bad in (float("nan"), 0.0):
        broken = df.copy()
        broken.iloc[i, broken.columns.get_loc("atr")] = bad
        assert strategy.entry_signal(broken, i) is None


# --------------------------------------------------------------------------- trend


def test_trend_turns_on_once_and_rearms_on_fast_sma_cross(bars_factory):
    strategy = build("trend", "SPY", {"fast": 2, "slow": 4, "atr_mult": 3})
    df = strategy.prepare(bars_factory([10] * 20 + [11, 12, 13, 14, 15, 14.5, 16, 17]))
    assert df.index[df["turned_on"]].tolist() == [df.index[20]]
    signals = {i: s for i in range(len(df)) if (s := strategy.entry_signal(df, i)) is not None}
    assert sorted(signals) == [20, 26]
    assert signals[20].reason.startswith("uptrend started")
    assert signals[26].reason.startswith("re-armed")
    assert set(signals[20].features) == {"sma_fast", "sma_slow", "atr"}
    assert signals[20].stop_price == pytest.approx(11 - 3 * df["atr"].iat[20])


def test_trend_already_running_when_data_starts_is_not_a_fresh_signal(bars_factory):
    strategy = build("trend", "SPY", {"fast": 2, "slow": 4, "atr_mult": 3})
    df = strategy.prepare(bars_factory([10 + i for i in range(40)]))
    assert df["trend_on"].iloc[3:].all()
    assert all(strategy.entry_signal(df, i) is None for i in range(len(df)))


def test_trend_exit_and_trailing_stop(bars_factory):
    strategy = build("trend", "SPY", {"fast": 2, "slow": 4, "atr_mult": 3})
    df = strategy.prepare(bars_factory([10] * 20 + [11, 12, 13, 14, 15, 14.5, 16, 17, 12]))
    position = _position(strategy, highest_close=20.0)
    assert strategy.exit_signal(df, 27, position) is None
    exit_ = strategy.exit_signal(df, 28, position)
    assert exit_.kind is SignalKind.EXIT and exit_.price == 12.0 and "below SMA(4)" in exit_.reason
    atr = df["atr"].iat[27]
    assert strategy.trailing_stop(df, 27, position) == pytest.approx(20.0 - 3 * atr)
    assert strategy.trailing_stop(df, 27, _position(strategy, highest_close=0.0)) == pytest.approx(17.0 - 3 * atr)
    assert strategy.trailing_stop(df, 5, position) is None  # ATR not warm yet


# --------------------------------------------------------------------------- breakout


def test_breakout_entry_exit_and_channel_trail(bars_factory):
    strategy = build("breakout", "SPY", {"entry_n": 20, "exit_n": 10, "atr_mult": 2})
    df = strategy.prepare(bars_factory([10] * 25 + [10.05, 10.5, 9.0]))
    assert strategy.entry_signal(df, 25) is None  # 10.05 is not above the prior high of 10.1
    entry = strategy.entry_signal(df, 26)
    assert entry.stop_price == pytest.approx(10.5 - 2 * df["atr"].iat[26])
    assert entry.features["donchian_high"] == pytest.approx(10.05 * 1.01)  # bar 25's high
    channel_low = df["donchian_low"].iat[27]
    assert strategy.exit_signal(df, 27, _position(strategy)).kind is SignalKind.EXIT
    assert strategy.exit_signal(df, 26, _position(strategy)) is None
    assert strategy.trailing_stop(df, 27, _position(strategy, stop_price=1.0)) == pytest.approx(channel_low)
    assert strategy.trailing_stop(df, 27, _position(strategy, stop_price=50.0)) == 50.0
    assert strategy.trailing_stop(df, 3, _position(strategy)) is None


# --------------------------------------------------------------------------- meanrev


def _frame(**columns) -> pd.DataFrame:
    return pd.DataFrame({k: [float(v)] for k, v in columns.items()}, index=pd.DatetimeIndex(["2024-03-01"], name="date"))


def test_meanrev_entry_rule_and_fixed_stop():
    strategy = build("meanrev", "SPY", {"entry_rsi": 10, "atr_mult": 2, "max_hold": 5})
    base = dict(close=100, sma200=90, sma5=103, rsi2=8, atr=2)
    entry = strategy.entry_signal(_frame(**base), 0)
    assert entry.stop_price == 96.0 and entry.take_profit is None
    assert strategy.entry_signal(_frame(**{**base, "rsi2": 10}), 0) is None
    assert strategy.entry_signal(_frame(**{**base, "close": 89}), 0) is None
    assert strategy.trailing_stop(_frame(**base), 0, _position(strategy)) is None


def test_meanrev_exits_above_sma5_or_after_max_hold():
    strategy = build("meanrev", "SPY", {"entry_rsi": 10, "atr_mult": 2, "max_hold": 5})
    above = strategy.exit_signal(_frame(close=101, sma5=100), 0, _position(strategy, bars_held=1))
    assert above.reason == "close rose above SMA(5)"
    below = _frame(close=99, sma5=100)
    assert strategy.exit_signal(below, 0, _position(strategy, bars_held=4)) is None
    timed_out = strategy.exit_signal(below, 0, _position(strategy, bars_held=5))
    assert timed_out.reason == "held 5 bars (max 5)" and timed_out.features["bars_held"] == 5.0
    no_indicator = strategy.exit_signal(_frame(close=99, sma5=float("nan")), 0, _position(strategy, bars_held=5))
    assert no_indicator is not None and "sma5" not in no_indicator.features


def test_meanrev_trades_never_outlast_max_hold_on_real_data():
    strategy = build("meanrev", "SPY", {"max_hold": 5})
    df = strategy.prepare(real_bars("SPY"))
    _, exits, _ = _simulate(strategy, df)
    assert exits and max(s.features["bars_held"] for s in exits) <= 5


# --------------------------------------------------------------------------- momentum


def test_momentum_entry_requires_all_three_filters():
    strategy = build("momentum", "SPY", {"lookback": 120, "atr_mult": 3})
    base = dict(close=100, roc=5, sma50=95, realized_vol=0.15, realized_vol_p90=0.25, atr=2)
    entry = strategy.entry_signal(_frame(**base), 0)
    assert entry.stop_price == 94.0 and set(entry.features) == set(base) - {"close"}
    for change in ({"roc": 0}, {"close": 95}, {"realized_vol": 0.25}, {"realized_vol_p90": float("nan")}):
        assert strategy.entry_signal(_frame(**{**base, **change}), 0) is None, change


def test_momentum_exit_on_negative_roc_only():
    strategy = build("momentum", "SPY", {})
    assert strategy.exit_signal(_frame(close=100, roc=-0.1), 0, _position(strategy)).kind is SignalKind.EXIT
    assert strategy.exit_signal(_frame(close=100, roc=0.0), 0, _position(strategy)) is None


def test_momentum_vol_filter_is_the_rolling_252_bar_90th_percentile():
    bars = real_bars("SPY")
    df = build("momentum", "SPY", {}).prepare(bars)
    i = 1000
    window = df["realized_vol"].iloc[i - 251 : i + 1]
    assert df["realized_vol_p90"].iat[i] == pytest.approx(float(np.quantile(window, 0.9)))


# --------------------------------------------------------------------------- describe


@pytest.mark.parametrize("name", NAMES)
def test_describe_has_the_rule_keys_and_the_actual_numbers(name):
    for params in REGISTRY[name].param_grid:
        text = build(name, "SPY", params).describe()
        assert set(text) == {"entry", "exit", "stop_loss", "take_profit", "timeframe"}
        assert all(isinstance(v, str) and v for v in text.values())
        joined = " ".join(text.values())
        for value in params.values():
            assert f"{value:g}" in joined, (params, value)


def test_describe_examples():
    meanrev = build("meanrev", "SPY", {"entry_rsi": 10}).describe()
    assert meanrev["entry"] == "Buy at the next open when the close is above its 200-day average and RSI(2) is below 10."
    trend = build("trend", "SPY", {}).describe()
    assert "crosses back above its 50-day average" in trend["entry"]
    assert "16:00 New York" in trend["timeframe"]
    assert "00:00 UTC" in build("trend", "BTC/USD", {}).describe()["timeframe"]
