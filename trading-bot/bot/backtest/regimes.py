"""Market-regime labels and a per-regime breakdown of a backtest.

Returns are attributed to the regime label of the bar on which they were earned (the return from
close t-1 to close t belongs to bar t). A regime's return compounds those daily returns and its
max drawdown is taken on that compounded curve, so for trend and vol regimes, whose bars are not
contiguous, it is the drawdown of the strategy "only while in this regime". A stress window's
curve is contiguous, so its drawdown is the real peak-to-trough fall inside the window. Trades
are counted in the regime or window of their entry bar.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from bot import indicators as ind
from bot.backtest.engine import BacktestResult
from bot.backtest.metrics import max_drawdown_pct
from bot.models import Trade

TREND_REGIMES = ("bull", "bear", "sideways")
VOL_REGIMES = ("high", "low")
SMA_N = 200
SLOPE_N = 20
VOL_N = 20
VOL_MEDIAN_N = 252

STRESS_WINDOWS: dict[str, tuple[str, str]] = {
    "crypto_winter_2018": ("2018-01-06", "2018-12-15"),
    "q4_2018_equity_selloff": ("2018-09-20", "2018-12-24"),
    "covid_crash_2020": ("2020-02-19", "2020-03-23"),
    "bear_market_2022": ("2022-01-03", "2022-10-12"),
    "crypto_crash_2021_2022": ("2021-11-10", "2022-11-21"),
    "tariff_shock_2025": ("2025-02-19", "2025-04-08"),
}


def label_regimes(bars: pd.DataFrame) -> pd.DataFrame:
    """Causal per-bar labels. Warmup bars (no SMA200 or vol median yet) read `sideways`/`low`,
    so pass the full history, not just the traded window."""
    close = bars["close"].astype(float)
    sma = ind.sma(close, SMA_N)
    slope = sma - sma.shift(SLOPE_N)
    trend = np.select(
        [(close > sma) & (slope > 0), (close < sma) & (slope < 0)], ["bull", "bear"], default="sideways"
    )
    vol = ind.realized_vol(close, VOL_N)
    high_vol = vol > vol.rolling(VOL_MEDIAN_N, min_periods=VOL_MEDIAN_N).median()
    return pd.DataFrame({"trend": trend, "vol": np.where(high_vol, "high", "low")}, index=bars.index)


def _entry_day(trade: Trade) -> pd.Timestamp:
    """The entry bar's date. Entries fill at a bar's open (09:30 New York or 00:00 UTC), whose
    UTC date is the bar's date for stocks and crypto alike."""
    ts: datetime = trade.entry_ts
    utc = ts.astimezone(timezone.utc) if ts.tzinfo is not None else ts
    return pd.Timestamp(utc.date())


def _summary(returns: pd.Series, n_trades: int) -> dict:
    if returns.empty:
        return {"return_pct": None, "max_dd_pct": None, "n_trades": n_trades, "n_bars": 0}
    curve = np.concatenate([[1.0], np.cumprod(1.0 + returns.to_numpy(dtype=float))])
    return {
        "return_pct": float((curve[-1] - 1.0) * 100.0),
        "max_dd_pct": max_drawdown_pct(curve),
        "n_trades": n_trades,
        "n_bars": int(len(returns)),
    }


def _by_label(returns: pd.Series, labels: pd.Series, entry_labels: pd.Series, names: tuple[str, ...]) -> dict:
    return {name: _summary(returns[labels == name], int((entry_labels == name).sum())) for name in names}


def _stress(returns: pd.Series, entry_days: pd.DatetimeIndex) -> dict:
    out = {}
    for name, (start, end) in STRESS_WINDOWS.items():
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        in_window = (returns.index >= lo) & (returns.index <= hi)
        n_trades = int(((entry_days >= lo) & (entry_days <= hi)).sum())
        out[name] = {"start": start, "end": end, **_summary(returns[in_window], n_trades)}
    return out


def regime_breakdown(result: BacktestResult, bars: pd.DataFrame) -> dict:
    """Return, max DD, trade and bar counts per trend regime, per vol regime and per stress
    window. `return_pct`/`max_dd_pct` are None where the backtest has no bars; all values are
    JSON-safe."""
    equity = result.equity.astype(float)
    returns = equity.pct_change().iloc[1:]
    labels = label_regimes(bars)
    bar_labels = labels.reindex(returns.index)
    entry_days = pd.DatetimeIndex([_entry_day(t) for t in result.trades])
    entry_labels = labels.reindex(entry_days)
    return {
        "trend": _by_label(returns, bar_labels["trend"], entry_labels["trend"], TREND_REGIMES),
        "vol": _by_label(returns, bar_labels["vol"], entry_labels["vol"], VOL_REGIMES),
        "stress": _stress(returns, entry_days),
    }
