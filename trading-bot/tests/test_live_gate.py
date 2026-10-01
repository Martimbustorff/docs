"""The kill-switch drill and the final check.

Every test works on copies: a tmp `var/`, a tmp copy of strategy.md and a fixture
tournament.json. The real strategy.md, results/ and var/ are never written.
"""

import json
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from bot import live_gate
from bot.backtest.engine import BacktestConfig, run_backtest
from bot.broker import SimBroker, assert_trading_allowed, make_client_order_id
from bot.config import LIVE_ACK_PHRASE, ConfigError, load_settings, load_strategy, read_config_block, write_config_block
from bot.data import load_daily
from bot.live_gate import BLOW_UP_HEADING, final_check, kill_switch_drill
from bot.models import OrderIntent, OrderPurpose, OrderResult, PositionState, Side, Signal, SignalKind
from bot.store import Store, parse_ts, to_iso
from bot.strategies import build
from bot.timeutil import NY, bar_close_ts

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PAPER_START = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)  # the bot's first equity snapshot
DRILL_STEPS = [
    "entry", "resting_order", "trip", "flatten", "no_open_orders", "flat", "entry_refused",
    "alert_sent", "exit_allowed", "bot_state_untouched",
]  # fmt: skip
REQUIRED = ["paper_history", "signal_parity", "fill_quality", "return_gap", "kill_switch_drill", "risk_config"]
REGIMES = {
    "trend": {
        "bull": {"return_pct": 14.0, "max_dd_pct": 3.1, "n_trades": 30, "n_bars": 1500},
        "bear": {"return_pct": -4.2, "max_dd_pct": 6.3, "n_trades": 6, "n_bars": 300},
        "sideways": {"return_pct": 0.8, "max_dd_pct": 2.0, "n_trades": 9, "n_bars": 400},
    },
    "vol": {
        "high": {"return_pct": -1.5, "max_dd_pct": 5.0, "n_trades": 20, "n_bars": 1000},
        "low": {"return_pct": 11.0, "max_dd_pct": 2.5, "n_trades": 25, "n_bars": 1200},
    },
    "stress": {
        "covid_crash_2020": {"start": "2020-02-19", "end": "2020-03-23", "return_pct": -5.5, "max_dd_pct": 6.1,
                             "n_trades": 2, "n_bars": 24},
        "bear_market_2022": {"start": "2022-01-03", "end": "2022-10-12", "return_pct": -2.0, "max_dd_pct": 4.0,
                             "n_trades": 5, "n_bars": 196},
        "tariff_shock_2025": {"start": "2025-02-19", "end": "2025-04-08", "return_pct": None, "max_dd_pct": None,
                              "n_trades": 0, "n_bars": 0},
    },
}  # fmt: skip


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def request_approval(self, approval_id, text):
        return None

    def poll(self):
        return []


class StuckBroker(SimBroker):
    """A broker whose positions can never be closed."""

    def close_position(self, symbol):
        raise RuntimeError("venue halted")


def no_sleep(seconds):
    pass


def at(ts):
    return lambda: ts


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_settings):
    return tmp_settings


@pytest.fixture
def strategy_file(tmp_path):
    """A copy of strategy.md with SPY (meanrev) and BTC/USD (breakout) enabled."""
    path = tmp_path / "strategy.md"
    shutil.copyfile(ROOT / "strategy.md", path)
    data = read_config_block(path.read_text())
    data["assets"] = {
        "SPY": {"enabled": True, "strategy": "meanrev", "params": {}},
        "QQQ": {"enabled": False, "strategy": "trend", "params": {}},
        "BTC/USD": {"enabled": True, "strategy": "breakout", "params": {}},
    }
    data["live_gate"] = {
        "min_paper_days": 30, "min_paper_trades": 8, "min_signal_match_rate": 0.9,
        "max_return_gap_pct": 3.0, "kill_switch_drill_max_age_days": 30,
    }  # fmt: skip
    data["execution"]["slippage_bps"] = {"stock": 5, "crypto": 10}
    data["execution"]["fee_bps"] = {"stock": 0, "crypto": 25}
    write_config_block(data, path)
    return path


@pytest.fixture
def cfg(strategy_file):
    return load_strategy(strategy_file)


