"""SQLite persistence for the live bot.

One WAL-mode connection is shared by the runner and the dashboard's worker threads. A lock
serialises access to it, so a reader never sees another thread's half-finished transaction.
Other processes open their own `Store` and see only committed data.

Conventions:
- Timestamps are fixed-width ISO-8601 UTC strings (`2026-09-30T13:31:00.000000+00:00`), so SQL
  string comparison is chronological. Naive datetimes are taken as UTC.
- JSON columns (`signals.features`, `jev_decisions.answers`, `events.data`) are written with
  non-finite floats as null. Read helpers return plain dicts keyed by column, with `features`,
  `data` and the joined `jev_answers` parsed.
- Nothing here accepts `Settings`, so no secret can reach the database.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from bot.models import JevDecision, OrderIntent, OrderPurpose, OrderResult, PositionState, Signal, Trade
from bot.timeutil import NY, UTC, ny_trading_day, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SIGNAL_GATES = frozenset({"passed", "vetoed", "error", "shadow", "off", "n/a"})
SIGNAL_STATUSES = frozenset(
    {"new", "blocked", "awaiting_approval", "rejected", "expired", "queued", "submitted", "filled", "skipped"}
)
APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})
# Broker order states that never change again. A later, staler result must not overwrite them.
# "rejected" is final only once the broker assigned an id: without one the order never reached
# the broker, and OrderGateway may retry it under the same client_order_id.
FINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})
ERROR_LEVELS = ("error", "critical")

Timestamp = datetime | str  # a datetime, or an ISO-8601 string

_SIGNAL_UPDATABLE = frozenset(
    {
        "reason", "price", "stop_price", "take_profit", "features", "jev_decision_id", "gate",
        "risk_action", "risk_reason", "approval_id", "order_client_id", "status", "outcome_pnl",
        "outcome_pnl_pct", "counterfactual_pnl_pct",
    }
)  # fmt: skip

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
        kind TEXT NOT NULL, reason TEXT, price REAL, stop_price REAL, take_profit REAL,
        features TEXT, bar_date TEXT NOT NULL, jev_decision_id INTEGER, gate TEXT,
        risk_action TEXT, risk_reason TEXT, approval_id INTEGER, order_client_id TEXT,
        status TEXT NOT NULL DEFAULT 'new', outcome_pnl REAL, outcome_pnl_pct REAL,
        counterfactual_pnl_pct REAL,
        UNIQUE(symbol, strategy, kind, bar_date))""",
    """CREATE TABLE IF NOT EXISTS jev_decisions (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, signal_id INTEGER, symbol TEXT, model TEXT,
        latency_ms REAL, input_tokens INTEGER, cost_usd REAL, passed INTEGER, shadow INTEGER,
        error TEXT, answers TEXT)""",
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE, broker_order_id TEXT,
        ts TEXT NOT NULL, symbol TEXT, side TEXT, qty REAL, ref_price REAL, notional REAL,
        purpose TEXT, reason TEXT, status TEXT, filled_qty REAL, filled_avg_price REAL,
        signal_id INTEGER, message TEXT)""",
    """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, symbol TEXT, strategy TEXT, entry_ts TEXT, entry_price REAL,
        exit_ts TEXT, exit_price REAL, qty REAL, pnl REAL, pnl_pct REAL, exit_reason TEXT,
        fees REAL, signal_id INTEGER)""",
    "CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, state TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY, signal_id INTEGER, ts_requested TEXT NOT NULL, notional REAL,
        status TEXT NOT NULL, decided_ts TEXT, message_id INTEGER, expires_ts TEXT)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT, kind TEXT, message TEXT, data TEXT)""",
    """CREATE TABLE IF NOT EXISTS equity (
        ts TEXT PRIMARY KEY, account_equity REAL, bot_equity REAL, cash REAL, exposure REAL)""",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)",
    "CREATE INDEX IF NOT EXISTS idx_jev_ts ON jev_decisions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_orders_purpose_ts ON orders(purpose, ts)",
    "CREATE INDEX IF NOT EXISTS idx_trades_exit_ts ON trades(exit_ts)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
)

