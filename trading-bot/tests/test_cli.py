"""`python -m bot` commands. Every test uses a tmp strategy.md, results dir and BOT_DATA_DIR, and
fakes the modules that talk to the network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bot.broker as broker_mod
from bot.__main__ import main, parse_params
from bot.broker import SimBroker
from bot.models import OrderIntent, OrderPurpose, Side
from bot.risk import KillSwitch
from bot.runner import EXTERNAL_ORDERS_KEY, HEARTBEAT_KEY
from bot.store import Store

ROOT = Path(__file__).resolve().parent.parent

STRATEGY_MD = """# Strategy rulebook (test copy)

<!-- BEGIN CONFIG -->

```yaml
version: 1
timeframe: 1d
assets:
  SPY: {enabled: true, strategy: trend, params: {fast: 20, slow: 100, atr_mult: 3}}
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
  approval_threshold_usd: 1000
  approval_timeout_minutes: 720
  approval_max_price_drift_pct: 2.0
jev:
  mode: gate
  price_per_million_input_tokens: 0.042
  questions:
    buying_pressure:
      type: noul
      instructions: Is buying pressure building?
      criteria: {'true': 'yes', 'false': 'no'}
      gate: {outcome: 'yes', min: 0.45}
execution:
  slippage_bps: {stock: 5, crypto: 10}
  fee_bps: {stock: 0, crypto: 25}
tournament:
  in_sample: ['2014-09-17', '2021-12-31']
  out_of_sample: ['2022-01-01', '2026-09-29']
  filters: {max_drawdown_pct: 15.0, min_win_rate: 0.4, min_profit_factor: 1.2, min_trades: 8}
live_gate: {}
```