def write_results(path, cfg, drawdowns=(6.0, 8.0), fires=()):
    """A tournament.json whose winners are the enabled assets of `cfg`."""
    winners, runs = {}, []
    for symbol, rule in cfg.enabled_assets.items():
        strategy = build(rule.strategy, symbol, dict(rule.params))
        winners[symbol] = {"label": strategy.label(), "strategy": rule.strategy, "params": dict(strategy.params)}
        runs.append(
            {
                "symbol": symbol, "strategy": rule.strategy, "label": strategy.label(), "params": dict(strategy.params),
                "passed": True, "regimes": REGIMES, "approvals": {"entry_orders": 40, "needing_approval": 38},
                "neighbors_passing": 0.5, "out_of_sample": {"n_trades": 12},
            }
        )  # fmt: skip
    data = {
        "n_configs": 90,
        "config": {"risk": cfg.risk.model_dump()},
        "runs": runs,
        "winners": {**winners, "QQQ": None},
        "portfolio": {"window": "out_of_sample", "metrics": {"max_drawdown_pct": drawdowns[0]},
                      "kill_switch_would_fire": list(fires), "daily_loss_limit_hits": 1},
        "portfolio_full": {"window": "full", "metrics": {"max_drawdown_pct": drawdowns[1]},
                           "kill_switch_would_fire": [], "daily_loss_limit_hits": 3},
    }  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def results_file(tmp_path, cfg):
    return write_results(tmp_path / "results" / "tournament.json", cfg)


def live_settings(settings):
    return load_settings(
        env_file=None,
        environ={
            "BOT_DATA_DIR": str(settings.data_dir), "TRADING_MODE": "live", "ALPACA_PAPER": "false",
            "LIVE_TRADING_ACK": LIVE_ACK_PHRASE,
        },
    )  # fmt: skip


def section(path, name="FINAL_CHECK"):
    text = Path(path).read_text()
    return text.split(f"<!-- BEGIN {name} -->", 1)[1].split(f"<!-- END {name} -->", 1)[0].strip()


def outside(path, name="FINAL_CHECK"):
    text = Path(path).read_text()
    head, rest = text.split(f"<!-- BEGIN {name} -->", 1)
    return head + rest.split(f"<!-- END {name} -->", 1)[1]


def check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


# --------------------------------------------------------------------------- a paper record


def closed_bars(symbol):
    bars = load_daily(symbol)
    return bars[[bar_close_ts(symbol, d.date()) <= NOW for d in bars.index]]


def bar_day(signal):
    """The bar a signal was computed on (the inverse of bar_close_ts)."""
    if "/" in signal.symbol:
        return (signal.ts.astimezone(UTC) - timedelta(days=1)).date()
    return signal.ts.astimezone(NY).date()


def vetoing(strategy, days):
    """Make `strategy` skip (and collect) its entries on `days`, as a Jev veto does."""
    vetoed = []
    original = strategy.entry_signal

    def entry_signal(df, i):
        signal = original(df, i)
        if signal is not None and df.index[i].date() in days:
            vetoed.append(signal)
            return None
        return signal

    strategy.entry_signal = entry_signal
    return vetoed


def backtest(cfg, symbol, strategy=None):
    rule = cfg.assets[symbol]
    strategy = strategy or build(rule.strategy, symbol, dict(rule.params))
    bars = closed_bars(symbol)
    # At its first tick the runner acts on the newest bar that has already closed.
    first = [d.date() for d in bars.index if bar_close_ts(symbol, d.date()) <= PAPER_START][-1]
    result = run_backtest(bars, strategy, BacktestConfig.from_strategy(cfg, symbol), start=first.isoformat())
    return bars, result


