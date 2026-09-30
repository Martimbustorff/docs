"""Bar loading: CSV cache, the yfinance downloader and the Alpaca reader, all offline."""

from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bot import data
from bot.data import cache_path, fetch_daily, load_daily

COLUMNS = ["open", "high", "low", "close", "volume"]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache"


def write_csv(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def assert_canonical(df: pd.DataFrame) -> None:
    assert list(df.columns) == COLUMNS
    assert all(dtype == np.float64 for dtype in df.dtypes)
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.name == "date" and df.index.tz is None
    assert df.index.is_monotonic_increasing and df.index.is_unique
    assert not df.isna().any().any()


# --------------------------------------------------------------------------- cache


def test_cache_path_maps_symbols(cache_dir):
    assert cache_path("BTC/USD") == cache_dir / "BTC_USD_1d.csv"
    assert cache_path("spy") == cache_dir / "SPY_1d.csv"
    assert cache_path("BRK-B") == cache_dir / "BRK_B_1d.csv"


def test_load_daily_normalises_the_cache(cache_dir):
    write_csv(
        cache_path("SPY"),
        "date,open,high,low,close,volume\n"
        "2024-01-04,3,4,2,3.5,100\n"
        "2024-01-02,1,2,0.5,1.5,100\n"
        "2024-01-03,,2,1,1.8,100\n"  # NaN: dropped
        "2024-01-05,5,6,4,5.5,100\n"
        "2024-01-05,5,6,4,5.6,120\n"  # duplicate date: last row wins
        "2024-01-08,0,1,0,0.5,100\n",  # non-positive price: dropped
    )

    bars = load_daily("SPY")

    assert_canonical(bars)
    assert list(bars.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-04", "2024-01-05"]
    assert bars.loc["2024-01-05", "close"] == 5.6
    assert bars.loc["2024-01-05", "volume"] == 120.0


def test_load_daily_slices_inclusive_dates(cache_dir):
    rows = "".join(f"2024-01-{d:02d},1,2,0.5,1.5,10\n" for d in range(1, 11))
    write_csv(cache_path("QQQ"), "date,open,high,low,close,volume\n" + rows)

    bars = load_daily("QQQ", start="2024-01-03", end=datetime(2024, 1, 6))

    assert list(bars.index.day) == [3, 4, 5, 6]
    assert load_daily("QQQ", end="2023-12-31").empty


def test_load_daily_missing_file_hints_at_fetch_data(cache_dir):
    with pytest.raises(FileNotFoundError, match="python -m bot fetch-data"):
        load_daily("BTC/USD")


def test_load_daily_rejects_missing_columns(cache_dir):
    write_csv(cache_path("SPY"), "date,open,high,low,close\n2024-01-02,1,2,0.5,1.5\n")
    with pytest.raises(ValueError, match="volume"):
        load_daily("SPY")


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "BTC/USD"])
def test_shipped_cache_loads(symbol):
    if not cache_path(symbol).exists():
        pytest.skip(f"{cache_path(symbol)} not present")
    bars = load_daily(symbol, start="2020-01-01", end="2020-12-31")
    assert_canonical(bars)
    assert len(bars) >= 250
    assert (bars["high"] >= bars["low"]).all()


# --------------------------------------------------------------------------- yfinance


class FakeTicker:
    frames: dict[str, pd.DataFrame] = {}
    calls: list[tuple[str, dict]] = []

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def history(self, **kwargs) -> pd.DataFrame:
        FakeTicker.calls.append((self.ticker, kwargs))
        frame = FakeTicker.frames[self.ticker]
        if isinstance(frame, Exception):
            raise frame
        return frame


@pytest.fixture
def fake_yf(monkeypatch):
    FakeTicker.frames, FakeTicker.calls = {}, []
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=FakeTicker))
    return FakeTicker


def yahoo_frame(dates: list[str], tz: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize(tz).rename("Date")
    n = len(dates)
    base = np.arange(1, n + 1, dtype=float)
    return pd.DataFrame(
        {"Open": base, "High": base + 1, "Low": base - 0.5, "Close": base + 0.5, "Volume": base * 10},
        index=idx,
    )


def test_fetch_daily_writes_the_cache(cache_dir, fake_yf):
    fake_yf.frames["SPY"] = yahoo_frame(["2024-01-02", "2024-01-03", "2100-01-04"], "America/New_York")
    fake_yf.frames["BTC-USD"] = yahoo_frame(["2024-01-01", "2024-01-02", "2024-01-03"], "UTC")

    counts = fetch_daily(["SPY", "BTC/USD"], start="2024-01-01")

    assert counts == {"SPY": 2, "BTC/USD": 3}  # the unfinished 2100 bar is dropped
    assert [t for t, _ in fake_yf.calls] == ["SPY", "BTC-USD"]
    assert all(kw["auto_adjust"] is True and kw["start"] == "2024-01-01" for _, kw in fake_yf.calls)
    spy = load_daily("SPY")
    assert_canonical(spy)
    assert list(spy.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert spy["close"].tolist() == [1.5, 2.5]
    btc = load_daily("BTC/USD")
    assert list(btc.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert cache_path("BTC/USD").read_text().splitlines()[0] == "date,open,high,low,close,volume"


def test_fetch_daily_failure_keeps_the_old_cache(cache_dir, fake_yf):
    old = "date,open,high,low,close,volume\n2020-01-02,1,2,0.5,1.5,10\n"
    write_csv(cache_path("SPY"), old)
    write_csv(cache_path("QQQ"), old)
    fake_yf.frames["SPY"] = RuntimeError("yahoo is down")
    fake_yf.frames["QQQ"] = yahoo_frame([], "America/New_York")

    assert fetch_daily(["SPY", "QQQ"]) == {"SPY": 0, "QQQ": 0}
    assert cache_path("SPY").read_text() == old
    assert cache_path("QQQ").read_text() == old


# --------------------------------------------------------------------------- Alpaca