_SIGNAL_JEV_SELECT = """
    SELECT s.*, j.model AS jev_model, j.latency_ms AS jev_latency_ms,
           j.input_tokens AS jev_input_tokens, j.cost_usd AS jev_cost_usd,
           j.passed AS jev_passed, j.shadow AS jev_shadow, j.error AS jev_error,
           j.answers AS jev_answers, a.status AS approval_status
    FROM signals s
    LEFT JOIN jev_decisions j ON j.id = s.jev_decision_id
    LEFT JOIN approvals a ON a.id = s.approval_id
"""

# A decision row where Jev was actually called. Uncalled decisions (mode off, exits) have neither
# a model nor an error and are left out of the Jev statistics.
_JEV_CALLED = "(model IS NOT NULL OR error IS NOT NULL)"
# Jev said no. A shadow decision may carry passed=1 (it did not gate), so its verdict is read from
# the answers, as JevGate computes it: at least one answer and every answer passing.
_JEV_SAID_NO = """(passed = 0 OR COALESCE(json_array_length(answers), 0) = 0
    OR EXISTS (SELECT 1 FROM json_each(answers) WHERE json_extract(value, '$.passed') = 0))"""

_JSON_COLUMNS = ("features", "jev_answers", "data")
_BOOL_COLUMNS = ("jev_passed", "jev_shadow")


# --------------------------------------------------------------------------- value helpers


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def to_iso(ts: Timestamp) -> str:
    """Fixed-width ISO-8601 UTC string for a datetime or an ISO string."""
    dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    return _aware(dt).astimezone(UTC).isoformat(timespec="microseconds")


def parse_ts(value: str) -> datetime:
    """Inverse of `to_iso`: an aware UTC datetime."""
    return _aware(datetime.fromisoformat(value)).astimezone(UTC)


def ny_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of the New York calendar day `day`, in UTC. DST days are 23 or 25 hours."""
    start = datetime.combine(day, time(0), tzinfo=NY)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=NY)
    return start.astimezone(UTC), end.astimezone(UTC)


def _bar_date(value: date | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), default=str)


def _loads(text: str | None, column: str) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("unreadable JSON in column %s; returning None", column)
        return None


def _sql(value: Any) -> Any:
    """A value SQLite can bind: enums by value, NaN/inf as NULL, datetimes as ISO UTC."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (Mapping, list, tuple)):
        return _dumps(value)
    return value


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in _JSON_COLUMNS:
        if key in out:
            out[key] = _loads(out[key], key)
    for key in _BOOL_COLUMNS:
        if out.get(key) is not None:
            out[key] = bool(out[key])
    return out


def _window(column: str, since: Timestamp | None, until: Timestamp | None) -> tuple[str, list[str]]:
    """SQL condition for since <= column < until (either bound optional). `column` is trusted."""
    clauses: list[str] = []
    params: list[str] = []
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(to_iso(since))
    if until is not None:
        clauses.append(f"{column} < ?")
        params.append(to_iso(until))
    return " AND ".join(clauses) or "1 = 1", params


def _is_final(order: sqlite3.Row) -> bool:
    status = order["status"]
    return status in FINAL_ORDER_STATUSES and (status != "rejected" or bool(order["broker_order_id"]))


def _position_to_json(state: PositionState) -> str:
    for name in ("qty", "entry_price", "stop_price"):
        value = getattr(state, name)
        if value is None or not math.isfinite(value):
            raise ValueError(f"position {state.symbol}: {name} must be a finite number, got {value!r}")
    if state.qty <= 0:
        raise ValueError(f"position {state.symbol}: qty must be positive, got {state.qty}")
    return _dumps(asdict(state))


def _position_from_json(text: str) -> PositionState:
    known = {f.name for f in fields(PositionState)}
    data = {k: v for k, v in json.loads(text).items() if k in known}
    data["entry_ts"] = parse_ts(data["entry_ts"])
    return PositionState(**data)