def seed_paper(store, cfg, veto=frozenset(), exit_shift=0, slippage=0.0002):
    """Record a paper run that behaves exactly like the backtest: every signal before Jev and
    risk, a fill at the next bar's open (`slippage` against us), the closed trades and daily bot
    equity. Entries on `veto` dates are vetoed by Jev instead; `exit_shift` misdates exits."""
    capital = cfg.risk.capital_usd
    store.record_equity(100_000.0, capital, 100_000.0, 0.0, ts=PAPER_START)
    curves = []
    for symbol, rule in cfg.enabled_assets.items():
        strategy = build(rule.strategy, symbol, dict(rule.params))
        vetoed = vetoing(strategy, veto)
        bars, result = backtest(cfg, symbol, strategy)
        for signal in result.signals:
            day = bar_day(signal)
            entry = signal.kind is SignalKind.ENTRY
            sid = store.insert_signal(signal, day if entry else day + timedelta(days=exit_shift))
            nxt = int(bars.index.searchsorted(pd.Timestamp(day), side="right"))
            if nxt >= len(bars):  # the last bar: its order goes out after NOW
                store.update_signal(sid, gate="passed" if entry else "n/a", status="queued")
                continue
            purpose = OrderPurpose.ENTRY if entry else OrderPurpose.EXIT
            side = Side.BUY if entry else Side.SELL
            fill = float(bars["open"].iat[nxt]) * (1 + slippage if entry else 1 - slippage)
            cid = make_client_order_id(purpose, symbol, f"{rule.strategy}|{day}|0")
            intent = OrderIntent(symbol, side, 1.0, signal.price, purpose, signal.reason, cid, sid)
            store.upsert_order(intent, OrderResult(cid, f"alpaca-{sid}", "filled", 1.0, fill), ts=signal.ts)
            store.update_signal(sid, gate="passed" if entry else "n/a", status="filled", order_client_id=cid)
        for signal in vetoed:
            sid = store.insert_signal(signal, bar_day(signal))
            store.update_signal(sid, gate="vetoed", status="blocked")
        for trade in result.trades:
            if trade.exit_reason != "end_of_data":  # still open: a position, not a closed trade
                entry_row = store.find_signal(symbol, rule.strategy, "entry", trade.meta["signal_bar"])
                store.insert_trade(trade, entry_row["id"])
        curves.append(result.equity - capital)
    pnl = pd.concat(curves, axis=1).sort_index().ffill().fillna(0.0).sum(axis=1)
    for day, value in pnl.items():
        ts = datetime.combine(day.date(), time(21), tzinfo=UTC)
        store.record_equity(100_000.0 + value, capital + value, 90_000.0, 0.0, ts=ts)


def first_entry_day(cfg, symbol):
    _, result = backtest(cfg, symbol)
    return bar_day(next(s for s in result.signals if s.kind is SignalKind.ENTRY))


@pytest.fixture
def fresh_drill(settings, cfg):
    return kill_switch_drill(settings, cfg, clock=at(NOW - timedelta(days=1)))


# --------------------------------------------------------------------------- kill-switch drill


def test_sim_drill_passes_and_leaves_the_bots_state_alone(settings, cfg):
    result = kill_switch_drill(settings, cfg, clock=at(NOW))

    assert result["passed"] is True
    assert result["mode"] == "sim"
    assert [s["name"] for s in result["steps"]] == DRILL_STEPS
    assert all(s["ok"] for s in result["steps"]), result["steps"]
    assert parse_ts(result["ts"]) == NOW
    # The verdict lands in the real var/, the drill's own state stays in its own directory.
    on_disk = json.loads(settings.drill_path.read_text())
    assert on_disk["passed"] is True and on_disk["steps"] == result["steps"]
    state_dir = Path(result["state_dir"])
    assert state_dir.parent == settings.data_dir / "drill"
    assert (state_dir / "KILL_SWITCH").exists()
    assert not settings.kill_switch_path.exists()
    assert not settings.db_path.exists()


def test_sim_drill_cancels_the_resting_order_refuses_entries_and_alerts(settings, cfg):
    broker, notifier = SimBroker(), FakeNotifier()

    result = kill_switch_drill(settings, cfg, broker=broker, notifier=notifier, clock=at(NOW))

    assert result["passed"] is True
    assert broker.cancel_all_calls >= 1
    assert "canceled" in {order.status for order in broker.orders.values()}
    assert broker.positions() == {}
    assert len(broker.submitted) == 2  # the entry and the resting order; the post-trip entry never left
    steps = {s["name"]: s for s in result["steps"]}
    assert "kill switch is tripped" in steps["entry_refused"]["detail"]
    assert "Flatten (kill-switch drill)" in steps["alert_sent"]["detail"]
    assert any(text.startswith("Flatten (kill-switch drill)") for text in notifier.sent)
    assert notifier.sent[-1].startswith("Kill-switch drill (sim) PASSED")


