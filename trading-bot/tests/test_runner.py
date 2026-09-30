"""The live runner, end to end: fake clock, SimBroker, FakeJevClient, a recording notifier and a
real Store on tmp_path. A scripted strategy (registered as "trend") makes every signal exact."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pytest
import yaml

from bot.backtest.engine import BacktestConfig, run_backtest
from bot.broker import OrderGateway, SimBroker, make_client_order_id
from bot.config import StrategyConfig, load_settings
from bot.data import load_daily
from bot.jev import FakeJevClient, JevGate
from bot.models import OrderIntent, OrderPurpose, OrderResult, PositionState, Side, Signal, SignalKind
from bot.notify import Command
from bot.risk import DAILY_LOSS_KEY, RESET_TS_KEY, KillSwitch, RiskManager
from bot.runner import (
    EQUITY_TS_KEY,
    HEARTBEAT_KEY,
    QUEUE_KEY,
    REPORT_DAY_KEY,
    Runner,
    bar_day,
    gate_label,
    hand_off_orders,
    last_bar_key,
    latest_closed_bar_day,
    load_queue,
    load_tracked,
)
from bot.store import Store, parse_ts, to_iso
from bot.strategies import REGISTRY, build
from bot.strategies.base import Strategy
from bot.timeutil import NY, bar_close_ts

UTC = timezone.utc

CONFIG_YAML = """
version: 1
timeframe: 1d
assets:
  SPY: {enabled: true, strategy: trend, params: {}}
  QQQ: {enabled: false, strategy: trend, params: {}}
  BTC/USD: {enabled: false, strategy: trend, params: {}}
risk:
  capital_usd: 10000
  risk_per_trade_pct: 1.0
  max_position_pct: 33.0
  max_position_usd: 3500
  max_total_exposure_pct: 100
  daily_loss_limit_pct: 2.0
  max_drawdown_kill_pct: 15.0
  max_orders_per_day: 10
  max_consecutive_errors: 5
  approval_threshold_usd: 1000000000
  approval_timeout_minutes: 720
  approval_max_price_drift_pct: 2.0
  kill_switch_flatten: true
jev:
  mode: gate
  model: jev-latest
  timeout_s: 3.0
  on_error: block
  price_per_million_input_tokens: 0.042
  max_headlines: 10
  headline_lookback_hours: 24
  questions:
    headline_sentiment:
      type: choice
      instructions: Are the untrusted_headlines bullish, bearish or neutral?
      criteria: {bullish: up, bearish: down, neutral: flat}
      uses_headlines: true
      gate: {outcome: bearish, max: 0.40}
    buying_pressure:
      type: noul
      instructions: Is buying pressure building?
      criteria: {'true': 'yes', 'false': 'no'}
      applies_to: [trend, breakout, momentum]
      gate: {outcome: 'yes', min: 0.45}
    selling_exhaustion:
      type: noul
      instructions: Is the selling exhausting?
      criteria: {'true': 'yes', 'false': 'no'}
      applies_to: [meanrev]
      gate: {outcome: 'yes', min: 0.45}
execution:
  poll_seconds: 60
  stock_entry_delay_minutes: 1
  crypto_bar_close_utc: '00:00'
  slippage_bps: {stock: 0, crypto: 0}
  fee_bps: {stock: 0, crypto: 25}
  daily_report_time: '17:15'
  timezone: America/New_York
tournament:
  in_sample: ['2014-09-17', '2021-12-31']
  out_of_sample: ['2022-01-01', '2026-09-29']
  filters: {max_drawdown_pct: 15.0, min_win_rate: 0.4, min_profit_factor: 1.2, min_trades: 8}
live_gate: {}
"""


def make_cfg(assets: dict[str, dict] | None = None, jev_mode: str = "gate", **risk: Any) -> StrategyConfig:
    data = yaml.safe_load(CONFIG_YAML)
    if assets is not None:
        data["assets"] = assets
    data["risk"].update(risk)
    data["jev"]["mode"] = jev_mode
    return StrategyConfig.model_validate(data)


def ny(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=NY).astimezone(UTC)


MON, TUE, WED, THU = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.approvals: list[tuple[int, str]] = []
        self.commands: list[Command] = []
        self.polls = 0

    def send(self, text: str) -> None:
        self.sent.append(text)

    def request_approval(self, approval_id: int, text: str) -> int | None:
        self.approvals.append((approval_id, text))
        return 1000 + approval_id

    def poll(self) -> list[Command]:
        self.polls += 1
        commands, self.commands = self.commands, []
        return commands

    def script(self, kind: str, approval_id: int | None = None, text: str = "", message_id: int | None = None) -> None:
        if message_id is None and approval_id is not None:
            message_id = 1000 + approval_id  # the id request_approval returned for this approval
        self.commands.append(Command(kind, approval_id, "42", text or f"{kind}:{approval_id}", message_id))

    def said(self, fragment: str) -> bool:
        return any(fragment in text for text in self.sent)


class Scripted(Strategy):
    """Enters and exits on listed bar dates ("2026-09-28" for every symbol, "QQQ:2026-09-28" for
    one); the stop is `stop_offset` below the close."""

    name: ClassVar[str] = "trend"
    title: ClassVar[str] = "Scripted"
    default_params: ClassVar[dict[str, Any]] = {}
    param_grid: ClassVar[list[dict[str, Any]]] = [{}]
    entries: ClassVar[set[str]] = set()
    exits: ClassVar[set[str]] = set()
    stop_offset: ClassVar[float] = 5.0
    trail_offset: ClassVar[float | None] = None
    seen: ClassVar[list[PositionState]] = []

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        return bars.copy()

    def _signal(self, df: pd.DataFrame, i: int, kind: SignalKind) -> Signal:
        close = float(df["close"].iat[i])
        return Signal(
            ts=bar_close_ts(self.symbol, df.index[i].date()),
            symbol=self.symbol,
            strategy=self.name,
            kind=kind,
            reason=f"scripted {kind.value}",
            price=close,
            stop_price=close - self.stop_offset if kind is SignalKind.ENTRY else None,
            features={"close": close},
        )

    def _listed(self, df: pd.DataFrame, i: int, days: set[str]) -> bool:
        day = df.index[i].date().isoformat()
        return day in days or f"{self.symbol}:{day}" in days

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        return self._signal(df, i, SignalKind.ENTRY) if self._listed(df, i, self.entries) else None

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        self.seen.append(position)
        return self._signal(df, i, SignalKind.EXIT) if self._listed(df, i, self.exits) else None

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        return None if self.trail_offset is None else position.highest_close - self.trail_offset

    def describe(self) -> dict[str, str]:
        return {"entry": "", "exit": "", "stop_loss": "", "take_profit": "", "timeframe": ""}


def flat_bars(end: date, n: int = 40, price: float = 100.0, crypto: bool = False) -> pd.DataFrame:
    if crypto:
        idx = pd.date_range(end=pd.Timestamp(end), periods=n, freq="D", name="date")
    else:
        idx = pd.bdate_range(end=pd.Timestamp(end), periods=n, name="date")
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1e6}, index=idx
    ).astype(float)


class Harness:
    def __init__(self, tmp_path: Path, cfg: StrategyConfig, jev_client: Any = None, **settings_env: str) -> None:
        env = {"BOT_DATA_DIR": str(tmp_path / "var"), **settings_env}
        self.settings = load_settings(env_file=None, environ=env)
        self.cfg = cfg
        self.clock = Clock(ny(MON, 16, 25))
        self.store = Store(self.settings.db_path, clock=self.clock)
        self.kill = KillSwitch(self.settings, self.store)
        self.risk = RiskManager(cfg.risk, self.store, self.kill)
        self.broker = SimBroker(cash=100_000.0, clock=self.clock)
        self.broker.market_open = False
        self.notifier = RecordingNotifier()
        self.gateway = OrderGateway(self.broker, self.risk, self.kill, self.store, self.settings, self.notifier)
        self.jev_client = FakeJevClient() if jev_client is None else jev_client
        self.gate = JevGate(cfg.jev, self.jev_client)
        self.bars: dict[str, pd.DataFrame] = {"SPY": flat_bars(THU + timedelta(days=1))}
        self.runner = self.new_runner()

    def new_runner(self) -> Runner:
        return Runner(
            self.settings, self.cfg, self.broker, self.gateway, self.risk, self.kill, self.gate,
            self.notifier, self.store, clock=self.clock, bars_source=self.source,
        )  # fmt: skip

    def source(self, symbol: str, start: date, end: date | None = None) -> pd.DataFrame:
        bars = self.bars[symbol]
        closed = [bar_close_ts(symbol, d.date()) <= self.clock() for d in bars.index]
        return bars[closed].loc[pd.Timestamp(start) :]

    def set_close(self, symbol: str, day: date, close: float, low: float | None = None) -> None:
        bars = self.bars[symbol]
        ts = pd.Timestamp(day)
        bars.loc[ts, ["close", "high"]] = [close, close + 1]
        bars.loc[ts, "low"] = close - 1 if low is None else low

    def tick(self, when: datetime, price: float | None = None, market_open: bool | None = None,
             symbol: str = "SPY") -> list[str]:  # fmt: skip
        self.clock.now = when
        if price is not None:
            self.broker.set_price(symbol, price)
        if market_open is not None:
            self.broker.market_open = market_open
        return self.runner.tick()

    def after_close(self, day: date, price: float = 100.0, next_open: date | None = None) -> list[str]:
        """16:25 New York on `day`: the day's bar is ready and the next session opens tomorrow."""
        nxt = next_open or day + timedelta(days=1)
        self.broker.next_open_at = ny(nxt, 9, 30)
        return self.tick(ny(day, 16, 25), price=price, market_open=False)

    def entry_signal_row(self) -> dict[str, Any]:
        rows = [r for r in self.store.signals() if r["kind"] == "entry"]
        assert len(rows) == 1, rows
        return rows[0]

    def open_position(self, entry_day: date = MON, fill: float = 101.0) -> PositionState:
        """Signal on `entry_day`'s close, filled at the next day's 09:31 at `fill`."""
        nxt = entry_day + timedelta(days=1)
        assert self.after_close(entry_day) == []
        assert self.tick(ny(nxt, 9, 31), price=fill, market_open=True) == []
        assert self.tick(ny(nxt, 9, 32)) == []
        return self.store.get_positions()["SPY"]