<!-- END CONFIG -->
"""

ENV_KEYS = (
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER", "TYPESAFE_API_KEY", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID", "DASHBOARD_USER", "DASHBOARD_PASSWORD", "DASHBOARD_HOST", "DASHBOARD_PORT",
    "TRADING_MODE", "LIVE_TRADING_ACK", "KILL_SWITCH", "BOT_DATA_DIR",
)  # fmt: skip


class Cli:
    """Runs `python -m bot` in-process against tmp files; a call returns (exit code, stdout, stderr)."""

    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.data_dir = tmp_path / "var"
        self.strategy = tmp_path / "strategy.md"
        self.results = tmp_path / "results"
        self.env_file = tmp_path / "no.env"
        self._capsys = capsys
        self.strategy.write_text(STRATEGY_MD)
        self.results.mkdir()

    def __call__(self, *argv: str) -> tuple[int, str, str]:
        global_options = ["--strategy", str(self.strategy), "--results-dir", str(self.results),
                          "--env-file", str(self.env_file)]  # fmt: skip
        code = main([*global_options, *argv])
        out, err = self._capsys.readouterr()
        return code, out, err


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    cli = Cli(tmp_path, capsys)
    monkeypatch.setenv("BOT_DATA_DIR", str(cli.data_dir))
    return cli


def settings_for(data_dir: Path):
    from bot.config import load_settings

    return load_settings(env_file=None, environ={"BOT_DATA_DIR": str(data_dir)})


# --------------------------------------------------------------------------- helpers


def test_parse_params_coerces_numbers_and_booleans():
    assert parse_params(["fast=20", "atr_mult=2.5", "flag=true", "name=x", "neg=-3"]) == {
        "fast": 20, "atr_mult": 2.5, "flag": True, "name": "x", "neg": -3,
    }  # fmt: skip


def test_bad_params_are_refused(run):
    code, _, err = run("backtest", "--symbol", "SPY", "--strategy", "trend", "--params", "fast")
    assert code == 2 and "key=value" in err


# --------------------------------------------------------------------------- status, kill, resume


def test_status_on_a_fresh_install(run):
    code, out, _ = run("status")
    assert code == 0
    assert "Trading mode: PAPER (TRADING_MODE=paper, ALPACA_PAPER=true)" in out
    assert "Live-trading guard: OK" in out
    assert "Live gate: not found" in out
    assert "enabled assets: SPY (trend)" in out
    assert "Kill switch: off" in out
    assert "Heartbeat: never" in out
    assert "Positions: none" in out and "Pending approvals: none" in out
    assert "Queued orders: 0" in out


def test_status_shows_runtime_state(run):
    with Store(settings_for(run.data_dir).db_path) as store:
        store.kv_set(HEARTBEAT_KEY, datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat())
        store.record_equity(100_000.0, 10_050.0, 90_000.0, 1_000.0)
    (run.data_dir / "live_gate.json").write_text(json.dumps({"passed": True, "ts": "2020-01-01T00:00:00+00:00"}))
    code, out, _ = run("status")
    assert code == 0
    assert "STALE" in out
    assert "Last equity: bot $10,050.00" in out
    assert "Live gate: passed" in out and "EXPIRED" in out


def test_status_reports_an_invalid_strategy(run):
    run.strategy.write_text("# no config here\n")
    code, out, _ = run("status")
    assert code == 2
    assert "Config: INVALID" in out and "Kill switch: off" in out


def test_kill_without_keys_trips_only(run):
    code, out, _ = run("kill", "--reason", "manual stop")
    assert code == 0
    assert "Kill switch tripped: manual stop" in out and "nothing was flattened" in out
    kill = KillSwitch(settings_for(run.data_dir))
    assert kill.is_tripped() and kill.status()["source"] == "cli"
    code, out, _ = run("status")
    assert "Kill switch: TRIPPED: manual stop (source cli" in out


def test_kill_with_keys_flattens_and_hands_orders_to_the_runner(run, monkeypatch):
    sim = SimBroker()
    sim.set_price("SPY", 100.0)
    sim.submit(OrderIntent("SPY", Side.BUY, 3, 100.0, OrderPurpose.ENTRY, "held", "held-spy"))
    monkeypatch.setattr(broker_mod, "AlpacaBroker", lambda settings: sim)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    code, out, _ = run("kill")
    assert code == 0 and sim.positions() == {}
    assert "filled" in out and "test-secret" not in out
    with Store(settings_for(run.data_dir).db_path) as store:
        assert len(json.loads(store.kv_get(EXTERNAL_ORDERS_KEY))) == 1


def test_kill_reports_a_failed_flatten(run, monkeypatch):
    sim = SimBroker(fail_next=0)
    sim.set_price("SPY", 100.0)
    sim.submit(OrderIntent("SPY", Side.BUY, 3, 100.0, OrderPurpose.ENTRY, "held", "held-spy"))
    sim.fail_next = 1
    monkeypatch.setattr(broker_mod, "AlpacaBroker", lambda settings: sim)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    code, out, _ = run("kill")
    assert code == 1 and "could NOT be closed" in out


def test_resume_needs_confirm_and_clears(run):
    run("kill")
    code, _, err = run("resume")
    assert code == 2 and "confirm" in err
    assert KillSwitch(settings_for(run.data_dir)).is_tripped()
    code, out, _ = run("resume", "--confirm")
    assert code == 0 and out.startswith("WARNING: kill switch cleared")
    assert not KillSwitch(settings_for(run.data_dir)).is_tripped()
    code, out, _ = run("resume", "--confirm")
    assert code == 0 and "not tripped" in out


def test_resume_refused_while_env_forces_the_switch(run, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "1")
    code, _, err = run("resume", "--confirm")
    assert code == 2 and "KILL_SWITCH" in err


# --------------------------------------------------------------------------- report, backtest


def test_report_writes_the_day(run):
    code, out, _ = run("report", "--day", "2026-09-28")
    assert code == 0
    assert out.startswith("# Daily report for 2026-09-28")
    assert (run.data_dir / "reports" / "2026-09-28.md").exists()


def test_report_rejects_a_bad_day(run):
    with pytest.raises(SystemExit) as exc:
        run("report", "--day", "yesterday")
    assert exc.value.code == 2


def test_backtest_happy_path(run):
    code, out, _ = run(
        "backtest", "--symbol", "spy", "--strategy", "trend", "--params", "fast=20", "slow=100", "atr_mult=3",
        "--start", "2023-01-03", "--end", "2023-12-29",
    )  # fmt: skip
    assert code == 0
    assert "Backtest SPY trend(atr_mult=3,fast=20,slow=100) from 2023-01-03 to 2023-12-29" in out
    assert "total_return_pct" in out and "max_drawdown_pct" in out and "entry_orders" in out


@pytest.mark.parametrize(
    "argv",
    [
        ("--symbol", "SPY", "--strategy", "nope"),
        ("--symbol", "SPY", "--strategy", "trend", "--params", "bogus=1"),
        ("--symbol", "SPY", "--strategy", "trend", "--params", "fast=200", "slow=100"),
    ],
)
def test_backtest_refuses_bad_strategy_or_params(run, argv):
    code, _, err = run("backtest", *argv)
    assert code == 2 and err.startswith("Refused:")


def test_backtest_without_cached_data_fails(run):
    code, _, err = run("backtest", "--symbol", "IWM", "--strategy", "trend")
    assert code == 1 and "fetch-data" in err


# --------------------------------------------------------------------------- run


def test_run_refuses_paper_live_mismatch(run, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    code, _, err = run("run", "--dry-run", "--once")
    assert code == 2 and "ALPACA_PAPER=false" in err


def test_run_refuses_live_without_a_passed_gate(run, monkeypatch):
    from bot.config import LIVE_ACK_PHRASE

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("LIVE_TRADING_ACK", LIVE_ACK_PHRASE)
    code, _, err = run("run", "--once")
    assert code == 2 and "final-check" in err


def test_run_without_alpaca_keys_is_refused_with_a_hint(run):
    code, _, err = run("run", "--once")
    assert code == 2 and "--dry-run" in err


def test_run_refuses_an_invalid_strategy(run):
    run.strategy.write_text(STRATEGY_MD.replace("capital_usd: 10000", "capital_usd: -5"))
    code, _, err = run("run", "--dry-run", "--once")
    assert code == 2 and "invalid CONFIG block" in err


def test_dry_run_once_uses_its_own_state(run):
    code, out, _ = run("run", "--dry-run", "--once")
    assert code == 0 and "One tick done: DRY RUN" in out
    dry = run.data_dir / "dry-run"
    with Store(dry / "bot.sqlite3") as store:
        assert store.kv_get(HEARTBEAT_KEY) is not None
    assert not (run.data_dir / "bot.sqlite3").exists()  # the real bot's database is untouched


# --------------------------------------------------------------------------- dashboard, jev-ping


def test_dashboard_refuses_public_bind_without_password(run, monkeypatch):
    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    code, _, err = run("dashboard")
    assert code == 2 and "DASHBOARD_PASSWORD" in err


def test_dashboard_serves_on_loopback(run, monkeypatch):
    served = {}
    fake_app = types.ModuleType("bot.dashboard.app")
    fake_app.create_app = lambda settings, cfg, store, results_path, strategy_path: (
        "app", settings.dashboard_host, results_path.name, strategy_path == run.strategy,
    )  # fmt: skip
    monkeypatch.setitem(sys.modules, "bot.dashboard.app", fake_app)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, log_level: served.update(app=app, host=host, port=port))
    code, _, _ = run("dashboard")
    assert code == 0 and served["host"] == "127.0.0.1" and served["port"] == 8080
    assert served["app"] == ("app", "127.0.0.1", "tournament.json", True)


def test_jev_ping_needs_a_key(run):
    code, _, err = run("jev-ping")
    assert code == 2 and "TYPESAFE_API_KEY" in err


def test_jev_ping_reports_latency_tokens_and_cost(run, monkeypatch):
    import bot.jev as jev_mod

    class Client:
        def __init__(self, settings, cfg):
            self.closed = False

        def ask(self, state, questions, model, timeout_s):
            return jev_mod.RawJev(
                model="jev-2026", input_tokens=1000, answers={"ping": {"probabilities": {"yes": 0.9}}}
            )

        def close(self):
            pass

    monkeypatch.setattr(jev_mod, "TypeSafeJevClient", Client)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key")
    code, out, _ = run("jev-ping")
    assert code == 0
    assert "model jev-2026" in out and "1000 input tokens" in out and "cost $0.000042" in out
    assert "ts-test-key" not in out


# --------------------------------------------------------------------------- modules written in parallel


def test_tournament_calls_run_and_write(run, monkeypatch):
    calls = []
    fake = types.ModuleType("bot.backtest.tournament")
    winners = {"SPY": {"label": "trend(atr_mult=3,fast=50,slow=200)"}, "BTC/USD": None}
    fake.run_and_write = lambda apply, cfg_path, out_dir: calls.append((apply, cfg_path, out_dir)) or {
        "n_configs": 90, "winners": winners,
    }  # fmt: skip
    monkeypatch.setitem(sys.modules, "bot.backtest.tournament", fake)
    code, out, _ = run("tournament", "--apply")
    assert code == 0 and "Configurations tested: 90" in out
    assert "SPY: trend(atr_mult=3,fast=50,slow=200)" in out and "BTC/USD: no winner" in out
    assert calls == [(True, run.strategy, run.results)]


def test_drill_and_final_check(run, monkeypatch):
    seen = {}
    fake = types.ModuleType("bot.live_gate")

    def kill_switch_drill(settings, cfg, paper=False):
        seen["paper"] = paper
        return {"passed": True, "steps": ["flat"]}

    fake.kill_switch_drill = kill_switch_drill
    fake.final_check = lambda settings, cfg, strategy_path, results_path: {
        "passed": False, "reasons": ["too few trades"], "paths": [str(strategy_path), str(results_path)],
    }  # fmt: skip
    monkeypatch.setitem(sys.modules, "bot.live_gate", fake)
    code, out, _ = run("drill-kill-switch", "--paper")
    assert code == 0 and seen == {"paper": True} and '"passed": true' in out
    code, out, _ = run("final-check")
    assert code == 1 and "too few trades" in out
    assert str(run.strategy) in out and str(run.results / "tournament.json") in out


def test_drill_refusal_exits_2(run, monkeypatch):
    from bot.config import ConfigError

    def refuse(settings, cfg, paper=False):
        raise ConfigError("the bot is running (fresh heartbeat); stop it before the paper drill")

    fake = types.ModuleType("bot.live_gate")
    fake.kill_switch_drill = refuse
    monkeypatch.setitem(sys.modules, "bot.live_gate", fake)
    code, _, err = run("drill-kill-switch", "--paper")
    assert code == 2 and "fresh heartbeat" in err


def test_export_report(run, monkeypatch):
    code, _, err = run("export-report")
    assert code == 1 and "tournament" in err
    (run.results / "tournament.json").write_text(json.dumps({"runs": [], "winners": {}}))
    fake = types.ModuleType("bot.dashboard.app")
    fake.render_static_report = lambda results: f"<html>{sorted(results)}</html>"
    monkeypatch.setitem(sys.modules, "bot.dashboard.app", fake)
    code, out, _ = run("export-report")
    assert code == 0
    assert (run.results / "tournament.html").read_text() == "<html>['runs', 'winners']</html>"


def test_python_dash_m_entry_point(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    env["BOT_DATA_DIR"] = str(tmp_path / "var")
    strategy = tmp_path / "strategy.md"
    strategy.write_text(STRATEGY_MD)
    proc = subprocess.run(
        [sys.executable, "-m", "bot", "--strategy", str(strategy), "--env-file", str(tmp_path / "no.env"), "status"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert "Kill switch: off" in proc.stdout


def test_run_starts_after_a_passed_gate_expires_so_stops_keep_working(run, monkeypatch):
    from bot.config import LIVE_ACK_PHRASE

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("LIVE_TRADING_ACK", LIVE_ACK_PHRASE)
    run.data_dir.mkdir(parents=True, exist_ok=True)
    (run.data_dir / "live_gate.json").write_text(json.dumps({"passed": True, "ts": "2020-01-01T00:00:00+00:00"}))
    code, _, err = run("run", "--once")
    # Past the live guard: it now stops only at the missing Alpaca keys.
    assert code == 2 and "--dry-run" in err and "final-check" not in err


def test_watchdog_exits_when_the_loop_stalls(monkeypatch):
    import threading
    import time as real_time

    from bot import __main__ as cli

    exited = threading.Event()
    monkeypatch.setattr(cli.os, "_exit", lambda code: exited.set())
    stalled = types.SimpleNamespace(last_progress=real_time.monotonic() - 1_000)
    stop = cli._start_watchdog(stalled, stall_s=300, interval_s=0.01)
    try:
        assert exited.wait(2)
    finally:
        stop.set()
        real_time.sleep(0.05)  # let the thread see the stop flag before the real os._exit is restored


def test_watchdog_stays_quiet_while_the_loop_progresses(monkeypatch):
    import threading
    import time as real_time

    from bot import __main__ as cli

    exited = threading.Event()
    monkeypatch.setattr(cli.os, "_exit", lambda code: exited.set())
    healthy = types.SimpleNamespace(last_progress=real_time.monotonic())
    stop = cli._start_watchdog(healthy, stall_s=300, interval_s=0.01)
    try:
        assert not exited.wait(0.2)
    finally:
        stop.set()
        real_time.sleep(0.05)


def test_backtest_refuses_the_yahoo_crypto_spelling(run):
    code, _, err = run("backtest", "--symbol", "BTC-USD", "--strategy", "momentum")
    assert code == 2 and "BTC/USD" in err


def test_run_starts_after_a_failed_gate_while_holding_positions(run, monkeypatch):
    from datetime import datetime, timezone

    from bot.config import LIVE_ACK_PHRASE
    from bot.models import PositionState

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("LIVE_TRADING_ACK", LIVE_ACK_PHRASE)
    run.data_dir.mkdir(parents=True, exist_ok=True)
    (run.data_dir / "live_gate.json").write_text(json.dumps({"passed": False, "ts": "2026-09-29T00:00:00+00:00"}))
    with Store(settings_for(run.data_dir).db_path) as store:
        store.put_position(PositionState("SPY", "breakout", 2.0, 500.0, datetime(2026, 9, 28, tzinfo=timezone.utc), 480.0))
    code, _, err = run("run", "--once")
    # Past the live guard: it now stops only at the missing Alpaca keys.
    assert code == 2 and "--dry-run" in err and "final-check" not in err
