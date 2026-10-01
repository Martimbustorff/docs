"""Daily OHLCV bars: the CSV cache used by the backtester and the yfinance downloader that fills
it. Live bars come from `AlpacaBroker.daily_bars` in the same frame shape.

Every loader returns the same frame shape: a sorted, unique, tz-naive `DatetimeIndex` named
`date` (the bar's calendar date: New York date for stocks, UTC date for crypto) and float columns
`open`, `high`, `low`, `close`, `volume`, with no NaNs. Bars that have not closed yet are dropped,
so a signal can never be computed on a partial bar.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from bot.config import ROOT
from bot.timeutil import bar_close_ts, utcnow

log = logging.getLogger(__name__)

CACHE_DIR = ROOT / "data" / "cache"
COLUMNS = ["open", "high", "low", "close", "volume"]
PRICE_COLUMNS = ["open", "high", "low", "close"]

DateLike = str | date | datetime


def cache_path(symbol: str) -> Path:
    """`BTC/USD` -> `data/cache/BTC_USD_1d.csv`, `BRK-B` -> `data/cache/BRK_B_1d.csv`."""
    stem = symbol.upper().replace("/", "_").replace("-", "_")
    return CACHE_DIR / f"{stem}_1d.csv"


def yahoo_ticker(symbol: str) -> str:
    """Yahoo spells crypto pairs with a dash: `BTC/USD` -> `BTC-USD`."""
    return symbol.upper().replace("/", "-")


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float) for c in COLUMNS}, index=pd.DatetimeIndex([], name="date"))


def _normalise(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Coerce to the canonical frame: float columns, tz-naive sorted unique date index, no NaNs,
    no non-positive prices."""
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{symbol}: bars are missing columns {missing}")
    out = df[COLUMNS].apply(pd.to_numeric, errors="coerce").astype(float)
    index = pd.DatetimeIndex(out.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    out.index = index.normalize().rename("date")
    out = out.dropna()
    bad = (out[PRICE_COLUMNS] <= 0).any(axis=1)
    if bad.any():
        log.warning("%s: dropping %d bars with non-positive prices", symbol, int(bad.sum()))
        out = out[~bad]
    out = out.sort_index()
    return out[~out.index.duplicated(keep="last")]


def _drop_unclosed(df: pd.DataFrame, symbol: str, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    closed = [bar_close_ts(symbol, ts.date()) <= now for ts in df.index]
    return df[closed]


def _between(df: pd.DataFrame, start: DateLike | None, end: DateLike | None) -> pd.DataFrame:
    lo = pd.Timestamp(start).tz_localize(None).normalize() if start is not None else None
    hi = pd.Timestamp(end).tz_localize(None).normalize() if end is not None else None
    return df.loc[lo:hi]


def load_daily(symbol: str, start: DateLike | None = None, end: DateLike | None = None) -> pd.DataFrame:
    """Cached daily bars for `symbol`, restricted to [start, end] (inclusive dates)."""
    path = cache_path(symbol)
    if not path.exists():
        raise FileNotFoundError(f"no cached bars for {symbol} at {path}; run `python -m bot fetch-data`")
    raw = pd.read_csv(path, index_col="date", parse_dates=["date"])
    return _between(_normalise(raw, symbol), start, end)


def _write_cache(df: pd.DataFrame, symbol: str) -> None:
    """Atomic write, so an interrupted download never leaves a truncated cache file."""
    path = cache_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index_label="date", date_format="%Y-%m-%d")
    os.replace(tmp, path)


def _download_yahoo(symbol: str, start: str) -> pd.DataFrame:
    import yfinance as yf  # imported lazily: only the fetch-data command needs it

    raw = yf.Ticker(yahoo_ticker(symbol)).history(
        start=start, interval="1d", auto_adjust=True, actions=False, raise_errors=True
    )
    return raw.rename(columns=str.lower)


def fetch_daily(symbols: Iterable[str], start: str = "2014-09-17") -> dict[str, int]:
    """Download split- and dividend-adjusted daily bars from Yahoo and rewrite the cache.

    Returns the number of rows written per symbol. A symbol whose download fails or comes back
    empty is logged, reported as 0 rows, and its existing cache file is left untouched.
    """
    counts: dict[str, int] = {}
    now = utcnow()
    for symbol in symbols:
        try:
            bars = _drop_unclosed(_normalise(_download_yahoo(symbol, start), symbol), symbol, now)
        except Exception:
            log.exception("%s: download from Yahoo failed; cache left unchanged", symbol)
            counts[symbol] = 0
            continue
        if bars.empty:
            log.warning("%s: Yahoo returned no bars since %s; cache left unchanged", symbol, start)
            counts[symbol] = 0
            continue
        _write_cache(bars, symbol)
        counts[symbol] = len(bars)
        log.info("%s: cached %d daily bars ending %s", symbol, len(bars), bars.index[-1].date())
    return counts