@pytest.fixture
def scripted(monkeypatch):
    cls = type("ScriptedTrend", (Scripted,), {"entries": {"2026-09-28"}, "exits": set(), "seen": []})
    monkeypatch.setitem(REGISTRY, "trend", cls)
    return cls


@pytest.fixture
def harness(tmp_path, scripted):
    return Harness(tmp_path, make_cfg())


@pytest.fixture(autouse=True)
def no_alpaca_env(monkeypatch):
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TYPESAFE_API_KEY", "TELEGRAM_BOT_TOKEN", "KILL_SWITCH"):
        monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- timing helpers


def test_bar_readiness_and_bar_days():
    # Stocks: the day's bar is usable from 16:20 New York, across DST.
    assert latest_closed_bar_day("SPY", ny(TUE, 16, 19)) == MON
    assert latest_closed_bar_day("SPY", ny(TUE, 16, 20)) == TUE
    assert latest_closed_bar_day("SPY", ny(date(2026, 11, 2), 16, 20)) == date(2026, 11, 2)  # first day of EST
    # Crypto: the UTC day closes at 00:00 and is usable from 00:05.
    assert latest_closed_bar_day("BTC/USD", datetime(2026, 9, 30, 0, 4, tzinfo=UTC)) == date(2026, 9, 28)
    assert latest_closed_bar_day("BTC/USD", datetime(2026, 9, 30, 0, 5, tzinfo=UTC)) == date(2026, 9, 29)
    assert bar_day("BTC/USD", datetime(2026, 9, 29, 23, 59, tzinfo=UTC)) == date(2026, 9, 29)
    assert bar_day("SPY", datetime(2026, 9, 30, 2, 0, tzinfo=UTC)) == TUE  # 22:00 New York


def test_gate_labels():
    from bot.jev import JevGateDecision

    def decision(**kw: Any) -> JevGateDecision:
        base = dict(passed=True, answers=(), model="jev", latency_ms=1.0, input_tokens=1, cost_usd=0.0)
        return JevGateDecision(**{**base, **kw})

    assert gate_label("off", decision(model=None)) == "off"
    assert gate_label("shadow", decision(shadow=True, error="boom")) == "shadow"
    assert gate_label("gate", decision(passed=False, error="timeout")) == "error"
    assert gate_label("gate", decision(passed=False)) == "vetoed"
    assert gate_label("gate", decision(model=None)) == "n/a"
    assert gate_label("gate", decision()) == "passed"


# --------------------------------------------------------------------------- entry flow


def test_full_entry_flow(harness):
    h = harness
    assert h.after_close(MON) == []
    signal = h.entry_signal_row()
    assert (signal["status"], signal["gate"], signal["bar_date"]) == ("queued", "passed", "2026-09-28")
    assert len(h.jev_client.calls) == 1 and h.store.jev_stats()["n"] == 1
    (item,) = load_queue(h.store)
    assert item.qty == pytest.approx(20)  # 1% of $10,000 over a $5 stop distance
    assert parse_ts(item.window_open) == ny(TUE, 9, 31)
    assert h.store.kv_get(last_bar_key("SPY")) == "2026-09-28"
    assert h.store.kv_get(HEARTBEAT_KEY) is not None

    assert h.tick(ny(TUE, 9, 30), price=101.0, market_open=True) == []
    assert h.broker.submitted == []  # the window opens a minute after the open

    assert h.tick(ny(TUE, 9, 31)) == []
    (sent,) = h.broker.submitted
    assert sent.client_order_id == make_client_order_id(OrderPurpose.ENTRY, "SPY", "trend|2026-09-28|0")
    assert (sent.side, sent.qty, sent.purpose) == (Side.BUY, 20, OrderPurpose.ENTRY)
    assert h.store.get_signal(signal["id"])["status"] == "submitted"
    assert load_queue(h.store) == []

    assert h.tick(ny(TUE, 9, 32)) == []
    position = h.store.get_positions()["SPY"]
    assert (position.qty, position.entry_price, position.stop_price) == (20, 101.0, 95.0)
    assert (position.highest_close, position.bars_held, position.strategy) == (101.0, 0, "trend")
    assert h.store.get_signal(signal["id"])["status"] == "filled"
    assert h.notifier.said("Filled: BUY 20 SPY @ 101.00 (entry, trend)")
    assert load_tracked(h.store) == {}

    # The fill bar's close counts as the first bar held, as in the backtest.
    h.set_close("SPY", TUE, 104.0)
    assert h.after_close(TUE, price=104.0) == []
    position = h.store.get_positions()["SPY"]
    assert (position.bars_held, position.highest_close) == (1, 104.0)
    assert len(h.store.signals()) == 1  # no entry is evaluated while long


