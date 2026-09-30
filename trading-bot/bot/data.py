"""Daily OHLCV bars: the CSV cache used by the backtester, the yfinance downloader that fills it,
and the Alpaca historical-data reader used live.

Every loader returns the same frame shape: a sorted, unique, tz-naive `DatetimeIndex` named
`date` (the bar's calendar date: New York date for stocks, UTC date for crypto) and float columns
`open`, `high`, `low`, `close`, `volume`, with no NaNs. Bars that have not closed yet are dropped,
so a signal can never be computed on a partial bar.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from bot.config import ROOT
from bot.models import AssetClass, asset_class
from bot.timeutil import NY, bar_close_ts, utcnow

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


# --------------------------------------------------------------------------- Alpaca


def _candidates(source: Any) -> list[Any]:
    """The object itself plus whatever it holds: mapping values, sequence items or attributes."""
    if isinstance(source, Mapping):
        inner = list(source.values())
    elif isinstance(source, (list, tuple)):
        inner = list(source)
    else:
        inner = list(getattr(source, "__dict__", {}).values())
    return [source, *inner]


def _data_client(source: Any, symbol: str) -> Any:
    method = "get_crypto_bars" if asset_class(symbol) is AssetClass.CRYPTO else "get_stock_bars"
    for candidate in _candidates(source):
        if callable(getattr(candidate, method, None)):
            return candidate
    raise TypeError(f"no Alpaca historical data client with {method}() found in {type(source).__name__}")


def _as_utc(value: DateLike) -> datetime:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize(timezone.utc) if ts.tz is None else ts.tz_convert(timezone.utc)
    return ts.to_pydatetime()


def _bar_request(symbol: str, start: DateLike, end: DateLike | None) -> Any:
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    # Pad the request by a day on each side: bar timestamps are midnight New York (stocks) or
    # midnight UTC (crypto), and the exact date range is applied after conversion.
    window = {
        "symbol_or_symbols": symbol,
        "timeframe": TimeFrame.Day,
        "start": _as_utc(start) - timedelta(days=1),
        "end": _as_utc(end) + timedelta(days=2) if end is not None else None,
    }
    if asset_class(symbol) is AssetClass.CRYPTO:
        return CryptoBarsRequest(**window)
    # Adjusted like the Yahoo cache, so live indicators match the backtest's.
    return StockBarsRequest(**window, adjustment=Adjustment.ALL)


_RAW_KEYS = {"ts": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}
_BAR_ATTRS = {"ts": "timestamp", **{c: c for c in COLUMNS}}


def _bar_rows(response: Any, symbol: str) -> list[dict[str, Any]]:
    """Rows from a `BarSet` or from the raw dict a client built with `raw_data=True` returns."""
    data = getattr(response, "data", response)
    bars = data.get(symbol, []) if isinstance(data, Mapping) else []
    return [
        (
            {k: bar[v] for k, v in _RAW_KEYS.items()}
            if isinstance(bar, Mapping)
            else {k: getattr(bar, v) for k, v in _BAR_ATTRS.items()}
        )
        for bar in bars
    ]


def _bar_dates(stamps: pd.DatetimeIndex, symbol: str) -> pd.DatetimeIndex:
    """Stock bars are stamped at midnight New York, crypto bars at midnight UTC."""
    local = stamps if asset_class(symbol) is AssetClass.CRYPTO else stamps.tz_convert(NY)
    return local.tz_localize(None).normalize()


def _alpaca_closed(
    stamps: pd.DatetimeIndex, dates: pd.DatetimeIndex, symbol: str, now: datetime
) -> list[bool]:
    """A crypto bar spans 24 hours from its timestamp, so it is complete only once both that and
    its nominal 00:00 UTC close have passed, whatever midnight Alpaca aligns it to."""
    crypto = asset_class(symbol) is AssetClass.CRYPTO
    closed = []
    for stamp, day in zip(stamps, dates, strict=True):
        close_ts = bar_close_ts(symbol, day.date())
        if crypto:
            close_ts = max(close_ts, (stamp + pd.Timedelta(days=1)).to_pydatetime())
        closed.append(close_ts <= now)
    return closed


def alpaca_daily(
    broker_or_clients: Any,
    symbol: str,
    start: DateLike,
    end: DateLike | None = None,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Completed daily bars for `symbol` in [start, end] from Alpaca's historical data API.

    `broker_or_clients` is an alpaca-py `StockHistoricalDataClient`/`CryptoHistoricalDataClient`,
    a dict or tuple of them, or any object (such as `AlpacaBroker`) holding them as attributes.
    Bars whose close time is after `now` (default: the current time) are dropped.
    """
    client = _data_client(broker_or_clients, symbol)
    request = _bar_request(symbol, start, end)
    if asset_class(symbol) is AssetClass.CRYPTO:
        response = client.get_crypto_bars(request)
    else:
        response = client.get_stock_bars(request)
    rows = _bar_rows(response, symbol)
    if not rows:
        return _empty_frame()
    frame = pd.DataFrame(rows)
    stamps = pd.DatetimeIndex(pd.to_datetime(frame.pop("ts"), utc=True))
    frame.index = _bar_dates(stamps, symbol)
    frame = frame[_alpaca_closed(stamps, frame.index, symbol, now or utcnow())]
    return _between(_normalise(frame, symbol), start, end)
