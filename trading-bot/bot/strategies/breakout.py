"""Donchian breakout: buy a close above the prior N-day high, trail the stop up the channel low."""

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


class BreakoutStrategy(Strategy):
    name: ClassVar[str] = "breakout"
    title: ClassVar[str] = "Donchian breakout"
    default_params: ClassVar[dict[str, Any]] = {"entry_n": 20, "exit_n": 10, "atr_mult": 2}
    param_grid: ClassVar[list[dict[str, Any]]] = [
        {"entry_n": entry_n, "exit_n": exit_n, "atr_mult": mult}
        for entry_n in (20, 55)
        for exit_n in (10, 20)
        for mult in (2, 3)
    ]

    def __init__(self, symbol: str, **params: Any) -> None:
        super().__init__(symbol, **params)
        for key in ("entry_n", "exit_n"):
            self.params[key] = ind.as_window(self.params[key])
        if not self.params["atr_mult"] > 0:
            raise ValueError(f"breakout: atr_mult must be positive, got {self.params['atr_mult']!r}")

    @property
    def warmup(self) -> int:
        return max(self.params["entry_n"], self.params["exit_n"], ATR_N) + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        df["donchian_high"] = ind.donchian_high(df, self.params["entry_n"])
        df["donchian_low"] = ind.donchian_low(df, self.params["exit_n"])
        df["atr"] = ind.atr(df, ATR_N)
        return df

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        features = _features(df, i, ("donchian_high", "atr"))
        close = float(df["close"].iat[i])
        if features is None or features["atr"] <= 0 or not close > features["donchian_high"]:
            return None
        stop = close - self.params["atr_mult"] * features["atr"]
        if not 0 < stop < close:
            return None
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.ENTRY,
            reason=f"close broke above the prior {self.params['entry_n']}-day high",
            price=close,
            stop_price=stop,
            features=features,
        )

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        features = _features(df, i, ("donchian_low",))
        close = float(df["close"].iat[i])
        if features is None or not close < features["donchian_low"]:
            return None
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.EXIT,
            reason=f"close broke below the prior {self.params['exit_n']}-day low",
            price=close,
            features=features,
        )

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        channel_low = float(df["donchian_low"].iat[i])
        if not math.isfinite(channel_low):
            return None
        return max(position.stop_price, channel_low)

    def describe(self) -> dict[str, str]:
        entry_n, exit_n, mult = self.params["entry_n"], self.params["exit_n"], self.params["atr_mult"]
        return {
            "entry": (
                f"Buy at the next open when the close is above the highest high of the previous "
                f"{entry_n} days."
            ),
            "exit": (
                f"Sell at the next open when the close is below the lowest low of the previous "
                f"{exit_n} days."
            ),
            "stop_loss": (
                f"Starts at the signal day's close minus {mult:g} × ATR(14). After every close it "
                f"moves up to the lowest low of the previous {exit_n} days when that is higher, "
                f"and it is never lowered."
            ),
            "take_profit": "None. The trailing channel stop and the exit rule take the profit.",
            "timeframe": _timeframe(self.symbol),
        }


def _timeframe(symbol: str) -> str:
    close = "00:00 UTC" if asset_class(symbol) is AssetClass.CRYPTO else "16:00 New York time"
    return (
        f"Daily bars closing at {close}. Signals use the completed bar's close; "
        "orders fill at the next bar's open."
    )