def test_jev_veto_blocks_entry(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg(), jev_client=FakeJevClient({"buying_pressure": 0.2}))
    assert h.after_close(MON) == []
    signal = h.entry_signal_row()
    assert (signal["status"], signal["gate"]) == ("blocked", "vetoed")
    assert "buying_pressure" in signal["risk_reason"]
    assert load_queue(h.store) == [] and h.store.pending_approvals() == []
    assert h.notifier.said("blocked. Jev vetoed")


@pytest.mark.parametrize("client", [FakeJevClient(error=RuntimeError("jev is down")), None])
def test_jev_error_or_missing_client_blocks_entry(tmp_path, scripted, client):
    h = Harness(tmp_path, make_cfg(), jev_client=client or FakeJevClient())
    if client is None:
        h.gate = JevGate(h.cfg.jev, None)
        h.runner = h.new_runner()
    assert h.after_close(MON) == []
    signal = h.entry_signal_row()
    assert (signal["status"], signal["gate"]) == ("blocked", "error")
    assert load_queue(h.store) == []


def test_jev_off_and_shadow_do_not_block(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg(jev_mode="off"))
    h.after_close(MON)
    assert h.entry_signal_row()["gate"] == "off" and h.jev_client.calls == []

    h2 = Harness(tmp_path / "b", make_cfg(jev_mode="shadow"), jev_client=FakeJevClient({"buying_pressure": 0.1}))
    h2.after_close(MON)
    signal = h2.entry_signal_row()
    assert (signal["gate"], signal["status"]) == ("shadow", "queued")


# --------------------------------------------------------------------------- approvals


@pytest.fixture
def approvals(tmp_path, scripted):
    return Harness(tmp_path, make_cfg(approval_threshold_usd=1000))


def test_approval_requested_then_approved_executes(approvals):
    h = approvals
    assert h.after_close(MON) == []
    signal = h.entry_signal_row()
    assert signal["status"] == "awaiting_approval"
    ((approval_id, text),) = h.notifier.approvals
    assert "BUY 20 SPY" in text and "Expires 2026-09-29 10:00 NY" in text
    approval = h.store.get_approval(approval_id)
    assert (approval["status"], approval["message_id"]) == ("pending", 1000 + approval_id)
    assert parse_ts(approval["expires_ts"]) == ny(TUE, 10, 0)  # 30 minutes after the next open

    h.clock.now = ny(MON, 20, 0)
    h.broker.set_price("SPY", 100.5)
    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()  # the fast loop
    assert h.store.get_approval(approval_id)["status"] == "approved"
    assert h.store.get_signal(signal["id"])["status"] == "queued"
    assert h.broker.submitted == []  # not before its window

    assert h.tick(ny(TUE, 9, 31), price=100.8, market_open=True) == []
    assert [o.qty for o in h.broker.submitted] == [20]
    h.tick(ny(TUE, 9, 32))
    assert h.store.get_positions()["SPY"].entry_price == 100.8


