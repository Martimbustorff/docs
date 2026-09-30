"""Pre-live safety checks: the kill-switch drill and the final check.

`kill_switch_drill` proves the kill switch works end to end. It runs in its own state directory
(`var/drill/<timestamp>/`) with its own store and kill switch, so it never trips or pollutes the
bot's real state. Only its verdict lands in the real `var/kill_switch_drill.json`.

`final_check` answers the three questions to ask before going live (does paper match the
backtest, did the kill switch fire in testing, what market regime would break this), writes
`var/live_gate.json` and rewrites the FINAL_CHECK section of strategy.md. It never switches the
bot to live and never touches `.env`: going live stays a human decision.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

import numpy as np
import pandas as pd

from bot import indicators as ind
from bot.backtest.engine import BacktestConfig, run_backtest
from bot.broker import (
    QTY_EPSILON,
    AlpacaBroker,
    Broker,
    OrderGateway,
    SimBroker,
    floor_qty,
    make_client_order_id,
    redact,
)
from bot.config import ROOT, STRATEGY_PATH, AssetRule, ConfigError, Settings, StrategyConfig, replace_section
from bot.data import load_daily
from bot.models import (
    AssetClass,
    OrderIntent,
    OrderPurpose,
    OrderResult,
    PositionState,
    RiskAction,
    Side,
    Signal,
    SignalKind,
    asset_class,
)
from bot.notify import Command, ConsoleNotifier, Notifier, TelegramNotifier, build_notifier
from bot.risk import KillSwitch, RiskContext, RiskManager
from bot.store import Store, parse_ts, to_iso
from bot.strategies import build
from bot.strategies.base import Strategy
from bot.timeutil import UTC, bar_close_ts, periods_per_year, utcnow

log = logging.getLogger(__name__)

RESULTS_PATH = ROOT / "results" / "tournament.json"
FINAL_CHECK_SECTION = "FINAL_CHECK"
BLOW_UP_HEADING = "WHAT COULD BLOW UP THIS ACCOUNT?"
FALLBACK_REPORT = "final_check.md"  # under data_dir, when strategy.md cannot be written

# Drill
DRILL_SYMBOL = "BTC/USD"  # trades 24/7, so the drill works at any hour
DRILL_NOTIONAL_USD = 5.0  # Alpaca's minimum is $1
SIM_PRICE = 50_000.0
DRILL_REASON = "kill-switch drill"
POLL_S = 2.0
FILL_POLLS = 30  # wait up to a minute for the paper entry to fill
SETTLE_POLLS = 15  # after each flatten_all, wait up to 30 s for Alpaca to cancel and close
FLATTEN_ATTEMPTS = 3  # Alpaca cancels asynchronously, so flatten again if still not flat
# The runner's liveness marker. The contract names "runner_heartbeat"; the dashboard reads
# "heartbeat", so either one being fresh means the bot is running.
HEARTBEAT_KEYS = ("runner_heartbeat", "heartbeat")
HEARTBEAT_MAX_AGE = timedelta(minutes=5)
EQUITY_MAX_AGE = timedelta(minutes=20)  # without any heartbeat: the runner records equity every 15 min
OPEN_ORDER_STATUSES = frozenset({"new", "accepted", "partially_filled"})

# Final check
EXECUTED_STATUSES = frozenset({"submitted", "filled"})
NEAR_KILL_FRACTION = 0.8  # a backtest drawdown past 80% of the kill level means it will likely fire
CLOCK_SKEW = timedelta(minutes=5)
DATA_LAG = timedelta(days=4)  # a long weekend: the replay's last bar may be this far behind now
HISTORY_FACTOR = 1.6  # calendar days per warmup bar fetched before the paper period (weekends, holidays)
HISTORY_EXTRA_DAYS = 60  # extra history so recursive indicators (ATR, RSI) have converged
ATR_N = 14
SCAN_LIMIT = 1_000_000  # "every row" for the store's paged readers

_BREAKS = {
    "trend": (
        "a choppy, sideways market: the averages keep crossing, so it buys false starts and is "
        "stopped out again and again, and a sudden crash can gap through the trailing stop "
        "before the close falls below the slow average"
    ),
    "breakout": (
        "a range-bound market full of false breakouts: it buys each new high just before the "
        "price falls back into the range"
    ),
    "meanrev": (
        "a sell-off that keeps going: it buys a dip while the close is still above the 200-day "
        "average, the next dips are deeper, and the fixed stop and the time exit both take the loss"
    ),
    "momentum": (
        "a sharp reversal after a long run (a momentum crash): the lookback return stays positive "
        "for weeks after the top, so it holds into the fall until the trailing stop hits"
    ),
}


# --------------------------------------------------------------------------- shared helpers


def _aware(ts: datetime) -> datetime:
    return (ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)).astimezone(UTC)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_ts(value)
    except ValueError:
        return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _jsonable(value: Any) -> Any:
    """JSON-safe copy: non-finite floats become null, floats are rounded, dates become ISO."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return round(number, 4) if math.isfinite(number) else None
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return str(value)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _write_atomic(path, json.dumps(_jsonable(data), indent=2) + "\n")


@contextmanager
def _real_store(settings: Settings) -> Iterator[Store]:
    """The bot's store for reading. A missing database reads as empty and is not created."""
    store = Store(settings.db_path if settings.db_path.exists() else ":memory:")
    try:
        yield store
    finally:
        store.close()


def _describe(exc: BaseException, settings: Settings) -> str:
    return redact(f"{type(exc).__name__}: {exc}", settings)[:300]


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- kill-switch drill


class _Outcome(NamedTuple):
    ok: bool
    detail: str
    skipped: bool = False


class _RecordingNotifier:
    """Forwards to the real notifier and keeps every alert, so the drill can prove one was sent."""

    def __init__(self, inner: Notifier) -> None:
        self.inner = inner
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
        self.inner.send(text)

    def request_approval(self, approval_id: int, text: str) -> int | None:
        return self.inner.request_approval(approval_id, text)

    def poll(self) -> list[Command]:
        return self.inner.poll()

    @property
    def channel(self) -> str:
        if isinstance(self.inner, TelegramNotifier):
            return "Telegram"
        if isinstance(self.inner, ConsoleNotifier):
            return "the console log (Telegram is not configured)"
        return type(self.inner).__name__


