"""Performance statistics for a `BacktestResult`.

Conventions:
- Percentages are in percent (12.5 means 12.5%); `win_rate` is a 0-1 fraction.
- `max_drawdown_pct` and `buy_hold_max_dd_pct` are positive numbers.
- `avg_loss_pct`, `largest_loss_usd` and `largest_loss_pct` are signed (<= 0; 0 when there were
  no losing trades). Per-trade percentages are `Trade.pnl_pct`, i.e. P&L over entry notional.
- `profit_factor` is `inf` when there are winning trades and no losing ones, and 0.0 when there
  is no gross profit at all (including no trades). Callers serialising to JSON must write every
  non-finite float (inf, NaN) as null.
- Sharpe and Sortino use daily returns of the mark-to-market equity (flat days included), a zero
  risk-free rate, and are annualised with sqrt(periods_per_year). They are 0.0 when undefined.
- CAGR and drawdown durations use calendar days (365.25 days per year).
- Buy & hold holds the asset from the first to the last close of the traded window, without
  costs. It is NaN when the result carries no `closes` (a hand-built result); likewise
  `exposure_pct` is NaN when the result carries no `exposure`.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from bot.backtest.engine import BacktestResult
from bot.models import Trade

DAYS_PER_YEAR = 365.25


def total_return_pct(values: pd.Series) -> float:
    if len(values) == 0 or not values.iloc[0] > 0:
        return 0.0
    return float((values.iloc[-1] / values.iloc[0] - 1.0) * 100.0)


def max_drawdown_pct(values: pd.Series | np.ndarray) -> float:
    """Largest peak-to-trough fall, as a positive percent."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(arr)
    drawdowns = np.where(peaks > 0, 1.0 - arr / np.where(peaks > 0, peaks, 1.0), 0.0)
    return float(max(drawdowns.max(), 0.0) * 100.0)


def max_dd_duration_days(values: pd.Series) -> int:
    """Longest stretch, in calendar days, from a peak until equity regains it (or until the end
    of the data if it never does)."""
    if len(values) < 2:
        return 0
    underwater = values < values.cummax()
    dates = pd.Series(values.index, index=values.index)
    last_peak = dates.where(~underwater).ffill()
    in_episode = underwater | underwater.shift(1, fill_value=False)
    durations = (dates - last_peak.shift(1))[in_episode]
    return int(durations.max().days) if not durations.empty else 0


def cagr_pct(values: pd.Series) -> float:
    if len(values) < 2 or not values.iloc[0] > 0:
        return 0.0
    days = (values.index[-1] - values.index[0]).days
    if days <= 0:
        return 0.0
    growth = values.iloc[-1] / values.iloc[0]
    if growth <= 0:
        return -100.0
    return float((growth ** (DAYS_PER_YEAR / days) - 1.0) * 100.0)


def sharpe_ratio(returns: pd.Series, periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    std = float(returns.std(ddof=1))
    if not (math.isfinite(std) and std > 0):
        return 0.0
    return float(returns.mean() / std * math.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    downside = math.sqrt(float((returns.clip(upper=0.0) ** 2).mean()))
    if not downside > 0:
        return 0.0
    return float(returns.mean() / downside * math.sqrt(periods_per_year))


def _trade_metrics(trades: list[Trade]) -> dict[str, float | int]:
    pnl = np.array([t.pnl for t in trades], dtype=float)
    pct = np.array([t.pnl_pct for t in trades], dtype=float) * 100.0
    wins, losses = pnl > 0, pnl < 0
    gross_profit, gross_loss = float(pnl[wins].sum()), float(-pnl[losses].sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    n = len(trades)
    return {
        "win_rate": float(wins.sum() / n) if n else 0.0,
        "profit_factor": profit_factor,
        "n_trades": n,
        "avg_win_pct": float(pct[wins].mean()) if wins.any() else 0.0,
        "avg_loss_pct": float(pct[losses].mean()) if losses.any() else 0.0,
        "expectancy_pct": float(pct.mean()) if n else 0.0,
        "largest_loss_usd": float(min(pnl.min(), 0.0)) if n else 0.0,
        "largest_loss_pct": float(min(pct.min(), 0.0)) if n else 0.0,
    }


def _exposure_pct(exposure: pd.Series) -> float:
    if exposure.empty:
        return math.nan
    return float((exposure > 0).mean() * 100.0)


def compute_metrics(result: BacktestResult, periods_per_year: int) -> dict:
    equity = result.equity.astype(float)
    returns = equity.pct_change().iloc[1:]
    closes = result.closes.astype(float)
    trade_stats = _trade_metrics(result.trades)
    return {
        "total_return_pct": total_return_pct(equity),
        "cagr_pct": cagr_pct(equity),
        "max_drawdown_pct": max_drawdown_pct(equity),
        "win_rate": trade_stats["win_rate"],
        "profit_factor": trade_stats["profit_factor"],
        "n_trades": trade_stats["n_trades"],
        "avg_win_pct": trade_stats["avg_win_pct"],
        "avg_loss_pct": trade_stats["avg_loss_pct"],
        "expectancy_pct": trade_stats["expectancy_pct"],
        "sharpe": sharpe_ratio(returns, periods_per_year),
        "sortino": sortino_ratio(returns, periods_per_year),
        "exposure_pct": _exposure_pct(result.exposure),
        "largest_loss_usd": trade_stats["largest_loss_usd"],
        "largest_loss_pct": trade_stats["largest_loss_pct"],
        "max_dd_duration_days": max_dd_duration_days(equity),
        "buy_hold_return_pct": total_return_pct(closes) if not closes.empty else math.nan,
        "buy_hold_max_dd_pct": max_drawdown_pct(closes) if not closes.empty else math.nan,
    }