def test_approval_rejected(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.notifier.script("reject", approval_id)
    h.runner.handle_commands()
    assert h.entry_signal_row()["status"] == "rejected"
    assert load_queue(h.store) == []
    h.tick(ny(TUE, 9, 31), price=100.0, market_open=True)
    assert h.broker.submitted == []
    assert h.notifier.said(f"Rejected #{approval_id}")


def test_approval_expires_and_late_approval_is_ignored(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    assert h.tick(ny(TUE, 9, 59), price=100.0, market_open=True) == []
    assert h.store.get_approval(approval_id)["status"] == "pending"
    assert h.tick(ny(TUE, 10, 0)) == []
    assert h.store.get_approval(approval_id)["status"] == "expired"
    assert h.entry_signal_row()["status"] == "expired"
    assert h.notifier.said(f"Approval #{approval_id} expired")

    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()
    assert h.store.get_approval(approval_id)["status"] == "expired"
    assert h.notifier.said(f"Approval #{approval_id} was already expired; nothing was sent.")
    h.tick(ny(TUE, 10, 1))
    assert h.broker.submitted == []


def test_approval_clicked_after_expiry_before_the_sweep(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.clock.now = ny(TUE, 10, 0)
    h.broker.set_price("SPY", 100.0)
    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()
    assert h.store.get_approval(approval_id)["status"] == "expired"
    assert h.broker.submitted == []


def test_approved_entry_skipped_on_price_drift(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.clock.now = ny(MON, 21, 0)
    h.broker.set_price("SPY", 103.0)  # 3% above the signal's 100 close; the limit is 2%
    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()
    signal = h.entry_signal_row()
    assert signal["status"] == "skipped" and "3.00%" in signal["risk_reason"]
    assert h.store.get_approval(approval_id)["status"] == "approved"
    h.tick(ny(TUE, 9, 31), price=100.0, market_open=True)
    assert h.broker.submitted == []


def test_approved_entry_rechecks_drift_at_execution(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.clock.now = ny(MON, 21, 0)
    h.broker.set_price("SPY", 100.0)
    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()
    h.tick(ny(TUE, 9, 31), price=97.0, market_open=True)  # gapped down 3% overnight
    assert h.broker.submitted == []
    assert h.entry_signal_row()["status"] == "skipped"


# --------------------------------------------------------------------------- exits


def test_stop_hit_exits_and_records_trade(harness, scripted):
    h = harness
    h.open_position()
    assert h.tick(ny(TUE, 11, 0), price=96.0) == []
    assert h.broker.submitted[-1].purpose is OrderPurpose.ENTRY  # above the stop: nothing
    assert h.tick(ny(TUE, 11, 1), price=94.0) == []
    stop_order = h.broker.submitted[-1]
    assert (stop_order.purpose, stop_order.side, stop_order.qty) == (OrderPurpose.STOP, Side.SELL, 20)
    assert h.notifier.said("Stop hit on SPY")
    assert h.tick(ny(TUE, 11, 2)) == []
    assert h.store.get_positions() == {}
    (trade,) = h.store.trades()
    assert trade["exit_reason"] == "stop"
    assert trade["pnl"] == pytest.approx(20 * (94 - 101))
    assert trade["pnl_pct"] == pytest.approx((94 - 101) / 101)  # a fraction, not percent
    signal = h.entry_signal_row()
    assert signal["outcome_pnl"] == pytest.approx(-140) and signal["outcome_pnl_pct"] == pytest.approx(-7 / 101)
    assert h.notifier.said("Filled: SELL 20 SPY @ 94.00 (stop, trend)")
    # No entry is evaluated on a bar during which an exit filled (engine parity).
    scripted.entries = {"2026-09-29"}
    h.after_close(TUE)
    assert [r["bar_date"] for r in h.store.signals() if r["kind"] == "entry"] == ["2026-09-28"]


def test_a_filled_but_unreconciled_stop_is_not_sent_again(harness):
    h = harness
    h.open_position()
    h.broker.set_price("SPY", 94.0)
    h.clock.now = ny(TUE, 11, 0)
    h.runner._check_stops(h.clock.now)
    h.runner._check_stops(h.clock.now)  # no reconcile in between: the fill is not applied yet
    assert [o.purpose for o in h.broker.submitted] == [OrderPurpose.ENTRY, OrderPurpose.STOP]
    assert "SPY" in h.store.get_positions()
    h.tick(ny(TUE, 11, 1))
    assert h.store.trades()[0]["exit_reason"] == "stop"


def test_stock_stops_wait_for_the_open(harness):
    h = harness
    h.open_position()
    h.tick(ny(TUE, 20, 0), price=90.0, market_open=False)  # after hours: not checked
    assert all(o.purpose is not OrderPurpose.STOP for o in h.broker.submitted)
    h.tick(ny(WED, 9, 30), price=90.0, market_open=True)  # first tick of the session catches the gap
    assert h.broker.submitted[-1].purpose is OrderPurpose.STOP


def test_take_profit(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg())
    h.open_position()
    h.store.put_position(PositionState(**{**h.store.open_positions()[0], "take_profit": 110.0}))
    h.tick(ny(TUE, 12, 0), price=111.0)
    assert h.broker.submitted[-1].purpose is OrderPurpose.TAKE_PROFIT
    h.tick(ny(TUE, 12, 1))
    assert h.store.trades()[0]["exit_reason"] == "take_profit"


def test_exit_signal_queues_and_sells_at_next_open(harness, scripted):
    h = harness
    scripted.exits = {"2026-09-29"}
    h.open_position()
    h.set_close("SPY", TUE, 103.0)
    assert h.after_close(TUE, price=103.0) == []
    exits = [r for r in h.store.signals() if r["kind"] == "exit"]
    assert [(r["status"], r["gate"]) for r in exits] == [("queued", "n/a")]
    assert h.broker.submitted[-1].purpose is OrderPurpose.ENTRY
    assert h.tick(ny(WED, 9, 31), price=104.0, market_open=True) == []
    sell = h.broker.submitted[-1]
    assert (sell.purpose, sell.qty) == (OrderPurpose.EXIT, 20)
    assert sell.client_order_id == make_client_order_id(OrderPurpose.EXIT, "SPY", "trend|2026-09-29|0")
    h.tick(ny(WED, 9, 32))
    (trade,) = h.store.trades()
    assert (trade["exit_reason"], trade["exit_price"]) == ("signal", 104.0)
    assert h.store.get_signal(exits[0]["id"])["status"] == "filled"
    h.tick(ny(WED, 9, 33))
    assert load_queue(h.store) == []


def test_queued_exits_go_before_queued_entries(tmp_path, scripted):
    assets = {"QQQ": {"strategy": "trend"}, "SPY": {"strategy": "trend"}}  # QQQ's bar is processed first
    scripted.entries = {"SPY:2026-09-28", "QQQ:2026-09-29"}
    scripted.exits = {"SPY:2026-09-29"}
    h = Harness(tmp_path, make_cfg(assets=assets))
    h.bars["QQQ"] = flat_bars(THU + timedelta(days=1))
    h.broker.set_price("QQQ", 100.0)
    h.open_position()
    h.after_close(TUE)
    assert [q.kind for q in load_queue(h.store)] == ["entry", "exit"]
    h.broker.set_price("QQQ", 100.0)
    h.tick(ny(WED, 9, 31), price=100.0, market_open=True)
    assert [(o.symbol, o.purpose) for o in h.broker.submitted[-2:]] == [
        ("SPY", OrderPurpose.EXIT), ("QQQ", OrderPurpose.ENTRY),
    ]  # fmt: skip


def test_trailing_stop_ratchets_up_and_persists(harness, scripted):
    h = harness
    scripted.trail_offset = 3.0
    h.open_position()
    h.set_close("SPY", TUE, 110.0)
    h.after_close(TUE, price=110.0)
    position = h.store.get_positions()["SPY"]
    assert (position.stop_price, position.highest_close, position.bars_held) == (107.0, 110.0, 1)
    # A new process reads the ratcheted stop; a lower close never lowers it.
    h.runner = h.new_runner()
    scripted.trail_offset = 10.0
    h.set_close("SPY", WED, 105.0)
    h.after_close(WED, price=105.0)
    position = h.store.get_positions()["SPY"]
    assert (position.stop_price, position.highest_close, position.bars_held) == (107.0, 110.0, 2)
    # The raised stop applies from the next session.
    h.tick(ny(THU, 9, 31), price=106.5, market_open=True)
    assert h.broker.submitted[-1].purpose is OrderPurpose.STOP


def test_trailing_stop_uses_updated_state_before_exit(harness, scripted):
    h = harness
    scripted.trail_offset = 3.0
    h.open_position()
    h.set_close("SPY", TUE, 108.0)
    h.after_close(TUE, price=108.0)
    seen = scripted.seen[-1]
    assert (seen.bars_held, seen.highest_close, seen.stop_price) == (1, 108.0, 105.0)


def test_no_new_entry_while_one_is_still_pending(tmp_path, scripted):
    assets = {"BTC/USD": {"strategy": "trend"}}
    h = Harness(tmp_path, make_cfg(assets=assets, approval_threshold_usd=1000, approval_timeout_minutes=3000))
    h.bars["BTC/USD"] = flat_bars(date(2026, 10, 2), crypto=True, price=60_000.0)
    scripted.entries = {"2026-09-28", "2026-09-29"}
    scripted.stop_offset = 3_000.0
    h.broker.set_price("BTC/USD", 60_000.0)
    h.tick(datetime(2026, 9, 29, 0, 5, tzinfo=UTC))
    assert h.entry_signal_row()["status"] == "awaiting_approval"
    h.tick(datetime(2026, 9, 30, 0, 5, tzinfo=UTC))  # the approval is still open a day later
    assert [r["bar_date"] for r in h.store.signals()] == ["2026-09-28"]
    assert h.store.kv_get(last_bar_key("BTC/USD")) == "2026-09-29"


def test_orders_today_count_restarts_after_a_resume(harness):
    h = harness
    for n in range(10):  # the cap was reached earlier today, then the operator resumed
        intent = OrderIntent("SPY", Side.BUY, 1, 100.0, OrderPurpose.ENTRY, "earlier", f"entry-earlier-{n}")
        h.store.upsert_order(
            intent, OrderResult(intent.client_order_id, f"b{n}", "filled", 1, 100.0), ts=ny(MON, 10, 0)
        )
    h.store.kv_set(RESET_TS_KEY, ny(MON, 12, 0).isoformat())
    h.after_close(MON)
    assert h.entry_signal_row()["status"] == "queued"
    assert not h.kill.is_tripped()


# --------------------------------------------------------------------------- kill switch


def test_kill_command_flattens_until_flat_and_blocks_entries(harness, scripted):
    h = harness
    h.open_position()
    h.clock.now = ny(TUE, 12, 0)
    h.broker.set_price("SPY", 99.0)
    h.notifier.script("kill", text="/kill spreads look wrong")
    h.runner.handle_commands()
    assert h.kill.is_tripped() and h.kill.status()["source"] == "telegram"
    assert h.kill.status()["reason"] == "spreads look wrong"
    assert h.broker.positions() == {}  # flattened at once
    assert h.notifier.said("Kill switch tripped: spreads look wrong. Flattening now.")

    assert h.tick(ny(TUE, 12, 1)) == []
    (trade,) = h.store.trades()
    assert (trade["exit_reason"], trade["exit_price"]) == ("kill", 99.0)
    assert h.store.get_positions() == {}
    flat_alerts = [t for t in h.notifier.sent if t.startswith("Kill switch: the account is flat")]
    assert len(flat_alerts) == 1
    h.tick(ny(TUE, 12, 2))
    assert len([t for t in h.notifier.sent if t.startswith("Kill switch: the account is flat")]) == 1

    # While tripped no bar is evaluated and nothing new is sent.
    scripted.entries = {"2026-09-29"}
    sent_before = len(h.broker.submitted)
    h.after_close(TUE)
    h.tick(ny(WED, 9, 31), market_open=True)
    assert len(h.broker.submitted) == sent_before
    assert [r["bar_date"] for r in h.store.signals()] == ["2026-09-28"]
    assert (
        h.gateway.submit(OrderIntent("SPY", Side.BUY, 1, 100.0, OrderPurpose.ENTRY, "x", "entry-manual-1")).status
        == "refused"
    )

    # After a resume the latest bar is evaluated (missed bars are skipped).
    h.kill.reset(confirm=True)
    scripted.entries = {"2026-09-30"}
    h.after_close(WED)
    assert sorted(r["bar_date"] for r in h.store.signals()) == ["2026-09-28", "2026-09-30"]


def test_kill_switch_repeats_flatten_while_positions_remain(harness):
    h = harness
    h.open_position()
    h.kill.trip("drill", "cli")
    h.broker.fail_next = 1  # the first close fails, as when Alpaca still holds the qty
    h.tick(ny(TUE, 12, 0), price=100.0)
    assert h.broker.positions() != {}
    assert h.risk.consecutive_errors >= 1
    h.tick(ny(TUE, 12, 1))
    assert h.broker.positions() == {}
    h.tick(ny(TUE, 12, 2))
    assert h.store.get_positions() == {} and h.store.trades()[0]["exit_reason"] == "kill"
    assert h.broker.cancel_all_calls == 2  # not repeated once flat


def test_kill_expires_pending_approvals_and_queued_entries(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.kill.trip("manual", "cli")
    h.tick(ny(MON, 16, 30))
    assert h.store.get_approval(approval_id)["status"] == "expired"
    assert h.entry_signal_row()["status"] == "expired"
    assert load_queue(h.store) == []


def test_approvals_are_not_accepted_while_tripped(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg(approval_threshold_usd=1000, kill_switch_flatten=False))
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.kill.trip("stop", "cli")
    h.clock.now = ny(MON, 20, 0)
    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()
    assert h.store.get_approval(approval_id)["status"] == "pending"
    assert h.notifier.said(f"Approval #{approval_id} not accepted: the kill switch is tripped.")


def test_exits_still_run_when_kill_switch_does_not_flatten(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg(kill_switch_flatten=False))
    h.open_position()
    h.kill.trip("hold positions", "cli")
    h.tick(ny(TUE, 11, 0), price=94.0)
    assert h.broker.submitted[-1].purpose is OrderPurpose.STOP  # exits are never gated
    h.tick(ny(TUE, 11, 1))
    assert h.store.trades()[0]["exit_reason"] == "stop"
    scripted.entries = {"2026-09-30"}
    h.after_close(WED)
    signal = [r for r in h.store.signals() if r["bar_date"] == "2026-09-30"][0]
    assert (signal["status"], signal["risk_reason"]) == ("blocked", "kill switch is tripped")


def test_cli_kill_orders_are_adopted_and_recorded(harness):
    h = harness
    h.open_position()
    h.kill.trip("cli", "cli")
    h.broker.set_price("SPY", 98.0)
    results = h.gateway.flatten_all("CLI kill: test")  # another process's flatten
    hand_off_orders(h.store, results)
    h.tick(ny(TUE, 12, 0))
    (trade,) = h.store.trades()
    assert (trade["exit_reason"], trade["exit_price"]) == ("kill", 98.0)


# --------------------------------------------------------------------------- risk


def test_daily_loss_limit_blocks_entries_not_exits(tmp_path, scripted):
    assets = {"SPY": {"strategy": "trend"}, "QQQ": {"strategy": "trend"}}
    scripted.entries = {"SPY:2026-09-28"}
    h = Harness(tmp_path, make_cfg(assets=assets))
    h.bars["QQQ"] = flat_bars(THU + timedelta(days=1))
    h.broker.set_price("QQQ", 100.0)
    h.open_position()  # SPY; QQQ has no signal on Monday
    h.store.kv_set(DAILY_LOSS_KEY, "2026-09-29")
    h.tick(ny(TUE, 11, 0), price=94.0)
    assert h.broker.submitted[-1].purpose is OrderPurpose.STOP
    h.tick(ny(TUE, 11, 1))
    assert h.store.trades()[0]["exit_reason"] == "stop"
    scripted.entries = {"2026-09-29"}
    h.after_close(TUE)  # SPY exited today, so only QQQ is evaluated
    qqq = [r for r in h.store.signals() if r["symbol"] == "QQQ"][0]
    assert qqq["status"] == "blocked" and "daily loss" in qqq["risk_reason"]
    assert [r["symbol"] for r in h.store.signals() if r["bar_date"] == "2026-09-29"] == ["QQQ"]


def test_entry_rechecked_by_risk_at_execution(harness):
    h = harness
    h.after_close(MON)
    h.store.kv_set(DAILY_LOSS_KEY, "2026-09-29")  # the limit was hit before the window opened
    h.tick(ny(TUE, 9, 31), price=100.0, market_open=True)
    assert h.broker.submitted == []
    signal = h.entry_signal_row()
    assert signal["status"] == "blocked" and "daily loss" in signal["risk_reason"]


def test_kill_between_signal_and_window_blocks_the_entry(harness):
    h = harness
    h.after_close(MON)
    h.kill.trip("late trip", "cli")
    h.tick(ny(TUE, 9, 31), price=100.0, market_open=True)
    assert h.broker.submitted == []
    signal = h.entry_signal_row()
    assert signal["status"] == "blocked" and "late trip" in signal["risk_reason"]


def test_stock_entry_expires_when_its_window_is_missed(harness):
    h = harness
    h.after_close(MON)
    h.tick(ny(TUE, 10, 1), price=100.0, market_open=True)  # the bot was down through the window
    assert h.broker.submitted == []
    signal = h.entry_signal_row()
    assert signal["status"] == "expired" and "window" in signal["risk_reason"]


def test_drawdown_kill_flattens_in_the_same_tick(harness):
    h = harness
    h.open_position()  # 20 SPY at 101 with a 95 stop
    h.store.put_position(PositionState(**{**h.store.open_positions()[0], "stop_price": 1.0}))
    h.tick(ny(TUE, 12, 0), price=20.0, market_open=True)  # bot equity 10,000 - 1,620
    assert h.kill.is_tripped()
    assert h.broker.positions() == {}


# --------------------------------------------------------------------------- restart safety


def test_restart_does_not_duplicate_signals_or_orders(harness):
    h = harness
    h.after_close(MON)
    queue_before = h.store.kv_get(QUEUE_KEY)
    # A restart during the same evening, even one that lost the processed-bar marker.
    h.store.kv_set(last_bar_key("SPY"), "2026-09-25")
    h.runner = h.new_runner()
    assert h.tick(ny(MON, 16, 40)) == []
    assert len(h.store.signals()) == 1 and len(h.jev_client.calls) == 1
    assert h.store.kv_get(QUEUE_KEY) == queue_before

    h.runner = h.new_runner()
    h.tick(ny(TUE, 9, 31), price=101.0, market_open=True)
    h.tick(ny(TUE, 9, 32))
    # Crash after sending but before the queue was updated: the item comes back.
    h.store.kv_set(QUEUE_KEY, queue_before)
    h.store.update_signal(h.entry_signal_row()["id"], status="queued")
    h.runner = h.new_runner()
    h.tick(ny(TUE, 9, 33))
    assert len(h.broker.submitted) == 1
    assert load_queue(h.store) == []
    assert h.store.get_positions()["SPY"].qty == 20
    assert h.entry_signal_row()["status"] == "filled"


def test_resume_a_signal_left_new_by_a_crash(harness):
    h = harness
    signal = Signal(bar_close_ts("SPY", MON), "SPY", "trend", SignalKind.ENTRY, "scripted entry", 100.0, 95.0)
    signal_id = h.store.insert_signal(signal, MON)  # the crash came right after this
    assert h.after_close(MON) == []
    row = h.entry_signal_row()
    assert (row["id"], row["status"], row["gate"]) == (signal_id, "queued", "passed")
    assert len(load_queue(h.store)) == 1


def test_restart_between_fill_and_reconcile(harness):
    h = harness
    h.after_close(MON)
    h.tick(ny(TUE, 9, 31), price=101.0, market_open=True)
    tracked = h.store.kv_get("runner:orders")
    h.runner = h.new_runner()
    h.tick(ny(TUE, 9, 32))
    h.store.kv_set("runner:orders", tracked)  # the applied marker was lost: the fill replays
    h.runner = h.new_runner()
    h.tick(ny(TUE, 9, 33))
    assert h.store.get_positions()["SPY"].qty == 20  # entries are set absolutely, not added
    assert all(e["kind"] != "position_drift" for e in h.store.recent_events())


def test_restart_between_trade_and_position_delete(harness):
    h = harness
    position = h.open_position()
    h.tick(ny(TUE, 11, 0), price=94.0)  # stop sent and filled
    tracked = h.store.kv_get("runner:orders")
    h.tick(ny(TUE, 11, 1))
    assert len(h.store.trades()) == 1
    # Crash after the trade was written but before the position was deleted and the fill marked.
    h.store.put_position(position)
    h.store.kv_set("runner:orders", tracked)
    h.runner = h.new_runner()
    h.tick(ny(TUE, 11, 2))
    assert len(h.store.trades()) == 1
    assert h.store.get_positions() == {}


def test_missed_bars_process_only_the_latest(harness, scripted, caplog):
    h = harness
    scripted.entries = {"2026-09-24", "2026-09-28"}
    h.store.kv_set(last_bar_key("SPY"), "2026-09-23")
    with caplog.at_level(logging.WARNING, logger="bot.runner"):
        assert h.after_close(MON) == []
    assert [r["bar_date"] for r in h.store.signals()] == ["2026-09-28"]
    assert "2 bar(s) missed" in caplog.text


def test_one_bar_per_symbol_per_tick_and_no_refetch(harness):
    h = harness
    calls: list[str] = []
    source = h.source

    def counting(symbol: str, start: date, end: date | None = None) -> pd.DataFrame:
        calls.append(symbol)
        return source(symbol, start, end)

    h.runner.bars_source = counting
    h.after_close(MON)
    h.tick(ny(MON, 16, 30))
    h.tick(ny(MON, 23, 0))
    assert calls == ["SPY"]  # processed once; later ticks know the bar is done


def test_crash_in_one_step_does_not_stop_the_others(harness):
    h = harness

    def boom(now: datetime) -> None:
        raise RuntimeError("price feed exploded")

    h.runner._check_stops = boom
    assert h.after_close(MON) == ["stops"]
    assert h.entry_signal_row()["status"] == "queued"  # the bar step still ran
    assert h.store.kv_get(EQUITY_TS_KEY) is not None  # and bookkeeping
    assert h.risk.consecutive_errors == 1
    assert h.notifier.said("Bot error in stops: RuntimeError: price feed exploded")
    del h.runner._check_stops
    assert h.tick(ny(MON, 16, 30)) == []
    assert h.risk.consecutive_errors == 0  # a clean tick resets the count


def test_repeated_errors_trip_the_kill_switch_and_alert_once(harness):
    h = harness

    def boom(now: datetime) -> None:
        raise RuntimeError("same failure")

    h.runner._check_stops = boom
    for minute in range(5):
        h.tick(ny(MON, 16, 25 + minute))
    assert h.kill.is_tripped()
    assert len([t for t in h.notifier.sent if "same failure" in t]) == 1


def test_bad_bars_are_retried_later(harness):
    h = harness

    def broken(symbol: str, start: date, end: date | None = None) -> pd.DataFrame:
        raise ConnectionError("data API down")

    h.runner.bars_source = broken
    assert h.after_close(MON) == []  # counted per symbol, not as a failed step
    assert h.risk.consecutive_errors == 1
    h.runner.bars_source = h.source
    h.tick(ny(MON, 16, 26))
    assert h.store.signals() == []  # waits BAR_RETRY before fetching again
    h.tick(ny(MON, 16, 31))
    assert h.entry_signal_row()["status"] == "queued"


# --------------------------------------------------------------------------- reconciliation


def test_drift_drops_unknown_bot_position_and_alerts_on_foreign_one(harness):
    h = harness
    h.store.put_position(
        PositionState("SPY", "trend", 5.0, 100.0, ny(MON, 9, 31), stop_price=90.0, highest_close=100.0)
    )
    h.broker.set_price("QQQ", 50.0)
    h.broker.submit(OrderIntent("QQQ", Side.BUY, 2, 50.0, OrderPurpose.ENTRY, "manual", "manual-qqq"))
    h.tick(ny(MON, 12, 0), price=100.0)
    assert h.store.get_positions() == {}
    assert h.notifier.said("dropped the bot's SPY position")
    assert h.notifier.said("holds 2 QQQ that the bot did not open. The bot won't trade it, but the kill switch will sell it.")
    h.tick(ny(MON, 12, 1))
    assert len([t for t in h.notifier.sent if "QQQ that the bot did not open" in t]) == 1


def test_partial_fill_then_cancel_uses_filled_qty(harness):
    h = harness
    h.after_close(MON)
    h.broker.fill_ratio = 0.5
    h.tick(ny(TUE, 9, 31), price=100.0, market_open=True)
    h.tick(ny(TUE, 9, 32))
    assert h.store.get_positions()["SPY"].qty == pytest.approx(10)
    assert load_tracked(h.store) != {}  # still working
    h.broker.cancel_all()  # the day order ends 'canceled' carrying its partial fill
    h.tick(ny(TUE, 9, 33))
    assert h.store.get_positions()["SPY"].qty == pytest.approx(10)
    assert load_tracked(h.store) == {}
    assert h.entry_signal_row()["status"] == "filled"


class AsyncSimBroker(SimBroker):
    """Like Alpaca: an order is 'accepted' when sent and reports its fill on a later poll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.polls_until_fill = 1
        self._pending: dict[str, int] = {}

    def submit(self, intent: OrderIntent) -> OrderResult:
        filled = super().submit(intent)
        self._pending[intent.client_order_id] = self.polls_until_fill
        return replace(filled, status="accepted", filled_qty=0.0, filled_avg_price=None)

    def get_order(self, client_order_id: str) -> OrderResult | None:
        result = super().get_order(client_order_id)
        waits = self._pending.get(client_order_id, 0)
        if result is None or waits <= 0:
            return result
        self._pending[client_order_id] = waits - 1
        return replace(result, status="accepted", filled_qty=0.0, filled_avg_price=None)


def test_async_fill_is_applied_when_the_broker_reports_it(harness):
    h = harness
    h.broker = AsyncSimBroker(cash=100_000.0, clock=h.clock)
    h.broker.market_open = False
    h.gateway = OrderGateway(h.broker, h.risk, h.kill, h.store, h.settings, h.notifier)
    h.runner = h.new_runner()
    h.after_close(MON)
    h.tick(ny(TUE, 9, 31), price=101.0, market_open=True)
    assert h.store.get_order_by_client_id(h.broker.submitted[0].client_order_id)["status"] == "accepted"
    h.tick(ny(TUE, 9, 32))  # still working at the broker
    assert h.store.get_positions() == {} and len(load_tracked(h.store)) == 1
    h.tick(ny(TUE, 9, 33))
    position = h.store.get_positions()["SPY"]
    assert position.qty == 20 and position.entry_ts == ny(TUE, 9, 32)  # after the last poll that saw no fill
    assert h.entry_signal_row()["status"] == "filled"
    assert load_tracked(h.store) == {}
    # While the entry was working no second entry could be evaluated, and no drift was reported.
    assert not h.notifier.said("did not open")


def test_drift_syncs_down_to_what_the_broker_holds(harness):
    h = harness
    h.open_position()
    h.broker.submit(OrderIntent("SPY", Side.SELL, 0.05, 101.0, OrderPurpose.EXIT, "fee in kind", "fee-spy"))
    h.tick(ny(TUE, 9, 40))
    assert h.store.get_positions()["SPY"].qty == pytest.approx(19.95)
    assert any(e["kind"] == "position_drift" and e["level"] == "warning" for e in h.store.recent_events())


def test_stale_position_dropped_when_broker_has_nothing_to_sell(harness):
    h = harness
    h.open_position()
    h.broker._holdings.clear()  # sold elsewhere between reconcile and the stop check
    h.broker.set_price("SPY", 90.0)
    h.clock.now = ny(TUE, 11, 0)
    h.runner._check_stops(h.clock.now)
    assert h.store.get_positions() == {}
    assert h.notifier.said("no long position to sell")


# --------------------------------------------------------------------------- guard, crypto, bookkeeping


def test_paper_live_mismatch_blocks_everything(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg(), ALPACA_PAPER="false")
    assert h.after_close(MON) == []
    signal = h.entry_signal_row()
    assert signal["status"] == "blocked" and "live-trading guard" in signal["risk_reason"]
    assert h.jev_client.calls == []  # no Jev call for an entry that cannot trade
    assert h.notifier.said("TRADING BLOCKED")


def test_crypto_entry_executes_right_after_the_close(tmp_path, scripted):
    assets = {"BTC/USD": {"strategy": "trend"}}
    h = Harness(tmp_path, make_cfg(assets=assets))
    h.bars["BTC/USD"] = flat_bars(date(2026, 10, 2), crypto=True, price=60_000.0)
    scripted.entries = {"2026-09-29"}
    scripted.stop_offset = 3_000.0
    h.clock.now = datetime(2026, 9, 30, 0, 4, tzinfo=UTC)
    h.broker.set_price("BTC/USD", 60_100.0)
    assert h.runner.tick() == []
    assert h.store.signals() == []  # data not ready until 00:05
    h.clock.now = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
    assert h.runner.tick() == []
    (order,) = h.broker.submitted
    assert order.qty == pytest.approx(10_000 * 0.01 / 3_000)
    h.clock.now = datetime(2026, 9, 30, 0, 6, tzinfo=UTC)
    h.runner.tick()
    position = h.store.get_positions()["BTC/USD"]
    assert bar_day("BTC/USD", position.entry_ts) == date(2026, 9, 30)
    # A stop just before midnight belongs to that UTC day's bar: no entry on it.
    scripted.entries = {"2026-09-30"}
    h.clock.now = datetime(2026, 9, 30, 23, 59, 30, tzinfo=UTC)
    h.broker.set_price("BTC/USD", 56_000.0)
    h.runner.tick()
    h.clock.now = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
    h.runner.tick()
    trade = h.store.trades()[0]
    assert trade["exit_reason"] == "stop"
    assert trade["fees"] == pytest.approx(0.0025 * trade["qty"] * (60_100.0 + 56_000.0))
    assert [r["bar_date"] for r in h.store.signals()] == ["2026-09-29"]


def test_equity_snapshots_start_of_day_and_daily_report(harness):
    h = harness
    h.tick(ny(MON, 17, 0), price=100.0)
    assert len(h.store.equity_series()) == 1
    h.tick(ny(MON, 17, 10))
    assert len(h.store.equity_series()) == 1  # every 15 minutes
    h.tick(ny(MON, 17, 15))
    assert len(h.store.equity_series()) == 2
    assert h.store.kv_get(REPORT_DAY_KEY) == "2026-09-28"
    reports = [t for t in h.notifier.sent if t.startswith("# Daily report for 2026-09-28")]
    assert len(reports) == 1
    h.tick(ny(MON, 17, 16))
    assert len([t for t in h.notifier.sent if t.startswith("# Daily report")]) == 1
    assert (h.settings.reports_dir / "2026-09-28.md").exists()
    snapshot = h.store.latest_equity()
    assert snapshot["bot_equity"] == pytest.approx(10_000)


def test_status_pnl_help_and_unknown_commands(harness):
    h = harness
    h.open_position()
    h.clock.now = ny(TUE, 12, 0)
    h.broker.set_price("SPY", 103.0)
    for kind in ("status", "pnl", "help", "unknown"):
        h.notifier.script(kind)
    h.runner.handle_commands()
    status, pnl, help_text, unknown = h.notifier.sent[-4:]
    assert "Mode: PAPER (paper account)" in status and "Kill switch: off" in status
    assert "SPY: 20 @ 101.00, stop 95.00, last 103.00" in status
    assert "Unrealized: +$40.00" in pnl and "Bot equity: $10,040.00" in pnl
    assert help_text.startswith("Commands:") and unknown == help_text


def test_run_forever_ticks_on_schedule_and_polls_commands_between(harness, monkeypatch):
    h = harness
    ticks: list[float] = []
    now = [0.0]
    monkeypatch.setattr(h.runner, "tick", lambda: ticks.append(now[0]) or [])

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 130:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        h.runner.run_forever(sleep=sleep, monotonic=lambda: now[0])
    assert ticks == [0.0, 60.0, 120.0]
    assert h.notifier.polls >= 55  # every 2 seconds between ticks


def test_run_forever_survives_a_bad_iteration(harness, monkeypatch):
    h = harness
    calls = []

    def flaky() -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected")
        raise KeyboardInterrupt

    monkeypatch.setattr(h.runner, "tick", flaky)
    clock = iter(range(0, 10_000, 100))
    with pytest.raises(KeyboardInterrupt):
        h.runner.run_forever(sleep=lambda s: None, monotonic=lambda: float(next(clock)))
    assert len(calls) == 2


# --------------------------------------------------------------------------- parity with the backtest


PARITY_START = "2023-03-01"
PARITY_DAYS = 150


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("trend", {"fast": 20, "slow": 100, "atr_mult": 3}),
        ("breakout", {"entry_n": 20, "exit_n": 10, "atr_mult": 2}),
        ("meanrev", {"entry_rsi": 10, "atr_mult": 2, "max_hold": 5}),
        ("momentum", {"lookback": 60, "atr_mult": 3}),
    ],
)
def test_paper_matches_backtest_on_real_spy_bars(tmp_path, name, params):
    """150 sessions of cached SPY bars through the runner (Jev off, approvals off): entries and
    exits land on the same bars as run_backtest's trades."""
    cfg = make_cfg(assets={"SPY": {"strategy": name, "params": params}}, jev_mode="off")
    cfg = cfg.model_copy(
        update={"execution": cfg.execution.model_copy(update={"slippage_bps": {"stock": 5, "crypto": 10}})}
    )
    bars = load_daily("SPY")
    days = bars.index[bars.index >= PARITY_START][:PARITY_DAYS]
    backtest = run_backtest(bars, build(name, "SPY", params), BacktestConfig.from_strategy(cfg, "SPY"),
                            PARITY_START, days[-1].date().isoformat())  # fmt: skip
    assert len(backtest.trades) >= 4

    h = Harness(tmp_path, cfg)
    h.broker.slippage_bps = 5
    h.bars = {"SPY": bars}
    closes_at = pd.Series([bar_close_ts("SPY", d.date()) for d in bars.index], index=bars.index)
    h.runner.bars_source = lambda symbol, start, end=None: bars[(closes_at <= h.clock()).to_numpy()].loc[
        pd.Timestamp(start) :
    ]
    previous = bars.index[bars.index < PARITY_START][-1].date()
    h.store.kv_set(last_bar_key("SPY"), previous.isoformat())  # the bot was already running
    for k, ts in enumerate(days):
        day = ts.date()
        o, low, c = (float(bars.at[ts, col]) for col in ("open", "low", "close"))
        assert h.tick(ny(day, 9, 31), price=o, market_open=True) == []
        assert h.tick(ny(day, 12, 0), price=low) == []  # the day's low: intrabar stops
        h.broker.next_open_at = ny(days[k + 1].date(), 9, 30) if k + 1 < len(days) else None
        assert h.tick(ny(day, 16, 25), price=c, market_open=False) == []

    trades = h.store.trades()
    live_entries = [bar_day("SPY", parse_ts(t["entry_ts"])) for t in trades]
    live_entries += [bar_day("SPY", p.entry_ts) for p in h.store.get_positions().values()]
    live_exits = [(bar_day("SPY", parse_ts(t["exit_ts"])), t["exit_reason"]) for t in trades]
    bt_entries = [bar_day("SPY", t.entry_ts) for t in backtest.trades]
    bt_exits = [(bar_day("SPY", t.exit_ts), t.exit_reason) for t in backtest.trades if t.exit_reason != "end_of_data"]
    assert live_entries == bt_entries
    assert [d for d, _ in live_exits] == [d for d, _ in bt_exits]
    # A strategy exit and a gap through the stop at the same open are both "the open"; otherwise
    # the reasons agree too.
    assert sum(r1 != r2 for (_, r1), (_, r2) in zip(live_exits, bt_exits)) <= 1
    assert h.risk.consecutive_errors == 0 and not h.kill.is_tripped()


class AmbiguousSubmitBroker(SimBroker):
    """The order reaches the broker and fills, but the client sees a timeout."""

    def submit(self, intent):
        super().submit(intent)
        raise TimeoutError("read timed out")


def test_ambiguous_entry_submit_is_adopted_with_its_stop(tmp_path, scripted):
    h = Harness(tmp_path, make_cfg())
    h.broker = AmbiguousSubmitBroker(cash=100_000.0, clock=h.clock)
    h.broker.market_open = False
    h.gateway = OrderGateway(h.broker, h.risk, h.kill, h.store, h.settings, h.notifier)
    h.runner = h.new_runner()
    assert h.after_close(MON) == []
    h.tick(ny(TUE, 9, 31), price=101.0, market_open=True)
    signal = h.entry_signal_row()
    assert signal["status"] == "submitted"  # not "skipped": the fill may exist
    h.tick(ny(TUE, 9, 32))
    position = h.store.get_positions()["SPY"]
    assert position.qty == 20 and position.stop_price == 95.0
    assert h.store.get_signal(signal["id"])["status"] == "filled"


def test_kill_buy_to_cover_of_a_foreign_short_is_not_booked_as_a_bot_entry(harness):
    from bot.runner import TrackedOrder

    h = harness
    now = ny(TUE, 10, 0)
    h.clock.now = now
    cid = "kill-cover-QQQ"
    h.store.upsert_order(
        OrderIntent("QQQ", Side.BUY, 2.0, 100.0, OrderPurpose.KILL, "flatten", cid),
        OrderResult(cid, "b-1", "filled", 2.0, 100.5),
    )
    order = TrackedOrder(cid, "QQQ", Side.BUY.value, OrderPurpose.KILL.value, "kill", None, 2.0, 100.0, "flatten", to_iso(now))
    assert h.runner._reconcile_order(order, now) is True  # final, no exception, no re-trip loop
    assert "QQQ" not in h.store.get_positions()
    assert h.notifier.said("(kill, not a bot position)")


def test_one_outage_counts_once_per_poll_toward_the_error_kill(harness):
    h = harness
    h.open_position()

    def down(*args, **kwargs):
        raise ConnectionError("alpaca 503")

    h.broker.positions = down
    h.broker.last_price = down
    failed = h.tick(ny(WED, 11, 0), market_open=True)
    assert len(failed) >= 2  # several steps broke on the same outage...
    assert h.risk.consecutive_errors == 1  # ...but it counts once
    for minute in range(1, 4):
        h.tick(ny(WED, 11, minute))
    assert h.risk.consecutive_errors == 4 and not h.kill.is_tripped()
    h.tick(ny(WED, 11, 4))
    assert h.kill.is_tripped()  # five failing polls in a row trip it, as documented


def test_approve_button_from_another_message_is_refused(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    h.clock.now = ny(MON, 20, 0)
    h.notifier.script("approve", approval_id, message_id=9999)  # e.g. a button from before a database reset
    h.runner.handle_commands()
    assert h.store.get_approval(approval_id)["status"] == "pending"
    assert h.notifier.said("belongs to a different message")


def test_approval_survives_a_failed_price_check(approvals):
    h = approvals
    h.after_close(MON)
    ((approval_id, _),) = h.notifier.approvals
    signal = h.entry_signal_row()

    def down(symbol):
        raise ConnectionError("alpaca 503")

    real_last_price = h.broker.last_price
    h.broker.last_price = down
    h.clock.now = ny(MON, 20, 0)
    h.notifier.script("approve", approval_id)
    h.runner.handle_commands()
    assert h.store.get_approval(approval_id)["status"] == "approved"
    assert h.store.get_signal(signal["id"])["status"] == "queued"
    h.broker.last_price = real_last_price
    assert h.tick(ny(TUE, 9, 31), price=100.8, market_open=True) == []
    assert [o.qty for o in h.broker.submitted] == [20]  # the drift check ran again at execution
