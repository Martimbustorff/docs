"""Command-line interface: `python -m bot [global options] <command> [options]`.

Global options go before the command: `--strategy PATH` (the strategy.md to read),
`--results-dir PATH` (tournament output) and `--env-file PATH` (the .env to load).

Exit codes: 0 success, 1 failure, 2 refused or configuration error. Output is plain text on
stdout; logs go to stderr. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.config import (
    ROOT,
    STRATEGY_PATH,
    ConfigError,
    Settings,
    StrategyConfig,
    check_symbol,
    load_settings,
    load_strategy,
)
from bot.models import OrderResult
from bot.timeutil import UTC, ny_trading_day, utcnow

if TYPE_CHECKING:
    from bot.store import Store

log = logging.getLogger("bot")

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
DEFAULT_SYMBOLS = ("SPY", "QQQ", "BTC/USD")
HEARTBEAT_STALE_S = 300
LOOPBACK_NAMES = {"localhost"}


class Refused(Exception):
    """The command cannot run as configured (exit code 2)."""


# --------------------------------------------------------------------------- shared helpers


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(env_file=args.env_file)


def _config(args: argparse.Namespace) -> StrategyConfig:
    return load_strategy(args.strategy_path)


def _store(settings: Settings) -> Store:
    from bot.store import Store

    return Store(settings.db_path)


def _age(ts: datetime, now: datetime) -> str:
    seconds = max(0.0, (now - ts).total_seconds())
    if seconds < 120:
        return f"{seconds:.0f}s ago"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def _utc(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _usd(value: float | None) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"${value:,.2f}"


def parse_params(pairs: list[str]) -> dict[str, Any]:
    """`k=v` pairs with numbers and booleans coerced: `fast=20` -> 20, `atr_mult=2.5` -> 2.5."""
    params: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise Refused(f"--params expects key=value, got {pair!r}")
        params[key.strip()] = _coerce(raw.strip())
    return params


def _coerce(raw: str) -> Any:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    try:
        number = float(raw)
    except ValueError:
        return raw
    return number if math.isfinite(number) else raw


def _is_loopback(host: str) -> bool:
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _results_json(args: argparse.Namespace) -> Path:
    return Path(args.results_dir) / "tournament.json"


def _print_result(result: Any) -> None:
    if isinstance(result, dict):
        print(json.dumps(result, indent=2, default=str, sort_keys=False))
    elif result is not None:
        print(result)


# --------------------------------------------------------------------------- commands


def cmd_fetch_data(args: argparse.Namespace) -> int:
    from bot.data import fetch_daily

    symbols = list(DEFAULT_SYMBOLS)
    try:
        symbols += [s for s in _config(args).assets if s not in symbols]
    except ConfigError as exc:
        log.warning("strategy.md unreadable (%s); fetching the default symbols", exc)
    counts = fetch_daily(symbols)
    for symbol, rows in counts.items():
        print(f"{symbol}: {rows} daily bars" if rows else f"{symbol}: FAILED (cache left unchanged)")
    return EXIT_OK if all(counts.values()) else EXIT_FAILED


def cmd_backtest(args: argparse.Namespace) -> int:
    from bot.backtest.engine import BacktestConfig, run_backtest
    from bot.backtest.metrics import compute_metrics
    from bot.data import load_daily
    from bot.strategies import build
    from bot.timeutil import periods_per_year

    cfg = _config(args)
    symbol = args.symbol.upper()
    params = parse_params(args.params)
    try:
        check_symbol(symbol)
        strategy = build(args.strategy_name, symbol, params)
        bt_cfg = BacktestConfig.from_strategy(cfg, symbol)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    try:
        bars = load_daily(symbol)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    result = run_backtest(bars, strategy, bt_cfg, args.start, args.end)
    if result.equity.empty:
        print(f"No {symbol} bars between {args.start or 'the start'} and {args.end or 'the end'}.", file=sys.stderr)
        return EXIT_FAILED
    metrics = compute_metrics(result, periods_per_year(symbol))
    first, last = result.equity.index[0].date(), result.equity.index[-1].date()
    print(f"Backtest {symbol} {strategy.label()} from {first} to {last}")
    print(f"Capital ${bt_cfg.capital_usd:,.0f}, slippage {bt_cfg.slippage_bps:g} bps, fees {bt_cfg.fee_bps:g} bps")
    print()
    for key, value in metrics.items():
        shown = f"{value:,.4f}" if isinstance(value, float) and math.isfinite(value) else str(value)
        print(f"  {key:<22} {shown}")
    print(
        f"  {'entry_orders':<22} {result.entry_orders} ({result.orders_needing_approval} above the approval threshold)"
    )
    if result.trades:
        print("\nLast trades:")
        for trade in result.trades[-10:]:
            print(
                f"  {trade.entry_ts.date()} -> {trade.exit_ts.date()}  {trade.exit_reason:<11} "
                f"{trade.pnl_pct * 100:+7.2f}%  {_usd(trade.pnl)}"
            )
    return EXIT_OK


def cmd_tournament(args: argparse.Namespace) -> int:
    from bot.backtest.tournament import run_and_write

    _config(args)  # refuse early on an invalid strategy.md
    result = run_and_write(args.apply, Path(args.strategy_path), Path(args.results_dir))
    if isinstance(result, dict):
        print(f"Configurations tested: {result.get('n_configs', 'n/a')}")
        for symbol, winner in (result.get("winners") or {}).items():
            print(f"  {symbol}: {winner.get('label') if isinstance(winner, dict) else 'no winner (stays disabled)'}")
    print(
        f"Results written to {args.results_dir}" + (f"; winners applied to {args.strategy_path}" if args.apply else "")
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from bot.broker import AlpacaBroker, OrderGateway
    from bot.jev import JevGate
    from bot.notify import ConsoleNotifier, build_notifier
    from bot.risk import KillSwitch, RiskManager
    from bot.runner import Runner

    settings = _settings(args)
    cfg = _config(args)
    if args.dry_run:
        # Separate state, so a dry run can never touch the real bot's database or kill switch.
        settings = settings.model_copy(update={"data_dir": settings.data_dir / "dry-run", "heartbeat_url": None})
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    _startup_guard(settings)
    if args.dry_run:
        broker: Any = _dry_run_broker(cfg)
    else:
        try:
            broker = AlpacaBroker(settings)
        except ConfigError as exc:
            raise Refused(f"{exc}. Put your Alpaca paper keys in .env, or use `run --dry-run`.") from None
    store = _store(settings)
    kill = KillSwitch(settings, store)
    risk = RiskManager(cfg.risk, store, kill)
    notifier = ConsoleNotifier() if args.dry_run else build_notifier(settings, store)
    gateway = OrderGateway(broker, risk, kill, store, settings, notifier)
    jev_gate = JevGate(cfg.jev, _jev_client(settings, cfg, args.dry_run))
    runner = Runner(settings, cfg, broker, gateway, risk, kill, jev_gate, notifier, store)
    mode = "DRY RUN with a simulated broker" if args.dry_run else settings.trading_mode.upper()
    enabled = ", ".join(f"{s} ({r.strategy})" for s, r in cfg.enabled_assets.items()) or "none"
    log.info("starting the bot: %s; enabled assets: %s; Jev mode %s", mode, enabled, cfg.jev.mode)
    if args.once:
        failed = runner.tick()
        print(f"One tick done: {mode}" + (f"; failed steps: {', '.join(failed)}" if failed else "."))
        return EXIT_FAILED if failed else EXIT_OK
    _start_watchdog(runner, stall_s=max(300.0, 5.0 * cfg.execution.poll_seconds))
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        log.info("stopped by the operator")
    return EXIT_OK


def _start_watchdog(runner: Any, stall_s: float, interval_s: float = 30.0) -> Any:
    """Exit the process if the loop stops making progress (a hung network call, a deadlock), so
    Docker's restart policy brings the bot back and stops, exits and /kill work again.
    Returns the Event that stops the watchdog."""
    import threading

    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(interval_s):
            last = getattr(runner, "last_progress", None)
            if last is not None and time.monotonic() - last > stall_s:
                # Log from a helper thread with a deadline: a stalled loop may hold the log lock.
                note = threading.Thread(
                    target=log.critical,
                    args=("the runner loop made no progress for %.0fs; exiting so it restarts", stall_s),
                    daemon=True,
                )
                note.start()
                note.join(2.0)
                os._exit(EXIT_FAILED)

    threading.Thread(target=watch, name="watchdog", daemon=True).start()
    return stop


LIVE_STARTED_KEY = "live_started_ts"


def _startup_guard(settings: Settings) -> None:
    """Refuse a mismatched mode/endpoint, and a first live start without a passed final check.

    Once a live start has passed the strict check (or the gate once passed), an expired or failed
    final check does not stop the process: the runner keeps blocking entries every tick, and
    refusing to start would leave open positions without stops after any restart or reboot.
    """
    from bot.broker import assert_trading_allowed

    assert_trading_allowed(settings, settings.live_gate_path, risk_increasing=False)
    if settings.trading_mode != "live":
        return
    try:
        assert_trading_allowed(settings, settings.live_gate_path)
    except ConfigError as exc:
        if not (_gate_passed_once(settings.live_gate_path) or _live_started_before(settings)):
            raise
        log.warning(
            "%s. Starting anyway so stops and exits keep protecting open positions; "
            "new entries stay blocked until final-check passes.", exc,
        )
        return
    with _store(settings) as store:  # final-check can't overwrite this, unlike live_gate.json
        store.kv_set(LIVE_STARTED_KEY, utcnow().isoformat())


def _live_started_before(settings: Settings) -> bool:
    """True once a live start has passed the strict check. Paper runs never set it, so paper rows
    in a shared database can't open the door to an unchecked live start."""
    if not settings.db_path.exists():
        return False
    with _store(settings) as store:
        return bool(store.kv_get(LIVE_STARTED_KEY))