def test_sim_drill_ignores_a_live_env_and_an_env_kill_switch(settings, cfg):
    live = live_settings(settings).model_copy(update={"kill_switch": True})

    result = kill_switch_drill(live, cfg, clock=at(NOW))

    assert result["passed"] is True, result["steps"]


def test_drill_fails_and_says_so_when_the_account_cannot_be_flattened(settings, cfg):
    result = kill_switch_drill(settings, cfg, broker=StuckBroker(), clock=at(NOW), sleep=no_sleep)

    assert result["passed"] is False
    steps = {s["name"]: s for s in result["steps"]}
    assert not steps["flatten"]["ok"] and not steps["flat"]["ok"]
    assert "BTC/USD" in steps["flat"]["detail"]
    assert steps["entry_refused"]["ok"] and steps["exit_allowed"]["ok"]
    assert json.loads(settings.drill_path.read_text())["passed"] is False


def test_sim_drill_needs_a_simulated_broker(settings, cfg):
    with pytest.raises(ConfigError, match="SimBroker"):
        kill_switch_drill(settings, cfg, broker=object())


@pytest.mark.parametrize(
    "environ",
    [
        {"TRADING_MODE": "live", "ALPACA_PAPER": "false", "LIVE_TRADING_ACK": LIVE_ACK_PHRASE},
        {"TRADING_MODE": "paper", "ALPACA_PAPER": "false"},
        {"TRADING_MODE": "live", "ALPACA_PAPER": "true"},
    ],
)
def test_paper_drill_refuses_anything_but_a_paper_deployment(settings, cfg, environ):
    wrong = load_settings(env_file=None, environ={"BOT_DATA_DIR": str(settings.data_dir), **environ})
    paper_broker = SimBroker(is_paper=True)

    with pytest.raises(ConfigError, match="TRADING_MODE=paper and ALPACA_PAPER=true"):
        kill_switch_drill(wrong, cfg, paper=True, broker=paper_broker, clock=at(NOW), sleep=no_sleep)

    assert paper_broker.submitted == []
    assert not settings.drill_path.exists()


def test_paper_drill_refuses_while_the_bot_holds_positions(settings, cfg):
    with Store(settings.db_path) as store:
        store.put_position(
            PositionState("SPY", "meanrev", 2.0, 500.0, NOW - timedelta(days=2), stop_price=480.0, highest_close=500.0)
        )
    paper_broker = SimBroker(is_paper=True)

    with pytest.raises(ConfigError, match=r"open positions \(SPY\)"):
        kill_switch_drill(settings, cfg, paper=True, broker=paper_broker, clock=at(NOW))

    assert paper_broker.submitted == []
    assert not settings.drill_path.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("runner_heartbeat", to_iso(NOW - timedelta(minutes=1))),
        ("heartbeat", to_iso(NOW - timedelta(minutes=4))),
        ("runner_heartbeat", str((NOW - timedelta(seconds=30)).timestamp())),
        ("runner_heartbeat", "not a time"),
    ],
)
def test_paper_drill_refuses_while_the_bot_may_be_running(settings, cfg, key, value):
    with Store(settings.db_path) as store:
        store.kv_set(key, value)

    with pytest.raises(ConfigError, match="bot|heartbeat"):
        kill_switch_drill(settings, cfg, paper=True, broker=SimBroker(is_paper=True), clock=at(NOW))

    assert not settings.drill_path.exists()


def test_paper_drill_without_a_heartbeat_waits_for_the_equity_snapshots_to_stop(settings, cfg):
    with Store(settings.db_path) as store:
        store.record_equity(100_000.0, 10_000.0, 100_000.0, 0.0, ts=NOW - timedelta(minutes=3))

    with pytest.raises(ConfigError, match="may still be running"):
        kill_switch_drill(settings, cfg, paper=True, broker=SimBroker(is_paper=True), clock=at(NOW))


