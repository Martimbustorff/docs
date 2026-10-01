"""The contract every strategy implements. The backtester and the live runner call the same
methods on the same prepared DataFrame, so paper trading can be compared bar for bar with the
backtest.

No look-ahead: `prepare` may only add columns whose value at row i depends on rows <= i, and
the `*_signal` methods may only read rows <= i. Orders from a signal on bar i fill on bar i+1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import pandas as pd

from bot.models import PositionState, Signal


class Strategy(ABC):
    name: ClassVar[str]  # registry key, e.g. "trend"
    title: ClassVar[str]  # e.g. "Trend following"
    default_params: ClassVar[dict[str, Any]]
    param_grid: ClassVar[list[dict[str, Any]]]  # small tournament grid; each dict is a full param set

    def __init__(self, symbol: str, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.symbol = symbol
        self.params: dict[str, Any] = {**self.default_params, **params}

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Bars of history needed before the first signal is valid."""

    @abstractmethod
    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of `bars` (DatetimeIndex, columns open/high/low/close/volume) with the
        causal indicator columns this strategy needs."""

    @abstractmethod
    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        """Called on bar i's close while flat. Return an ENTRY Signal with stop_price set
        (and take_profit if the strategy uses one), or None."""

    @abstractmethod
    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        """Called on bar i's close while long. Return an EXIT Signal, or None to hold."""

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        """New stop level after bar i's close, or None to keep the current one.
        Callers only ever ratchet a long stop upward."""
        return None

    @abstractmethod
    def describe(self) -> dict[str, str]:
        """Plain-English rules with the params filled in. Keys: entry, exit, stop_loss,
        take_profit, timeframe. Written into strategy.md for the winner."""

    def label(self) -> str:
        """Stable id for results, e.g. "trend(fast=50,slow=200,atr_mult=3)"."""
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}({inner})"
