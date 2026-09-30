"""Mean reversion: buy a sharp short-term dip (RSI(2)) inside a long-term uptrend (SMA 200)."""

from __future__ import annotations

import math
from typing import Any, ClassVar

import pandas as pd

from bot import indicators as ind
from bot.models import AssetClass, PositionState, Signal, SignalKind, asset_class
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts

ATR_N = 14
TREND_N = 200
EXIT_N = 5
RSI_N = 2


def _features(df: pd.DataFrame, i: int, columns: tuple[str, ...]) -> dict[str, float] | None:
    """Row i's values for `columns`, or None if any is NaN or infinite."""
    values = {c: float(df[c].iat[i]) for c in columns}
    return values if all(math.isfinite(v) for v in values.values()) else None


class MeanRevStrategy(Strategy):
    name: ClassVar[str] = "meanrev"
    title: ClassVar[str] = "Mean reversion"
    default_params: ClassVar[dict[str, Any]] = {"entry_rsi": 10, "atr_mult": 2, "max_hold": 5}
    param_grid: ClassVar[list[dict[str, Any]]] = [
        {"entry_rsi": entry_rsi, "atr_mult": mult, "max_hold": max_hold}
        for entry_rsi in (5, 10)
        for mult in (2, 3)
        for max_hold in (5, 10)
    ]

    def __init__(self, symbol: str, **params: Any) -> None:
        super().__init__(symbol, **params)
        self.params["max_hold"] = ind.as_window(self.params["max_hold"])
        if not 0 < self.params["entry_rsi"] <= 100:
            raise ValueError(f"meanrev: entry_rsi must be in (0, 100], got {self.params['entry_rsi']!r}")
        if not self.params["atr_mult"] > 0:
            raise ValueError(f"meanrev: atr_mult must be positive, got {self.params['atr_mult']!r}")

    @property
    def warmup(self) -> int:
        return max(TREND_N - 1, EXIT_N - 1, RSI_N, ATR_N) + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        close = df["close"].astype(float)
        df["sma200"] = ind.sma(close, TREND_N)
        df["sma5"] = ind.sma(close, EXIT_N)
        df["rsi2"] = ind.rsi(close, RSI_N)
        df["atr"] = ind.atr(df, ATR_N)
        return df

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        features = _features(df, i, ("sma200", "rsi2", "atr"))
        close = float(df["close"].iat[i])
        if features is None or features["atr"] <= 0:
            return None
        if not (close > features["sma200"] and features["rsi2"] < self.params["entry_rsi"]):
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
                f"RSI(2) at {features['rsi2']:.1f} is below {self.params['entry_rsi']:g} "
                f"with the close above SMA(200)"
            ),
            price=close,
            stop_price=stop,
            features=features,
        )

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        """Exits on a close above SMA(5) or once `position.bars_held` (completed bars in the
        trade, counting bar i) reaches `max_hold`. The time exit never waits on indicators."""
        close = float(df["close"].iat[i])
        sma5 = float(df["sma5"].iat[i])
        if math.isfinite(sma5) and close > sma5:
            reason = "close rose above SMA(5)"
        elif position.bars_held >= self.params["max_hold"]:
            reason = f"held {position.bars_held} bars (max {self.params['max_hold']})"
        else:
            return None
        features = {"bars_held": float(position.bars_held)}
        if math.isfinite(sma5):
            features["sma5"] = sma5
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=SignalKind.EXIT,
            reason=reason,
            price=close,
            features=features,
        )

    def describe(self) -> dict[str, str]:
        entry_rsi, mult, max_hold = self.params["entry_rsi"], self.params["atr_mult"], self.params["max_hold"]
        return {
            "entry": (
                f"Buy at the next open when the close is above its 200-day average and RSI(2) "
                f"is below {entry_rsi:g}."
            ),
            "exit": (
                f"Sell at the next open when the close rises above its 5-day average, or after "
                f"{max_hold} days in the trade, whichever comes first."
            ),
            "stop_loss": f"Fixed stop at the signal day's close minus {mult:g} × ATR(14). It does not trail.",
            "take_profit": (
                "No fixed target. The exit on a close above the 5-day average takes the profit."
            ),
            "timeframe": _timeframe(self.symbol),
        }


def _timeframe(symbol: str) -> str:
    close = "00:00 UTC" if asset_class(symbol) is AssetClass.CRYPTO else "16:00 New York time"
    return (
        f"Daily bars closing at {close}. Signals use the completed bar's close; "
        "orders fill at the next bar's open."
    )