def test_paper_drill_refuses_a_live_broker_or_an_account_with_positions(settings, cfg):
    with pytest.raises(ConfigError, match="not an Alpaca paper account"):
        kill_switch_drill(settings, cfg, paper=True, broker=SimBroker(is_paper=False), clock=at(NOW))

    busy = SimBroker(is_paper=True)
    busy.set_price("AAPL", 200.0)
    busy.submit(OrderIntent("AAPL", Side.BUY, 1.0, 200.0, OrderPurpose.ENTRY, "manual trade", "manual-1"))
    with pytest.raises(ConfigError, match="paper account holds positions"):
        kill_switch_drill(settings, cfg, paper=True, broker=busy, clock=at(NOW))
    assert busy.positions()["AAPL"].qty == 1.0
    assert not settings.drill_path.exists()


def test_paper_drill_runs_once_the_bot_is_stopped_and_flat(settings, cfg):
    with Store(settings.db_path) as store:
        store.kv_set("runner_heartbeat", to_iso(NOW - timedelta(minutes=10)))
    paper_broker = SimBroker(is_paper=True)
    paper_broker.set_price("BTC/USD", 65_000.0)

    result = kill_switch_drill(settings, cfg, paper=True, broker=paper_broker, clock=at(NOW), sleep=no_sleep)

    assert result["passed"] is True, result["steps"]
    assert result["mode"] == "paper"
    assert paper_broker.positions() == {}
    assert paper_broker.submitted[0].qty * 65_000.0 == pytest.approx(live_gate.DRILL_NOTIONAL_USD, abs=0.01)
    assert not settings.kill_switch_path.exists()


# --------------------------------------------------------------------------- final check


def test_final_check_on_an_empty_store_fails_with_clear_reasons(settings, cfg, strategy_file, fresh_drill):
    missing = strategy_file.parent / "results" / "tournament.json"
    config_before = read_config_block(strategy_file.read_text())
    rest_before = outside(strategy_file)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=missing)

    assert result["passed"] is False
    assert [c["name"] for c in result["checks"] if c["required"]] == REQUIRED
    for name in ("paper_history", "signal_parity", "fill_quality", "return_gap"):
        assert check(result, name)["passed"] is False
        assert "Not enough paper history" in check(result, name)["detail"]
    assert check(result, "kill_switch_drill")["passed"] is True
    assert "No tournament results" in check(result, "risk_config")["detail"]
    assert check(result, "regime_risk")["required"] is False
    assert result["summary"].startswith("FAILED: 5 of 6")
    assert not settings.db_path.exists()  # reading an absent store does not create it

    gate = json.loads(settings.live_gate_path.read_text())
    assert gate["passed"] is False and parse_ts(gate["ts"]) == NOW
    assert {"passed", "ts", "checks", "summary"} <= set(gate)
    with pytest.raises(ConfigError, match="does not record a passed final check"):
        assert_trading_allowed(live_settings(settings), settings.live_gate_path, now=NOW + timedelta(hours=1))

    report = section(strategy_file)
    assert report.startswith("**Verdict: NOT READY FOR LIVE.**")
    assert "### Does paper match the backtest?\n\nNot yet known." in report
    assert "### Did the kill switch fire in testing?\n\nYes." in report
    assert report.split("\n### ")[-1].startswith(BLOW_UP_HEADING)
    assert read_config_block(strategy_file.read_text()) == config_before
    assert outside(strategy_file) == rest_before


def test_final_check_passes_when_paper_matches_the_backtest(settings, cfg, strategy_file, results_file, fresh_drill):
    with Store(settings.db_path) as store:
        seed_paper(store, cfg)
    config_before = read_config_block(strategy_file.read_text())

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    failed = [c for c in result["checks"] if c["required"] and not c["passed"]]
    assert failed == [] and result["passed"] is True
    parity = check(result, "signal_parity")["value"]
    assert parity["match_rate"] == 1.0 and parity["paper_match_rate"] == 1.0
    assert parity["backtest_signals"] >= 20
    assert set(parity["bars"].values()) == {"cached data (bot.data.load_daily)"}
    fills = check(result, "fill_quality")["value"]
    assert fills["stock"]["avg_bps"] == pytest.approx(2.0, abs=0.01)
    assert fills["crypto"]["avg_bps"] == pytest.approx(2.0, abs=0.01)
    assert abs(check(result, "return_gap")["value"]["gap_pct"]) < 1e-6
    assert check(result, "paper_history")["value"]["closed_trades"] >= 8

    # The live-trading guard accepts the file, and only while it is fresh.
    live = live_settings(settings)
    assert_trading_allowed(live, settings.live_gate_path, now=NOW + timedelta(hours=1))
    with pytest.raises(ConfigError, match="days old"):
        assert_trading_allowed(live, settings.live_gate_path, now=NOW + timedelta(days=8))
    gate = json.loads(settings.live_gate_path.read_text())
    assert gate["passed"] is True and isinstance(gate["passed"], bool)
    assert live.trading_mode == "live" and settings.trading_mode == "paper"  # nothing was switched

    report = section(strategy_file)
    assert report.startswith("**Verdict: PASSED.**")
    for heading in ("Does paper match the backtest?", "Did the kill switch fire in testing?",
                    "What market regime would break this?"):  # fmt: skip
        assert f"### {heading}" in report
    assert "### Does paper match the backtest?\n\nYes." in report
    assert "a sell-off that keeps going" in report  # SPY meanrev's breaking regime
    assert report.split("\n### ")[-1].startswith(BLOW_UP_HEADING)
    assert "2020-03-16" in report  # SPY's largest overnight gap in the cached data
    assert read_config_block(strategy_file.read_text()) == config_before
    assert load_strategy(strategy_file) == cfg