class _Drill:
    """One drill run: every step records ok/detail, and later steps run even when earlier ones
    fail, so the account is always flattened at the end."""

    def __init__(
        self,
        settings: Settings,
        real_kill_path: Path,
        cfg: StrategyConfig,
        broker: Broker,
        store: Store,
        notifier: _RecordingNotifier,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        stamp: str,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.stamp = stamp
        self.symbol = DRILL_SYMBOL
        self.kill = KillSwitch(settings, store)
        self.risk = RiskManager(cfg.risk, store, self.kill)
        self.gateway = OrderGateway(broker, self.risk, self.kill, store, settings, notifier)
        self.real_kill_path = real_kill_path
        self.real_kill_before = _file_bytes(real_kill_path)
        self.cids: list[str] = []
        self.alerts_before = 0
        self.price: float | None = None
        self.steps: list[dict[str, Any]] = []

    def run(self) -> list[dict[str, Any]]:
        steps: tuple[tuple[str, Callable[[], _Outcome]], ...] = (
            ("entry", self._entry),
            ("resting_order", self._resting_order),
            ("trip", self._trip),
            ("flatten", self._flatten),
            ("no_open_orders", self._no_open_orders),
            ("flat", self._flat),
            ("entry_refused", self._entry_refused),
            ("alert_sent", self._alert_sent),
            ("exit_allowed", self._exit_allowed),
            ("bot_state_untouched", self._bot_state_untouched),
        )
        for name, action in steps:
            self._run_step(name, action)
        return self.steps

    def _run_step(self, name: str, action: Callable[[], _Outcome]) -> None:
        try:
            outcome = action()
        except Exception as exc:
            log.exception("drill step %s raised", name)
            outcome = _Outcome(False, f"error: {_describe(exc, self.settings)}")
        step: dict[str, Any] = {"name": name, "ok": outcome.ok, "detail": outcome.detail}
        if outcome.skipped:
            step["skipped"] = True
        log.log(logging.INFO if outcome.ok else logging.ERROR, "drill step %s: %s", name, outcome.detail)
        self.steps.append(step)

    # ------------------------------------------------------------------ steps

    def _entry(self) -> _Outcome:
        intent, result, why = self._submit_entry("entry")
        if result is None:
            return _Outcome(False, why)
        result = self._await_fill(result)
        ok = result.status == "filled" and result.filled_qty > 0
        return _Outcome(
            ok,
            f"bought {result.filled_qty:g} {self.symbol} (about ${intent.notional:,.2f}) through "
            f"OrderGateway after RiskManager said '{why}': {result.status}{_message(result)}",
        )

    def _resting_order(self) -> _Outcome:
        if not isinstance(self.broker, SimBroker):
            return _Outcome(
                True,
                "skipped: the broker only takes market orders, and Alpaca fills a small BTC/USD "
                "market order at once, so no order can be left resting",
                skipped=True,
            )
        original = self.broker.fill_ratio
        self.broker.fill_ratio = 0.5  # SimBroker leaves the rest of the order open
        try:
            intent, result, why = self._submit_entry("resting")
        finally:
            self.broker.fill_ratio = original
        if result is None:
            return _Outcome(False, why)
        ok = result.status in OPEN_ORDER_STATUSES
        return _Outcome(
            ok, f"order {intent.client_order_id} is {result.status} ({result.filled_qty:g} of {intent.qty:g} filled)"
        )

    def _trip(self) -> _Outcome:
        self.alerts_before = len(self.notifier.sent)
        self.kill.trip(DRILL_REASON, source="drill")
        ok = self.kill.is_tripped() and self.kill.path.exists()
        return _Outcome(ok, f"tripped the drill's own kill switch at {self.kill.path}")

    def _flatten(self) -> _Outcome:
        closes: list[str] = []
        for attempt in range(1, FLATTEN_ATTEMPTS + 1):
            results = self.gateway.flatten_all(DRILL_REASON)
            self.cids += [r.client_order_id for r in results if r.broker_order_id]
            closes += [f"{r.client_order_id} {r.status}" for r in results]
            if self._settled():
                return _Outcome(
                    True,
                    f"flatten_all cancelled every open order and closed every position after {attempt} "
                    f"call(s): {', '.join(closes) or 'nothing to close'}",
                )
        return _Outcome(False, f"still not flat after {FLATTEN_ATTEMPTS} flatten_all calls: {', '.join(closes)}")

    def _no_open_orders(self) -> _Outcome:
        still_open = self._open_orders()
        if still_open:
            return _Outcome(False, "still open: " + ", ".join(f"{cid} ({s})" for cid, s in still_open.items()))
        return _Outcome(True, f"none of the drill's {len(set(self.cids))} orders is still open")

    def _flat(self) -> _Outcome:
        held = self._open_positions()
        if held:
            return _Outcome(False, "still holding " + ", ".join(f"{s} {q:g}" for s, q in held.items()))
        return _Outcome(True, "the account holds no positions")

    def _entry_refused(self) -> _Outcome:
        price = self._price()
        intent = self._intent("after-trip", floor_qty(DRILL_NOTIONAL_USD / price), price)
        verdict = self.risk.check(intent, self._context())
        result = self.gateway.submit(intent)
        risk_ok = verdict.action is RiskAction.BLOCK and "kill switch" in verdict.reason
        gateway_ok = result.status == "refused" and "kill switch is tripped" in result.message
        return _Outcome(
            risk_ok and gateway_ok,
            f"RiskManager: {verdict.action.value} ({verdict.reason}); OrderGateway: {result.status}{_message(result)}",
        )

    def _alert_sent(self) -> _Outcome:
        alerts = self.notifier.sent[self.alerts_before :]
        flatten = [text for text in alerts if text.startswith("Flatten (")]
        if not flatten:
            return _Outcome(False, f"no flatten alert reached the notifier ({len(alerts)} other alert(s))")
        return _Outcome(
            True, f"{len(alerts)} alert(s) sent to {self.notifier.channel} after the trip, including: {_clip(flatten[0])}"
        )

    def _exit_allowed(self) -> _Outcome:
        price = self._price()
        qty = floor_qty(DRILL_NOTIONAL_USD / price)
        ctx = self._context()
        verdicts = {
            purpose.value: self.risk.check(self._intent(f"check-{purpose.value}", qty, price, purpose), ctx)
            for purpose in OrderPurpose
            if not purpose.increases_risk
        }
        blocked = {name: v.reason for name, v in verdicts.items() if v.action is not RiskAction.ALLOW}
        if blocked:
            return _Outcome(False, "blocked while tripped: " + "; ".join(f"{k}: {v}" for k, v in blocked.items()))
        return _Outcome(True, f"RiskManager still allows {', '.join(verdicts)} orders while the switch is tripped")

    def _bot_state_untouched(self) -> _Outcome:
        now = _file_bytes(self.real_kill_path)
        if now != self.real_kill_before:
            return _Outcome(False, f"the bot's kill switch file {self.real_kill_path} changed during the drill")
        state = "still absent" if now is None else "unchanged"
        return _Outcome(True, f"the bot's own kill switch ({self.real_kill_path}) is {state}")

    # ------------------------------------------------------------------ helpers

    def _price(self) -> float:
        if self.price is None:
            self.price = self.broker.last_price(self.symbol)
        return self.price

    def _intent(
        self, tag: str, qty: float, price: float, purpose: OrderPurpose = OrderPurpose.ENTRY
    ) -> OrderIntent:
        return OrderIntent(
            symbol=self.symbol,
            side=Side.BUY if purpose.increases_risk else Side.SELL,
            qty=qty,
            ref_price=price,
            purpose=purpose,
            reason=f"{DRILL_REASON}: {tag}",
            client_order_id=make_client_order_id(purpose, self.symbol, f"drill|{self.stamp}|{tag}"),
        )

    def _context(self) -> RiskContext:
        capital = self.cfg.risk.capital_usd
        positions = self.broker.positions()
        now = _aware(self.clock())
        return RiskContext(
            bot_equity=capital,
            start_of_day_equity=capital,
            open_exposure_usd=sum(abs(p.market_value) for p in positions.values()),
            symbol_exposure_usd=abs(positions[self.symbol].market_value) if self.symbol in positions else 0.0,
            orders_today=self.store.orders_today(now),
            now=now,
        )

    def _submit_entry(self, tag: str) -> tuple[OrderIntent, OrderResult | None, str]:
        """A small entry through the same path as the bot's: RiskManager.check, then OrderGateway."""
        price = self._price()
        intent = self._intent(tag, floor_qty(DRILL_NOTIONAL_USD / price), price)
        verdict = self.risk.check(intent, self._context())
        if verdict.action is RiskAction.BLOCK:
            return intent, None, f"RiskManager blocked the {tag} order: {verdict.reason}"
        if verdict.adjusted_qty is not None:
            intent = replace(intent, qty=verdict.adjusted_qty)
        self.cids.append(intent.client_order_id)
        return intent, self.gateway.submit(intent), verdict.reason

    def _await_fill(self, result: OrderResult) -> OrderResult:
        for _ in range(FILL_POLLS):
            if result.status in ("filled", "refused", "rejected", "canceled"):
                break
            self.sleep(POLL_S)
            result = self.broker.get_order(result.client_order_id) or result
        return result

    def _settled(self) -> bool:
        for poll in range(SETTLE_POLLS):
            if not self._open_positions() and not self._open_orders():
                return True
            if poll + 1 < SETTLE_POLLS:
                self.sleep(POLL_S)
        return False

    def _open_positions(self) -> dict[str, float]:
        return {s: p.qty for s, p in sorted(self.broker.positions().items()) if abs(p.qty) > QTY_EPSILON}

    def _open_orders(self) -> dict[str, str]:
        out = {}
        for cid in dict.fromkeys(self.cids):
            order = self.broker.get_order(cid)
            if order is not None and order.status in OPEN_ORDER_STATUSES:
                out[cid] = order.status
        return out


def _message(result: OrderResult) -> str:
    return f" ({_clip(result.message, 200)})" if result.message else ""


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _heartbeat_problem(store: Store, now: datetime) -> str | None:
    """Why the bot might still be running, or None when it is safely stopped."""
    beats = {key: value for key in HEARTBEAT_KEYS if (value := store.kv_get(key)) is not None}
    for key, value in beats.items():
        ts = _parse_heartbeat(value)
        if ts is None:
            return f"kv {key!r} holds an unreadable heartbeat; make sure the bot is stopped"
        age = now - ts
        if age < HEARTBEAT_MAX_AGE:
            return (
                f"the bot looks alive (heartbeat {max(age.total_seconds(), 0) / 60:.1f} min ago); stop it "
                "with `docker compose stop bot` and wait 5 minutes"
            )
    if beats:
        return None
    latest = store.latest_equity()
    ts = _parse_iso(latest["ts"]) if latest else None
    if ts is not None and now - ts < EQUITY_MAX_AGE:
        return (
            f"no heartbeat is recorded and the last equity snapshot is only "
            f"{max((now - ts).total_seconds(), 0) / 60:.0f} min old, so the bot may still be running; "
            f"stop it and wait {EQUITY_MAX_AGE.total_seconds() / 60:.0f} minutes"
        )
    return None


def _parse_heartbeat(value: str) -> datetime | None:
    """ISO-8601 (as `bot.store.to_iso` writes) or Unix seconds."""
    ts = _parse_iso(value)
    if ts is not None:
        return ts
    try:
        seconds = float(value)
    except ValueError:
        return None
    return datetime.fromtimestamp(seconds, UTC) if math.isfinite(seconds) else None


def _paper_account(settings: Settings, broker: Broker | None, now: datetime) -> Broker:
    """The Alpaca paper account, after every refusal check. Raises ConfigError to refuse."""
    if settings.trading_mode != "paper" or not settings.alpaca_paper:
        raise ConfigError(
            "the --paper drill runs only with TRADING_MODE=paper and ALPACA_PAPER=true; "
            "refusing to place orders on a live account"
        )
    if settings.kill_switch:
        raise ConfigError("KILL_SWITCH=1 is set in the environment; unset it to run the --paper drill")
    with _real_store(settings) as store:
        positions = store.get_positions()
        alive = _heartbeat_problem(store, now)
    if positions:
        raise ConfigError(
            f"the bot holds open positions ({', '.join(sorted(positions))}) and the drill's flatten would "
            "close them; run the --paper drill only while the bot is flat"
        )
    if alive:
        raise ConfigError(f"refusing the --paper drill: {alive}")
    broker = broker if broker is not None else AlpacaBroker(settings)
    if not broker.is_paper:
        raise ConfigError("the broker is not an Alpaca paper account; refusing the --paper drill")
    held = {s: p.qty for s, p in broker.positions().items() if abs(p.qty) > QTY_EPSILON}
    if held:
        raise ConfigError(
            "the paper account holds positions (" + ", ".join(f"{s} {q:g}" for s, q in sorted(held.items()))
            + ") and the drill's flatten closes every position in the account; close them first "
            "(the account must be dedicated to the bot)"
        )
    return broker


def _sim_broker(broker: Broker | None, clock: Callable[[], datetime]) -> SimBroker:
    if broker is None:
        broker = SimBroker(clock=clock)
    if not isinstance(broker, SimBroker):
        raise ConfigError("the simulated drill needs a SimBroker; pass paper=True to drill the Alpaca paper account")
    if DRILL_SYMBOL not in broker.prices:
        broker.set_price(DRILL_SYMBOL, SIM_PRICE)
    return broker


def _drill_settings(settings: Settings, state_dir: Path, paper: bool) -> Settings:
    update: dict[str, Any] = {"data_dir": state_dir}
    if not paper:
        # A simulation stands in for a correctly configured paper deployment, whatever .env says.
        update |= {"trading_mode": "paper", "alpaca_paper": True, "live_trading_ack": None, "kill_switch": False}
    return settings.model_copy(update=update)


def kill_switch_drill(
    settings: Settings,
    cfg: StrategyConfig,
    paper: bool = False,
    broker: Broker | None = None,
    notifier: Notifier | None = None,
    *,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Open a small position, trip the kill switch and prove the bot cancels orders, goes flat,
    refuses new entries, alerts, and still allows exits.

    Default: SimBroker. `paper=True`: the Alpaca paper account (BTC/USD, about $5), refused with
    ConfigError unless the deployment is paper, the bot is stopped and flat, and the account holds
    no positions. The verdict `{passed, ts, mode, state_dir, steps}` is written to
    `settings.drill_path`; a refusal writes nothing.
    """
    now = _aware(clock())
    mode = "paper" if paper else "sim"
    drill_broker = _paper_account(settings, broker, now) if paper else _sim_broker(broker, clock)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    state_dir = settings.data_dir / "drill" / stamp
    drill_settings = _drill_settings(settings, state_dir, paper)
    log.info("kill-switch drill (%s) starting in %s", mode, state_dir)
    with Store(drill_settings.db_path, clock=clock) as store:
        inner = notifier if notifier is not None else build_notifier(settings, store)
        recorder = _RecordingNotifier(inner)
        try:
            steps = _Drill(
                drill_settings, settings.kill_switch_path, cfg, drill_broker, store, recorder, clock, sleep, stamp
            ).run()
            passed = all(step["ok"] for step in steps)
            result = {"passed": passed, "ts": to_iso(now), "mode": mode, "state_dir": str(state_dir), "steps": steps}
            _write_json(settings.drill_path, result)
            failed = [step["name"] for step in steps if not step["ok"]]
            inner.send(
                f"Kill-switch drill ({mode}) {'PASSED' if passed else 'FAILED'}: "
                f"{len(steps) - len(failed)}/{len(steps)} steps ok" + (f"; failed: {', '.join(failed)}" if failed else "")
            )
        finally:
            if notifier is None and isinstance(inner, TelegramNotifier):
                inner.close()
    log.log(logging.INFO if passed else logging.ERROR, "kill-switch drill (%s) %s", mode, "passed" if passed else "FAILED")
    return result


# --------------------------------------------------------------------------- final check: data


@dataclass
class _Paper:
    """What the bot recorded while paper trading, up to `now`."""

    now: datetime
    # The runner's first equity snapshot, taken on its first tick, else the first signal. A signal
    # is stamped with its bar's close, which may predate the bot, so it only stands in.
    start: datetime | None
    signals: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    equity: list[dict[str, Any]]
    orders: dict[str, dict[str, Any]]
    kill_events: list[dict[str, Any]]
    jev: dict[str, int]
    approvals: dict[str, int]
    errors: int

    @property
    def days(self) -> float:
        return 0.0 if self.start is None else max((self.now - self.start) / timedelta(days=1), 0.0)

    def rows(self, symbol: str, kind: str) -> list[dict[str, Any]]:
        return [s for s in self.signals if s["symbol"] == symbol and s["kind"] == kind]

    def executed_entries(self, symbol: str) -> set[date]:
        traded = {t["signal_id"] for t in self.trades if t["signal_id"] is not None}
        return {
            date.fromisoformat(s["bar_date"])
            for s in self.rows(symbol, SignalKind.ENTRY.value)
            if s["status"] in EXECUTED_STATUSES or s["id"] in traded
        }

    def bot_equity(self, capital: float) -> float:
        """The newest bot equity snapshot, else capital plus realized P&L."""
        for row in reversed(self.equity):
            value = _finite(row["bot_equity"])
            if value is not None:
                return value
        return capital + sum(_finite(t["pnl"]) or 0.0 for t in self.trades)

    def longest_outage(self) -> tuple[timedelta, datetime] | None:
        """The longest gap between consecutive equity snapshots, and when it ended."""
        stamps = [ts for row in self.equity if (ts := _parse_iso(row["ts"])) is not None]
        gaps = [(b - a, b) for a, b in zip(stamps, stamps[1:])]
        return max(gaps) if gaps else None


def _load_paper(store: Store, now: datetime) -> _Paper:
    signals = sorted(
        (s for s in store.signals(limit=SCAN_LIMIT) if parse_ts(s["ts"]) <= now), key=lambda s: (s["ts"], s["id"])
    )
    equity = [row for row in store.equity_series() if parse_ts(row["ts"]) <= now]
    firsts = [parse_ts(rows[0]["ts"]) for rows in (equity, signals) if rows]  # equity first
    orders = {}
    for row in signals:
        cid = row.get("order_client_id")
        if cid and (order := store.get_order_by_client_id(cid)) is not None:
            orders[cid] = order
    return _Paper(
        now=now,
        start=firsts[0] if firsts else None,
        signals=signals,
        trades=store.trades(until=now),
        equity=equity,
        orders=orders,
        kill_events=[e for e in store.recent_events(limit=SCAN_LIMIT) if e["kind"] == "kill_switch_tripped"],
        jev=store.jev_outcome_counts(),
        approvals=store.approval_counts(),
        errors=store.count_events(),
    )


class _FollowPaper(Strategy):
    """A replay of `inner` that takes only the entries `take_entry` approves (those the paper bot
    actually executed), while recording every signal the strategy produced before Jev and risk.

    Following the bot's real entries keeps the replay in the same position state as the bot, so a
    Jev veto or a rejected approval is not counted as a mismatch; any mismatch left is a code or
    data difference."""

    name: ClassVar[str] = "follow_paper"
    title: ClassVar[str] = "Replay that follows the paper bot's entries"
    default_params: ClassVar[dict[str, Any]] = {}
    param_grid: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, inner: Strategy, take_entry: Callable[[date], bool]) -> None:
        super().__init__(inner.symbol)
        self.inner = inner
        self.params = dict(inner.params)
        self.take_entry = take_entry
        self.signals: list[tuple[date, Signal]] = []

    @property
    def warmup(self) -> int:
        return self.inner.warmup

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        return self.inner.prepare(bars)

    def entry_signal(self, df: pd.DataFrame, i: int) -> Signal | None:
        signal = self.inner.entry_signal(df, i)
        if signal is None:
            return None
        day = df.index[i].date()
        self.signals.append((day, signal))
        return signal if self.take_entry(day) else None

    def exit_signal(self, df: pd.DataFrame, i: int, position: PositionState) -> Signal | None:
        signal = self.inner.exit_signal(df, i, position)
        if signal is not None:
            self.signals.append((df.index[i].date(), signal))
        return signal

    def trailing_stop(self, df: pd.DataFrame, i: int, position: PositionState) -> float | None:
        return self.inner.trailing_stop(df, i, position)

    def describe(self) -> dict[str, str]:
        return self.inner.describe()


class _BarSource:
    """Replay bars from the broker the bot trades through (the same bars it used) when one is
    available, else from the cache."""

    CACHE = "cached data (bot.data.load_daily)"

    def __init__(self, settings: Settings, broker: Broker | None) -> None:
        self.settings = settings
        self.broker = broker
        self.notes: list[str] = []
        if broker is None and settings.has_alpaca:
            try:
                self.broker = AlpacaBroker(settings)
            except Exception as exc:
                self.notes.append(f"Alpaca is unavailable ({_describe(exc, settings)}), so the replay used the cache")

    def load(self, symbol: str, start: date, end: date) -> tuple[pd.DataFrame, str]:
        if self.broker is not None:
            try:
                bars = self.broker.daily_bars(symbol, start, end)
            except Exception as exc:
                log.warning("could not fetch %s bars from the broker: %s", symbol, _describe(exc, self.settings))
                self.notes.append(f"{symbol}: broker bars failed ({_describe(exc, self.settings)}); used the cache")
            else:
                if not bars.empty:
                    return bars, f"{type(self.broker).__name__}.daily_bars"
                self.notes.append(f"{symbol}: the broker returned no bars; used the cache")
        return load_daily(symbol), self.CACHE


@dataclass
class _Replay:
    symbol: str
    rule: AssetRule
    source: str = ""
    bars: pd.DataFrame | None = None
    start: date | None = None
    end: date | None = None
    signals: set[tuple[str, str, date]] = field(default_factory=set)
    final_equity: float | None = None
    trades: int = 0
    counterfactuals: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _replay(symbol: str, rule: AssetRule, cfg: StrategyConfig, paper: _Paper, source: _BarSource) -> _Replay:
    """Replay the configured strategy over the paper period, following the bot's entries."""
    replay = _Replay(symbol, rule)
    if paper.start is None:
        replay.error = "no paper history to replay"
        return replay
    try:
        _run_replay(replay, paper.start, cfg, paper, source)
    except Exception as exc:  # a broken replay fails the checks that need it, never the whole final check
        log.exception("replay of %s failed", symbol)
        replay.error = f"replay failed: {_describe(exc, source.settings)}"
    return replay


def _run_replay(replay: _Replay, start: datetime, cfg: StrategyConfig, paper: _Paper, source: _BarSource) -> None:
    symbol, rule = replay.symbol, replay.rule
    strategy = build(rule.strategy, symbol, dict(rule.params))
    history = timedelta(days=int(strategy.warmup * HISTORY_FACTOR) + HISTORY_EXTRA_DAYS)
    bars, replay.source = source.load(symbol, start.date() - history, paper.now.date())
    closes = pd.Series([bar_close_ts(symbol, ts.date()) for ts in bars.index], index=bars.index)
    bars = bars[(closes <= paper.now).to_numpy()]
    first = _first_bar(symbol, closes[closes <= paper.now], paper)
    if first is None:
        replay.error = "no completed bar in the paper period yet"
        return
    replay.bars, replay.start, replay.end = bars, first, bars.index[-1].date()
    bt_cfg = BacktestConfig.from_strategy(cfg, symbol)
    follower = _FollowPaper(strategy, paper.executed_entries(symbol).__contains__)
    result = run_backtest(bars, follower, bt_cfg, start=replay.start.isoformat(), end=replay.end.isoformat())
    replay.signals = {(symbol, signal.kind.value, day) for day, signal in follower.signals}
    replay.final_equity = float(result.equity.iloc[-1]) if len(result.equity) else bt_cfg.capital_usd
    replay.trades = len(result.trades)
    for row in paper.rows(symbol, SignalKind.ENTRY.value):
        day = date.fromisoformat(row["bar_date"])
        if row["gate"] == "vetoed" and replay.start <= day <= replay.end:
            replay.counterfactuals.append(_counterfactual(rule, symbol, bars, bt_cfg, day, replay.end))


def _first_bar(symbol: str, closes: pd.Series, paper: _Paper) -> date | None:
    """The first bar the bot evaluated: at its first tick it acts on the newest bar already
    closed, so that bar, or an earlier bar the store already holds a signal for."""
    if closes.empty or paper.start is None:
        return None
    before = closes[closes <= paper.start]
    first = (before.index[-1] if len(before) else closes.index[0]).date()
    recorded = [date.fromisoformat(s["bar_date"]) for s in paper.signals if s["symbol"] == symbol]
    return min([first, *recorded])


def _counterfactual(
    rule: AssetRule, symbol: str, bars: pd.DataFrame, bt_cfg: BacktestConfig, day: date, end: date
) -> dict[str, Any]:
    """What a vetoed entry would have made: the replay takes that one entry and nothing else."""
    follower = _FollowPaper(build(rule.strategy, symbol, dict(rule.params)), lambda d: d == day)
    result = run_backtest(bars, follower, bt_cfg, start=day.isoformat(), end=end.isoformat())
    out: dict[str, Any] = {"symbol": symbol, "bar_date": day.isoformat(), "pnl_usd": None, "return_pct": None}
    if not any(d == day and s.kind is SignalKind.ENTRY for d, s in follower.signals):
        out["note"] = "the replay has no entry on that bar"
    elif not result.trades:
        out["note"] = "too recent to have filled"
    else:
        trade = result.trades[0]
        out.update(pnl_usd=trade.pnl, return_pct=trade.pnl_pct * 100, exit_reason=trade.exit_reason)
    return out


def _load_tournament(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"{path} not found"
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable ({exc})"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    return data, None


def _worst(group: Any) -> dict[str, Any] | None:
    """The regime or window with the lowest return in a `regime_breakdown` group."""
    if not isinstance(group, dict):
        return None
    rows = [
        {"name": name, "return_pct": ret, "max_dd_pct": _finite(v.get("max_dd_pct")),
         "n_trades": v.get("n_trades", v.get("trades"))}
        for name, v in group.items()
        if isinstance(v, dict) and (ret := _finite(v.get("return_pct"))) is not None
    ]  # fmt: skip
    return min(rows, key=lambda r: r["return_pct"]) if rows else None


def _winners(tournament: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each tournament winner with its run and its worst regimes."""
    if tournament is None:
        return []
    runs = [r for r in tournament.get("runs") or [] if isinstance(r, dict)]
    out = []
    for symbol, winner in (tournament.get("winners") or {}).items():
        if not isinstance(winner, dict):
            continue
        run = next((r for r in runs if r.get("symbol") == symbol and r.get("label") == winner.get("label")), {})
        regimes = run.get("regimes") or winner.get("regimes") or {}
        out.append(
            {
                "symbol": symbol,
                "label": str(winner.get("label") or winner.get("strategy") or "?"),
                "strategy": str(winner.get("strategy") or ""),
                "run": run,
                "worst_stress": _worst(regimes.get("stress")),
                "worst_trend": _worst(regimes.get("trend")),
                "worst_vol": _worst(regimes.get("vol")),
            }
        )
    return out


@dataclass(frozen=True)
class _MarketRisk:
    """Tail numbers for one symbol from the cached daily history."""

    symbol: str
    strategy: str
    move_pct: float  # worst overnight gap (stocks) or one-day fall to the low (crypto), negative
    move_date: date
    stop_pct: float  # median initial stop distance, % of the close
    position_usd: float  # a full-size position at that stop distance
    vol_pct: float  # annualised realized volatility over the last year
    first: date
    last: date

    @property
    def loss_usd(self) -> float:
        return self.position_usd * max(0.0, -self.move_pct) / 100


def _position_usd(cfg: StrategyConfig, stop_pct: float) -> float:
    risk = cfg.risk
    by_risk = risk.capital_usd * risk.risk_per_trade_pct / stop_pct if stop_pct > 0 else math.inf
    return min(by_risk, risk.capital_usd * risk.max_position_pct / 100, risk.max_position_usd)


def _market_risk(symbol: str, rule: AssetRule, cfg: StrategyConfig) -> _MarketRisk:
    bars = load_daily(symbol)
    close = bars["close"]
    crypto = asset_class(symbol) is AssetClass.CRYPTO
    move = ((bars["low"] if crypto else bars["open"]) / close.shift(1) - 1.0) * 100
    worst_day = move.idxmin()
    strategy = build(rule.strategy, symbol, dict(rule.params))
    mult = float(strategy.params.get("atr_mult", 3))
    stop_pct = float((mult * ind.atr(bars, ATR_N) / close).median() * 100)
    n = periods_per_year(symbol)
    returns = np.log(close).diff().iloc[-n:]
    return _MarketRisk(
        symbol=symbol,
        strategy=strategy.label(),
        move_pct=float(move.min()),
        move_date=worst_day.date(),
        stop_pct=stop_pct,
        position_usd=_position_usd(cfg, stop_pct),
        vol_pct=float(returns.std() * math.sqrt(n) * 100),
        first=bars.index[0].date(),
        last=bars.index[-1].date(),
    )


@dataclass
class _Correlation:
    pairs: list[tuple[str, str, float, float]]  # a, b, full-history corr, last-year corr
    worst_day: date
    worst_day_loss: float  # USD, full-size positions in every symbol


def _correlation(risks: list[_MarketRisk]) -> _Correlation | None:
    if len(risks) < 2:
        return None
    closes = pd.concat({r.symbol: load_daily(r.symbol)["close"] for r in risks}, axis=1, join="inner")
    returns = closes.pct_change().dropna()
    if len(returns) < 30:
        return None
    full, recent = returns.corr(), returns.iloc[-252:].corr()
    symbols = [r.symbol for r in risks]
    pairs = [(a, b, float(full.at[a, b]), float(recent.at[a, b])) for i, a in enumerate(symbols) for b in symbols[i + 1 :]]
    book = returns.mul(pd.Series({r.symbol: r.position_usd for r in risks})).sum(axis=1)
    return _Correlation(pairs=pairs, worst_day=book.idxmin().date(), worst_day_loss=float(-book.min()))


# --------------------------------------------------------------------------- final check: checks


@dataclass
class _Check:
    name: str
    title: str
    passed: bool
    detail: str
    value: Any = None
    threshold: Any = None
    shown_value: str = ""
    shown_threshold: str = ""
    required: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "required": self.required,
            "detail": self.detail,
            "value": _jsonable(self.value),
            "threshold": _jsonable(self.threshold),
        }


NO_HISTORY = "Not enough paper history: the store has no signals or equity snapshots yet."


def _check_paper_history(paper: _Paper, cfg: StrategyConfig) -> _Check:
    gate = cfg.live_gate
    days, trades = paper.days, len(paper.trades)
    passed = paper.start is not None and days >= gate.min_paper_days and trades >= gate.min_paper_trades
    if paper.start is None:
        detail = f"{NO_HISTORY} Paper trade for at least {gate.min_paper_days} days and {gate.min_paper_trades} closed trades."
    else:
        detail = f"{days:.1f} days of paper trading since {paper.start:%Y-%m-%d %H:%M} UTC and {trades} closed trades"
        detail += "." if passed else (
            f"; not enough paper history yet (need {gate.min_paper_days} days and {gate.min_paper_trades} trades)."
        )
    return _Check(
        "paper_history", "Paper history", passed, detail,
        value={"days": days, "closed_trades": trades, "start": paper.start},
        threshold={"min_paper_days": gate.min_paper_days, "min_paper_trades": gate.min_paper_trades},
        shown_value=f"{days:.1f} days, {trades} trades",
        shown_threshold=f"≥ {gate.min_paper_days} days, ≥ {gate.min_paper_trades} trades",
    )  # fmt: skip


def _describe_keys(keys: set[tuple[str, str, date]], limit: int = 5) -> str:
    items = [f"{s} {k} {d.isoformat()}" for s, k, d in sorted(keys, key=lambda x: (x[2], x[0], x[1]))]
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _replay_problem(paper: _Paper, replays: list[_Replay]) -> str | None:
    """Why the replay cannot be held against the paper record, or None when it can."""
    if paper.start is None:
        return NO_HISTORY
    if not replays:
        return "No asset is enabled in strategy.md, so there is nothing to replay."
    broken = [f"{r.symbol}: {r.error}" for r in replays if r.error]
    if broken:
        return "The replay could not run: " + "; ".join(broken) + "."
    stale = [r for r in replays if r.end is not None and bar_close_ts(r.symbol, r.end) < paper.now - DATA_LAG]
    if stale:
        return (
            "The replay bars stop before the paper period ends ("
            + ", ".join(f"{r.symbol} at {r.end}" for r in stale)
            + "); run `python -m bot fetch-data` or set the Alpaca keys so the replay uses the bot's own bars."
        )
    return None


def _check_signal_parity(paper: _Paper, replays: list[_Replay], cfg: StrategyConfig) -> _Check:
    minimum = cfg.live_gate.min_signal_match_rate
    threshold = {"min_signal_match_rate": minimum}
    shown_threshold = f"≥ {minimum:.0%} both ways"

    def fail(detail: str, value: Any = None) -> _Check:
        return _Check("signal_parity", "Signal parity", False, detail, value, threshold, "n/a", shown_threshold)

    problem = _replay_problem(paper, replays)
    if problem:
        return fail(problem)
    backtest: set[tuple[str, str, date]] = set()
    recorded: set[tuple[str, str, date]] = set()
    for r in replays:
        assert r.start is not None and r.end is not None
        backtest |= r.signals
        recorded |= {
            (s["symbol"], s["kind"], day)
            for s in paper.signals
            if s["symbol"] == r.symbol and r.start <= (day := date.fromisoformat(s["bar_date"])) <= r.end
        }
    other = sorted({s["symbol"] for s in paper.signals} - {r.symbol for r in replays})
    matched = backtest & recorded
    rate = len(matched) / len(backtest) if backtest else 0.0
    paper_rate = len(matched) / len(recorded) if recorded else 0.0
    value = {
        "match_rate": rate, "paper_match_rate": paper_rate, "matched": len(matched),
        "backtest_signals": len(backtest), "paper_signals": len(recorded),
        "missing_in_paper": len(backtest - recorded), "paper_only": len(recorded - backtest),
        "bars": {r.symbol: r.source for r in replays}, "windows": {r.symbol: [r.start, r.end] for r in replays},
    }  # fmt: skip
    if not backtest and not recorded:
        return fail("Neither the replay nor the bot produced a signal in the paper period, so parity is unproven.", value)
    passed = bool(backtest) and rate >= minimum and paper_rate >= minimum
    detail = (
        f"{len(matched)} of {len(backtest)} replay signals ({rate:.0%}) are in the store, and "
        f"{len(recorded) - len(matched)} paper signal(s) have no replay twin ({paper_rate:.0%} of paper signals match). "
        f"Compared by (symbol, kind, bar date) before Jev and risk, on "
        + ", ".join(f"{r.symbol} bars from {r.source}" for r in replays)
        + ". The replay follows the bot's actual entries, so a Jev veto or a rejected approval is not a mismatch."
    )
    if backtest - recorded:
        detail += f" Missing in paper: {_describe_keys(backtest - recorded)}."
    if recorded - backtest:
        detail += f" Paper only: {_describe_keys(recorded - backtest)}."
    if other:
        detail += f" Not compared (not enabled now): {', '.join(other)}."
    return _Check(
        "signal_parity", "Signal parity", passed, detail, value, threshold,
        f"{rate:.0%} of replay, {paper_rate:.0%} of paper", shown_threshold,
    )  # fmt: skip


def _fill_slippage_bps(row: dict[str, Any], order: dict[str, Any], replay: _Replay | None) -> float | None:
    """Slippage against the next bar's open (where the backtest fills), positive = worse.

    The open is rescaled by the bot's own close for the signal bar over the replay's close, so
    dividend adjustments made since (the bars are adjusted, fills are not) do not read as slippage.
    """
    bars = replay.bars if replay is not None else None
    day = pd.Timestamp(row["bar_date"])
    if bars is None or day not in bars.index:
        return None
    nxt = int(bars.index.get_loc(day)) + 1
    if nxt >= len(bars):
        return None
    close = float(bars["close"].iat[nxt - 1])
    live_close = _finite(row.get("price"))
    scale = live_close / close if live_close and close > 0 else 1.0
    reference = float(bars["open"].iat[nxt]) * scale
    fill = float(order["filled_avg_price"])
    change = (fill / reference - 1.0) * 1e4
    return change if order["side"] == Side.BUY.value else -change


def _check_fill_quality(paper: _Paper, replays: list[_Replay], cfg: StrategyConfig) -> _Check:
    assumed: dict[str, float] = {k: float(v) for k, v in cfg.execution.slippage_bps.items()}
    threshold = {"slippage_bps": assumed}
    shown_threshold = ", ".join(f"{k} ≤ {v:g} bps" for k, v in sorted(assumed.items()))
    by_symbol = {r.symbol: r for r in replays}
    samples: dict[str, list[float]] = {}
    simulated = unmatched = 0
    for row in paper.signals:
        order = paper.orders.get(row.get("order_client_id") or "")
        if order is None or not (_finite(order["filled_qty"]) or 0) > 0 or not _finite(order["filled_avg_price"]):
            continue
        if str(order["broker_order_id"] or "").startswith("sim-"):
            simulated += 1
            continue
        bps = _fill_slippage_bps(row, order, by_symbol.get(row["symbol"]))
        if bps is None:
            unmatched += 1
        else:
            samples.setdefault(asset_class(row["symbol"]).value, []).append(bps)
    stats = {
        klass: {"fills": len(v), "avg_bps": float(np.mean(v)), "worst_bps": float(max(v))}
        for klass, v in sorted(samples.items())
    }
    value = {**stats, "not_matched": unmatched, "simulated": simulated}
    if paper.start is None:
        return _Check("fill_quality", "Fill quality", False, NO_HISTORY, value, threshold, "n/a", shown_threshold)
    if simulated:
        detail = (
            f"{simulated} fill(s) in the store came from SimBroker (simulated), so this database is not a "
            "paper-trading record; paper evidence must come from Alpaca fills only."
        )
        return _Check("fill_quality", "Fill quality", False, detail, value, threshold, "simulated fills", shown_threshold)
    if not stats:
        detail = "No filled paper orders with a replay bar to compare against yet."
        return _Check("fill_quality", "Fill quality", False, detail, value, threshold, "no fills", shown_threshold)
    over = [k for k, s in stats.items() if s["avg_bps"] > assumed.get(k, 0.0)]
    parts = [
        f"{k}: {s['fills']} fills averaged {s['avg_bps']:+.1f} bps against the next bar's open "
        f"(worst {s['worst_bps']:+.1f}) vs {assumed.get(k, 0.0):g} bps assumed in the backtest"
        for k, s in stats.items()
    ]
    detail = "; ".join(parts) + "."
    detail += (
        " Paper fills are worse than the backtest assumes, so its returns are overstated."
        if over
        else " Paper fills are within the backtest's cost assumption (live fills will be worse than paper)."
    )
    if unmatched:
        detail += f" {unmatched} fill(s) had no replay bar to compare against yet."
    detail += " Stop and kill-switch exits are not included: the backtest fills those at the stop, not an open."
    return _Check(
        "fill_quality", "Fill quality", not over, detail, value, threshold,
        ", ".join(f"{k} {s['avg_bps']:+.1f} bps ({s['fills']})" for k, s in stats.items()), shown_threshold,
    )  # fmt: skip


def _jev_sentence(counterfactuals: list[dict[str, Any]]) -> str:
    if not counterfactuals:
        return "Jev vetoed no entries in the paper period."
    known = [c for c in counterfactuals if c["pnl_usd"] is not None]
    pnl = sum(c["pnl_usd"] for c in known)
    text = f"Jev vetoed {len(counterfactuals)} entr{'y' if len(counterfactuals) == 1 else 'ies'}"
    if not known:
        return text + "; none can be replayed yet."
    effect = (
        f"the vetoes avoided ${-pnl:,.2f} of losses" if pnl < 0
        else f"the vetoes cost ${pnl:,.2f} of profit" if pnl > 0 else "the vetoes made no difference"
    )  # fmt: skip
    text += f"; replaying {len(known)} of them as if taken gives {_usd(pnl, signed=True)}, so {effect}"
    return text + (f" ({len(counterfactuals) - len(known)} too recent or not reproduced)." if len(known) < len(counterfactuals) else ".")


def _check_return_gap(paper: _Paper, replays: list[_Replay], cfg: StrategyConfig) -> _Check:
    limit = cfg.live_gate.max_return_gap_pct
    threshold = {"max_return_gap_pct": limit}
    shown_threshold = f"|gap| ≤ {limit:g} pp"

    problem = _replay_problem(paper, replays)
    if problem:
        return _Check("return_gap", "Return gap", False, problem, None, threshold, "n/a", shown_threshold)
    capital = cfg.risk.capital_usd
    paper_return = (paper.bot_equity(capital) / capital - 1.0) * 100
    replay_return = sum((r.final_equity or capital) - capital for r in replays) / capital * 100
    gap = round(paper_return - replay_return, 6) + 0.0  # no "-0.00"
    counterfactuals = [c for r in replays for c in r.counterfactuals]
    passed = abs(gap) <= limit
    detail = (
        f"The paper bot returned {paper_return:+.2f}% of capital; the replay of the "
        f"{sum(r.trades for r in replays)} trade(s) it actually executed returned {replay_return:+.2f}%: "
        f"a gap of {gap:+.2f} percentage points"
        + (" (within the limit). " if passed else f", beyond the ±{limit:g} limit. ")
        + _jev_sentence(counterfactuals)
    )
    value = {
        "paper_return_pct": paper_return, "replay_return_pct": replay_return, "gap_pct": gap,
        "replay_trades": sum(r.trades for r in replays), "jev_vetoed": len(counterfactuals),
        "jev_counterfactual_pnl_usd": sum(c["pnl_usd"] for c in counterfactuals if c["pnl_usd"] is not None),
        "jev_counterfactuals": counterfactuals,
    }  # fmt: skip
    return _Check("return_gap", "Return gap", passed, detail, value, threshold, f"{gap:+.2f} pp", shown_threshold)


def _read_drill(settings: Settings) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(settings.drill_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"{settings.drill_path} is unreadable ({exc})"
    return (data, None) if isinstance(data, dict) else (None, f"{settings.drill_path} is not a JSON object")


def _check_drill(drill: dict[str, Any] | None, error: str | None, cfg: StrategyConfig, now: datetime) -> _Check:
    max_age = cfg.live_gate.kill_switch_drill_max_age_days
    threshold = {"max_age_days": max_age}
    shown_threshold = f"passed, ≤ {max_age} days old"
    ts = _parse_iso(drill.get("ts")) if drill else None
    age_days = (now - ts) / timedelta(days=1) if ts else None
    steps = [s for s in (drill or {}).get("steps") or [] if isinstance(s, dict)]
    failed = [str(s.get("name")) for s in steps if s.get("ok") is not True]
    mode = str((drill or {}).get("mode") or "unknown")
    value = {"drill_passed": (drill or {}).get("passed"), "mode": mode, "ts": ts, "age_days": age_days}
    shown = "none" if drill is None else f"{mode}, {'passed' if drill.get('passed') is True else 'FAILED'}"
    if age_days is not None:
        shown += f", {age_days:.1f} days old"
    if drill is None:
        detail = error or "No kill-switch drill on record. Run `python -m bot drill-kill-switch` (and `--paper`)."
    elif ts is None or age_days is None:
        detail = "The drill file has no valid ISO-8601 'ts'; run the drill again."
    elif drill.get("passed") is not True:
        detail = f"The last drill ({mode}, {ts:%Y-%m-%d}) FAILED" + (f" at: {', '.join(failed)}." if failed else ".")
    elif now - ts < -CLOCK_SKEW:
        detail = f"The drill is dated in the future ({ts:%Y-%m-%d %H:%M} UTC); check the server clock."
    elif age_days >= max_age:
        detail = f"The last drill passed but is {age_days:.0f} days old (limit {max_age}); run it again."
    else:
        detail = f"The {mode} drill of {ts:%Y-%m-%d %H:%M} UTC passed all {len(steps)} steps ({age_days:.1f} days ago)."
        if mode != "paper":
            detail += " A `--paper` drill against the real paper account is stronger evidence."
        return _Check("kill_switch_drill", "Kill-switch drill", True, detail, value, threshold, shown, shown_threshold)
    return _Check("kill_switch_drill", "Kill-switch drill", False, detail, value, threshold, shown, shown_threshold)


def _config_drift(tournament: dict[str, Any], cfg: StrategyConfig) -> list[str]:
    """Differences between the running config and the one the tournament validated."""
    notes = []
    winners = {s: w for s, w in (tournament.get("winners") or {}).items() if isinstance(w, dict)}
    for symbol, rule in cfg.enabled_assets.items():
        winner = winners.get(symbol)
        try:
            same = winner is not None and winner.get("strategy") == rule.strategy and (
                build(rule.strategy, symbol, dict(rule.params)).params
                == build(str(winner.get("strategy")), symbol, dict(winner.get("params") or {})).params
            )
        except (ValueError, TypeError):
            same = False
        if not same:
            notes.append(f"{symbol} runs {rule.strategy} {dict(rule.params)} but the tournament winner is "
                         f"{(winner or {}).get('label', 'none')}")  # fmt: skip
    risk = ((tournament.get("config") or {}).get("risk")) or {}
    for key, value in cfg.risk.model_dump().items():
        tested, current = _finite(risk.get(key)), _finite(value)
        if tested is not None and current is not None and not math.isclose(tested, current):
            notes.append(f"risk.{key} is {value} but the tournament used {risk.get(key)}")
    return notes


def _check_risk_config(tournament: dict[str, Any] | None, error: str | None, cfg: StrategyConfig) -> _Check:
    kill = cfg.risk.max_drawdown_kill_pct
    near = kill * NEAR_KILL_FRACTION
    threshold = {"max_drawdown_kill_pct": kill, "near_kill_pct": near}
    shown_threshold = f"max DD < {near:g}% ({NEAR_KILL_FRACTION:.0%} of the {kill:g}% kill), no kill in backtest"
    # The tournament's portfolio backtest (out of sample) and, when present, its full-history twin:
    # the worse of the two is the honest drawdown to hold against the kill level.
    portfolios = {
        str(p.get("window") or key): p
        for key in ("portfolio", "portfolio_full")
        if isinstance(p := (tournament or {}).get(key), dict)
    }
    drawdowns = {
        window: dd
        for window, p in portfolios.items()
        if (dd := _finite((p.get("metrics") or {}).get("max_drawdown_pct"))) is not None
    }
    dd = max(drawdowns.values()) if drawdowns else None
    fires = [f for p in portfolios.values() for f in p.get("kill_switch_would_fire") or [] if isinstance(f, dict)]
    hits = max((_finite(p.get("daily_loss_limit_hits")) or 0.0 for p in portfolios.values()), default=0.0)
    value = {
        "portfolio_max_drawdown_pct": dd, "max_drawdown_by_window": drawdowns, "kill_switch_would_fire": fires,
        "daily_loss_limit_hits": hits,
    }  # fmt: skip
    if tournament is None:
        detail = f"No tournament results ({error}); run `python -m bot tournament --apply` first."
        return _Check("risk_config", "Risk config", False, detail, value, threshold, "n/a", shown_threshold)
    if dd is None:
        detail = "results/tournament.json has no portfolio backtest (no winners?), so the risk limits are untested."
        return _Check("risk_config", "Risk config", False, detail, value, threshold, "n/a", shown_threshold)
    drift = _config_drift(tournament, cfg)
    windows = ", ".join(f"{v:.1f}% ({w})" for w, v in drawdowns.items())
    if fires:
        first = fires[0]
        detail = (
            f"The portfolio backtest would have tripped the {kill:g}% drawdown kill switch {len(fires)} time(s) "
            f"(first {first.get('date')}: {first.get('reason')}): expect it to fire in normal operation."
        )
    elif dd >= near:
        detail = (
            f"The portfolio backtest's max drawdown is already {windows}, {dd / kill:.0%} of the {kill:g}% kill "
            f"level: the kill switch is likely to fire in normal operation."
        )
    else:
        detail = (
            f"The portfolio backtest's max drawdown is {windows} against the {kill:g}% kill level "
            f"({dd / kill:.0%} of it at worst), and the kill switch never fired in the backtest."
        )
    if hits:
        detail += f" The {cfg.risk.daily_loss_limit_pct:g}% daily loss limit was hit {hits:.0f} time(s)."
    if drift:
        detail += " The running config differs from the one tested: " + "; ".join(drift) + "."
    passed = not fires and dd < near and not drift
    return _Check("risk_config", "Risk config", passed, detail, value, threshold, f"max DD {dd:.1f}%", shown_threshold)


def _regime_line(w: dict[str, Any]) -> str:
    """The winner's worst stress window, trend regime and volatility regime in the backtest."""
    parts = []
    for key, label in (("worst_stress", "stress window"), ("worst_trend", "trend regime"), ("worst_vol", "volatility regime")):
        worst = w[key]
        if worst:
            dd = f", max DD {worst['max_dd_pct']:.1f}%" if worst["max_dd_pct"] is not None else ""
            parts.append(f"{label} {worst['name']} ({worst['return_pct']:+.1f}%{dd})")
    return "worst in the backtest: " + "; ".join(parts) if parts else "the results have no regime breakdown"


def _breaks(w: dict[str, Any]) -> str:
    """The regime that breaks the winner, then the backtest's evidence."""
    return f"{_BREAKS.get(w['strategy'], 'a market unlike its backtest')}; {_regime_line(w)}"


def _check_regime_risk(winners: list[dict[str, Any]], error: str | None) -> _Check:
    if not winners:
        detail = f"No tournament winners to analyse ({error or 'no symbol has a winner'})."
        return _Check("regime_risk", "Regime risk", False, detail, None, None, "n/a", "info only", required=False)
    detail = " ".join(f"{w['symbol']} {w['label']} breaks in {_breaks(w)}." for w in winners)
    value = {
        w["symbol"]: {"label": w["label"], "worst_stress": w["worst_stress"], "worst_trend": w["worst_trend"],
                      "worst_vol": w["worst_vol"], "breaks": _BREAKS.get(w["strategy"])}
        for w in winners
    }  # fmt: skip
    shown = "; ".join(
        f"{w['symbol']}: {w['worst_stress']['name'] if w['worst_stress'] else 'n/a'}" for w in winners
    )
    return _Check("regime_risk", "Regime risk", True, detail, value, None, shown, "info only", required=False)


# --------------------------------------------------------------------------- final check: report


@dataclass
class _Findings:
    now: datetime
    cfg: StrategyConfig
    paper: _Paper
    replays: list[_Replay]
    bar_notes: list[str]
    checks: list[_Check]
    drill: dict[str, Any] | None
    tournament: dict[str, Any] | None
    tournament_error: str | None
    winners: list[dict[str, Any]]
    risks: list[_MarketRisk]
    risk_notes: list[str]
    correlation: _Correlation | None

    def check(self, name: str) -> _Check:
        return next(c for c in self.checks if c.name == name)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.required)


def _usd(value: float, signed: bool = False) -> str:
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    return f"{sign}${abs(value):,.0f}" if abs(value) >= 100 else f"{sign}${abs(value):,.2f}"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _summary(f: _Findings) -> str:
    required = [c for c in f.checks if c.required]
    failed = [c.name for c in required if not c.passed]
    if not failed:
        return f"PASSED: all {len(required)} required checks passed."
    return f"FAILED: {len(failed)} of {len(required)} required checks failed ({', '.join(failed)})."


def _verdict(f: _Findings) -> str:
    required = [c for c in f.checks if c.required]
    failed = [c.title for c in required if not c.passed]
    if not failed:
        return (
            f"**Verdict: PASSED.** All {len(required)} required checks passed. This does not switch the "
            "bot to live: that still takes `TRADING_MODE=live`, `ALPACA_PAPER=false`, the typed "
            "acknowledgement and your decision, and this pass expires after 7 days."
        )
    return (
        f"**Verdict: NOT READY FOR LIVE.** {len(failed)} of {len(required)} required checks failed: "
        f"{', '.join(failed)}. The bot stays on paper."
    )


def _meta(f: _Findings) -> str:
    period = (
        f"{f.paper.start:%Y-%m-%d %H:%M} UTC to {f.now:%Y-%m-%d %H:%M} UTC ({f.paper.days:.1f} days)"
        if f.paper.start else "none yet"
    )  # fmt: skip
    sources = ", ".join(f"{r.symbol} from {r.source}" for r in f.replays if r.source) or "none"
    lines = [
        f"Generated {f.now:%Y-%m-%d %H:%M} UTC by `python -m bot final-check`.",
        f"Paper period: {period}. Replay bars: {sources}.",
    ]
    if f.bar_notes:
        lines.append("Data notes: " + "; ".join(f.bar_notes) + ".")
    return "\n".join(lines)


def _table(f: _Findings) -> str:
    rows = [
        "| Check | Result | Value | Threshold | Detail |",
        "|---|---|---|---|---|",
    ]
    for c in f.checks:
        result = ("PASS" if c.passed else "FAIL") if c.required else "info"
        rows.append(
            f"| {_cell(c.title)} | {result} | {_cell(c.shown_value or 'n/a')} | {_cell(c.shown_threshold)} | {_cell(c.detail)} |"
        )
    return "### Checks\n\n" + "\n".join(rows)


def _answer_paper(f: _Findings) -> str:
    head = "### Does paper match the backtest?\n\n"
    if f.paper.start is None:
        return head + (
            "Not yet known. The store has no paper trading history, so there is nothing to compare. "
            f"Run the bot in paper mode for at least {f.cfg.live_gate.min_paper_days} days and "
            f"{f.cfg.live_gate.min_paper_trades} closed trades, then run the final check again."
        )
    parts = [f.check(n) for n in ("signal_parity", "fill_quality", "return_gap")]
    answer = "Yes." if all(c.passed for c in parts) else "No."
    if not f.check("paper_history").passed:
        answer += " The paper history is also still too short to trust either way."
    bullets = "\n".join(f"- **{c.title}:** {c.detail}" for c in parts)
    return head + answer + "\n\n" + bullets + "\n\n" + (
        "Two gaps no replay closes: the backtest cannot include Jev (replaying old headlines would leak "
        "hindsight), and Alpaca paper fills are kinder than live fills, which also pay the spread and "
        "market impact."
    )


def _answer_kill(f: _Findings) -> str:
    lines = [f"- **Drill:** {f.check('kill_switch_drill').detail}"]
    if f.drill and isinstance(f.drill.get("steps"), list):
        steps = [s for s in f.drill["steps"] if isinstance(s, dict)]
        lines.append(
            "- **Drill steps:** "
            + ", ".join(f"{s.get('name')} {'ok' if s.get('ok') is True else 'FAILED'}" for s in steps)
            + "."
        )
    if f.paper.start is None:
        lines.append("- **Paper trading:** no paper history yet.")
    elif f.paper.kill_events:
        events = sorted(f.paper.kill_events, key=lambda e: e["ts"])
        lines.append(
            f"- **Paper trading:** the real kill switch tripped {len(events)} time(s): "
            + "; ".join(f"{e['ts'][:16]} UTC, {_cell(e['message'])}" for e in events[:5])
            + "."
        )
    else:
        lines.append("- **Paper trading:** the real kill switch never tripped.")
    lines.append(f"- **Backtest:** {f.check('risk_config').detail}")
    if f.drill and f.drill.get("passed") is True:
        answer = "Yes."
    elif f.drill:
        answer = "Only partly: the switch was tripped in the last drill, but the drill failed."
    elif f.paper.kill_events:
        answer = "Yes, during paper trading, but no drill is on record."
    else:
        answer = "No: there is no drill on record, and the switch never tripped during paper trading."
    return "### Did the kill switch fire in testing?\n\n" + answer + "\n\n" + "\n".join(lines)


def _answer_regime(f: _Findings) -> str:
    head = "### What market regime would break this?\n\n"
    if not f.winners:
        return head + (
            f"Unknown: there are no tournament winners to analyse ({f.tournament_error or 'no symbol has a winner'}). "
            "Run `python -m bot tournament --apply`."
        )
    lines = [f"- **{w['symbol']} {w['label']}** breaks in {_breaks(w)}." for w in f.winners]
    return head + "\n".join(lines)


def _blow_up_items(f: _Findings) -> list[tuple[str, str]]:
    return [
        _gap_item(f),
        _software_stop_item(f),
        _correlation_item(f),
        _crypto_item(f),
        _regime_item(f),
        _outage_item(f),
        _approval_item(f),
        _config_item(f),
        _overfit_item(f),
    ]


def _gap_item(f: _Findings) -> tuple[str, str]:
    title = "Gaps and crashes jump the stop."
    risk = f.cfg.risk
    planned = risk.capital_usd * risk.risk_per_trade_pct / 100
    if not f.risks:
        return title, "Stops are market orders sent after the price crosses the level, so a gap fills wherever the market opens. " + (
            "No cached data to size this risk: " + "; ".join(f.risk_notes) + "." if f.risk_notes else ""
        )
    parts = []
    for r in f.risks:
        kind = "one-day fall (prior close to low)" if asset_class(r.symbol) is AssetClass.CRYPTO else "overnight gap"
        parts.append(
            f"{r.symbol}: largest {kind} {r.move_pct:.1f}% on {r.move_date} vs a typical stop distance of "
            f"{r.stop_pct:.1f}% for {r.strategy}; a full-size position (about {_usd(r.position_usd)}) would lose "
            f"about {_usd(r.loss_usd)}, {r.loss_usd / planned:.1f}× the planned {_usd(planned)} risk"
        )
    total = sum(r.loss_usd for r in f.risks)
    daily = risk.capital_usd * risk.daily_loss_limit_pct / 100
    kill = risk.capital_usd * risk.max_drawdown_kill_pct / 100
    return title, (
        "Stops are market orders sent after the price crosses the level, so a gap fills wherever the "
        "market opens. " + "; ".join(parts) + f". If every position took its worst day at once the book "
        f"would lose about {_usd(total)} ({total / risk.capital_usd:.1%} of capital), against a "
        f"{_usd(daily)} daily loss limit and a {_usd(kill)} drawdown kill: those limits block entries "
        f"and flatten after the fact; they cannot cap a gap."
    )


def _software_stop_item(f: _Findings) -> tuple[str, str]:
    outage = f.paper.longest_outage()
    if outage is None:
        observed = "There is no paper equity history yet, so the bot's real uptime is unknown."
    else:
        gap, ended = outage
        observed = (
            f"During paper trading the longest gap between equity snapshots (normally every 15 minutes) "
            f"was {gap / timedelta(hours=1):.1f} hours, ending {ended:%Y-%m-%d %H:%M} UTC: that long, "
            "nothing may have watched the stops."
        )
    return "The stops live in the bot, not at Alpaca.", (
        "The bot checks prices every poll and sends a market exit when a stop is crossed. If the VPS, "
        "Docker, the network or the bot is down, open positions have no stop at all, and crypto keeps "
        f"trading through nights and weekends. {observed} Add a dead-man's switch: an external heartbeat "
        "monitor that expects a ping from the bot every few minutes and pages you when it stops, so you "
        "can flatten from the Alpaca app."
    )


def _correlation_item(f: _Findings) -> tuple[str, str]:
    title = "Correlated positions lose together."
    stocks = [s for s in f.cfg.assets if asset_class(s) is AssetClass.STOCK]
    text = ""
    if {"SPY", "QQQ"} <= set(stocks):
        text = "SPY and QQQ often move together, so holding both is close to one double-size bet. "
    corr = f.correlation
    if corr is None:
        return title, text + "The book has fewer than two symbols with cached data, so no correlation is measured."
    pairs = "; ".join(f"{a}/{b} {full:.2f} over the whole history, {recent:.2f} over the last year" for a, b, full, recent in corr.pairs)
    risk = f.cfg.risk
    return title, (
        f"{text}Daily return correlations: {pairs}. On {corr.worst_day} a book holding every enabled symbol "
        f"at full size would have lost about {_usd(corr.worst_day_loss)} "
        f"({corr.worst_day_loss / risk.capital_usd:.1%} of capital) in one day. The "
        f"{risk.max_total_exposure_pct:g}% total exposure cap allows every position to be open at once."
    )


def _crypto_item(f: _Findings) -> tuple[str, str]:
    title = "Crypto never closes."
    crypto = [r for r in f.risks if asset_class(r.symbol) is AssetClass.CRYPTO]
    stock = next((r for r in f.risks if asset_class(r.symbol) is AssetClass.STOCK), None)
    fee = f.cfg.execution.fee_bps.get("crypto")
    fee_text = f" Fees ({fee:g} bps a side in the config) make every whipsaw expensive." if fee else ""
    if not crypto:
        return title, (
            "No crypto asset is in the book now. If you enable one, it trades nights and weekends when "
            "nobody is watching, and crypto held at a broker is not covered by SIPC." + fee_text
        )
    vol = "; ".join(f"{r.symbol} annualised volatility over the last year {r.vol_pct:.0f}%" for r in crypto)
    if stock is not None:
        vol += f" vs {stock.vol_pct:.0f}% for {stock.symbol}"
    return title, (
        f"{vol}. It trades nights and weekends, when you are least likely to notice a VPS outage, and "
        "a venue outage or halt can stop the bot from exiting. Crypto held at Alpaca is not covered by "
        "SIPC the way stocks are." + fee_text
    )


def _regime_item(f: _Findings) -> tuple[str, str]:
    title = "The regime that breaks each winner."
    if not f.winners:
        return title, "Unknown until the tournament has run (see the regime answer above)."
    return title, " ".join(f"{w['symbol']} {w['label']} breaks in {_breaks(w)}." for w in f.winners)


def _outage_item(f: _Findings) -> tuple[str, str]:
    jev = f.paper.jev
    decisions = jev.get("passed", 0) + jev.get("vetoed", 0) + jev.get("shadow_vetoed", 0) + jev.get("errors", 0)
    jev_text = (
        f" During paper trading Jev errored on {jev.get('errors', 0)} of {decisions} decisions."
        if decisions else ""
    )  # fmt: skip
    logged = f" The bot logged {f.paper.errors} errors during paper trading." if f.paper.start else ""
    return "Outages of Alpaca, Jev or Telegram.", (
        "If Alpaca is down the bot cannot exit; after "
        f"{f.cfg.risk.max_consecutive_errors} consecutive errors the kill switch trips, but its flatten "
        "also needs Alpaca, so positions stay open until it is back. If Jev is down every entry is "
        f"blocked (fail-closed): missed trades, not losses.{jev_text} If Telegram is down, approvals "
        f"expire (after {f.cfg.risk.approval_timeout_minutes / 60:g} hours for crypto, 30 minutes after the "
        "next open for stocks), so those are missed trades, and /kill never "
        f"arrives: use `python -m bot kill` on the server or set KILL_SWITCH=1.{logged}"
    )


def _approval_item(f: _Findings) -> tuple[str, str]:
    threshold = f.cfg.risk.approval_threshold_usd
    needing = entries = 0
    for w in f.winners:
        approvals = (w["run"] or {}).get("approvals") or {}
        needing += int(_finite(approvals.get("needing_approval")) or 0)
        entries += int(_finite(approvals.get("entry_orders")) or 0)
    backtest = f" In the backtest {needing} of the winners' {entries} entries needed approval." if entries else ""
    counts = f.paper.approvals
    paper = (
        f" During paper trading: {sum(counts.values())} requested, {counts.get('approved', 0)} approved, "
        f"{counts.get('rejected', 0)} rejected, {counts.get('expired', 0)} expired."
    )
    typical = max((r.position_usd for r in f.risks), default=f.cfg.risk.max_position_usd)
    share = "so nearly every entry asks" if typical > threshold else "so only the larger entries ask"
    return "Approval fatigue.", (
        f"Every entry above {_usd(threshold)} waits for a Telegram tap, and a full-size position is about "
        f"{_usd(typical)}, {share}.{backtest}{paper} Tapping Approve many times a month turns the check "
        "into a reflex; read each request."
    )


def _config_item(f: _Findings) -> tuple[str, str]:
    return "Configuration mistakes.", (
        "The kill switch's flatten closes every position in the Alpaca account, not just the bot's, so "
        "the paper (and any live) account must be dedicated to the bot. `ALPACA_PAPER=false` with live "
        "keys points the bot at real money: the live guard refuses a mismatched mode, but it cannot tell "
        "whether you meant it. Risk limits live in strategy.md, so editing one (for example "
        "max_position_usd) changes every order without a new backtest."
    )


def _overfit_item(f: _Findings) -> tuple[str, str]:
    title = "Overfitting."
    n_configs = _finite((f.tournament or {}).get("n_configs"))
    if f.tournament is None or n_configs is None:
        return title, "The number of configurations tested is unknown until the tournament has run; treat any backtest with suspicion."
    details = []
    for w in f.winners:
        run = w["run"] or {}
        oos = _finite((run.get("out_of_sample") or {}).get("n_trades"))
        neighbours = _finite(run.get("neighbors_passing"))
        bits = []
        if oos is not None:
            bits.append(f"{oos:.0f} out-of-sample trades")
        if neighbours is not None:
            bits.append(f"{neighbours:.0%} of neighbouring parameter sets also passed")
        if bits:
            details.append(f"{w['symbol']}: " + ", ".join(bits))
    return title, (
        f"The tournament tested {n_configs:.0f} configurations and kept the best in-sample, so the winners' "
        "numbers are flattered by selection, and a handful of trades a year gives wide error bars. "
        + ("; ".join(details) + ". " if details else "")
        + "Expect live results to be worse than the backtest."
    )


def _blow_up(f: _Findings) -> str:
    items = _blow_up_items(f)
    lines = [f"{i}. **{title}** {body}" for i, (title, body) in enumerate(items, start=1)]
    source = (
        f"cached daily data ({min(r.first for r in f.risks)} to {max(r.last for r in f.risks)})"
        if f.risks else "no cached data"
    )  # fmt: skip
    return (
        f"### {BLOW_UP_HEADING}\n\n"
        f"Concrete ways this bot can lose much more than its {f.cfg.risk.risk_per_trade_pct:g}% risk per "
        f"trade. Figures come from the {source}, the tournament results and the paper record.\n\n"
        + "\n".join(lines)
    )


def _render(f: _Findings) -> str:
    sections = [_verdict(f), _meta(f), _table(f), _answer_paper(f), _answer_kill(f), _answer_regime(f), _blow_up(f)]
    # Nothing in the report may look like a section marker, or it could shadow the CONFIG block.
    return "\n\n".join(sections).replace("<!--", "&lt;!--")


def _write_report(report: str, strategy_path: Path, settings: Settings) -> Path:
    """Rewrite the FINAL_CHECK section, or save the report under data_dir when strategy.md is not
    writable (the containers mount it read-only)."""
    try:
        replace_section(FINAL_CHECK_SECTION, report, strategy_path)
        return strategy_path
    except (OSError, ConfigError) as exc:
        fallback = settings.data_dir / FALLBACK_REPORT
        log.warning("could not update %s (%s); writing the report to %s", strategy_path, exc, fallback)
        _write_atomic(fallback, report + "\n")
        return fallback


def final_check(
    settings: Settings,
    cfg: StrategyConfig,
    broker: Broker | None = None,
    now: datetime | None = None,
    strategy_path: Path | str = STRATEGY_PATH,
    results_path: Path | str = RESULTS_PATH,
) -> dict[str, Any]:
    """Compare paper trading with the backtest, confirm the drill and the risk limits, and write
    `var/live_gate.json` and the FINAL_CHECK section of strategy.md.

    Returns the live-gate record (`passed`, `ts`, `checks`, `summary`, `blow_up`) plus
    `report_path`, where the Markdown report went. Never switches the bot to live.
    """
    now = _aware(now or utcnow())
    tournament, tournament_error = _load_tournament(Path(results_path))
    with _real_store(settings) as store:
        paper = _load_paper(store, now)
    source = _BarSource(settings, broker)
    replays = [_replay(symbol, rule, cfg, paper, source) for symbol, rule in cfg.enabled_assets.items()]
    drill, drill_error = _read_drill(settings)
    winners = _winners(tournament)
    risks: list[_MarketRisk] = []
    risk_notes: list[str] = []
    for symbol, rule in (cfg.enabled_assets or cfg.assets).items():
        try:
            risks.append(_market_risk(symbol, rule, cfg))
        except Exception as exc:  # tail numbers are informational: report the gap, never crash
            log.warning("no tail-risk numbers for %s: %s", symbol, exc)
            risk_notes.append(f"{symbol}: {exc}")
    findings = _Findings(
        now=now, cfg=cfg, paper=paper, replays=replays, bar_notes=source.notes,
        checks=[
            _check_paper_history(paper, cfg),
            _check_signal_parity(paper, replays, cfg),
            _check_fill_quality(paper, replays, cfg),
            _check_return_gap(paper, replays, cfg),
            _check_drill(drill, drill_error, cfg, now),
            _check_risk_config(tournament, tournament_error, cfg),
            _check_regime_risk(winners, tournament_error),
        ],
        drill=drill, tournament=tournament, tournament_error=tournament_error, winners=winners,
        risks=risks, risk_notes=risk_notes, correlation=_correlation(risks),
    )  # fmt: skip
    gate = {
        "passed": findings.passed,
        "ts": to_iso(now),
        "checks": [c.as_json() for c in findings.checks],
        "summary": _summary(findings),
        "blow_up": [{"title": title, "detail": body} for title, body in _blow_up_items(findings)],
    }
    _write_json(settings.live_gate_path, gate)
    report = _render(findings)
    written = _write_report(report, Path(strategy_path), settings)
    log.log(logging.INFO if findings.passed else logging.WARNING, "final check: %s", gate["summary"])
    return {**_jsonable(gate), "report_path": str(written)}