# --------------------------------------------------------------------------- store


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], datetime] = utcnow) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        # RLock so a helper that writes can be called while the same thread holds the lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def now(self) -> datetime:
        return _aware(self._clock())

    # ----------------------------------------------------------------------- plumbing

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction under the lock; rolled back on any error."""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _rows(self, sql: str, params: Sequence[Any] = (), decode: bool = False) -> list[dict[str, Any]]:
        """Rows as dicts; `decode` parses the JSON columns and turns jev_passed/jev_shadow into bools."""
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode(row) if decode else dict(row) for row in rows]

    def _one(self, sql: str, params: Sequence[Any] = (), decode: bool = False) -> dict[str, Any] | None:
        rows = self._rows(sql, params, decode)
        return rows[0] if rows else None

    def _ts(self, ts: Timestamp | None) -> str:
        return to_iso(ts if ts is not None else self.now())

    def _create_schema(self) -> None:
        with self._write() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            row = conn.execute("SELECT value FROM kv WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO kv (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has schema v{row['value']}, newer than this code (v{SCHEMA_VERSION})"
                )

    # ----------------------------------------------------------------------- signals

    def insert_signal(self, signal: Signal, bar_date: date | str) -> int | None:
        """New signal with status 'new'. None if (symbol, strategy, kind, bar_date) exists."""
        with self._write() as conn:
            rows = conn.execute(
                """INSERT INTO signals (ts, symbol, strategy, kind, reason, price, stop_price,
                                        take_profit, features, bar_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                   ON CONFLICT(symbol, strategy, kind, bar_date) DO NOTHING
                   RETURNING id""",
                (
                    to_iso(signal.ts), signal.symbol, signal.strategy, _sql(signal.kind), signal.reason,
                    _sql(signal.price), _sql(signal.stop_price), _sql(signal.take_profit),
                    _dumps(signal.features), _bar_date(bar_date),
                ),
            ).fetchall()
        return int(rows[0]["id"]) if rows else None

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        unknown = set(fields) - _SIGNAL_UPDATABLE
        if unknown:
            raise ValueError(f"update_signal: unknown or read-only fields {sorted(unknown)}")
        values = {name: _sql(value) for name, value in fields.items()}
        if "gate" in values and values["gate"] not in SIGNAL_GATES:
            raise ValueError(f"update_signal: invalid gate {values['gate']!r}")
        if "status" in values and values["status"] not in SIGNAL_STATUSES:
            raise ValueError(f"update_signal: invalid status {values['status']!r}")
        if not values:
            return
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._write() as conn:
            cursor = conn.execute(f"UPDATE signals SET {assignments} WHERE id = ?", (*values.values(), signal_id))
            if cursor.rowcount == 0:
                raise LookupError(f"signal {signal_id} not found")

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM signals WHERE id = ?", (signal_id,), decode=True)

    def find_signal(self, symbol: str, strategy: str, kind: str, bar_date: date | str) -> dict[str, Any] | None:
        """The signal row behind a duplicate `insert_signal`, e.g. to resume after a restart."""
        return self._one(
            "SELECT * FROM signals WHERE symbol = ? AND strategy = ? AND kind = ? AND bar_date = ?",
            (symbol, strategy, _sql(kind), _bar_date(bar_date)),
            decode=True,
        )

    def signals(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Newest first by signal ts."""
        return self._rows(
            "SELECT * FROM signals ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (max(0, int(limit)), max(0, int(offset))),
            decode=True,
        )

    def signal_with_jev(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Signals newest first, each with its Jev decision (`jev_*` keys, `jev_answers` parsed)
        and `approval_status`. The `jev_*` keys are None when Jev was not consulted."""
        return self._rows(
            _SIGNAL_JEV_SELECT + " ORDER BY s.ts DESC, s.id DESC LIMIT ? OFFSET ?",
            (max(0, int(limit)), max(0, int(offset))),
            decode=True,
        )

    def vetoed_signals_with_counterfactual(self) -> list[dict[str, Any]]:
        """Signals with gate 'vetoed', newest first, shaped like `signal_with_jev`.
        `counterfactual_pnl_pct` is None until the runner has computed it."""
        return self._rows(
            _SIGNAL_JEV_SELECT + " WHERE s.gate = 'vetoed' ORDER BY s.ts DESC, s.id DESC", decode=True
        )

    # ----------------------------------------------------------------------- Jev

    def insert_jev(
        self, decision: JevDecision, signal_id: int | None, symbol: str, ts: Timestamp | None = None
    ) -> int:
        """Record a decision and point `signals.jev_decision_id` at it."""
        with self._write() as conn:
            (row,) = conn.execute(
                """INSERT INTO jev_decisions (ts, signal_id, symbol, model, latency_ms, input_tokens,
                                              cost_usd, passed, shadow, error, answers)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    self._ts(ts), signal_id, symbol, decision.model, _sql(decision.latency_ms),
                    int(decision.input_tokens), _sql(decision.cost_usd), int(decision.passed),
                    int(decision.shadow), decision.error, _dumps([asdict(a) for a in decision.answers]),
                ),
            ).fetchall()
            decision_id = int(row["id"])
            if signal_id is not None:
                conn.execute("UPDATE signals SET jev_decision_id = ? WHERE id = ?", (decision_id, signal_id))
        return decision_id

    def jev_stats(self, since: Timestamp | None = None, until: Timestamp | None = None) -> dict[str, Any]:
        """Over decisions where Jev was called. Averages are 0.0 when n is 0."""
        where, params = _window("ts", since, until)
        row = self._one(
            f"""SELECT COUNT(*) AS n, AVG(latency_ms) AS avg_latency_ms, AVG(cost_usd) AS avg_cost_usd,
                       TOTAL(cost_usd) AS total_cost_usd
                FROM jev_decisions WHERE {where} AND {_JEV_CALLED}""",
            params,
        )
        assert row is not None
        return {
            "n": int(row["n"]),
            "avg_latency_ms": float(row["avg_latency_ms"] or 0.0),
            "avg_cost_usd": float(row["avg_cost_usd"] or 0.0),
            "total_cost_usd": float(row["total_cost_usd"]),
        }

    def jev_outcome_counts(self, since: Timestamp | None = None, until: Timestamp | None = None) -> dict[str, int]:
        """Called decisions by outcome: passed, vetoed (enforced), shadow_vetoed (Jev said no in
        shadow mode, not enforced) and errors."""
        where, params = _window("ts", since, until)
        row = self._one(
            f"""SELECT
                  COUNT(CASE WHEN error IS NULL AND NOT {_JEV_SAID_NO} THEN 1 END) AS passed,
                  COUNT(CASE WHEN error IS NULL AND shadow = 0 AND passed = 0 THEN 1 END) AS vetoed,
                  COUNT(CASE WHEN error IS NULL AND shadow = 1 AND {_JEV_SAID_NO} THEN 1 END) AS shadow_vetoed,
                  COUNT(CASE WHEN error IS NOT NULL THEN 1 END) AS errors
                FROM jev_decisions WHERE {where} AND {_JEV_CALLED}""",
            params,
        )
        assert row is not None
        return {key: int(value) for key, value in row.items()}

    # ----------------------------------------------------------------------- orders and trades

    def upsert_order(self, intent: OrderIntent, result: OrderResult, ts: Timestamp | None = None) -> int:
        """Insert or update by client_order_id; returns the row id.

        `ts` (default: now) is set on insert only, so `orders_today` counts an order on the day
        it was created. A result that arrives after a final status (see FINAL_ORDER_STATUSES), or
        reports less filled quantity than already recorded, is stale: only broker_order_id and
        message are merged.
        """
        cid = intent.client_order_id
        filled_qty = result.filled_qty or 0.0
        if result.client_order_id != cid:
            raise ValueError(f"order result {result.client_order_id!r} does not match intent {cid!r}")
        with self._write() as conn:
            existing = conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (cid,)).fetchone()
            if existing is None:
                (row,) = conn.execute(
                    """INSERT INTO orders (client_order_id, broker_order_id, ts, symbol, side, qty,
                                           ref_price, notional, purpose, reason, status, filled_qty,
                                           filled_avg_price, signal_id, message)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                    (
                        cid, result.broker_order_id, self._ts(ts), intent.symbol, _sql(intent.side),
                        _sql(intent.qty), _sql(intent.ref_price), _sql(intent.notional),
                        _sql(intent.purpose), intent.reason, result.status, _sql(filled_qty),
                        _sql(result.filled_avg_price), intent.signal_id, result.message,
                    ),
                ).fetchall()
                return int(row["id"])
            order_id = int(existing["id"])
            message = result.message or existing["message"]
            if _is_final(existing) or filled_qty < (existing["filled_qty"] or 0.0):
                if result.status != existing["status"] or filled_qty != existing["filled_qty"]:
                    log.warning(
                        "ignoring stale update for order %s: %s/%s after %s/%s", cid, result.status,
                        filled_qty, existing["status"], existing["filled_qty"],
                    )
                conn.execute(
                    "UPDATE orders SET broker_order_id = COALESCE(broker_order_id, ?), message = ? WHERE id = ?",
                    (result.broker_order_id, message, order_id),
                )
                return order_id
            conn.execute(
                """UPDATE orders SET broker_order_id = COALESCE(?, broker_order_id), qty = ?,
                          ref_price = ?, notional = ?, reason = ?, status = ?, filled_qty = ?,
                          filled_avg_price = COALESCE(?, filled_avg_price),
                          signal_id = COALESCE(?, signal_id), message = ?
                   WHERE id = ?""",
                (
                    result.broker_order_id, _sql(intent.qty), _sql(intent.ref_price), _sql(intent.notional),
                    intent.reason, result.status, _sql(filled_qty), _sql(result.filled_avg_price),
                    intent.signal_id, message, order_id,
                ),
            )
            return order_id

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))

    def orders_today(self, now: datetime, since: datetime | None = None) -> int:
        """ENTRY orders created on the New York calendar day that contains `now`, counted from
        `since` when that is later (the runner passes the last kill-switch reset, so a same-day
        resume starts a fresh count instead of re-tripping the order cap)."""
        start, end = ny_day_bounds(ny_trading_day(_aware(now)))
        if since is not None:
            start = max(start, _aware(since))
        row = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE purpose = ? AND ts >= ? AND ts < ?",
            (OrderPurpose.ENTRY.value, to_iso(start), to_iso(end)),
        )
        assert row is not None
        return int(row["n"])

    def insert_trade(self, trade: Trade, signal_id: int | None = None) -> int:
        """`trade.meta` is not stored."""
        with self._write() as conn:
            (row,) = conn.execute(
                """INSERT INTO trades (symbol, strategy, entry_ts, entry_price, exit_ts, exit_price,
                                       qty, pnl, pnl_pct, exit_reason, fees, signal_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    trade.symbol, trade.strategy, to_iso(trade.entry_ts), _sql(trade.entry_price),
                    to_iso(trade.exit_ts), _sql(trade.exit_price), _sql(trade.qty), _sql(trade.pnl),
                    _sql(trade.pnl_pct), trade.exit_reason, _sql(trade.fees), signal_id,
                ),
            ).fetchall()
        return int(row["id"])

    def trades(self, since: Timestamp | None = None, until: Timestamp | None = None) -> list[dict[str, Any]]:
        """Closed trades with since <= exit_ts < until, oldest exit first."""
        where, params = _window("exit_ts", since, until)
        return self._rows(f"SELECT * FROM trades WHERE {where} ORDER BY exit_ts, id", params)

    def realized_pnl_since(self, ts: Timestamp) -> float:
        where, params = _window("exit_ts", ts, None)
        row = self._one(f"SELECT TOTAL(pnl) AS pnl FROM trades WHERE {where}", params)
        assert row is not None
        return float(row["pnl"])

    # ----------------------------------------------------------------------- positions

    def get_positions(self) -> dict[str, PositionState]:
        """Raises on a corrupt row: forgetting a position would leave it unmanaged."""
        with self._lock:
            rows = self._conn.execute("SELECT symbol, state FROM positions ORDER BY symbol").fetchall()
        return {row["symbol"]: _position_from_json(row["state"]) for row in rows}

    def put_position(self, state: PositionState) -> None:
        """Insert or replace. Rejects a non-finite qty, entry or stop, or qty <= 0."""
        payload = _position_to_json(state)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO positions (symbol, state) VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET state = excluded.state",
                (state.symbol, payload),
            )

    def delete_position(self, symbol: str) -> bool:
        with self._write() as conn:
            return conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,)).rowcount > 0

    def open_positions(self) -> list[dict[str, Any]]:
        """`get_positions()` as dicts (PositionState fields, entry_ts a datetime), by symbol."""
        return [asdict(state) for state in self.get_positions().values()]

    # ----------------------------------------------------------------------- approvals

    def create_approval(
        self, signal_id: int, notional: float, expires_ts: Timestamp, now: Timestamp | None = None
    ) -> int:
        with self._write() as conn:
            (row,) = conn.execute(
                """INSERT INTO approvals (signal_id, ts_requested, notional, status, expires_ts)
                   VALUES (?, ?, ?, 'pending', ?) RETURNING id""",
                (signal_id, self._ts(now), _sql(notional), to_iso(expires_ts)),
            ).fetchall()
        return int(row["id"])

    def set_approval(self, approval_id: int, status: str, now: Timestamp | None = None) -> bool:
        """Decide a pending approval. Returns False (and changes nothing) if it was already
        decided or does not exist, so a late click can never approve an expired request."""
        if status not in APPROVAL_STATUSES - {"pending"}:
            raise ValueError(f"set_approval: invalid status {status!r}")
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = ?, decided_ts = ? WHERE id = ? AND status = 'pending'",
                (status, self._ts(now), approval_id),
            )
            return cursor.rowcount == 1

    def set_approval_message_id(self, approval_id: int, message_id: int | None) -> None:
        with self._write() as conn:
            conn.execute("UPDATE approvals SET message_id = ? WHERE id = ?", (message_id, approval_id))

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def approvals_by_status(self, status: str) -> list[dict[str, Any]]:
        """Oldest first."""
        if status not in APPROVAL_STATUSES:
            raise ValueError(f"approvals_by_status: invalid status {status!r}")
        return self._rows("SELECT * FROM approvals WHERE status = ? ORDER BY id", (status,))

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.approvals_by_status("pending")

    def approval_counts(self, since: Timestamp | None = None, until: Timestamp | None = None) -> dict[str, int]:
        """Approvals requested in [since, until), by current status (every status present)."""
        where, params = _window("ts_requested", since, until)
        rows = self._rows(f"SELECT status, COUNT(*) AS n FROM approvals WHERE {where} GROUP BY status", params)
        counts = dict.fromkeys(sorted(APPROVAL_STATUSES), 0)
        counts.update({row["status"]: int(row["n"]) for row in rows})
        return counts

    # ----------------------------------------------------------------------- events, equity, kv

    def log_event(
        self, level: str, kind: str, message: str, data: Any = None, ts: Timestamp | None = None
    ) -> int:
        """`level` is stored lower-case; `data` as JSON."""
        with self._write() as conn:
            (row,) = conn.execute(
                "INSERT INTO events (ts, level, kind, message, data) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (self._ts(ts), level.lower(), kind, message, None if data is None else _dumps(data)),
            ).fetchall()
        return int(row["id"])

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first, `data` parsed."""
        return self._rows(
            "SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (max(0, int(limit)),), decode=True
        )

    def count_events(
        self,
        since: Timestamp | None = None,
        until: Timestamp | None = None,
        levels: Sequence[str] = ERROR_LEVELS,
    ) -> int:
        where, params = _window("ts", since, until)
        marks = ", ".join("?" for _ in levels) or "NULL"
        row = self._one(
            f"SELECT COUNT(*) AS n FROM events WHERE {where} AND level IN ({marks})",
            [*params, *(level.lower() for level in levels)],
        )
        assert row is not None
        return int(row["n"])

    def record_equity(
        self,
        account_equity: float,
        bot_equity: float,
        cash: float,
        exposure: float,
        ts: Timestamp | None = None,
    ) -> None:
        """One snapshot per ts (replaced if the same ts is recorded twice)."""
        with self._write() as conn:
            conn.execute(
                """INSERT INTO equity (ts, account_equity, bot_equity, cash, exposure)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ts) DO UPDATE SET account_equity = excluded.account_equity,
                       bot_equity = excluded.bot_equity, cash = excluded.cash, exposure = excluded.exposure""",
                (self._ts(ts), _sql(account_equity), _sql(bot_equity), _sql(cash), _sql(exposure)),
            )

    def equity_series(self, since: Timestamp | None = None) -> list[dict[str, Any]]:
        """Oldest first."""
        where, params = _window("ts", since, None)
        return self._rows(f"SELECT * FROM equity WHERE {where} ORDER BY ts", params)

    def latest_equity(self, before: Timestamp | None = None) -> dict[str, Any] | None:
        """The newest snapshot with ts < before (default: the newest overall)."""
        where, params = _window("ts", None, before)
        return self._one(f"SELECT * FROM equity WHERE {where} ORDER BY ts DESC LIMIT 1", params)

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def kv_set(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