def test_misdated_signals_fail_the_parity_check(settings, cfg, strategy_file, results_file, fresh_drill):
    with Store(settings.db_path) as store:
        seed_paper(store, cfg, exit_shift=1)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    parity = check(result, "signal_parity")
    assert parity["passed"] is False and result["passed"] is False
    assert parity["value"]["match_rate"] < 0.9
    assert parity["value"]["paper_only"] > 0
    assert "Missing in paper" in parity["detail"] and "Paper only" in parity["detail"]


def test_jev_vetoes_are_not_mismatches_and_their_counterfactual_is_reported(
    settings, cfg, strategy_file, results_file, fresh_drill
):
    vetoed_day = first_entry_day(cfg, "SPY")
    with Store(settings.db_path) as store:
        seed_paper(store, cfg, veto={vetoed_day})

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    assert check(result, "signal_parity")["value"]["match_rate"] == 1.0
    gap = check(result, "return_gap")
    assert gap["passed"] is True
    assert gap["value"]["jev_vetoed"] == 1
    (counterfactual,) = gap["value"]["jev_counterfactuals"]
    assert counterfactual["bar_date"] == vetoed_day.isoformat()
    assert counterfactual["pnl_usd"] is not None
    assert "Jev vetoed 1 entry" in gap["detail"]


def test_replay_can_use_the_brokers_bars(settings, cfg, strategy_file, results_file, fresh_drill):
    with Store(settings.db_path) as store:
        seed_paper(store, cfg)
    broker = SimBroker()
    for symbol in cfg.enabled_assets:
        broker.set_bars(symbol, load_daily(symbol))

    result = final_check(settings, cfg, broker=broker, now=NOW, strategy_path=strategy_file, results_path=results_file)

    parity = check(result, "signal_parity")
    assert set(parity["value"]["bars"].values()) == {"SimBroker.daily_bars"}
    assert "SimBroker.daily_bars" in parity["detail"] and parity["passed"] is True


def test_simulated_fills_in_the_paper_record_fail_the_fill_check(settings, cfg, strategy_file, results_file):
    with Store(settings.db_path) as store:
        seed_paper(store, cfg)
        row = store.signals(limit=1)[0]
        signal_id = row["id"]
        cid = "entry-SPY-dryrun000001"
        intent = OrderIntent("SPY", Side.BUY, 1.0, 500.0, OrderPurpose.ENTRY, "dry run", cid, signal_id)
        store.upsert_order(intent, OrderResult(cid, "sim-7", "filled", 1.0, 500.0))
        store.update_signal(signal_id, order_client_id=cid)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    fills = check(result, "fill_quality")
    assert fills["passed"] is False and "SimBroker" in fills["detail"]


