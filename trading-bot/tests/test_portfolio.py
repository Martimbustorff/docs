"""Shared-capital portfolio backtest: engine parity, exposure cap, daily loss limit, kill switch."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import BacktestConfig, run_backtest
from bot.backtest.portfolio import regimes_on_proxy, run_portfolio
from bot.config import StrategyConfig
from bot.models import PositionState, Signal, SignalKind
from bot.strategies import build
from bot.strategies.base import Strategy
from bot.timeutil import bar_close_ts

STOCK_SLIP = 5 / 10_000  # strategy.md: execution.slippage_bps.stock, fee_bps.stock = 0


def with_risk(cfg: StrategyConfig, **risk: Any) -> StrategyConfig:
    return cfg.model_copy(update={"risk": cfg.risk.model_copy(update=risk)})


def flat(start: str, periods: int, freq: str = "B", price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq=freq, name="date")
    return pd.DataFrame(
        {"open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1e6}, index=idx
    )


def set_day(bars: pd.DataFrame, day: str, **values: float) -> None:
    for column, value in values.items():
        bars.loc[pd.Timestamp(day), column] = value


def random_walk(n: int, seed: int, freq: str = "B", start: str = "2018-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0006, 0.015, n)))
    open_ = np.concatenate([[100.0], close[:-1]]) * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    idx = pd.date_range(start, periods=n, freq=freq, name="date")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx)


class Dated(Strategy):
    """Enters on the ISO dates in `entries` with a stop `stop_frac` below the close; exits on `exits`."""

    name = "dated"
    title = "Dated test strategy"
    default_params: dict[str, Any] = {"entries": (), "exits": (), "stop_frac": 0.02}
    param_grid: list[dict[str, Any]] = []

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        return bars.copy()

    def _signal(self, df: pd.DataFrame, i: int, kind: SignalKind, stop: float | None = None) -> Signal:
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=kind,
            reason="dated",
            price=float(df["close"].iat[i]),
            stop_price=stop,
        )

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        if df.index[i].date().isoformat() not in self.params["entries"]:
            return None
        return self._signal(df, i, SignalKind.ENTRY, float(df["close"].iat[i]) * (1 - self.params["stop_frac"]))

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        return self._signal(df, i, SignalKind.EXIT) if df.index[i].date().isoformat() in self.params["exits"] else None

    def describe(self) -> dict[str, str]:
        return {"entry": "", "exit": "", "stop_loss": "", "take_profit": "", "timeframe": "1d"}


def entries_by_symbol(result) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    for t in result.trades:
        out.setdefault(t.symbol, []).append((t.meta["entry_bar"], round(t.qty, 6)))
    return out


# --------------------------------------------------------------------------- parity with the engine


@pytest.mark.parametrize(
    "symbol, freq, name, params",
    [
        ("SPY", "B", "breakout", {"entry_n": 20, "exit_n": 10, "atr_mult": 2}),
        ("SPY", "B", "trend", {"fast": 20, "slow": 100, "atr_mult": 3}),
        ("QQQ", "B", "meanrev", {"entry_rsi": 10, "atr_mult": 3, "max_hold": 5}),
        ("BTC/USD", "D", "momentum", {"lookback": 60, "atr_mult": 3}),
    ],
)
def test_one_symbol_portfolio_matches_the_engine(strategy_cfg, symbol, freq, name, params):
    cfg = with_risk(strategy_cfg, daily_loss_limit_pct=10.0)  # a limit the walk can't hit, so no cap binds
    bars = random_walk(1000, seed=11, freq=freq)
    start, end = bars.index[300].date().isoformat(), bars.index[-40].date().isoformat()
    strategy = build(name, symbol, params)

    engine = run_backtest(bars, strategy, BacktestConfig.from_strategy(cfg, symbol), start, end)
    port = run_portfolio({symbol: bars}, {symbol: strategy}, cfg, start, end)

    assert len(engine.trades) >= 5  # the comparison is not vacuous
    assert port.equity.index.equals(engine.equity.index)
    np.testing.assert_allclose(port.equity.to_numpy(), engine.equity.to_numpy(), rtol=1e-12)
    np.testing.assert_allclose(port.exposure.to_numpy(), engine.exposure.to_numpy(), rtol=1e-12, atol=1e-9)
    assert len(port.trades) == len(engine.trades)
    for mine, theirs in zip(port.trades, engine.trades):
        assert (mine.entry_ts, mine.exit_ts, mine.exit_reason, mine.meta) == (
            theirs.entry_ts,
            theirs.exit_ts,
            theirs.exit_reason,
            theirs.meta,
        )
        assert mine.qty == pytest.approx(theirs.qty, rel=1e-12)
        assert mine.pnl == pytest.approx(theirs.pnl, rel=1e-9, abs=1e-9)
    assert port.signals == engine.signals
    assert (port.entry_orders, port.orders_needing_approval) == (engine.entry_orders, engine.orders_needing_approval)
    assert port.shrunk_entries == 0 and port.blocked_entries == [] and port.daily_loss_limit_hits == 0


# --------------------------------------------------------------------------- exposure cap


def test_total_exposure_cap_shrinks_the_later_entry(strategy_cfg):
    cfg = with_risk(strategy_cfg, max_total_exposure_pct=50.0)
    bars = flat("2024-01-01", 10)
    strategies = {  # QQQ decides first: same close time, listed first
        "QQQ": Dated("QQQ", entries=("2024-01-03",)),
        "SPY": Dated("SPY", entries=("2024-01-03",)),
    }

    result = run_portfolio({"SPY": bars, "QQQ": bars}, strategies, cfg)

    # Risk sizing wants 10000 * 1% / $2 = 50 shares; the 33% cap cuts QQQ to 33. SPY gets what the
    # 50% total cap leaves after QQQ's queued $3,300: $1,700, i.e. 17 shares.
    assert entries_by_symbol(result) == {"QQQ": [("2024-01-04", 33.0)], "SPY": [("2024-01-04", 17.0)]}
    assert result.shrunk_entries == 1
    assert (result.entry_orders, result.orders_needing_approval) == (2, 2)
    assert result.exposure.max() == pytest.approx(5_000.0)


def test_entry_is_blocked_when_the_cap_leaves_less_than_a_dollar(strategy_cfg):
    cfg = with_risk(strategy_cfg, max_total_exposure_pct=33.0)
    bars = flat("2024-01-01", 10)
    strategies = {"QQQ": Dated("QQQ", entries=("2024-01-03",)), "SPY": Dated("SPY", entries=("2024-01-03",))}

    result = run_portfolio({"SPY": bars, "QQQ": bars}, strategies, cfg)

    assert list(entries_by_symbol(result)) == ["QQQ"]
    assert result.blocked_entries == [
        {"date": "2024-01-03", "symbol": "SPY", "reason": "total exposure cap leaves $0.00"}
    ]
    assert result.entry_orders == 1


def test_stocks_close_before_crypto_and_decide_first(strategy_cfg):
    cfg = with_risk(strategy_cfg, max_total_exposure_pct=50.0)
    stock, crypto = flat("2024-01-01", 10), flat("2024-01-01", 14, freq="D")
    strategies = {  # BTC is listed first, but its bar closes at 00:00 UTC, after the 16:00 New York close
        "BTC/USD": Dated("BTC/USD", entries=("2024-01-03",)),
        "SPY": Dated("SPY", entries=("2024-01-03",)),
    }

    result = run_portfolio({"SPY": stock, "BTC/USD": crypto}, strategies, cfg)

    entries = entries_by_symbol(result)
    assert entries["SPY"] == [("2024-01-04", 33.0)]
    # BTC sizes on equity 10,000 and gets the $1,700 left under the 50% cap.
    assert entries["BTC/USD"] == [("2024-01-04", 17.0)]


def test_weekends_mark_stocks_at_their_latest_close(strategy_cfg):
    stock = flat("2024-01-01", 15)  # Monday 2024-01-01 onwards, business days
    crypto = flat("2024-01-01", 21, freq="D", price=50.0)
    set_day(stock, "2024-01-05", close=110.0, high=110.5)  # Friday close
    strategies = {"SPY": Dated("SPY", entries=("2024-01-02",)), "BTC/USD": Dated("BTC/USD")}

    result = run_portfolio({"SPY": stock, "BTC/USD": crypto}, strategies, strategy_cfg)

    assert pd.Timestamp("2024-01-06") in result.equity.index  # Saturday: BTC trades, SPY doesn't
    qty = 33.0  # 33% of 10,000 at 100
    cash = 10_000.0 - qty * 100.0 * (1 + STOCK_SLIP)
    assert result.exposure[pd.Timestamp("2024-01-06")] == pytest.approx(qty * 110.0)
    assert result.equity[pd.Timestamp("2024-01-06")] == pytest.approx(cash + qty * 110.0)
    assert result.equity[pd.Timestamp("2024-01-07")] == pytest.approx(cash + qty * 110.0)


# --------------------------------------------------------------------------- daily loss limit


def test_daily_loss_limit_blocks_entries_for_the_rest_of_the_day(strategy_cfg):
    cfg = with_risk(strategy_cfg, daily_loss_limit_pct=1.0, risk_per_trade_pct=2.0)
    spy, qqq = flat("2024-01-01", 12), flat("2024-01-01", 12)
    # SPY: 10 shares (2% of 10,000 over a $20 stop) filled 2024-01-04; it closes 12% lower on
    # 2024-01-08, a $120 loss (1.2% of the day's start) that stays above the stop.
    set_day(spy, "2024-01-08", close=88.0, low=87.5)
    for day in ("2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12", "2024-01-15", "2024-01-16"):
        set_day(spy, day, open=88.0, high=88.5, low=87.5, close=88.0)
    strategies = {  # QQQ decides first, yet SPY's simultaneous close must already count
        "QQQ": Dated("QQQ", entries=("2024-01-08", "2024-01-09"), stop_frac=0.2),
        "SPY": Dated("SPY", entries=("2024-01-03",), stop_frac=0.2),
    }

    result = run_portfolio({"SPY": spy, "QQQ": qqq}, strategies, cfg)

    assert result.daily_loss_limit_days == ["2024-01-08"]
    assert result.daily_loss_limit_hits == 1
    assert result.blocked_entries == [
        {"date": "2024-01-08", "symbol": "QQQ", "reason": "daily loss limit hit earlier this New York day"}
    ]
    assert [bar for bar, _ in entries_by_symbol(result)["QQQ"]] == ["2024-01-10"]  # signalled the next day


def test_a_loss_below_the_limit_blocks_nothing(strategy_cfg):
    cfg = with_risk(strategy_cfg, daily_loss_limit_pct=2.0, risk_per_trade_pct=2.0)
    spy, qqq = flat("2024-01-01", 12), flat("2024-01-01", 12)
    set_day(spy, "2024-01-08", close=88.0, low=87.5)  # $120 = 1.2% < 2%
    strategies = {
        "QQQ": Dated("QQQ", entries=("2024-01-08",), stop_frac=0.2),
        "SPY": Dated("SPY", entries=("2024-01-03",), stop_frac=0.2),
    }

    result = run_portfolio({"SPY": spy, "QQQ": qqq}, strategies, cfg)

    assert result.daily_loss_limit_hits == 0
    assert [bar for bar, _ in entries_by_symbol(result)["QQQ"]] == ["2024-01-09"]


# --------------------------------------------------------------------------- kill switch


def test_kill_switch_dates_are_recorded_and_trading_continues(strategy_cfg):
    cfg = with_risk(strategy_cfg, max_drawdown_kill_pct=5.0, max_position_pct=100.0, max_position_usd=1e6,
                    risk_per_trade_pct=2.0)
    bars = flat("2024-01-01", 40)
    # 20 shares (2% of equity over a $10 stop). Each crash gaps through the stop at 70: -$600, -6%.
    set_day(bars, "2024-01-08", open=70.0, high=70.5, low=69.5, close=70.0)
    set_day(bars, "2024-01-09", open=100.0)
    set_day(bars, "2024-01-24", open=70.0, high=70.5, low=69.5, close=70.0)
    set_day(bars, "2024-01-25", open=100.0)
    strategy = Dated("SPY", entries=("2024-01-03", "2024-01-15"), stop_frac=0.1)

    result = run_portfolio({"SPY": bars}, {"SPY": strategy}, cfg)

    assert [k["date"] for k in result.kill_switch_would_fire] == ["2024-01-08", "2024-01-24"]
    assert result.kill_switch_would_fire[0]["reason"].startswith("drawdown 6.")
    assert "from peak $10,000.00 >= 5%" in result.kill_switch_would_fire[0]["reason"]
    # Trading went on after the first trip, and the re-based peak needed a fresh 5% fall to trip again.
    assert [t.meta["entry_bar"] for t in result.trades] == ["2024-01-04", "2024-01-16"]
    assert [t.exit_reason for t in result.trades] == ["stop", "stop"]
    assert result.daily_loss_limit_days == ["2024-01-08", "2024-01-24"]


def test_no_kill_switch_record_while_equity_stays_within_the_limit(strategy_cfg):
    bars = flat("2024-01-01", 20)
    result = run_portfolio({"SPY": bars}, {"SPY": Dated("SPY", entries=("2024-01-03",))}, strategy_cfg)
    assert result.kill_switch_would_fire == []


# --------------------------------------------------------------------------- misc


def test_regimes_on_proxy_sample_the_equity_on_proxy_days(strategy_cfg):
    stock = random_walk(700, seed=5)
    crypto = random_walk(980, seed=6, freq="D")
    start, end = "2019-06-03", "2020-08-31"
    strategies = {
        "SPY": build("breakout", "SPY", {"entry_n": 20, "exit_n": 10, "atr_mult": 2}),
        "BTC/USD": build("breakout", "BTC/USD", {"entry_n": 20, "exit_n": 10, "atr_mult": 2}),
    }
    result = run_portfolio({"SPY": stock, "BTC/USD": crypto}, strategies, strategy_cfg, start, end)

    regimes = regimes_on_proxy(result, stock)

    proxy_days = stock.loc[start:end].index
    assert sum(r["n_bars"] for r in regimes["trend"].values()) == len(proxy_days) - 1
    assert set(regimes["stress"]) >= {"covid_crash_2020"}
    assert result.equity.index[0] == pd.Timestamp(start) and len(result.equity) > len(proxy_days)


def test_rejects_mismatched_inputs(strategy_cfg):
    bars = flat("2024-01-01", 10)
    with pytest.raises(ValueError, match="no bars"):
        run_portfolio({"SPY": bars}, {"QQQ": Dated("QQQ")}, strategy_cfg)
    with pytest.raises(ValueError, match="built for"):
        run_portfolio({"SPY": bars}, {"SPY": Dated("QQQ")}, strategy_cfg)


def test_empty_window_gives_an_empty_result(strategy_cfg):
    bars = flat("2024-01-01", 10)
    result = run_portfolio({"SPY": bars}, {"SPY": Dated("SPY")}, strategy_cfg, "2030-01-01", "2030-12-31")
    assert result.equity.empty and result.trades == [] and result.symbols == []
