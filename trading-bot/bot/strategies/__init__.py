"""Strategy registry: every strategy by its config name, and a factory for configured instances."""

from __future__ import annotations

from typing import Any

from bot.strategies.base import Strategy
from bot.strategies.breakout import BreakoutStrategy
from bot.strategies.meanrev import MeanRevStrategy
from bot.strategies.momentum import MomentumStrategy
from bot.strategies.trend import TrendStrategy

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (TrendStrategy, BreakoutStrategy, MeanRevStrategy, MomentumStrategy)
}


def build(name: str, symbol: str, params: dict[str, Any]) -> Strategy:
    """Instantiate a registered strategy. Raises ValueError for an unknown name or bad params."""
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}; choose one of {sorted(REGISTRY)}") from None
    return cls(symbol, **params)


__all__ = [
    "REGISTRY",
    "BreakoutStrategy",
    "MeanRevStrategy",
    "MomentumStrategy",
    "Strategy",
    "TrendStrategy",
    "build",
]