def _gate_passed_once(path: Path) -> bool:
    try:
        return json.loads(path.read_text()).get("passed") is True
    except (OSError, ValueError, AttributeError):
        return False


def _dry_run_broker(cfg: StrategyConfig) -> Any:
    from bot.broker import SimBroker
    from bot.data import load_daily

    broker = SimBroker(
        cash=cfg.risk.capital_usd, slippage_bps=cfg.execution.slippage_bps.get("stock", 0.0), market_hours=True
    )
    for symbol in cfg.assets:
        try:
            bars = load_daily(symbol)
        except FileNotFoundError as exc:
            raise Refused(str(exc)) from None
        broker.set_bars(symbol, bars)
        broker.set_price(symbol, float(bars["close"].iloc[-1]))
    return broker


def _jev_client(settings: Settings, cfg: StrategyConfig, dry_run: bool) -> Any:
    from bot.jev import FakeJevClient, TypeSafeJevClient

    if settings.has_jev:
        return TypeSafeJevClient(settings, cfg.jev)
    if dry_run:
        log.info("no TYPESAFE_API_KEY: the dry run uses a fake Jev")
        return FakeJevClient()
    if cfg.jev.mode != "off":
        log.warning("TYPESAFE_API_KEY is not set: Jev fails closed, so every entry will be blocked")
    return None


