"""The live loop. One `tick()` per poll runs these steps in order, each isolated so a failure is
logged, alerted and counted by `RiskManager.after_error()` while the remaining steps still run:

1. heartbeat and guards: the live-trading guard, then the kill switch (flatten until flat),
2. reconcile tracked orders with the broker: fills become positions and trades,
3. Telegram commands and approval decisions (also polled every 2 seconds by `run_forever`),
4. approval expiry,
5. stops and take-profits against the last trade price,
6. new daily bars: trailing stops and exit signals while long, entry signals while flat,
7. execution of queued orders in their window (exits first),
8. risk bookkeeping: start-of-day equity, drawdown kill, equity snapshots, the daily report.

Parity with `bot/backtest/engine.py`: a signal on bar i's close fills at bar i+1's open (stocks
at the open plus `stock_entry_delay_minutes`, crypto right after 00:00 UTC). While long, every
completed close (the fill bar's included) updates `bars_held` and `highest_close` before
`trailing_stop` and `exit_signal`; stops only ratchet up and apply from the next bar. No entry is
evaluated on a bar during which an exit filled. Entries are sized on mark-to-market bot equity:
capital + realized P&L of bot trades + unrealized P&L of bot positions.

Restart safety: processed bars (`last_bar:<symbol>`), the order queue, the tracked orders and
the daily markers live in the kv table; signals are UNIQUE per bar; client_order_ids are
deterministic, so OrderGateway never sends the same order twice.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd

from bot import news
from bot.broker import (
    LIVE_GATE_MAX_AGE,
    QTY_EPSILON,
    Broker,
    OrderGateway,
    assert_trading_allowed,
    make_client_order_id,
    redact,
)
from bot.config import AssetRule, ConfigError, Settings, StrategyConfig
from bot.jev import JevGate
from bot.models import (
    AssetClass,
    BrokerPosition,
    JevDecision,
    OrderIntent,
    OrderPurpose,
    OrderResult,
    PositionState,
    RiskAction,
    Side,
    Signal,
    Trade,
    asset_class,
)
from bot.notify import Command, Notifier
from bot.report import daily_report
from bot.risk import RESET_TS_KEY, KillSwitch, RiskContext, RiskManager
from bot.store import FINAL_ORDER_STATUSES, Store, ny_day_bounds, parse_ts, to_iso
from bot.strategies import build
from bot.strategies.base import Strategy
from bot.timeutil import NY, UTC, ny_trading_day, utcnow

log = logging.getLogger(__name__)

HISTORY_DAYS = 550  # calendar days of bars fetched per decision; covers every strategy's warmup
STOCK_DATA_READY = timedelta(hours=16, minutes=20)  # New York wall clock: SIP daily bars are usable
CRYPTO_DATA_READY = timedelta(minutes=5)  # after the 00:00 UTC close
BAR_RECHECK = timedelta(minutes=10)  # no new bar yet (weekend, holiday, late data): look again later
BAR_RETRY = timedelta(minutes=5)  # after a failed fetch
EXECUTION_WINDOW = timedelta(minutes=30)  # an entry not sent within this of its window start expires
STOCK_OPEN = clock_time(9, 30)
EQUITY_EVERY = timedelta(minutes=15)
FAST_POLL_S = 2.0  # Telegram only accepts callback answers for about 15 seconds
LIVE_GATE_WARN_BEFORE = timedelta(hours=48)
ALERT_DEDUPE = timedelta(minutes=30)  # the same error is alerted at most this often
MAX_ORDER_ATTEMPTS = 10  # per exit or stop; each failure also counts toward the kill switch
KEEP_IDS = 200  # external order ids remembered for hand-off
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

HEARTBEAT_KEY = "runner_heartbeat"
QUEUE_KEY = "runner:queue"
ORDERS_KEY = "runner:orders"
EXTERNAL_ORDERS_KEY = "runner:external_orders"
ADOPTED_KEY = "runner:adopted_orders"
SOD_KEY = "runner:start_of_day_equity"
EQUITY_TS_KEY = "runner:last_equity_ts"
REPORT_DAY_KEY = "runner:daily_report_day"
KILL_FLAT_KEY = "runner:kill_flat_alerted"

_EXIT_REASONS = {
    OrderPurpose.EXIT: "signal",
    OrderPurpose.STOP: "stop",
    OrderPurpose.TAKE_PROFIT: "take_profit",
    OrderPurpose.KILL: "kill",
}
_WORKING = frozenset({"new", "accepted", "partially_filled"})

HELP_TEXT = (
    "Commands:\n"
    "/status - mode, kill switch, positions, today's P&L\n"
    "/pnl - realized and unrealized P&L\n"
    "/kill [reason] - trip the kill switch: cancel every order and close every position in the account, "
    "stop new entries\n"
    "/help - this list\n"
    "Approve or reject entries with the buttons on each request. The kill switch is cleared only "
    "on the server: python -m bot resume --confirm"
)


def last_bar_key(symbol: str) -> str:
    return f"last_bar:{symbol}"


def exit_bar_key(symbol: str) -> str:
    return f"runner:exit_bar:{symbol}"


def entry_signal_key(symbol: str) -> str:
    return f"runner:entry_signal:{symbol}"


# --------------------------------------------------------------------------- persisted records

_R = TypeVar("_R", "QueuedOrder", "TrackedOrder")


def _from_dict(cls: type[_R], data: dict[str, Any]) -> _R:
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class QueuedOrder:
    """An order waiting for its execution window (and, for large entries, for approval)."""

    kind: str  # "entry" | "exit"
    symbol: str
    strategy: str
    signal_id: int
    bar_date: str
    signal_price: float
    reason: str
    window_open: str
    window_close: str | None = None  # entries only; exits never expire
    qty: float | None = None  # entries only; an exit sells the whole position
    stop: float | None = None
    take_profit: float | None = None
    approval_id: int | None = None
    approved: bool = False

    @property
    def awaiting_approval(self) -> bool:
        return self.approval_id is not None and not self.approved


@dataclass
class TrackedOrder:
    """An order the runner watches until it is final and its fills are applied."""

    client_order_id: str
    symbol: str
    side: str
    purpose: str
    strategy: str
    signal_id: int | None
    qty: float
    ref_price: float
    reason: str
    submitted_ts: str
    stop: float | None = None
    take_profit: float | None = None
    applied_qty: float = 0.0
    applied_avg: float = 0.0
    unfilled_seen_ts: str | None = None  # last poll that saw it not fully filled

    @property
    def fill_ts(self) -> datetime:
        """Best estimate of when new fills happened: after the last poll that saw none."""
        return parse_ts(self.unfilled_seen_ts or self.submitted_ts)


def load_queue(store: Store) -> list[QueuedOrder]:
    raw = store.kv_get(QUEUE_KEY)
    return [_from_dict(QueuedOrder, item) for item in json.loads(raw)] if raw else []


def load_tracked(store: Store) -> dict[str, TrackedOrder]:
    raw = store.kv_get(ORDERS_KEY)
    items = json.loads(raw) if raw else {}
    return {cid: _from_dict(TrackedOrder, item) for cid, item in items.items()}


def hand_off_orders(store: Store, results: Iterable[OrderResult]) -> None:
    """Let the runner reconcile orders another process sent (the CLI kill's flatten), so their
    fills are recorded as trades. The runner adopts them on its next tick."""
    ids = [r.client_order_id for r in results if r.broker_order_id]
    if not ids:
        return
    raw = store.kv_get(EXTERNAL_ORDERS_KEY)
    current = json.loads(raw) if raw else []
    store.kv_set(EXTERNAL_ORDERS_KEY, json.dumps((current + ids)[-KEEP_IDS:]))


# --------------------------------------------------------------------------- small helpers


@dataclass(frozen=True)
class Guard:
    entries: bool
    exits: bool
    reason: str | None = None


@dataclass(frozen=True)
class Book:
    """Bot positions marked to market."""

    capital: float
    prices: dict[str, float]
    exposure: dict[str, float]  # market value per bot position
    realized: float
    unrealized: float

    @property
    def bot_equity(self) -> float:
        return self.capital + self.realized + self.unrealized

    @property
    def total_exposure(self) -> float:
        return sum(self.exposure.values())


def _is_crypto(symbol: str) -> bool:
    return asset_class(symbol) is AssetClass.CRYPTO


def bar_day(symbol: str, ts: datetime) -> date:
    """The daily bar a moment belongs to: the New York date for stocks, the UTC date for crypto."""
    return ts.astimezone(UTC if _is_crypto(symbol) else NY).date()


def latest_closed_bar_day(symbol: str, now: datetime) -> date:
    """The newest bar date whose data the runner may act on at `now`."""
    if _is_crypto(symbol):
        return (now.astimezone(UTC) - CRYPTO_DATA_READY).date() - timedelta(days=1)
    return (now.astimezone(NY) - STOCK_DATA_READY).date()  # wall-clock arithmetic, DST-safe


def gate_label(mode: str, decision: JevDecision) -> str:
    if mode == "off":
        return "off"
    if decision.shadow:
        return "shadow"
    if decision.error:
        return "error"
    if not decision.passed:
        return "vetoed"
    if decision.model is None and not decision.answers:
        return "n/a"
    return "passed"


def _row_final(row: dict[str, Any]) -> bool:
    status = row["status"]
    return status in FINAL_ORDER_STATUSES and (status != "rejected" or bool(row["broker_order_id"]))


def _never_sent(row: dict[str, Any]) -> bool:
    return row["status"] == "refused" or (row["status"] == "rejected" and not row["broker_order_id"])


def _mark_price(position: BrokerPosition | None) -> float | None:
    if position is None or abs(position.qty) <= QTY_EPSILON:
        return None
    price = abs(position.market_value / position.qty)
    return price if math.isfinite(price) and price > 0 else None


def _qty(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".")


def _usd(value: float, signed: bool = False) -> str:
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    return f"{sign}${abs(value):,.2f}"


def _price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _ny(ts: datetime | str) -> str:
    moment = parse_ts(ts) if isinstance(ts, str) else ts
    return moment.astimezone(NY).strftime("%Y-%m-%d %H:%M NY")


# --------------------------------------------------------------------------- runner


class Runner:
    def __init__(
        self,
        settings: Settings,
        cfg: StrategyConfig,
        broker: Broker,
        gateway: OrderGateway,
        risk: RiskManager,
        kill: KillSwitch,
        jev_gate: JevGate,
        notifier: Notifier,
        store: Store,
        clock: Callable[[], datetime] = utcnow,
        bars_source: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.last_progress: float | None = None  # monotonic time of the last loop iteration (watchdog)
        self.broker = broker
        self.gateway = gateway
        self.risk = risk
        self.kill = kill
        self.jev_gate = jev_gate
        self.notifier = notifier
        self.store = store
        self.clock = clock
        self.bars_source = bars_source or broker.daily_bars
        self._next_bar_check: dict[str, datetime] = {}
        self._alerted: dict[str, datetime] = {}
        self._unknown_positions: set[str] = set()
        self._errors_this_tick = 0

    # ------------------------------------------------------------------ loop

    def tick(self) -> list[str]:
        """One poll. Never raises; returns the names of the steps that failed."""
        now = self._now()
        errors_before = self.risk.consecutive_errors
        self._errors_this_tick = 0
        failed: list[str] = []

        def run(name: str, fn: Callable[..., Any], *args: Any) -> Any:
            try:
                return fn(*args)
            except Exception as exc:
                failed.append(name)
                self._report_error(name, exc, now)
                return None

        run("heartbeat", self._heartbeat, now)
        guard = run("live guard", self._check_guard, now) or Guard(False, True, "the live-trading guard check failed")
        flatten = self._flatten_mode()
        if flatten and guard.exits:
            run("flatten", self._ensure_flat, now)
        run("reconcile", self._reconcile, now)
        if flatten:
            run("kill cleanup", self._kill_cleanup, now)
        run("commands", self.handle_commands)  # a /kill here flattens at once
        flatten = flatten or self._flatten_mode()
        if not flatten:
            entries_ok = guard.entries and not self._tripped()
            run("approvals", self._expire_approvals, now)
            if guard.exits:
                run("stops", self._check_stops, now)
            run("bars", self._process_bars, now, entries_ok, guard.reason)
            if guard.exits:
                run("execution", self._execute_queue, now)
        run("bookkeeping", self._bookkeeping, now)
        if not flatten and self._flatten_mode() and guard.exits:
            run("flatten", self._ensure_flat, now)  # tripped during this tick: don't wait a poll
        if not failed and self._errors_this_tick == 0 and self.risk.consecutive_errors <= errors_before:
            self.risk.after_success()
        return failed

    def _tripped(self) -> bool:
        try:
            return self.kill.is_tripped()
        except Exception:
            log.exception("could not read the kill switch; treating it as tripped")
            return True

    def _flatten_mode(self) -> bool:
        return self._tripped() and self.cfg.risk.kill_switch_flatten

    def run_forever(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Full tick every `execution.poll_seconds`, Telegram every 2 seconds in between.
        KeyboardInterrupt propagates for a clean shutdown; nothing else stops the loop."""
        poll_s = float(self.cfg.execution.poll_seconds)
        log.info("runner started: tick every %.0fs, commands every %.0fs", poll_s, FAST_POLL_S)
        next_tick = monotonic()
        while True:
            self.last_progress = monotonic()
            try:
                if monotonic() >= next_tick:
                    next_tick = monotonic() + poll_s
                    self.tick()
                else:
                    self._guarded("commands", self.handle_commands)
            except Exception:
                log.exception("runner loop iteration failed; continuing")
            sleep(FAST_POLL_S)

    # ------------------------------------------------------------------ step 1: heartbeat, guards, kill

    def _heartbeat(self, now: datetime) -> None:
        self.store.kv_set(HEARTBEAT_KEY, to_iso(now))

    def _check_guard(self, now: datetime) -> Guard:
        settings = self.settings
        try:
            assert_trading_allowed(settings, settings.live_gate_path, now=now, risk_increasing=False)
            if not self.broker.is_paper and settings.trading_mode != "live":
                raise ConfigError("the broker is live but TRADING_MODE is not live")
        except ConfigError as exc:
            self._alert_daily("guard_all", f"TRADING BLOCKED by the live-trading guard: {exc}", now)
            return Guard(entries=False, exits=False, reason=f"live-trading guard: {exc}")
        try:
            assert_trading_allowed(settings, settings.live_gate_path, now=now, risk_increasing=True)
        except ConfigError as exc:
            self._alert_daily("guard_entries", f"New entries blocked by the live-trading guard: {exc}", now)
            return Guard(entries=False, exits=True, reason=f"live-trading guard: {exc}")
        if settings.trading_mode == "live":
            self._warn_live_gate_expiry(now)
        return Guard(entries=True, exits=True)

    def _warn_live_gate_expiry(self, now: datetime) -> None:
        data = json.loads(self.settings.live_gate_path.read_text())
        expires = parse_ts(data["ts"]) + LIVE_GATE_MAX_AGE
        if expires - now < LIVE_GATE_WARN_BEFORE:
            hours = max(0.0, (expires - now).total_seconds() / 3600)
            self._alert_daily(
                "live_gate_expiry",
                f"The live gate expires in {hours:.0f}h ({_ny(expires)}). New entries stop then; "
                "run `python -m bot final-check` to renew it.",
                now,
            )

    def _ensure_flat(self, now: datetime) -> None:
        """Flatten while the account holds a position with no close order working, or an order
        the runner sent is still working. Alpaca cancels asynchronously, so a close can fail
        until the cancels land: later ticks repeat this until the account is flat."""
        held = {s for s, p in self.broker.positions().items() if abs(p.qty) > QTY_EPSILON}
        tracked = load_tracked(self.store)
        closing = {o.symbol for o in tracked.values() if o.purpose == OrderPurpose.KILL.value and self._working(o)}
        working_other = any(o.purpose != OrderPurpose.KILL.value and self._working(o) for o in tracked.values())
        if not (held - closing) and not working_other:
            return
        results = self.gateway.flatten_all(self._kill_reason())
        self._track_results(results, now)

    def _kill_cleanup(self, now: datetime) -> None:
        """While tripped: expire approvals, drop queued entries and exits, and once the account is
        flat drop any bot position left and say so once."""
        reason = self._kill_reason()
        for approval in self.store.pending_approvals():
            self._expire_approval(approval, now, reason)
        queue = load_queue(self.store)
        if queue:
            for item in queue:
                if item.kind == "entry":
                    self._set_signal(item.signal_id, status="blocked", risk_reason=reason)
            self._save_queue([])
        tracked = load_tracked(self.store)
        held = [s for s, p in self.broker.positions().items() if abs(p.qty) > QTY_EPSILON]
        if held or any(self._working(o) for o in tracked.values()):
            return
        busy = {o.symbol for o in tracked.values()}  # fills not applied yet are recorded first
        for symbol in self.store.get_positions():
            if symbol not in busy:
                self._drop_position(symbol, "the kill switch flattened the account", now)
        status = self.kill.status() or {}
        marker = f"{status.get('ts')}|{status.get('reason')}"
        if self.store.kv_get(KILL_FLAT_KEY) != marker:
            self.store.kv_set(KILL_FLAT_KEY, marker)
            self._alert(
                f"Kill switch: the account is flat and no orders are working ({reason}). "
                "No new entries until `python -m bot resume --confirm` on the server."
            )

    def _kill_reason(self) -> str:
        status = self.kill.status() or {}
        return f"kill switch: {status.get('reason') or 'tripped'}"

    # ------------------------------------------------------------------ step 2: reconcile

    def _reconcile(self, now: datetime) -> None:
        self._adopt_external_orders(now)
        tracked = load_tracked(self.store)
        for cid in list(tracked):
            order = tracked[cid]
            try:
                if self._reconcile_order(order, now):
                    del tracked[cid]
            except Exception as exc:
                self._report_error(f"reconcile {order.symbol}", exc, now)
            finally:
                self._save_tracked(tracked)  # after each order, so a crash never re-applies a fill
        self._check_drift(now, tracked)

    def _reconcile_order(self, order: TrackedOrder, now: datetime) -> bool:
        """Apply new fills; True once the order is final and every fill is applied."""
        row, terminal = self._poll(order)
        filled = float(row["filled_qty"] or 0.0) if row else 0.0
        avg = row["filled_avg_price"] if row else None
        if filled > order.applied_qty + QTY_EPSILON and avg:
            if order.side == Side.BUY.value:
                self._apply_buy(order, filled, float(avg))
            else:
                self._apply_sell(order, filled, float(avg), row, now)
            order.applied_qty, order.applied_avg = filled, float(avg)
        elif not terminal:
            order.unfilled_seen_ts = to_iso(now)
        if terminal:
            self._finish(order, row)
        return terminal

    def _poll(self, order: TrackedOrder) -> tuple[dict[str, Any] | None, bool]:
        """(store row after polling the broker, whether the order can no longer change)."""
        cid = order.client_order_id
        row = self.store.get_order_by_client_id(cid)
        if row is not None and (_row_final(row) or row["status"] == "refused"):
            return row, True
        result = self.broker.get_order(cid)
        if result is None:
            if row is not None and row["broker_order_id"]:
                log.warning("broker no longer knows order %s (%s); treating it as final", cid, row["status"])
            return row, True
        self.store.upsert_order(self._intent_for(order, row), result)
        row = self.store.get_order_by_client_id(cid)
        return row, row is not None and _row_final(row)

    @staticmethod
    def _intent_for(order: TrackedOrder, row: dict[str, Any] | None) -> OrderIntent:
        return OrderIntent(
            symbol=order.symbol,
            side=Side(order.side),
            qty=float(row["qty"]) if row else order.qty,
            ref_price=float(row["ref_price"]) if row else order.ref_price,
            purpose=OrderPurpose(order.purpose),
            reason=order.reason,
            client_order_id=order.client_order_id,
            signal_id=order.signal_id,
        )

    def _apply_buy(self, order: TrackedOrder, filled: float, avg: float) -> None:
        """Entry fills set the position absolutely (idempotent if a crash replays them)."""
        symbol = order.symbol
        position = self.store.get_positions().get(symbol)
        if position is None:
            if order.stop is None:
                raise ValueError(f"entry {order.client_order_id} filled without a stop level")
            position = PositionState(
                symbol=symbol,
                strategy=order.strategy,
                qty=filled,
                entry_price=avg,
                entry_ts=order.fill_ts,
                stop_price=float(order.stop),
                take_profit=order.take_profit,
                bars_held=0,
                highest_close=avg,
            )
        else:
            highest = max(position.highest_close, avg) if position.bars_held else avg
            position = replace(position, qty=filled, entry_price=avg, highest_close=highest)
        self.store.put_position(position)
        if order.signal_id is not None:
            self.store.kv_set(entry_signal_key(symbol), str(order.signal_id))
            self._set_signal(order.signal_id, status="filled")
        delta = filled - order.applied_qty
        self._alert(f"Filled: BUY {_qty(delta)} {symbol} @ {_price(avg)} (entry, {order.strategy})")

    def _apply_sell(
        self, order: TrackedOrder, filled: float, avg: float, row: dict[str, Any] | None, now: datetime
    ) -> None:
        symbol = order.symbol
        delta = filled - order.applied_qty
        price = (filled * avg - order.applied_qty * order.applied_avg) / delta
        purpose = OrderPurpose(order.purpose)
        position = self.store.get_positions().get(symbol)
        if position is None:
            log.info(
                "sell fill %s for %s: no bot position (already closed or not the bot's)", order.client_order_id, symbol
            )
            self._alert(f"Filled: SELL {_qty(delta)} {symbol} @ {_price(price)} ({purpose.value}, not a bot position)")
            return
        qty = min(delta, position.qty)
        fill_ts = order.fill_ts
        fees = self._fee_rate(symbol) * qty * (position.entry_price + price)
        pnl = qty * (price - position.entry_price) - fees
        trade = Trade(
            symbol=symbol,
            strategy=position.strategy,
            entry_ts=position.entry_ts,
            entry_price=position.entry_price,
            exit_ts=fill_ts,
            exit_price=price,
            qty=qty,
            pnl=pnl,
            pnl_pct=pnl / (position.entry_price * qty),
            exit_reason=_EXIT_REASONS.get(purpose, "signal"),
            fees=fees,
        )
        entry_sid = self._entry_signal_id(symbol)
        if not self._trade_recorded(trade):
            self.store.insert_trade(trade, entry_sid)
        remaining = position.qty - qty
        if remaining > QTY_EPSILON and row is not None and row["status"] == "filled" and not self._broker_holds(symbol):
            log.info("%s: sold everything the broker held; dropping %s residual qty", symbol, _qty(remaining))
            remaining = 0.0
        if remaining > QTY_EPSILON:
            self.store.put_position(replace(position, qty=remaining))
        else:
            self.store.delete_position(symbol)
            self.store.kv_set(exit_bar_key(symbol), bar_day(symbol, fill_ts).isoformat())
            self.store.kv_set(entry_signal_key(symbol), "")
        if entry_sid is not None:
            self._update_outcome(entry_sid)
        if purpose is OrderPurpose.EXIT and order.signal_id is not None:
            self._set_signal(order.signal_id, status="filled")
        self._alert(
            f"Filled: SELL {_qty(qty)} {symbol} @ {_price(price)} ({purpose.value}, {position.strategy}). "
            f"P&L {_usd(pnl, signed=True)} ({trade.pnl_pct * 100:+.2f}%)"
        )

    def _finish(self, order: TrackedOrder, row: dict[str, Any] | None) -> None:
        """An order is final: settle its signal when nothing filled."""
        if order.applied_qty > QTY_EPSILON or row is None:
            return
        message = row["message"] or row["status"]
        if order.purpose == OrderPurpose.ENTRY.value and order.signal_id is not None:
            status = "blocked" if row["status"] == "refused" else "skipped"
            self._set_signal(order.signal_id, status=status, risk_reason=f"order {row['status']}: {message}")
        if row["status"] in ("canceled", "expired"):
            self._alert(f"Order {order.purpose} {order.symbol} was {row['status']} without a fill: {message}")

    def _check_drift(self, now: datetime, tracked: dict[str, TrackedOrder]) -> None:
        positions = self.store.get_positions()
        held = self.broker.positions()
        busy = {o.symbol for o in tracked.values()}
        for symbol, position in positions.items():
            if symbol in busy:
                continue
            at_broker = held.get(symbol)
            if at_broker is None or at_broker.qty <= QTY_EPSILON:
                self._drop_position(symbol, "the broker has no position", now)
            elif at_broker.qty < position.qty - QTY_EPSILON:
                text = (
                    f"{symbol}: the broker holds {_qty(at_broker.qty)}, the bot recorded {_qty(position.qty)}; "
                    "using the broker's quantity"
                )
                log.warning(text)
                self.store.log_event("warning", "position_drift", text, ts=now)
                self.store.put_position(replace(position, qty=at_broker.qty))
        for symbol, at_broker in held.items():
            if symbol in positions or symbol in busy or abs(at_broker.qty) <= QTY_EPSILON:
                continue
            if symbol not in self._unknown_positions:
                self._unknown_positions.add(symbol)
                qty = _qty(at_broker.qty)
                text = (
                    f"Drift: the account holds {qty} {symbol} that the bot did not open. The bot won't trade it, "
                    "but the kill switch will sell it. Use an account only the bot trades."
                )
                log.warning(text)
                self.store.log_event("warning", "position_drift", text, ts=now)
                self._alert(text)
        self._unknown_positions &= set(held)

    def _drop_position(self, symbol: str, why: str, now: datetime) -> None:
        if not self.store.delete_position(symbol):
            return
        self.store.kv_set(entry_signal_key(symbol), "")
        text = f"Drift: dropped the bot's {symbol} position because {why}. Check the broker account."
        log.error(text)
        self.store.log_event("error", "position_drift", text, ts=now)
        self._alert(text)

    def _adopt_external_orders(self, now: datetime) -> None:
        raw = self.store.kv_get(EXTERNAL_ORDERS_KEY)
        if not raw:
            return
        adopted_raw = self.store.kv_get(ADOPTED_KEY)
        adopted = json.loads(adopted_raw) if adopted_raw else []
        new = [cid for cid in json.loads(raw) if cid not in adopted]
        if not new:
            return
        self._track_ids(new, now)
        self.store.kv_set(ADOPTED_KEY, json.dumps((adopted + new)[-KEEP_IDS:]))

    # ------------------------------------------------------------------ step 3: commands

    def handle_commands(self) -> None:
        for command in self.notifier.poll():
            now = self._now()
            log.info("command %s%s", command.kind, f" #{command.approval_id}" if command.approval_id else "")
            try:
                self._handle_command(command, now)
            except Exception as exc:
                self._report_error(f"command {command.kind}", exc, now)

    def _handle_command(self, command: Command, now: datetime) -> None:
        if command.kind == "kill":
            reason = command.argument or "Telegram /kill"
            self.kill.trip(reason, "telegram")
            flatten = self.cfg.risk.kill_switch_flatten
            self._alert(
                f"Kill switch tripped: {reason}." + (" Flattening now." if flatten else " New entries are blocked.")
            )
            if flatten:
                self._ensure_flat(now)
        elif command.kind == "status":
            self._alert(self.status_text(now))
        elif command.kind == "pnl":
            self._alert(self.pnl_text(now))
        elif command.kind in ("approve", "reject") and command.approval_id is not None:
            self._decide(command.kind, command.approval_id, now)
        else:
            self._alert(HELP_TEXT)

    def _decide(self, kind: str, approval_id: int, now: datetime) -> None:
        approval = self.store.get_approval(approval_id)
        if approval is None:
            self._alert(f"Approval #{approval_id} does not exist.")
            return
        if approval["status"] == "pending" and now >= parse_ts(approval["expires_ts"]):
            self._expire_approval(approval, now, "it was answered after it expired")
            return
        if approval["status"] != "pending":
            self._alert(f"Approval #{approval_id} was already {approval['status']}; nothing was sent.")
            return
        if kind == "reject":
            self._reject(approval, now)
            return
        if self.kill.is_tripped():
            self._alert(f"Approval #{approval_id} not accepted: the kill switch is tripped.")
            return
        item = self._queued_for_approval(approval_id)
        price = self.broker.last_price(item.symbol) if item is not None else None  # before deciding
        if not self.store.set_approval(approval_id, "approved", now=now):
            self._alert(f"Approval #{approval_id} was already decided or has expired; nothing was sent.")
            return
        if item is None or price is None:
            self._alert(f"Approval #{approval_id} has no queued order (superseded); nothing will be sent.")
            return
        drift = abs(price / item.signal_price - 1) * 100
        limit = self.cfg.risk.approval_max_price_drift_pct
        if drift > limit:
            self._remove_queued(lambda q: q.approval_id == approval_id)
            reason = f"approved, but the price moved {drift:.2f}% (> {limit:g}%) since the signal"
            self._set_signal(item.signal_id, status="skipped", risk_reason=reason)
            self._alert(f"Approval #{approval_id}: {item.symbol} entry skipped: {reason}.")
            return
        self._update_queued(approval_id, approved=True)
        self._set_signal(item.signal_id, status="queued")
        self._alert(
            f"Approved #{approval_id}: BUY {_qty(item.qty or 0)} {item.symbol} goes out at {_ny(item.window_open)}"
            " after the risk checks run again."
        )

    def _reject(self, approval: dict[str, Any], now: datetime) -> None:
        approval_id = approval["id"]
        if not self.store.set_approval(approval_id, "rejected", now=now):
            self._alert(f"Approval #{approval_id} was already decided or has expired.")
            return
        self._remove_queued(lambda q: q.approval_id == approval_id)
        self._settle_approval_signal(approval, status="rejected", reason="rejected on Telegram")
        self._alert(f"Rejected #{approval_id}: no order will be sent.")

    # ------------------------------------------------------------------ step 4: approval expiry

    def _expire_approvals(self, now: datetime) -> None:
        for approval in self.store.pending_approvals():
            if now >= parse_ts(approval["expires_ts"]):
                self._expire_approval(approval, now, "no answer in time")

    def _expire_approval(self, approval: dict[str, Any], now: datetime, why: str) -> None:
        approval_id = approval["id"]
        if not self.store.set_approval(approval_id, "expired", now=now):
            return
        self._remove_queued(lambda q: q.approval_id == approval_id)
        self._settle_approval_signal(approval, status="expired", reason=f"approval expired: {why}")
        self._alert(f"Approval #{approval_id} expired ({why}); the entry was not sent.")

    def _settle_approval_signal(self, approval: dict[str, Any], status: str, reason: str) -> None:
        signal = self.store.get_signal(approval["signal_id"]) if approval["signal_id"] is not None else None
        if signal is not None and signal["approval_id"] == approval["id"] and signal["status"] == "awaiting_approval":
            self._set_signal(signal["id"], status=status, risk_reason=reason)

    # ------------------------------------------------------------------ step 5: stops

    def _check_stops(self, now: datetime) -> None:
        positions = self.store.get_positions()
        if not positions:
            return
        tracked = load_tracked(self.store)
        stock_open: bool | None = None
        for symbol, position in positions.items():
            if self._pending_sell(tracked, symbol):
                continue
            if not _is_crypto(symbol):
                if stock_open is None:
                    stock_open = self.broker.is_market_open()
                if not stock_open:
                    continue  # the first tick after the open catches a gap, like the backtest
            try:
                self._check_levels(position, now)
            except Exception as exc:
                self._report_error(f"stops {symbol}", exc, now)

    def _check_levels(self, position: PositionState, now: datetime) -> None:
        price = self.broker.last_price(position.symbol)
        if price <= position.stop_price:
            purpose, why = OrderPurpose.STOP, f"last {_price(price)} <= stop {_price(position.stop_price)}"
        elif position.take_profit is not None and price >= position.take_profit:
            purpose, why = (
                OrderPurpose.TAKE_PROFIT,
                f"last {_price(price)} >= take-profit {_price(position.take_profit)}",
            )
        else:
            return
        base = f"{position.strategy}|{to_iso(position.entry_ts)}"
        result = self._submit_sell(position, purpose, price, why, base, None, now)
        if result is not None and not _never_sent_result(result):
            label = purpose.value.replace("_", " ").capitalize()
            self._alert(f"{label} hit on {position.symbol}: {why}. Selling {_qty(position.qty)}.")

    def _submit_sell(
        self,
        position: PositionState,
        purpose: OrderPurpose,
        ref_price: float,
        reason: str,
        base_key: str,
        signal_id: int | None,
        now: datetime,
    ) -> OrderResult | None:
        cid = self._next_client_order_id(purpose, position.symbol, base_key, now)
        if cid is None:
            return None
        intent = OrderIntent(position.symbol, Side.SELL, position.qty, ref_price, purpose, reason, cid, signal_id)
        self._track(intent, position.strategy, now)
        result = self.gateway.submit(intent)
        if _never_sent_result(result):
            self._untrack(cid)
            if result.message.startswith(f"no long {position.symbol} position to sell"):
                self._drop_position(position.symbol, "the broker has no long position to sell", now)
        return result

    def _next_client_order_id(self, purpose: OrderPurpose, symbol: str, base_key: str, now: datetime) -> str | None:
        """Attempt n's deterministic id: reuse an id that never reached the broker, move past
        final ones, and return None while one is still working or attempts are exhausted."""
        for attempt in range(MAX_ORDER_ATTEMPTS):
            cid = make_client_order_id(purpose, symbol, f"{base_key}|{attempt}")
            row = self.store.get_order_by_client_id(cid)
            if row is None or _never_sent(row):
                return cid
            if not _row_final(row):
                return None
        self._alert(
            f"{symbol}: {MAX_ORDER_ATTEMPTS} {purpose.value} orders failed; not retrying. Close it manually.",
            dedupe_key=f"attempts:{purpose.value}:{symbol}:{base_key}",
            now=now,
        )
        return None

    # ------------------------------------------------------------------ step 6: new bars

    def _process_bars(self, now: datetime, entries_ok: bool, blocked_reason: str | None) -> None:
        positions = self.store.get_positions()
        symbols = list(self.cfg.enabled_assets) + [s for s in positions if s not in self.cfg.enabled_assets]
        for symbol in symbols:
            if not self._bar_due(symbol, now):
                continue
            try:
                self._process_symbol(symbol, now, entries_ok, blocked_reason)
            except Exception as exc:
                self._next_bar_check[symbol] = now + BAR_RETRY
                self._report_error(f"bars {symbol}", exc, now)

    def _bar_due(self, symbol: str, now: datetime) -> bool:
        last = self.store.kv_get(last_bar_key(symbol))
        if last and date.fromisoformat(last) >= latest_closed_bar_day(symbol, now):
            return False
        return now >= self._next_bar_check.get(symbol, EPOCH)

    def _process_symbol(self, symbol: str, now: datetime, entries_ok: bool, blocked_reason: str | None) -> None:
        target = latest_closed_bar_day(symbol, now)
        start = now.astimezone(UTC).date() - timedelta(days=HISTORY_DAYS)
        bars = self.bars_source(symbol, start)
        bars = bars[bars.index <= pd.Timestamp(target)]
        last_raw = self.store.kv_get(last_bar_key(symbol))
        last = date.fromisoformat(last_raw) if last_raw else None
        latest = bars.index[-1].date() if not bars.empty else None
        if latest is None or (last is not None and latest <= last):
            self._next_bar_check[symbol] = now + BAR_RECHECK
            return
        if last is not None:
            missed = [d.date().isoformat() for d in bars.index if last < d.date() < latest]
            if missed:
                log.warning(
                    "%s: %d bar(s) missed while down (%s..%s) were not processed; acting on %s only",
                    symbol, len(missed), missed[0], missed[-1], latest,
                )  # fmt: skip
        rule = self.cfg.assets.get(symbol)
        position = self.store.get_positions().get(symbol)
        log.info("%s: processing the %s bar (%s)", symbol, latest, "long" if position else "flat")
        strategy = self._strategy_for(symbol, rule, position)
        df = strategy.prepare(bars)
        i = len(df) - 1
        if position is not None:
            self._on_close_long(strategy, df, i, position, latest, now)
        elif rule is not None and rule.enabled:
            self._on_close_flat(strategy, bars, df, i, latest, now, entries_ok, blocked_reason)
        self.store.kv_set(last_bar_key(symbol), latest.isoformat())  # only after the signal is durable

    def _strategy_for(self, symbol: str, rule: AssetRule | None, position: PositionState | None) -> Strategy:
        if position is not None and (rule is None or rule.strategy != position.strategy):
            log.warning("%s: managing the open %s position with default params (config says %s)",
                        symbol, position.strategy, rule.strategy if rule else "nothing")  # fmt: skip
            return build(position.strategy, symbol, {})
        assert rule is not None
        return build(rule.strategy, symbol, dict(rule.params))

    def _on_close_long(
        self, strategy: Strategy, df: pd.DataFrame, i: int, position: PositionState, day: date, now: datetime
    ) -> None:
        """Engine step 3. bars_held and highest_close are recomputed from the bars since the fill
        bar, so replaying a bar after a restart (or skipping missed ones) cannot double count."""
        symbol = position.symbol
        entry_day = bar_day(symbol, position.entry_ts)
        if entry_day > day:
            return  # the position was opened after this bar closed
        since_entry = df.index >= pd.Timestamp(entry_day)
        updated = replace(
            position,
            bars_held=int(since_entry.sum()),
            highest_close=max(position.highest_close, float(df["close"][since_entry].max())),
        )
        new_stop = strategy.trailing_stop(df, i, replace(updated))
        if new_stop is not None and math.isfinite(new_stop) and new_stop > updated.stop_price:
            log.info("%s: stop raised %s -> %s", symbol, _price(updated.stop_price), _price(new_stop))
            updated.stop_price = float(new_stop)
        self.store.put_position(updated)
        signal = strategy.exit_signal(df, i, replace(updated))
        if signal is not None:
            self._queue_exit(signal, updated, day, now)

    def _queue_exit(self, signal: Signal, position: PositionState, day: date, now: datetime) -> None:
        signal_id = self._record_signal(signal, day)
        row = self.store.get_signal(signal_id)
        if row is not None and row["status"] != "new":
            return  # already handled before a restart
        symbol = signal.symbol
        if any(q.kind == "exit" and q.symbol == symbol for q in load_queue(self.store)):
            self._set_signal(signal_id, gate="n/a", status="skipped", risk_reason="an exit is already queued")
            return
        if self._pending_sell(load_tracked(self.store), symbol):
            self._set_signal(signal_id, gate="n/a", status="skipped", risk_reason="a sell order is already working")
            return
        window_open, _, _ = self._window(symbol, now)
        item = QueuedOrder(
            kind="exit",
            symbol=symbol,
            strategy=position.strategy,
            signal_id=signal_id,
            bar_date=day.isoformat(),
            signal_price=signal.price,
            reason=signal.reason,
            window_open=to_iso(window_open),
        )
        self._upsert_queued(item)
        self._set_signal(signal_id, gate="n/a", status="queued")
        self._alert(f"Exit signal on {symbol} ({position.strategy}): {signal.reason}. Selling at {_ny(window_open)}.")

    def _on_close_flat(
        self,
        strategy: Strategy,
        bars: pd.DataFrame,
        df: pd.DataFrame,
        i: int,
        day: date,
        now: datetime,
        entries_ok: bool,
        blocked_reason: str | None,
    ) -> None:
        """Engine step 4: no entry on a bar during which an exit filled, or while one is pending."""
        symbol = strategy.symbol
        if self.store.kv_get(exit_bar_key(symbol)) == day.isoformat():
            log.info("%s: an exit filled during bar %s; no entry is evaluated on it", symbol, day)
            return
        if self._entry_pending(symbol):
            log.warning("%s: an entry is still queued or working; bar %s is not evaluated for entry", symbol, day)
            return
        if i + 1 < strategy.warmup:
            log.warning("%s: %d bars are fewer than the %d-bar warmup; no entry", symbol, i + 1, strategy.warmup)
            return
        signal = strategy.entry_signal(df, i)
        if signal is not None:
            self._handle_entry(signal, bars, day, now, entries_ok, blocked_reason)

    def _handle_entry(
        self,
        signal: Signal,
        bars: pd.DataFrame,
        day: date,
        now: datetime,
        entries_ok: bool,
        blocked_reason: str | None,
    ) -> None:
        symbol = signal.symbol
        signal_id = self._record_signal(signal, day)
        row = self.store.get_signal(signal_id)
        assert row is not None
        if row["status"] != "new":
            self._resume(row)
            return
        if not entries_ok:
            reason = "kill switch is tripped" if self.kill.is_tripped() else (blocked_reason or "entries are blocked")
            self._set_signal(signal_id, status="blocked", risk_reason=reason)
            log.warning("%s entry signal blocked: %s", symbol, reason)
            return
        gate, jev_reason = row["gate"], row["risk_reason"] or ""
        if gate is None:
            gate, jev_reason = self._consult_jev(signal, bars, signal_id, now)
        if gate in ("vetoed", "error"):
            reason = f"Jev {gate}: {jev_reason}"
            self._set_signal(signal_id, status="blocked", risk_reason=reason)
            self._alert(f"Entry on {symbol} ({signal.strategy}) blocked. {reason}")
            return
        book = self._book()
        qty = self.risk.size_entry(signal, book.bot_equity)
        if qty <= 0:
            self._set_signal(
                signal_id, status="blocked", risk_reason="position size is zero (stop at or above the price)"
            )
            return
        cid = make_client_order_id(OrderPurpose.ENTRY, symbol, f"{signal.strategy}|{day.isoformat()}|0")
        intent = OrderIntent(symbol, Side.BUY, qty, signal.price, OrderPurpose.ENTRY, signal.reason, cid, signal_id)
        verdict = self.risk.check(intent, self._risk_context(book, symbol, now))
        if verdict.action is RiskAction.BLOCK:
            self._set_signal(signal_id, risk_action=verdict.action, risk_reason=verdict.reason, status="blocked")
            self._alert(f"Entry on {symbol} ({signal.strategy}) blocked by risk: {verdict.reason}")
            return
        qty = verdict.adjusted_qty if verdict.adjusted_qty is not None else qty
        window_open, window_close, expires = self._window(symbol, now)
        item = QueuedOrder(
            kind="entry",
            symbol=symbol,
            strategy=signal.strategy,
            signal_id=signal_id,
            bar_date=day.isoformat(),
            signal_price=signal.price,
            reason=signal.reason,
            window_open=to_iso(window_open),
            window_close=to_iso(window_close),
            qty=qty,
            stop=signal.stop_price,
            take_profit=signal.take_profit,
        )
        if verdict.action is RiskAction.NEEDS_APPROVAL:
            if expires <= now:  # the bar was processed after its execution window had closed
                self._set_signal(signal_id, status="expired", risk_reason="bar processed after its execution window closed")
                return
            self._request_approval(item, signal, verdict.reason, jev_reason, expires, now)
            return
        self._upsert_queued(item)
        self._set_signal(signal_id, risk_action=verdict.action, risk_reason=verdict.reason, status="queued")
        self._alert(
            f"Entry queued: BUY {_qty(qty)} {symbol} (~{_usd(qty * signal.price)}), stop {_price(signal.stop_price)}, "
            f"at {_ny(window_open)}. {signal.strategy}: {signal.reason}"
        )

    def _consult_jev(self, signal: Signal, bars: pd.DataFrame, signal_id: int, now: datetime) -> tuple[str, str]:
        jev = self.cfg.jev
        headlines = (
            [] if jev.mode == "off"
            else news.fetch_headlines(self.settings, signal.symbol, jev.headline_lookback_hours, jev.max_headlines)
        )  # fmt: skip
        decision = self.jev_gate.evaluate(signal, bars, headlines)
        self.store.insert_jev(decision, signal_id, signal.symbol, ts=now)
        gate = gate_label(jev.mode, decision)
        self._set_signal(signal_id, gate=gate, risk_reason=decision.reason)
        return gate, decision.reason

    def _request_approval(
        self, item: QueuedOrder, signal: Signal, risk_reason: str, jev_reason: str, expires: datetime, now: datetime
    ) -> None:
        qty = item.qty or 0.0
        approval_id = self.store.create_approval(item.signal_id, qty * signal.price, expires, now=now)
        item.approval_id = approval_id
        if _is_crypto(item.symbol):  # approved crypto entries go out as soon as they are approved
            item.window_close = to_iso(expires + EXECUTION_WINDOW)
        self._upsert_queued(item)
        self._set_signal(
            item.signal_id, approval_id=approval_id, risk_action=RiskAction.NEEDS_APPROVAL,
            risk_reason=risk_reason, status="awaiting_approval",
        )  # fmt: skip
        text = "\n".join(
            [
                f"Approve entry #{approval_id}?",
                f"BUY {_qty(qty)} {item.symbol} at ~{_price(signal.price)} (about {_usd(qty * signal.price)})",
                f"Stop {_price(signal.stop_price)}. {signal.strategy}: {signal.reason}",
                f"Jev: {jev_reason or 'n/a'}",
                f"Risk: {risk_reason}",
                f"Goes out at {_ny(item.window_open)}. Expires {_ny(expires)}.",
            ]
        )
        message_id = self.notifier.request_approval(approval_id, text)
        if message_id is not None:
            self.store.set_approval_message_id(approval_id, message_id)

    def _resume(self, row: dict[str, Any]) -> None:
        """The bar was being processed when the runner stopped; its signal already moved on."""
        log.info(
            "signal %s (%s %s %s) already %s; not re-sending",
            row["id"],
            row["symbol"],
            row["kind"],
            row["bar_date"],
            row["status"],
        )
        if row["status"] != "awaiting_approval" or row["approval_id"] is None:
            return
        approval = self.store.get_approval(row["approval_id"])
        if approval is not None and approval["status"] == "pending" and approval["message_id"] is None:
            text = f"Approve entry #{approval['id']}? BUY {row['symbol']} ({row['strategy']}): {row['reason']}"
            message_id = self.notifier.request_approval(approval["id"], text)
            if message_id is not None:
                self.store.set_approval_message_id(approval["id"], message_id)

    def _window(self, symbol: str, now: datetime) -> tuple[datetime, datetime, datetime]:
        """(window open, window close, approval expiry) for an order from `signal`.

        Crypto goes out at once; approvals last `approval_timeout_minutes`. Stocks go out
        `stock_entry_delay_minutes` after the next session's open, within EXECUTION_WINDOW of it;
        approvals last until the window closes, so an evening signal can be approved overnight.
        """
        if _is_crypto(symbol):
            return now, now + EXECUTION_WINDOW, now + timedelta(minutes=self.cfg.risk.approval_timeout_minutes)
        if self.broker.is_market_open():  # a bar processed late, during the next session
            session = datetime.combine(now.astimezone(NY).date(), STOCK_OPEN, tzinfo=NY).astimezone(UTC)
        else:
            session = self.broker.next_open().astimezone(UTC)
        delay = timedelta(minutes=self.cfg.execution.stock_entry_delay_minutes)
        return session + delay, session + EXECUTION_WINDOW, session + EXECUTION_WINDOW

    # ------------------------------------------------------------------ step 7: execution

    def _execute_queue(self, now: datetime) -> None:
        queue = load_queue(self.store)
        ordered = [q for q in queue if q.kind == "exit"] + [q for q in queue if q.kind == "entry"]
        for item in ordered:
            try:
                done = self._execute_exit(item, now) if item.kind == "exit" else self._execute_entry(item, now)
            except Exception as exc:
                self._report_error(f"execute {item.kind} {item.symbol}", exc, now)
                continue
            if done:
                self._remove_queued(_same_order(item))

    def _execute_exit(self, item: QueuedOrder, now: datetime) -> bool:
        """Exits never expire: they stay queued (and retry) until the position is gone."""
        position = self.store.get_positions().get(item.symbol)
        if position is None:
            signal = self.store.get_signal(item.signal_id)
            if signal is not None and signal["status"] == "queued":
                self._set_signal(item.signal_id, status="skipped", risk_reason="the position was already closed")
            return True
        if self._pending_sell(load_tracked(self.store), item.symbol) or now < parse_ts(item.window_open):
            return False
        if not _is_crypto(item.symbol) and not self.broker.is_market_open():
            return False
        price = self.broker.last_price(item.symbol)
        base = f"{item.strategy}|{item.bar_date}"
        result = self._submit_sell(position, OrderPurpose.EXIT, price, item.reason, base, item.signal_id, now)
        if result is not None and not _never_sent_result(result):
            self._set_signal(item.signal_id, order_client_id=result.client_order_id, status="submitted")
        return False

    def _execute_entry(self, item: QueuedOrder, now: datetime) -> bool:
        if item.awaiting_approval:
            return False  # step 4 expires it
        signal = self.store.get_signal(item.signal_id)
        if signal is None or signal["status"] != "queued":
            return True
        if item.window_close is not None and now > parse_ts(item.window_close):
            reason = f"execution window closed at {_ny(item.window_close)} before the order could go out"
            self._set_signal(item.signal_id, status="expired", risk_reason=reason)
            self._alert(f"Entry on {item.symbol} expired: {reason}.")
            return True
        if now < parse_ts(item.window_open):
            return False
        if not _is_crypto(item.symbol) and not self.broker.is_market_open():
            return False
        cid = make_client_order_id(OrderPurpose.ENTRY, item.symbol, f"{item.strategy}|{item.bar_date}|0")
        existing = self.store.get_order_by_client_id(cid)
        if existing is not None and not _never_sent(existing):  # sent before a restart
            status = "filled" if existing["filled_qty"] else "submitted"
            self._set_signal(item.signal_id, order_client_id=cid, status=status)
            return True
        price = self.broker.last_price(item.symbol)
        limit = self.cfg.risk.approval_max_price_drift_pct
        drift = abs(price / item.signal_price - 1) * 100
        if item.approval_id is not None and drift > limit:
            reason = f"approved, but the price moved {drift:.2f}% (> {limit:g}%) since the signal"
            self._set_signal(item.signal_id, status="skipped", risk_reason=reason)
            self._alert(f"Entry on {item.symbol} skipped: {reason}.")
            return True
        book = self._book()
        intent = OrderIntent(
            item.symbol, Side.BUY, item.qty or 0.0, price, OrderPurpose.ENTRY, item.reason, cid, item.signal_id
        )
        verdict = self.risk.check(intent, self._risk_context(book, item.symbol, now, exclude=item))
        if verdict.action is RiskAction.BLOCK or (
            verdict.action is RiskAction.NEEDS_APPROVAL and item.approval_id is None
        ):
            reason = verdict.reason if verdict.action is RiskAction.BLOCK else f"not approved: {verdict.reason}"
            self._set_signal(item.signal_id, risk_action=verdict.action, risk_reason=reason, status="blocked")
            self._alert(f"Entry on {item.symbol} blocked at execution: {reason}")
            return True
        if verdict.adjusted_qty is not None:
            intent = replace(intent, qty=verdict.adjusted_qty)
        self._track(intent, item.strategy, now, stop=item.stop, take_profit=item.take_profit)
        result = self.gateway.submit(intent)
        if result.status == "refused":
            self._untrack(cid)
            self._set_signal(item.signal_id, status="blocked", risk_reason=f"order refused: {result.message}")
            return True
        # A "rejected" result without a broker id may still have reached Alpaca (timeout, reset). The order
        # stays tracked: the next poll asks the broker and adopts any fill, so the position gets its stop.
        self._set_signal(item.signal_id, order_client_id=cid, status="submitted")
        return True

    # ------------------------------------------------------------------ step 8: bookkeeping

    def _bookkeeping(self, now: datetime) -> None:
        book = self._book()
        equity = book.bot_equity
        self._start_of_day_equity(now, equity)
        self.risk.check_drawdown(equity)
        last = self.store.kv_get(EQUITY_TS_KEY)
        if last is None or now - parse_ts(last) >= EQUITY_EVERY:
            account = self.broker.account()
            self.store.record_equity(account.equity, equity, account.cash, book.total_exposure, ts=now)
            self.store.kv_set(EQUITY_TS_KEY, to_iso(now))
        self._maybe_daily_report(now)

    def _maybe_daily_report(self, now: datetime) -> None:
        execution = self.cfg.execution
        local = now.astimezone(ZoneInfo(execution.timezone))
        hour, minute = (int(part) for part in execution.daily_report_time.split(":"))
        today = local.date().isoformat()
        if local.time() < clock_time(hour, minute) or self.store.kv_get(REPORT_DAY_KEY) == today:
            return
        self.store.kv_set(REPORT_DAY_KEY, today)  # at most once a day, even if building it fails
        self._alert(daily_report(self.store, self.settings, self.cfg, local.date()))

    def _start_of_day_equity(self, now: datetime, equity: float) -> float:
        day = ny_trading_day(now).isoformat()
        raw = self.store.kv_get(SOD_KEY)
        data = json.loads(raw) if raw else {}
        value = data.get("equity")
        if data.get("day") == day and isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
        self.store.kv_set(SOD_KEY, json.dumps({"day": day, "equity": equity}))
        return equity

    # ------------------------------------------------------------------ texts

    def status_text(self, now: datetime) -> str:
        mode = "PAPER" if self.settings.trading_mode == "paper" else "LIVE"
        account = "paper account" if self.broker.is_paper else "LIVE account"
        lines = [f"Mode: {mode} ({account})", f"Kill switch: {self._kill_text()}"]
        try:
            book: Book | None = self._book()
        except Exception as exc:
            log.warning("status: could not price positions: %s", type(exc).__name__)
            book = None
        positions = self.store.get_positions()
        if not positions:
            lines.append("Positions: none")
        for symbol, pos in positions.items():
            last = book.prices.get(symbol) if book else None
            lines.append(
                f"{symbol}: {_qty(pos.qty)} @ {_price(pos.entry_price)}, stop {_price(pos.stop_price)}, "
                f"last {_price(last)} ({pos.strategy}, {pos.bars_held} bars)"
            )
        start, _ = ny_day_bounds(ny_trading_day(now))
        lines.append(f"Realized today: {_usd(self.store.realized_pnl_since(start), signed=True)}")
        if book is not None:
            lines.append(f"Unrealized: {_usd(book.unrealized, signed=True)}")
            lines.append(f"Bot equity: {_usd(book.bot_equity)}")
        queue = load_queue(self.store)
        lines.append(f"Pending approvals: {len(self.store.pending_approvals())}; queued orders: {len(queue)}")
        return "\n".join(lines)

    def pnl_text(self, now: datetime) -> str:
        start, _ = ny_day_bounds(ny_trading_day(now))
        capital = self.cfg.risk.capital_usd
        lines = [
            f"Realized today: {_usd(self.store.realized_pnl_since(start), signed=True)}",
            f"Realized, cumulative: {_usd(self.store.realized_pnl_since(EPOCH), signed=True)}",
        ]
        try:
            book = self._book()
        except Exception as exc:
            log.warning("pnl: could not price positions: %s", type(exc).__name__)
            lines.append("Unrealized: n/a (prices unavailable)")
            return "\n".join(lines)
        lines += [
            f"Unrealized: {_usd(book.unrealized, signed=True)}",
            f"Bot equity: {_usd(book.bot_equity)} ({(book.bot_equity / capital - 1) * 100:+.2f}% on {_usd(capital)})",
        ]
        return "\n".join(lines)

    def _kill_text(self) -> str:
        if not self.kill.is_tripped():
            return "off"
        status = self.kill.status() or {}
        return f"TRIPPED ({status.get('reason')}, by {status.get('source')})"

    # ------------------------------------------------------------------ book and risk context

    def _book(self) -> Book:
        positions = self.store.get_positions()
        held = self.broker.positions() if positions else {}
        prices: dict[str, float] = {}
        exposure: dict[str, float] = {}
        unrealized = 0.0
        for symbol, pos in positions.items():
            price = _mark_price(held.get(symbol)) or self.broker.last_price(symbol)
            prices[symbol] = price
            exposure[symbol] = pos.qty * price
            # The backtest pays the entry fee at the fill, so it is already out of equity.
            unrealized += pos.qty * (price - pos.entry_price) - self._fee_rate(symbol) * pos.qty * pos.entry_price
        realized = self.store.realized_pnl_since(EPOCH)
        return Book(self.cfg.risk.capital_usd, prices, exposure, realized, unrealized)

    def _risk_context(self, book: Book, symbol: str, now: datetime, exclude: QueuedOrder | None = None) -> RiskContext:
        pending = self._pending_entry_notional(exclude)
        reset = self.store.kv_get(RESET_TS_KEY)
        return RiskContext(
            bot_equity=book.bot_equity,
            start_of_day_equity=self._start_of_day_equity(now, book.bot_equity),
            open_exposure_usd=book.total_exposure + sum(pending.values()),
            symbol_exposure_usd=book.exposure.get(symbol, 0.0) + pending.get(symbol, 0.0),
            orders_today=self.store.orders_today(now, since=parse_ts(reset) if reset else None),
            now=now,
        )

    def _pending_entry_notional(self, exclude: QueuedOrder | None) -> dict[str, float]:
        """Entries not yet in a position count toward the exposure caps."""
        pending: dict[str, float] = {}
        for q in load_queue(self.store):
            if q.kind == "entry" and not (exclude is not None and q.signal_id == exclude.signal_id):
                pending[q.symbol] = pending.get(q.symbol, 0.0) + (q.qty or 0.0) * q.signal_price
        for o in load_tracked(self.store).values():
            if o.side == Side.BUY.value:
                pending[o.symbol] = pending.get(o.symbol, 0.0) + max(0.0, o.qty - o.applied_qty) * o.ref_price
        return pending

    def _fee_rate(self, symbol: str) -> float:
        """Alpaca does not report fees per order, so P&L uses the configured fee_bps like the backtest."""
        return self.cfg.execution.fee_bps.get("crypto" if _is_crypto(symbol) else "stock", 0.0) / 10_000

    # ------------------------------------------------------------------ persisted state

    def _save_queue(self, queue: list[QueuedOrder]) -> None:
        self.store.kv_set(QUEUE_KEY, json.dumps([asdict(q) for q in queue]))

    def _upsert_queued(self, item: QueuedOrder) -> None:
        queue = [q for q in load_queue(self.store) if not (q.kind == item.kind and q.signal_id == item.signal_id)]
        self._save_queue([*queue, item])

    def _remove_queued(self, match: Callable[[QueuedOrder], bool]) -> None:
        queue = load_queue(self.store)
        kept = [q for q in queue if not match(q)]
        if len(kept) != len(queue):
            self._save_queue(kept)

    def _update_queued(self, approval_id: int, **changes: Any) -> None:
        self._save_queue([replace(q, **changes) if q.approval_id == approval_id else q for q in load_queue(self.store)])

    def _queued_for_approval(self, approval_id: int) -> QueuedOrder | None:
        return next((q for q in load_queue(self.store) if q.approval_id == approval_id), None)

    def _entry_pending(self, symbol: str) -> bool:
        if any(q.kind == "entry" and q.symbol == symbol for q in load_queue(self.store)):
            return True
        return any(o.side == Side.BUY.value and o.symbol == symbol for o in load_tracked(self.store).values())

    def _save_tracked(self, tracked: dict[str, TrackedOrder]) -> None:
        self.store.kv_set(ORDERS_KEY, json.dumps({cid: asdict(o) for cid, o in tracked.items()}))

    def _track(
        self,
        intent: OrderIntent,
        strategy: str,
        now: datetime,
        stop: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        """Recorded before the order is sent, so a crash right after sending still reconciles it."""
        tracked = load_tracked(self.store)
        if intent.client_order_id in tracked:
            return
        tracked[intent.client_order_id] = TrackedOrder(
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side.value,
            purpose=intent.purpose.value,
            strategy=strategy,
            signal_id=intent.signal_id,
            qty=intent.qty,
            ref_price=intent.ref_price,
            reason=intent.reason,
            submitted_ts=to_iso(now),
            stop=stop,
            take_profit=take_profit,
        )
        self._save_tracked(tracked)

    def _untrack(self, client_order_id: str) -> None:
        tracked = load_tracked(self.store)
        if tracked.pop(client_order_id, None) is not None:
            self._save_tracked(tracked)

    def _track_results(self, results: Iterable[OrderResult], now: datetime) -> None:
        self._track_ids([r.client_order_id for r in results if r.broker_order_id], now)

    def _track_ids(self, client_order_ids: Iterable[str], now: datetime) -> None:
        """Track orders the runner did not build itself (flatten closes), from their store rows."""
        positions = self.store.get_positions()
        for cid in client_order_ids:
            row = self.store.get_order_by_client_id(cid)
            if row is None:
                log.warning("order %s is not in the store; cannot reconcile it", cid)
                continue
            intent = OrderIntent(
                symbol=row["symbol"],
                side=Side(row["side"]),
                qty=float(row["qty"]),
                ref_price=float(row["ref_price"] or 0.0),
                purpose=OrderPurpose(row["purpose"]),
                reason=row["reason"] or "",
                client_order_id=cid,
            )
            position = positions.get(row["symbol"])
            submitted = parse_ts(row["ts"]) if row["ts"] else now
            self._track(intent, position.strategy if position else "", submitted)

    def _working(self, order: TrackedOrder) -> bool:
        row = self.store.get_order_by_client_id(order.client_order_id)
        return row is not None and row["status"] in _WORKING

    @staticmethod
    def _pending_sell(tracked: dict[str, TrackedOrder], symbol: str) -> bool:
        return any(o.side == Side.SELL.value and o.symbol == symbol for o in tracked.values())

    def _broker_holds(self, symbol: str) -> bool:
        position = self.broker.positions().get(symbol)
        return position is not None and position.qty > QTY_EPSILON

    def _entry_signal_id(self, symbol: str) -> int | None:
        raw = self.store.kv_get(entry_signal_key(symbol))
        return int(raw) if raw else None

    def _record_signal(self, signal: Signal, day: date) -> int:
        signal_id = self.store.insert_signal(signal, day)
        if signal_id is not None:
            return signal_id
        row = self.store.find_signal(signal.symbol, signal.strategy, signal.kind.value, day)
        if row is None:
            raise LookupError(f"signal {signal.symbol}/{signal.strategy}/{signal.kind.value}/{day} vanished")
        return int(row["id"])

    def _set_signal(self, signal_id: int, **values: Any) -> None:
        try:
            self.store.update_signal(signal_id, **values)
        except LookupError:
            log.warning("signal %s not found; could not record %s", signal_id, sorted(values))

    def _update_outcome(self, signal_id: int) -> None:
        trades = [t for t in self.store.trades() if t["signal_id"] == signal_id]
        pnl = sum(t["pnl"] or 0.0 for t in trades)
        basis = sum((t["entry_price"] or 0.0) * (t["qty"] or 0.0) for t in trades)
        self._set_signal(signal_id, outcome_pnl=pnl, outcome_pnl_pct=pnl / basis if basis else None)

    def _trade_recorded(self, trade: Trade) -> bool:
        """A crash between recording a fill's trade and marking the fill applied must not
        record it twice."""
        rows = self.store.trades(since=trade.exit_ts, until=trade.exit_ts + timedelta(microseconds=1))
        return any(
            r["symbol"] == trade.symbol
            and abs(r["qty"] - trade.qty) <= QTY_EPSILON
            and r["exit_price"] == trade.exit_price
            for r in rows
        )

    # ------------------------------------------------------------------ errors and alerts

    def _now(self) -> datetime:
        now = self.clock()
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    def _guarded(self, name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception as exc:
            self._report_error(name, exc, self._now())

    def _report_error(self, where: str, exc: Exception, now: datetime) -> None:
        self._errors_this_tick += 1
        text = redact(f"{type(exc).__name__}: {exc}", self.settings)[:500]
        log.error("runner step %s failed: %s", where, text, exc_info=exc)
        try:
            self.risk.after_error()
        except Exception:
            log.exception("could not count the error")
        try:
            self.store.log_event("error", "runner_error", f"{where}: {text}", ts=now)
        except Exception:
            log.exception("could not record the error event")
        self._alert(f"Bot error in {where}: {text}", dedupe_key=f"error:{where}:{text[:120]}", now=now)

    def _alert(self, text: str, dedupe_key: str | None = None, now: datetime | None = None) -> None:
        if dedupe_key is not None:
            moment = now or self._now()
            last = self._alerted.get(dedupe_key)
            if last is not None and moment - last < ALERT_DEDUPE:
                log.info("alert suppressed (repeat): %s", text)
                return
            self._alerted[dedupe_key] = moment
        try:
            self.notifier.send(text)
        except Exception:
            log.exception("notifier failed")

    def _alert_daily(self, key: str, text: str, now: datetime) -> None:
        marker = f"runner:alerted:{key}"
        day = ny_trading_day(now).isoformat()
        if self.store.kv_get(marker) != day:
            log.warning(text)
            self.store.kv_set(marker, day)
            self._alert(text)


def _same_order(item: QueuedOrder) -> Callable[[QueuedOrder], bool]:
    return lambda q: q.kind == item.kind and q.signal_id == item.signal_id


def _never_sent_result(result: OrderResult) -> bool:
    return result.status == "refused" or (result.status == "rejected" and not result.broker_order_id)
