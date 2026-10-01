"""Trend following: buy when a moving-average uptrend starts, ride it with an ATR trailing stop."""

from __future__ import annotations

import math
from typing import Any, ClassVar

import pandas as pd

from bot import indicators as ind
from bot.models import AssetClass, PositionState, Signal, SignalKind, asset_class
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts

ATR_N = 14


def _features(df: pd.DataFrame, i: int, columns: tuple[str, ...]) -> dict[str, float] | None:
    """Row i's values for `columns`, or None if any is NaN or infinite."""
    values = {c: float(df[c].iat[i]) for c in columns}
    return values if all(math.isfinite(v) for v in values.values()) else None


class TrendStrategy(Strategy):
    name: ClassVar[str] = "trend"
    title: ClassVar[str] = "Trend following"
    default_params: ClassVar[dict[str, Any]] = {"fast": 50, "slow": 200, "atr_mult": 3}
    param_grid: ClassVar[list[dict[str, Any]]] = [
        {"fast": fast, "slow": slow, "atr_mult": mult}
        for fast in (20, 50)
        for slow in (100, 200)
        for mult in (3, 4)
    ]

    def __init__(self, symbol: str, **params: Any) -> None:
        super().__init__(symbol, **params)
        for key in ("fast", "slow"):
            self.params[key] = ind.as_window(self.params[key])
        fast, slow = self.params["fast"], self.params["slow"]
        if fast >= slow:
            raise ValueError(f"trend: fast ({fast}) must be shorter than slow ({slow})")
        if not self.params["atr_mult"] > 0:
            raise ValueError(f"trend: atr_mult must be positive, got {self.params['atr_mult']!r}")

    @property
    def warmup(self) -> int:
        # The first signal needs SMA(slow) on the bar before it, to tell a fresh uptrend apart.
        return max(self.params["slow"], ATR_N) + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        close = df["close"].astype(float)
        fast = ind.sma(close, self.params["fast"])
        slow = ind.sma(close, self.params["slow"])
        trend_on = (close > slow) & (fast > slow)
        known_before = slow.shift(1).notna()  # SMA(fast) is valid whenever SMA(slow) is
        df["sma_fast"] = fast
        df["sma_slow"] = slow
        df["atr"] = ind.atr(df, ATR_N)
        df["trend_on"] = trend_on
        df["turned_on"] = trend_on & ~trend_on.shift(1, fill_value=False) & known_before
        df["rearm"] = trend_on & (close > fast) & (close.shift(1) <= fast.shift(1)) & known_before
        return df

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        turned_on = bool(df["turned_on"].iat[i])
        if not (turned_on or bool(df["rearm"].iat[i])):
            return None
        features = _features(df, i, ("sma_fast", "sma_slow", "atr"))
        if features is None or features["atr"] <= 0:
            return None
        close = float(df["close"].iat[i])
        stop = close - self.params["atr_mult"] * features["atr"]
        if not 0 < stop < close:
            return None
        fast, slow = self.params["fast"], self.params["slow"]
        reason = (
            f"uptrend started: close above SMA({slow}) and SMA({fast}) above SMA({slow})"
            if turned_on
            else f"re-armed in uptrend: close crossed back above SMA({fast})"
        )
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.ENTRY,
            reason=reason,
            price=close,
            stop_price=stop,
            features=features,
        )

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        features = _features(df, i, ("sma_slow",))
        close = float(df["close"].iat[i])
        if features is None or not close < features["sma_slow"]:
            return None
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.EXIT,
            reason=f"close fell below SMA({self.params['slow']})",
            price=close,
            features=features,
        )

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        atr = float(df["atr"].iat[i])
        if not (math.isfinite(atr) and atr > 0):
            return None
        highest = max(position.highest_close, float(df["close"].iat[i]))
        return highest - self.params["atr_mult"] * atr

    def describe(self) -> dict[str, str]:
        fast, slow, mult = self.params["fast"], self.params["slow"], self.params["atr_mult"]
        return {
            "entry": (
                f"Buy at the next open when the close is above its {slow}-day average and the "
                f"{fast}-day average is above the {slow}-day average, on the first day both "
                f"become true. If the trade is stopped out (or an entry is skipped) while both "
                f"still hold, buy again only on a day the close crosses back above its "
                f"{fast}-day average."
            ),
            "exit": f"Sell at the next open when the close falls below its {slow}-day average.",
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