@pytest.mark.parametrize(
    ("drill", "expected"),
    [
        ({"passed": True, "ts": to_iso(NOW - timedelta(days=31)), "mode": "sim", "steps": []}, "31 days old"),
        (
            {"passed": False, "ts": to_iso(NOW - timedelta(days=1)), "mode": "paper",
             "steps": [{"name": "entry", "ok": True}, {"name": "flat", "ok": False}]},
            "FAILED at: flat",
        ),
        ({"passed": True, "ts": "yesterday", "mode": "sim", "steps": []}, "no valid ISO-8601"),
        ({"passed": True, "ts": to_iso(NOW + timedelta(days=2)), "mode": "sim", "steps": []}, "in the future"),
    ],
)  # fmt: skip
def test_a_stale_failed_or_undated_drill_fails_the_drill_check(settings, cfg, strategy_file, results_file, drill, expected):
    settings.drill_path.write_text(json.dumps(drill))

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    drill_check = check(result, "kill_switch_drill")
    assert drill_check["passed"] is False
    assert expected in drill_check["detail"]


def test_a_missing_drill_fails_the_drill_check(settings, cfg, strategy_file, results_file):
    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    assert "No kill-switch drill on record" in check(result, "kill_switch_drill")["detail"]
    assert "### Did the kill switch fire in testing?\n\nNo: there is no drill on record" in section(strategy_file)


def test_a_failed_drill_is_answered_honestly(settings, cfg, strategy_file, results_file):
    kill_switch_drill(settings, cfg, broker=StuckBroker(), clock=at(NOW - timedelta(hours=1)), sleep=no_sleep)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    assert "FAILED at: flatten, flat." in check(result, "kill_switch_drill")["detail"]
    assert "Only partly: the switch was tripped in the last drill, but the drill failed." in section(strategy_file)


def test_paper_returns_far_from_the_replay_fail_the_return_gap(settings, cfg, strategy_file, results_file, fresh_drill):
    with Store(settings.db_path) as store:
        seed_paper(store, cfg)
        store.record_equity(100_000.0, cfg.risk.capital_usd * 1.10, 90_000.0, 0.0, ts=NOW - timedelta(minutes=5))

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    gap = check(result, "return_gap")
    assert gap["passed"] is False and result["passed"] is False
    assert gap["value"]["paper_return_pct"] == pytest.approx(10.0)
    assert gap["value"]["gap_pct"] > 3.0
    assert "beyond the ±3 limit" in gap["detail"]
    assert check(result, "signal_parity")["passed"] is True


def test_fills_worse_than_the_backtest_assumes_fail_the_fill_check(settings, cfg, strategy_file, results_file):
    with Store(settings.db_path) as store:
        seed_paper(store, cfg, slippage=0.0008)  # 8 bps: over the 5 bps assumed for stocks, under crypto's 10

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    fills = check(result, "fill_quality")
    assert fills["passed"] is False
    assert fills["value"]["stock"]["avg_bps"] == pytest.approx(8.0, abs=0.01)
    assert "returns are overstated" in fills["detail"]


@pytest.mark.parametrize(
    ("drawdowns", "fires", "expected"),
    [
        ((6.0, 8.0), (), None),
        ((6.0, 13.0), (), "likely to fire in normal operation"),
        ((6.0, 8.0), ({"date": "2020-03-20", "reason": "drawdown 15.2%"},), "expect it to fire in normal operation"),
    ],
)
def test_risk_config_warns_when_the_backtest_drawdown_nears_the_kill_level(
    settings, cfg, strategy_file, tmp_path, drawdowns, fires, expected
):
    results = write_results(tmp_path / "results.json", cfg, drawdowns, fires)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results)

    risk = check(result, "risk_config")
    assert risk["passed"] is (expected is None)
    assert risk["value"]["portfolio_max_drawdown_pct"] == max(drawdowns)
    if expected:
        assert expected in risk["detail"]


def test_risk_config_fails_when_the_running_config_is_not_the_tested_one(settings, cfg, strategy_file, tmp_path):
    results = write_results(tmp_path / "results.json", cfg)
    data = read_config_block(strategy_file.read_text())
    data["assets"]["SPY"]["params"] = {"entry_rsi": 5}
    data["risk"]["max_position_usd"] = 5000
    write_config_block(data, strategy_file)

    result = final_check(settings, load_strategy(strategy_file), now=NOW, strategy_path=strategy_file, results_path=results)

    risk = check(result, "risk_config")
    assert risk["passed"] is False
    assert "SPY runs meanrev" in risk["detail"] and "risk.max_position_usd" in risk["detail"]


