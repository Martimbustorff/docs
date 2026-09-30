"""Time-series momentum: hold while the price is up over the lookback, skipping volatility spikes."""

from __future__ import annotations

import math
from typing import Any, ClassVar

import pandas as pd

from bot import indicators as ind
from bot.models import AssetClass, PositionState, Signal, SignalKind, asset_class
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts, periods_per_year

ATR_N = 14
SMA_N = 50
VOL_N = 20  # realized-vol window
VOL_RANK_N = 252  # bars in the rolling percentile of realized vol
VOL_PCTL = 0.90


def _features(df: pd.DataFrame, i: int, columns: tuple[str, ...]) -> dict[str, float] | None:
    """Row i's values for `columns`, or None if any is NaN or infinite."""
    values = {c: float(df[c].iat[i]) for c in columns}
    return values if all(math.isfinite(v) for v in values.values()) else None


class MomentumStrategy(Strategy):
    name: ClassVar[str] = "momentum"
    title: ClassVar[str] = "Time-series momentum"
    default_params: ClassVar[dict[str, Any]] = {"lookback": 252, "atr_mult": 5}
    param_grid: ClassVar[list[dict[str, Any]]] = [
        {"lookback": lookback, "atr_mult": mult} for lookback in (60, 120, 252) for mult in (3, 5)
    ]

    def __init__(self, symbol: str, **params: Any) -> None:
        super().__init__(symbol, **params)
        self.params["lookback"] = ind.as_window(self.params["lookback"])
        if not self.params["atr_mult"] > 0:
            raise ValueError(f"momentum: atr_mult must be positive, got {self.params['atr_mult']!r}")

    @property
    def warmup(self) -> int:
        # Realized vol is first valid at row VOL_N; its percentile needs VOL_RANK_N of those.
        return max(self.params["lookback"], SMA_N - 1, VOL_N + VOL_RANK_N - 1, ATR_N) + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        close = df["close"].astype(float)
        vol = ind.realized_vol(close, VOL_N, periods_per_year(self.symbol))
        df["roc"] = ind.roc(close, self.params["lookback"])
        df["sma50"] = ind.sma(close, SMA_N)
        df["realized_vol"] = vol
        df["realized_vol_p90"] = vol.rolling(VOL_RANK_N, min_periods=VOL_RANK_N).quantile(VOL_PCTL)
        df["atr"] = ind.atr(df, ATR_N)
        return df

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        features = _features(df, i, ("roc", "sma50", "realized_vol", "realized_vol_p90", "atr"))
        close = float(df["close"].iat[i])
        if features is None or features["atr"] <= 0:
            return None
        if not (
            features["roc"] > 0
            and close > features["sma50"]
            and features["realized_vol"] < features["realized_vol_p90"]
        ):
            return None
        stop = close - self.params["atr_mult"] * features["atr"]
        if not 0 < stop < close:
            return None
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.ENTRY,
            reason=(
                f"up {features['roc']:.1f}% over {self.params['lookback']} bars, above SMA(50), "
                f"volatility below its 1-year 90th percentile"
            ),
            price=close,
            stop_price=stop,
            features=features,
        )

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        features = _features(df, i, ("roc",))
        if features is None or not features["roc"] < 0:
            return None
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.EXIT,
            reason=f"down {-features['roc']:.1f}% over {self.params['lookback']} bars",
            price=float(df["close"].iat[i]),
            features=features,
        )

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        atr = float(df["atr"].iat[i])
        if not (math.isfinite(atr) and atr > 0):
            return None
        highest = max(position.highest_close, float(df["close"].iat[i]))
        return highest - self.params["atr_mult"] * atr

    def describe(self) -> dict[str, str]:
        lookback, mult = self.params["lookback"], self.params["atr_mult"]
        return {
            "entry": (
                f"Buy at the next open when the close is higher than it was {lookback} days ago, "
                f"the close is above its 50-day average, and 20-day realized volatility is below "
                f"the 90th percentile of its last 252 days."
            ),
            "exit": f"Sell at the next open when the close is lower than it was {lookback} days ago.",
            "stop_loss": (
                f"Trailing stop at the highest close since entry minus {mult:g} × ATR(14), "
                f"recomputed after every close and never lowered. It starts at the signal "
                f"day's close minus {mult:g} × ATR(14)."
            ),
            "take_profit": "None. The trailing stop and the exit rule take the profit.",
            "timeframe": _timeframe(self.symbol),
        }


def _timeframe(symbol: str) -> str:
    close = "00:00 UTC" if asset_class(symbol) is AssetClass.CRYPTO else "16:00 New York time"
    return (
        f"Daily bars closing at {close}. Signals use the completed bar's close; "
        "orders fill at the next bar's open."
    )
