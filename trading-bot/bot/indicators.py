"""Causal technical indicators.

Every function returns a float Series aligned to its input and NaN until enough history exists.
The value at row i depends only on rows <= i, so running an indicator on a truncated frame gives
exactly the values it gives on the full frame.

EMA, ATR and RSI are recursive averages seeded with the simple mean of their first n inputs
(the TA-Lib convention). Inputs are expected to be NaN-free after their first valid value, which
`bot.data.load_daily` guarantees by dropping NaN rows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def as_window(n: float) -> int:
    """Validate a lookback length (an integral number >= 1) and return it as an int."""
    if isinstance(n, bool) or not isinstance(n, (int, float, np.integer, np.floating)):
        raise ValueError(f"window length must be a number, got {n!r}")
    if not np.isfinite(n) or n < 1 or int(n) != n:
        raise ValueError(f"window length must be a positive integer, got {n!r}")
    return int(n)


def _seeded_ewm(s: pd.Series, n: int, alpha: float) -> pd.Series:
    """y[t] = alpha * x[t] + (1 - alpha) * y[t-1], seeded at the n-th valid input with the mean
    of the first n valid inputs; NaN before the seed."""
    x = s.astype(float)
    values = x.to_numpy()
    valid = ~np.isnan(values)
    if not valid.any():
        return pd.Series(np.nan, index=s.index, dtype=float)
    start = int(valid.argmax())
    seed = start + n - 1
    if seed >= len(values):
        return pd.Series(np.nan, index=s.index, dtype=float)
    seeded = values.copy()
    seeded[:seed] = np.nan
    seeded[seed] = values[start : seed + 1].mean()
    return pd.Series(seeded, index=s.index).ewm(alpha=alpha, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average of the last n values, including the current one."""
    n = as_window(n)
    return s.astype(float).rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average with alpha = 2 / (n + 1), seeded with SMA(n)."""
    n = as_window(n)
    return _seeded_ewm(s, n, 2.0 / (n + 1))


def true_range(df: pd.DataFrame) -> pd.Series:
    """max(high - low, |high - prev close|, |low - prev close|); NaN on the first bar."""
    high, low = df["high"].astype(float), df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1, skipna=False)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder's average true range. The first value, at row n, is the mean of the true ranges
    of rows 1..n."""
    n = as_window(n)
    return _seeded_ewm(true_range(df), n, 1.0 / n)


def rsi(s: pd.Series, n: int) -> pd.Series:
    """Wilder's RSI on a 0-100 scale; the first value is at row n. A window with no price change
    at all reads 50."""
    n = as_window(n)
    delta = s.astype(float).diff()
    avg_gain = _seeded_ewm(delta.clip(lower=0.0), n, 1.0 / n)
    avg_loss = _seeded_ewm((-delta).clip(lower=0.0), n, 1.0 / n)
    total = avg_gain + avg_loss
    return (100.0 * avg_gain / total.where(total > 0)).mask(total == 0, 50.0)


def donchian_high(df: pd.DataFrame, n: int) -> pd.Series:
    """Highest high of the n bars before the current one (the current bar is excluded)."""
    n = as_window(n)
    return df["high"].astype(float).rolling(n, min_periods=n).max().shift(1)


def donchian_low(df: pd.DataFrame, n: int) -> pd.Series:
    """Lowest low of the n bars before the current one (the current bar is excluded)."""
    n = as_window(n)
    return df["low"].astype(float).rolling(n, min_periods=n).min().shift(1)


def roc(s: pd.Series, n: int) -> pd.Series:
    """Rate of change in percent: 100 * (s / s shifted n bars - 1)."""
    x = s.astype(float)
    change = 100.0 * (x / x.shift(as_window(n)) - 1.0)
    return change.replace([np.inf, -np.inf], np.nan)


def realized_vol(s: pd.Series, n: int, periods_per_year: float = 252) -> pd.Series:
    """Annualised standard deviation (ddof=1) of the last n log returns, as a fraction
    (0.2 = 20%). Pass `bot.timeutil.periods_per_year(symbol)` for crypto (365)."""
    n = as_window(n)
    if n < 2:
        raise ValueError("realized_vol needs n >= 2 returns")
    if not periods_per_year > 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year!r}")
    x = s.astype(float)
    log_returns = np.log(x.where(x > 0)).diff()
    return log_returns.rolling(n, min_periods=n).std() * np.sqrt(periods_per_year)