def test_regime_risk_names_the_worst_windows_but_never_gates(settings, cfg, strategy_file, results_file):
    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    regimes = check(result, "regime_risk")
    assert regimes["required"] is False and regimes["passed"] is True
    spy = regimes["value"]["SPY"]
    assert spy["worst_stress"]["name"] == "covid_crash_2020"
    assert spy["worst_trend"]["name"] == "bear"
    assert spy["worst_vol"]["name"] == "high"
    report = section(strategy_file)
    assert "### What market regime would break this?" in report and "covid_crash_2020" in report


def test_the_blow_up_section_lists_concrete_risks(settings, cfg, strategy_file, results_file):
    final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    blow_up = section(strategy_file).split(f"### {BLOW_UP_HEADING}", 1)[1]
    for phrase in (
        "Gaps and crashes jump the stop", "The stops live in the bot", "dead-man's switch",
        "Correlated positions lose together", "Crypto never closes", "Outages of Alpaca, Jev or Telegram",
        "Approval fatigue", "dedicated to the bot", "tested 90 configurations",
    ):  # fmt: skip
        assert phrase in blow_up
    items = [line for line in blow_up.strip().splitlines() if line[:1].isdigit()]
    assert len(items) == 9  # one line per risk, as the dashboard reads them
    titles = [item["title"] for item in json.loads(settings.live_gate_path.read_text())["blow_up"]]
    assert titles[0] == "Gaps and crashes jump the stop."


def test_report_goes_to_var_when_strategy_md_cannot_be_written(settings, cfg, tmp_path, results_file):
    unwritable = tmp_path / "read-only" / "strategy.md"

    result = final_check(settings, cfg, now=NOW, strategy_path=unwritable, results_path=results_file)

    fallback = settings.data_dir / "final_check.md"
    assert result["report_path"] == str(fallback)
    assert fallback.read_text().strip().split("\n### ")[-1].startswith(BLOW_UP_HEADING)
    assert settings.live_gate_path.exists()


def test_report_text_can_never_forge_a_section_marker(settings, cfg, strategy_file, tmp_path):
    results = write_results(tmp_path / "results.json", cfg)
    data = json.loads(results.read_text())
    data["runs"][0]["regimes"]["stress"]["<!-- END CONFIG -->"] = {"return_pct": -99.0, "max_dd_pct": 99.0}
    results.write_text(json.dumps(data))

    final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results)

    assert "<!-- END CONFIG -->" not in section(strategy_file)
    assert load_strategy(strategy_file) == cfg


def test_the_first_equity_snapshot_starts_the_paper_period(settings, cfg, strategy_file, results_file):
    with Store(settings.db_path) as store:
        store.record_equity(100_000.0, 10_000.0, 100_000.0, 0.0, ts=NOW - timedelta(days=10))
        # Acted on at the first tick, but stamped with its bar's close two days before the bot started.
        early = date(2026, 9, 18)
        store.insert_signal(Signal(bar_close_ts("SPY", early), "SPY", "meanrev", SignalKind.ENTRY, "dip", 660.0, 650.0), early)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    history = check(result, "paper_history")
    assert history["passed"] is False
    assert history["value"]["days"] == pytest.approx(10.0)
    assert "not enough paper history yet" in history["detail"]
    assert date.fromisoformat(history["value"]["start"][:10]) == (NOW - timedelta(days=10)).date()


def test_each_symbol_is_compared_from_the_first_bar_the_bot_evaluated(settings, cfg, strategy_file, results_file):
    first_tick = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)  # a Monday morning
    friday = date(2026, 9, 25)
    with Store(settings.db_path) as store:
        store.record_equity(100_000.0, 10_000.0, 100_000.0, 0.0, ts=first_tick)
        signal = Signal(bar_close_ts("SPY", friday), "SPY", "meanrev", SignalKind.ENTRY, "dip", 660.0, 650.0)
        store.insert_signal(signal, friday)

    result = final_check(settings, cfg, now=NOW, strategy_path=strategy_file, results_path=results_file)

    windows = check(result, "signal_parity")["value"]["windows"]
    # SPY starts at its recorded Friday signal; BTC/USD at Sunday's bar, the newest one closed at
    # the first tick, not at the Friday stock signal before it.
    assert windows == {"SPY": ["2026-09-25", "2026-09-29"], "BTC/USD": ["2026-09-27", "2026-09-29"]}
