import math

import numpy as np
import pandas as pd
import pytest

from bot import indicators as ind


def _series(values):
    return pd.Series([float(v) for v in values], index=pd.date_range("2020-01-01", periods=len(values), name="date"))


def _frame(rows):
    """rows: (high, low, close) tuples."""
    high, low, close = zip(*rows)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1.0},
        index=pd.date_range("2020-01-01", periods=len(rows), name="date"),
    )


def _assert_values(actual: pd.Series, expected: list[float]) -> None:
    assert len(actual) == len(expected)
    for got, want in zip(actual.tolist(), expected):
        if math.isnan(want):
            assert math.isnan(got)
        else:
            assert got == pytest.approx(want, rel=1e-12, abs=1e-12)


NAN = float("nan")

# High, low, close. True ranges from row 1: 2, 2, 3.5, 5 (gap up over the prior close), 1.5.
ATR_ROWS = [(10, 8, 9), (11, 9, 10.5), (12, 10, 10), (10.5, 7, 8), (13, 9, 12), (12.5, 11, 12)]


def test_sma_hand_values():
    _assert_values(ind.sma(_series([1, 2, 3, 4, 5]), 3), [NAN, NAN, 2, 3, 4])


def test_ema_is_seeded_with_sma_then_recursive():
    # alpha = 2 / (3 + 1) = 0.5; seed = mean(2, 4, 6) = 4
    _assert_values(ind.ema(_series([2, 4, 6, 8, 4, 2]), 3), [NAN, NAN, 4, 6, 5, 3.5])


def test_true_range_uses_previous_close_and_is_nan_on_first_bar():
    _assert_values(ind.true_range(_frame(ATR_ROWS)), [NAN, 2, 2, 3.5, 5, 1.5])


def test_atr_is_wilder_seeded_with_mean_of_first_n_true_ranges():
    # seed at row 3 = mean(2, 2, 3.5) = 2.5; then (prev * 2 + tr) / 3
    _assert_values(ind.atr(_frame(ATR_ROWS), 3), [NAN, NAN, NAN, 2.5, 10 / 3, 49 / 18])


def test_rsi_wilder_hand_values():
    # deltas: +1, -0.5, +1, +0.5, -1. Seed at row 2: gain 0.5, loss 0.25.
    expected = [NAN, NAN, 100 * 0.5 / 0.75, 100 * 0.75 / 0.875, 100 * 0.625 / 0.6875, 100 * 0.3125 / 0.84375]
    _assert_values(ind.rsi(_series([10, 11, 10.5, 11.5, 12, 11]), 2), expected)


def test_rsi_edge_cases():
    assert ind.rsi(_series([5, 5, 5, 5]), 2).tolist()[2:] == [50.0, 50.0]
    assert ind.rsi(_series([1, 2, 3, 4]), 2).tolist()[2:] == [100.0, 100.0]
    assert ind.rsi(_series([4, 3, 2, 1]), 2).tolist()[2:] == [0.0, 0.0]


def test_donchian_excludes_the_current_bar():
    df = _frame([(1, 0.5, 1), (3, 2, 2), (2, 1, 1.5), (5, 0.8, 4), (4, 3, 3.5)])
    _assert_values(ind.donchian_high(df, 2), [NAN, NAN, 3, 3, 5])
    _assert_values(ind.donchian_low(df, 2), [NAN, NAN, 0.5, 1, 0.8])


def test_roc_in_percent():
    _assert_values(ind.roc(_series([100, 110, 99]), 1), [NAN, 10, -10])
    _assert_values(ind.roc(_series([100, 110, 99]), 2), [NAN, NAN, -1])


def test_roc_zero_base_is_nan_not_inf():
    assert math.isnan(ind.roc(_series([0, 5]), 1).iloc[1])


def test_realized_vol_hand_value_and_annualisation():
    closes = [100, 102, 99, 101]
    r = [math.log(102 / 100), math.log(99 / 102), math.log(101 / 99)]

    def sample_std(xs):
        mean = sum(xs) / len(xs)
        return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))

    expected = [NAN, NAN, sample_std(r[:2]) * math.sqrt(252), sample_std(r[1:]) * math.sqrt(252)]
    _assert_values(ind.realized_vol(_series(closes), 2), expected)
    crypto = ind.realized_vol(_series(closes), 2, periods_per_year=365)
    assert crypto.iloc[3] == pytest.approx(expected[3] * math.sqrt(365 / 252))


def test_realized_vol_rejects_bad_arguments():
    with pytest.raises(ValueError):
        ind.realized_vol(_series([1, 2, 3]), 1)
    with pytest.raises(ValueError):
        ind.realized_vol(_series([1, 2, 3]), 2, periods_per_year=0)


@pytest.mark.parametrize("bad", [0, -3, 2.5, float("nan"), True, "20", None])
def test_window_validation(bad):
    with pytest.raises(ValueError):
        ind.sma(_series([1, 2, 3]), bad)


def test_as_window_accepts_integral_floats():
    assert ind.as_window(20.0) == 20
    assert ind.as_window(np.int64(5)) == 5


def _all_indicators(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    return {
        "sma": ind.sma(close, 10),
        "ema": ind.ema(close, 10),
        "atr": ind.atr(df, 14),
        "rsi": ind.rsi(close, 2),
        "donchian_high": ind.donchian_high(df, 20),
        "donchian_low": ind.donchian_low(df, 10),
        "roc": ind.roc(close, 5),
        "realized_vol": ind.realized_vol(close, 20),
    }


def _random_bars(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    spread = np.abs(rng.normal(0, 0.01, n))
    return pd.DataFrame(
        {"open": close, "high": close * (1 + spread), "low": close * (1 - spread), "close": close, "volume": 1.0},
        index=pd.date_range("2020-01-01", periods=n, name="date"),
    )


def test_indicators_are_causal_under_truncation():
    df = _random_bars(300)
    full = _all_indicators(df)
    for k in (0, 1, 5, 13, 14, 15, 19, 20, 21, 50, 150, 299):
        truncated = _all_indicators(df.iloc[: k + 1])
        for name, series in truncated.items():
            pd.testing.assert_series_equal(series, full[name].iloc[: k + 1], check_exact=True, obj=name)


def test_outputs_are_aligned_float_series_and_nan_during_warmup():
    df = _random_bars(40)
    for name, series in _all_indicators(df).items():
        assert series.index.equals(df.index), name
        assert series.dtype == np.float64, name
    out = _all_indicators(df)
    assert out["sma"].iloc[:9].isna().all() and out["sma"].iloc[9:].notna().all()
    assert out["atr"].iloc[:14].isna().all() and out["atr"].iloc[14:].notna().all()
    assert out["rsi"].iloc[:2].isna().all() and out["rsi"].iloc[2:].notna().all()
    assert out["donchian_high"].iloc[:20].isna().all() and out["donchian_high"].iloc[20:].notna().all()
    assert out["realized_vol"].iloc[:20].isna().all() and out["realized_vol"].iloc[20:].notna().all()


@pytest.mark.parametrize("length", [0, 1, 2])
def test_short_and_empty_inputs_give_all_nan(length):
    df = _random_bars(length)
    for name, series in _all_indicators(df).items():
        assert len(series) == length, name
        assert series.isna().all(), name