def cmd_dashboard(args: argparse.Namespace) -> int:
    settings = _settings(args)
    cfg = _config(args)
    host, port = settings.dashboard_host, settings.dashboard_port
    if not _is_loopback(host) and settings.dashboard_password is None:
        raise Refused(f"refusing to serve the dashboard on {host} without DASHBOARD_PASSWORD; set it in .env")
    import uvicorn

    from bot.dashboard.app import create_app

    app = create_app(
        settings, cfg, _store(settings), results_path=_results_json(args), strategy_path=args.strategy_path
    )
    print(f"Dashboard on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from bot.report import daily_report, report_path

    settings = _settings(args)
    cfg = _config(args)
    day = args.day or ny_trading_day(utcnow())
    with _store(settings) as store:
        text = daily_report(store, settings, cfg, day)
    print(text)
    print(f"Saved to {report_path(settings, day)}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from bot.broker import assert_trading_allowed
    from bot.risk import KillSwitch
    from bot.runner import HEARTBEAT_KEY, load_queue
    from bot.store import parse_ts

    settings = _settings(args)
    now = utcnow()
    config_error: str | None = None
    try:
        cfg: StrategyConfig | None = _config(args)
    except ConfigError as exc:
        cfg, config_error = None, str(exc)
    paper = "true" if settings.alpaca_paper else "false"
    print(f"Trading mode: {settings.trading_mode.upper()} (TRADING_MODE={settings.trading_mode}, ALPACA_PAPER={paper})")
    try:
        assert_trading_allowed(settings, settings.live_gate_path, now=now)
        print("Live-trading guard: OK")
    except ConfigError as exc:
        print(f"Live-trading guard: REFUSED: {exc}")
    print(f"Live gate: {_live_gate_text(settings.live_gate_path, now)}")
    if config_error:
        print(f"Config: INVALID: {config_error}")
    else:
        assert cfg is not None
        enabled = ", ".join(f"{s} ({r.strategy})" for s, r in cfg.enabled_assets.items()) or "none"
        print(f"Config: {args.strategy_path} OK; enabled assets: {enabled}; Jev mode {cfg.jev.mode}")
    with _store(settings) as store:
        kill = KillSwitch(settings, store)
        if kill.is_tripped():
            info = kill.status() or {}
            print(f"Kill switch: TRIPPED: {info.get('reason')} (source {info.get('source')}, at {info.get('ts')})")
        else:
            print("Kill switch: off")
        beat = store.kv_get(HEARTBEAT_KEY)
        if beat:
            ts = parse_ts(beat)
            stale = " (STALE: is the bot running?)" if (now - ts).total_seconds() > HEARTBEAT_STALE_S else ""
            print(f"Heartbeat: {_utc(ts)} ({_age(ts, now)}){stale}")
        else:
            print("Heartbeat: never (the bot has not run with this data directory)")
        _print_positions(store)
        equity = store.latest_equity()
        if equity is None:
            print("Last equity: none recorded yet")
        else:
            print(
                f"Last equity: bot {_usd(equity['bot_equity'])}, account {_usd(equity['account_equity'])}, "
                f"exposure {_usd(equity['exposure'])} at {_utc(parse_ts(equity['ts']))}"
            )
        _print_approvals(store)
        print(f"Queued orders: {len(load_queue(store))}")
    return EXIT_REFUSED if config_error else EXIT_OK


def _live_gate_text(path: Path, now: datetime) -> str:
    from bot.broker import LIVE_GATE_MAX_AGE
    from bot.store import parse_ts

    if not path.exists():
        return f"not found ({path}); run `python -m bot final-check` after paper trading"
    try:
        data = json.loads(path.read_text())
        ts = parse_ts(data["ts"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"unreadable ({type(exc).__name__})"
    passed = data.get("passed") is True
    left = LIVE_GATE_MAX_AGE - (now - ts)
    expiry = f"expires in {left.total_seconds() / 86400:.1f} days" if left.total_seconds() > 0 else "EXPIRED"
    return f"{'passed' if passed else 'NOT passed'}, written {_age(ts, now)} ({expiry})"


def _print_positions(store: Any) -> None:
    positions = store.get_positions()
    if not positions:
        print("Positions: none")
        return
    print("Positions:")
    for symbol, pos in positions.items():
        print(
            f"  {symbol}: {pos.qty:g} @ {pos.entry_price:,.2f}, stop {pos.stop_price:,.2f}, "
            f"{pos.strategy}, {pos.bars_held} bars, entered {_utc(pos.entry_ts)}"
        )


def _print_approvals(store: Any) -> None:
    from bot.store import parse_ts

    pending = store.pending_approvals()
    if not pending:
        print("Pending approvals: none")
        return
    print("Pending approvals:")
    for approval in pending:
        signal = store.get_signal(approval["signal_id"]) if approval["signal_id"] is not None else None
        symbol = signal["symbol"] if signal else "?"
        expires = _utc(parse_ts(approval["expires_ts"]))
        print(f"  #{approval['id']} {symbol} {_usd(approval['notional'])}, expires {expires}")


def cmd_kill(args: argparse.Namespace) -> int:
    from bot.broker import AlpacaBroker, OrderGateway
    from bot.notify import build_notifier
    from bot.risk import KillSwitch, RiskManager
    from bot.runner import hand_off_orders

    settings = _settings(args)
    reason = args.reason or "manual kill from the CLI"
    with _store(settings) as store:
        kill = KillSwitch(settings, store)
        kill.trip(reason, "cli")
        print(f"Kill switch tripped: {reason}")
        if not settings.has_alpaca:
            print(
                "No Alpaca keys are configured here, so nothing was flattened. A running bot flattens on its next poll."
            )
            return EXIT_OK
        try:
            risk: Any = RiskManager(_config(args).risk, store, kill)
        except ConfigError as exc:
            print(f"strategy.md is invalid ({exc}); flattening without error counting.")
            risk = _UncountedErrors()
        broker = AlpacaBroker(settings)
        gateway = OrderGateway(broker, risk, kill, store, settings, build_notifier(settings, store))
        results = gateway.flatten_all(f"CLI kill: {reason}")
        hand_off_orders(store, results)
        return _report_flatten(results)


class _UncountedErrors:
    """Stands in for RiskManager when strategy.md cannot be read: the flatten must still run."""

    def after_error(self) -> None:
        log.error("order error during the CLI flatten")

    def after_success(self) -> None:
        pass


def _report_flatten(results: list[OrderResult]) -> int:
    if not results:
        print("No open positions to close; open orders were cancelled.")
        return EXIT_OK
    failed = False
    for result in results:
        ok = result.status not in ("rejected", "refused")
        failed = failed or not ok
        detail = f": {result.message}" if result.message and not ok else ""
        print(f"  {result.client_order_id}: {result.status}{detail}")
    if failed:
        print("Some positions could NOT be closed. Close them in the Alpaca dashboard now.")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    from bot.risk import KillSwitch

    settings = _settings(args)
    with _store(settings) as store:
        kill = KillSwitch(settings, store)
        if not kill.is_tripped():
            print("The kill switch is not tripped; nothing to resume.")
            return EXIT_OK
        previous = kill.status() or {}
        try:
            kill.reset(confirm=args.confirm)
        except ValueError as exc:
            raise Refused(str(exc)) from None
    print(f"WARNING: kill switch cleared (it was tripped by {previous.get('source')}: {previous.get('reason')}).")
    print("The bot resumes new entries on its next poll. Make sure you know why it tripped.")
    return EXIT_OK


def cmd_drill(args: argparse.Namespace) -> int:
    from bot.live_gate import kill_switch_drill

    result = kill_switch_drill(_settings(args), _config(args), paper=args.paper)
    _print_result(result)
    return EXIT_OK if isinstance(result, dict) and result.get("passed") else EXIT_FAILED


def cmd_final_check(args: argparse.Namespace) -> int:
    from bot.live_gate import final_check

    result = final_check(
        _settings(args), _config(args), strategy_path=Path(args.strategy_path), results_path=_results_json(args)
    )
    _print_result(result)
    return EXIT_OK if isinstance(result, dict) and result.get("passed") else EXIT_FAILED


def cmd_jev_ping(args: argparse.Namespace) -> int:
    from bot.broker import redact
    from bot.jev import TypeSafeJevClient

    settings = _settings(args)
    cfg = _config(args)
    if not settings.has_jev:
        raise Refused("TYPESAFE_API_KEY is not set; put it in .env")
    client = TypeSafeJevClient(settings, cfg.jev)
    state = {"note": "connectivity check from `python -m bot jev-ping`; no trade depends on this answer"}
    questions = {
        "ping": {
            "type": "noul",
            "instructions": "Is this request a connectivity check?",
            "criteria": {"true": "It is a connectivity check.", "false": "It is anything else."},
        }
    }
    start = time.perf_counter()
    try:
        raw = client.ask(state, questions, cfg.jev.model, cfg.jev.timeout_s)
    except Exception as exc:
        print(f"Jev call failed after {(time.perf_counter() - start) * 1000:.0f} ms: "
              f"{redact(f'{type(exc).__name__}: {exc}', settings)[:300]}")  # fmt: skip
        return EXIT_FAILED
    finally:
        client.close()
    latency_ms = (time.perf_counter() - start) * 1000
    cost = raw.input_tokens * cfg.jev.price_per_million_input_tokens / 1e6
    p_yes = raw.answers.get("ping", {}).get("probabilities", {}).get("yes")
    print(f"Jev OK: model {raw.model}, {latency_ms:.0f} ms, {raw.input_tokens} input tokens, cost ${cost:.6f}")
    print(f"P(connectivity check) = {p_yes}")
    return EXIT_OK


def cmd_export_report(args: argparse.Namespace) -> int:
    from bot.dashboard.app import render_static_report

    results_dir = Path(args.results_dir)
    source = _results_json(args)
    if not source.exists():
        print(f"{source} not found; run `python -m bot tournament` first.", file=sys.stderr)
        return EXIT_FAILED
    html = render_static_report(json.loads(source.read_text()))
    target = results_dir / "tournament.html"
    _write_atomic(target, html)
    print(f"Wrote {target}")
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bot", description="Paper-first trading bot.")
    parser.add_argument("--strategy", dest="strategy_path", type=Path, default=STRATEGY_PATH,
                        help="strategy.md to read (default: %(default)s)")  # fmt: skip
    parser.add_argument("--results-dir", type=Path, default=ROOT / "results",
                        help="tournament results directory (default: %(default)s)")  # fmt: skip
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".env", help="the .env file to load (default: %(default)s)"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("fetch-data", help="download daily bars into data/cache/").set_defaults(handler=cmd_fetch_data)

    p = sub.add_parser("backtest", help="backtest one strategy on one symbol")
    p.add_argument("--symbol", required=True)
    p.add_argument("--strategy", dest="strategy_name", required=True, help="trend | breakout | meanrev | momentum")
    p.add_argument("--params", nargs="*", default=[], metavar="K=V")
    p.add_argument("--start", help="first traded date, YYYY-MM-DD")
    p.add_argument("--end", help="last traded date, YYYY-MM-DD")
    p.set_defaults(handler=cmd_backtest)

    p = sub.add_parser("tournament", help="run every strategy on every symbol and write results/")
    p.add_argument("--apply", action="store_true", help="enable the winners in strategy.md")
    p.set_defaults(handler=cmd_tournament)

    p = sub.add_parser("run", help="start the live loop")
    p.add_argument("--dry-run", action="store_true", help="simulated broker, console alerts, fake Jev without a key")
    p.add_argument("--once", action="store_true", help="run one tick and exit")
    p.set_defaults(handler=cmd_run)

    sub.add_parser("dashboard", help="serve the read-only dashboard").set_defaults(handler=cmd_dashboard)

    p = sub.add_parser("report", help="build the daily report")
    p.add_argument("--day", type=date.fromisoformat, help="New York day, YYYY-MM-DD (default: today)")
    p.set_defaults(handler=cmd_report)

    sub.add_parser("status", help="mode, kill switch, positions, approvals, heartbeat").set_defaults(handler=cmd_status)

    p = sub.add_parser("kill", help="trip the kill switch and flatten")
    p.add_argument("--reason", default="")
    p.set_defaults(handler=cmd_kill)

    p = sub.add_parser("resume", help="clear the kill switch")
    p.add_argument("--confirm", action="store_true", help="required")
    p.set_defaults(handler=cmd_resume)

    p = sub.add_parser("drill-kill-switch", help="rehearse the kill switch")
    p.add_argument("--paper", action="store_true", help="against the Alpaca paper account instead of a simulator")
    p.set_defaults(handler=cmd_drill)

    sub.add_parser("final-check", help="compare paper with the backtest and write the live gate").set_defaults(
        handler=cmd_final_check
    )
    sub.add_parser("jev-ping", help="one tiny Jev call to check the key").set_defaults(handler=cmd_jev_ping)
    sub.add_parser("export-report", help="write results/tournament.html").set_defaults(handler=cmd_export_report)
    return parser


def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging()
    try:
        return args.handler(args)
    except (ConfigError, Refused) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        log.exception("%s failed", args.command)
        print(f"{args.command} failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
