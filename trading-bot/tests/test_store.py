import math
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from bot.models import (
    JevAnswer,
    JevDecision,
    OrderIntent,
    OrderPurpose,
    OrderResult,
    PositionState,
    Side,
    Signal,
    SignalKind,
    Trade,
)
from bot.store import SCHEMA_VERSION, Store, ny_day_bounds, parse_ts, to_iso

UTC = timezone.utc
NOW = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "bot.sqlite3", clock=lambda: NOW) as s:
        yield s


def make_signal(symbol="SPY", kind=SignalKind.ENTRY, ts=utc(2026, 9, 28, 20), **kw) -> Signal:
    return Signal(
        ts=ts, symbol=symbol, strategy="trend", kind=kind, reason="close crossed above SMA",
        price=500.0, stop_price=480.0, features={"sma_fast": 495.5, "atr": 5.25}, **kw,
    )  # fmt: skip


def make_intent(cid="entry-SPY-abc", purpose=OrderPurpose.ENTRY, qty=5.0, signal_id=None) -> OrderIntent:
    return OrderIntent(
        symbol="SPY", side=Side.BUY, qty=qty, ref_price=500.0, purpose=purpose, reason="trend entry",
        client_order_id=cid, signal_id=signal_id,
    )  # fmt: skip


def make_decision(
    latency_ms=100.0, cost=0.001, passed=True, shadow=False, error=None, answer_passed=None, model="jev-latest"
) -> JevDecision:
    answer = JevAnswer(
        name="buying_pressure", type="noul", probabilities={"yes": 0.6, "no": 0.4}, top="yes",
        gate_value=0.6, passed=passed if answer_passed is None else answer_passed, rule="P(yes) >= 0.45",
    )  # fmt: skip
    return JevDecision(
        passed=passed, answers=(answer,), model=model, latency_ms=latency_ms, input_tokens=1200,
        cost_usd=cost, error=error, shadow=shadow,
    )  # fmt: skip


UNCALLED = JevDecision(passed=True, answers=(), model=None, latency_ms=0.0, input_tokens=0, cost_usd=0.0)


def make_trade(exit_ts, pnl, pnl_pct=0.01, symbol="SPY") -> Trade:
    return Trade(
        symbol=symbol, strategy="trend", entry_ts=exit_ts - timedelta(days=3), entry_price=500.0,
        exit_ts=exit_ts, exit_price=505.0, qty=2.0, pnl=pnl, pnl_pct=pnl_pct, exit_reason="signal",
    )  # fmt: skip


# --------------------------------------------------------------------------- schema


def test_schema_is_idempotent_and_persistent(tmp_path):
    path = tmp_path / "nested" / "bot.sqlite3"
    with Store(path) as first:
        first.kv_set("last_bar:SPY", "2026-09-28")
        signal_id = first.insert_signal(make_signal(), date(2026, 9, 28))
    with Store(path) as second:
        assert second.kv_get("last_bar:SPY") == "2026-09-28"
        assert second.kv_get("schema_version") == str(SCHEMA_VERSION)
        assert second.get_signal(signal_id)["symbol"] == "SPY"
        with second._lock:
            mode = second._conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {r[0] for r in second._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert mode == "wal"
    assert tables >= {"signals", "jev_decisions", "orders", "trades", "positions", "approvals", "events", "equity", "kv"}


def test_refuses_database_from_newer_code(tmp_path):
    path = tmp_path / "bot.sqlite3"
    with Store(path) as s:
        s.kv_set("schema_version", str(SCHEMA_VERSION + 1))
    with pytest.raises(RuntimeError, match="newer than this code"):
        Store(path)


def test_timestamps_are_fixed_width_utc():
    assert to_iso(utc(2026, 9, 30, 13, 31)) == "2026-09-30T13:31:00.000000+00:00"
    assert to_iso(datetime(2026, 9, 30, 13, 31)) == "2026-09-30T13:31:00.000000+00:00"  # naive = UTC
    assert to_iso("2026-09-30T09:31:00-04:00") == "2026-09-30T13:31:00.000000+00:00"
    assert parse_ts(to_iso(utc(2026, 1, 2, 3, 4, 5, 6))) == utc(2026, 1, 2, 3, 4, 5, 6)


# --------------------------------------------------------------------------- signals


def test_duplicate_signal_returns_none(store):
    first = store.insert_signal(make_signal(), date(2026, 9, 28))
    assert isinstance(first, int)
    assert store.insert_signal(make_signal(), "2026-09-28") is None
    assert store.insert_signal(make_signal(kind=SignalKind.EXIT), date(2026, 9, 28)) not in (None, first)
    assert store.insert_signal(make_signal(), date(2026, 9, 29)) not in (None, first)
    assert len(store.signals(limit=10)) == 3
    assert store.find_signal("SPY", "trend", SignalKind.ENTRY, date(2026, 9, 28))["id"] == first


def test_signal_round_trip_and_update(store):
    signal = make_signal()
    signal.features["nan_during_warmup"] = math.nan
    signal_id = store.insert_signal(signal, date(2026, 9, 28))
    row = store.get_signal(signal_id)
    assert row["kind"] == "entry" and row["status"] == "new" and row["bar_date"] == "2026-09-28"
    assert row["features"] == {"sma_fast": 495.5, "atr": 5.25, "nan_during_warmup": None}
    assert parse_ts(row["ts"]) == signal.ts

    store.update_signal(signal_id, gate="vetoed", status="blocked", risk_reason="jev veto", outcome_pnl=math.inf)
    row = store.get_signal(signal_id)
    assert (row["gate"], row["status"], row["risk_reason"], row["outcome_pnl"]) == ("vetoed", "blocked", "jev veto", None)


@pytest.mark.parametrize(
    "fields, message",
    [({"symbol": "QQQ"}, "unknown or read-only"), ({"gate": "veto"}, "invalid gate"), ({"status": "done"}, "invalid status")],
)
def test_update_signal_rejects_bad_fields(store, fields, message):
    signal_id = store.insert_signal(make_signal(), date(2026, 9, 28))
    with pytest.raises(ValueError, match=message):
        store.update_signal(signal_id, **fields)


def test_update_missing_signal_raises(store):
    with pytest.raises(LookupError):
        store.update_signal(999, status="blocked")


def test_signals_newest_first_with_paging(store):
    ids = [store.insert_signal(make_signal(ts=utc(2026, 9, d, 20)), date(2026, 9, d)) for d in (21, 22, 23, 24)]
    assert [r["id"] for r in store.signals(limit=2)] == [ids[3], ids[2]]
    assert [r["id"] for r in store.signals(limit=2, offset=2)] == [ids[1], ids[0]]
    assert store.signals(limit=-1) == []


# --------------------------------------------------------------------------- Jev


def test_jev_insert_links_signal_and_joins(store):
    signal_id = store.insert_signal(make_signal(), date(2026, 9, 28))
    other_id = store.insert_signal(make_signal(symbol="QQQ"), date(2026, 9, 28))
    decision_id = store.insert_jev(make_decision(passed=False), signal_id, "SPY")
    store.update_signal(signal_id, gate="vetoed", status="blocked", counterfactual_pnl_pct=-0.02)

    rows = {r["id"]: r for r in store.signal_with_jev(limit=10)}
    spy, qqq = rows[signal_id], rows[other_id]
    assert spy["jev_decision_id"] == decision_id
    assert spy["jev_passed"] is False and spy["jev_shadow"] is False and spy["jev_model"] == "jev-latest"
    assert spy["jev_answers"][0]["probabilities"] == {"yes": 0.6, "no": 0.4}
    assert spy["jev_answers"][0]["rule"] == "P(yes) >= 0.45"
    assert qqq["jev_passed"] is None and qqq["jev_answers"] is None

    vetoed = store.vetoed_signals_with_counterfactual()
    assert [r["id"] for r in vetoed] == [signal_id]
    assert vetoed[0]["counterfactual_pnl_pct"] == -0.02


def test_jev_stats_math(store):
    assert store.jev_stats() == {"n": 0, "avg_latency_ms": 0.0, "avg_cost_usd": 0.0, "total_cost_usd": 0.0}
    store.insert_jev(make_decision(latency_ms=1000.0, cost=0.009), None, "SPY", ts=utc(2026, 9, 27, 20))
    store.insert_jev(UNCALLED, None, "SPY", ts=utc(2026, 9, 29, 20))  # mode off: not a Jev call
    for latency, cost in ((100.0, 0.001), (200.0, 0.002), (600.0, 0.003)):
        store.insert_jev(make_decision(latency_ms=latency, cost=cost), None, "SPY", ts=utc(2026, 9, 29, 20))

    recent = store.jev_stats(since=utc(2026, 9, 29))
    assert recent["n"] == 3
    assert recent["avg_latency_ms"] == pytest.approx(300.0)
    assert recent["avg_cost_usd"] == pytest.approx(0.002)
    assert recent["total_cost_usd"] == pytest.approx(0.006)

    everything = store.jev_stats()
    assert everything["n"] == 4
    assert everything["avg_latency_ms"] == pytest.approx(475.0)
    assert everything["total_cost_usd"] == pytest.approx(0.015)
    assert store.jev_stats(until=utc(2026, 9, 29))["n"] == 1


def test_jev_outcome_counts(store):
    store.insert_jev(make_decision(passed=True), None, "SPY")
    store.insert_jev(make_decision(passed=True, shadow=True), None, "SPY")
    store.insert_jev(make_decision(passed=False), None, "SPY")
    store.insert_jev(make_decision(passed=False, shadow=True), None, "SPY")
    # JevGate records shadow decisions as passed; the failing answer still makes it a would-be veto.
    store.insert_jev(make_decision(passed=True, shadow=True, answer_passed=False), None, "SPY")
    store.insert_jev(make_decision(passed=False, error="timeout", model=None), None, "SPY")
    store.insert_jev(make_decision(passed=True, shadow=True, error="timeout"), None, "SPY")
    store.insert_jev(UNCALLED, None, "SPY")
    assert store.jev_outcome_counts() == {"passed": 2, "vetoed": 1, "shadow_vetoed": 2, "errors": 2}


# --------------------------------------------------------------------------- orders


def test_upsert_order_inserts_then_updates(store):
    intent = make_intent()
    first_id = store.upsert_order(intent, OrderResult(intent.client_order_id, "b-1", "accepted"), ts=utc(2026, 9, 29, 13, 31))
    later = utc(2026, 9, 30, 15)
    partial = OrderResult(intent.client_order_id, None, "partially_filled", filled_qty=2.0, filled_avg_price=500.5)
    assert store.upsert_order(intent, partial, ts=later) == first_id
    filled = OrderResult(intent.client_order_id, "b-1", "filled", filled_qty=5.0, filled_avg_price=500.8, message="ok")
    assert store.upsert_order(intent, filled, ts=later) == first_id

    row = store.get_order_by_client_id(intent.client_order_id)
    assert row["status"] == "filled" and row["filled_qty"] == 5.0 and row["filled_avg_price"] == 500.8
    assert row["broker_order_id"] == "b-1" and row["message"] == "ok"
    assert row["purpose"] == "entry" and row["side"] == "buy" and row["notional"] == pytest.approx(2500.0)
    assert row["ts"] == to_iso(utc(2026, 9, 29, 13, 31))  # creation time is kept
    with store._lock:
        assert store._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_stale_order_result_does_not_regress(store):
    intent = make_intent()
    store.upsert_order(intent, OrderResult(intent.client_order_id, "b-1", "filled", 5.0, 500.8))
    store.upsert_order(intent, OrderResult(intent.client_order_id, "b-1", "accepted", 0.0, None, "late echo"))
    row = store.get_order_by_client_id(intent.client_order_id)
    assert (row["status"], row["filled_qty"], row["filled_avg_price"]) == ("filled", 5.0, 500.8)
    assert row["message"] == "late echo"


def test_refused_order_can_be_submitted_later(store):
    intent = make_intent()
    store.upsert_order(intent, OrderResult(intent.client_order_id, None, "refused", message="kill switch"))
    store.upsert_order(intent, OrderResult(intent.client_order_id, "b-2", "accepted"))
    assert store.get_order_by_client_id(intent.client_order_id)["status"] == "accepted"


def test_rejected_before_reaching_broker_can_be_retried(store):
    intent = make_intent()
    store.upsert_order(intent, OrderResult(intent.client_order_id, None, "rejected", message="submit timed out"))
    store.upsert_order(intent, OrderResult(intent.client_order_id, "b-3", "filled", 5.0, 501.0))
    row = store.get_order_by_client_id(intent.client_order_id)
    assert (row["status"], row["broker_order_id"], row["filled_qty"]) == ("filled", "b-3", 5.0)


def test_broker_rejection_is_final(store):
    intent = make_intent()
    store.upsert_order(intent, OrderResult(intent.client_order_id, "b-4", "rejected", message="insufficient bp"))
    store.upsert_order(intent, OrderResult(intent.client_order_id, "b-4", "accepted"))
    assert store.get_order_by_client_id(intent.client_order_id)["status"] == "rejected"


def test_upsert_order_rejects_mismatched_result(store):
    with pytest.raises(ValueError, match="does not match"):
        store.upsert_order(make_intent(), OrderResult("other-id", None, "accepted"))


def test_orders_today_uses_new_york_day(store):
    entries = {
        "e-prev-day": utc(2026, 9, 29, 3, 59),  # 23:59 NY on Sep 28
        "e-morning": utc(2026, 9, 29, 13, 31),  # 09:31 NY on Sep 29
        "e-late": utc(2026, 9, 30, 3, 30),  # 23:30 NY on Sep 29
        "e-next-day": utc(2026, 9, 30, 4, 0),  # 00:00 NY on Sep 30
    }
    for cid, ts in entries.items():
        store.upsert_order(make_intent(cid), OrderResult(cid, None, "accepted"), ts=ts)
    store.upsert_order(make_intent("x-exit", OrderPurpose.EXIT), OrderResult("x-exit", None, "filled"), ts=utc(2026, 9, 29, 15))
    store.upsert_order(make_intent("x-stop", OrderPurpose.STOP), OrderResult("x-stop", None, "filled"), ts=utc(2026, 9, 29, 16))

    # 03:30 UTC on Sep 30 is still Sep 29 in New York.
    assert store.orders_today(utc(2026, 9, 30, 3, 30)) == 2
    assert store.orders_today(utc(2026, 9, 29, 13, 0)) == 2
    assert store.orders_today(utc(2026, 9, 29, 3, 0)) == 1
    assert store.orders_today(utc(2026, 9, 30, 4, 0)) == 1


def test_orders_today_winter_and_dst_end(store):
    # EST: the NY day starts at 05:00 UTC.
    store.upsert_order(make_intent("w1"), OrderResult("w1", None, "accepted"), ts=utc(2026, 1, 15, 4, 59))
    store.upsert_order(make_intent("w2"), OrderResult("w2", None, "accepted"), ts=utc(2026, 1, 15, 5, 0))
    assert store.orders_today(utc(2026, 1, 15, 12)) == 1
    assert store.orders_today(utc(2026, 1, 15, 4, 0)) == 1  # still Jan 14 in NY
    # Nov 1 2026 (DST ends) is a 25-hour NY day: 04:00 UTC Nov 1 to 05:00 UTC Nov 2.
    assert ny_day_bounds(date(2026, 11, 1)) == (utc(2026, 11, 1, 4), utc(2026, 11, 2, 5))
    store.upsert_order(make_intent("d1"), OrderResult("d1", None, "accepted"), ts=utc(2026, 11, 2, 4, 30))
    assert store.orders_today(utc(2026, 11, 1, 12)) == 1


# --------------------------------------------------------------------------- trades and P&L


def test_trades_and_realized_pnl(store):
    store.insert_trade(make_trade(utc(2026, 9, 27, 15), 40.0), signal_id=None)
    store.insert_trade(make_trade(utc(2026, 9, 29, 15), -25.5), signal_id=7)
    store.insert_trade(make_trade(utc(2026, 9, 29, 14), 10.0))

    assert store.realized_pnl_since(utc(2026, 9, 29)) == pytest.approx(-15.5)
    assert store.realized_pnl_since("2026-09-30T00:00:00+00:00") == 0.0
    rows = store.trades(since=utc(2026, 9, 29))
    assert [r["pnl"] for r in rows] == [10.0, -25.5]  # oldest exit first
    assert rows[1]["signal_id"] == 7 and parse_ts(rows[1]["exit_ts"]) == utc(2026, 9, 29, 15)
    assert len(store.trades()) == 3
    assert len(store.trades(until=utc(2026, 9, 29))) == 1


# --------------------------------------------------------------------------- positions


def test_positions_round_trip(store):
    spy = PositionState(
        symbol="SPY", strategy="trend", qty=5.5, entry_price=500.25,
        entry_ts=datetime(2026, 9, 29, 13, 31, 5, 123456, tzinfo=UTC), stop_price=480.0,
        take_profit=None, bars_held=3, highest_close=510.0,
    )  # fmt: skip
    btc = PositionState(
        symbol="BTC/USD", strategy="breakout", qty=0.0123, entry_price=65000.0,
        entry_ts=utc(2026, 9, 29, 0, 1), stop_price=61000.0, take_profit=72000.0,
    )  # fmt: skip
    store.put_position(spy)
    store.put_position(btc)
    loaded = store.get_positions()
    assert loaded == {"BTC/USD": btc, "SPY": spy}
    assert loaded["SPY"].entry_ts.tzinfo is not None

    spy.stop_price = 490.0
    spy.bars_held = 4
    store.put_position(spy)
    assert store.get_positions()["SPY"].stop_price == 490.0
    assert [p["symbol"] for p in store.open_positions()] == ["BTC/USD", "SPY"]

    assert store.delete_position("SPY") is True
    assert store.delete_position("SPY") is False
    assert list(store.get_positions()) == ["BTC/USD"]


@pytest.mark.parametrize("field, value", [("stop_price", math.nan), ("qty", 0.0), ("entry_price", math.inf)])
def test_put_position_rejects_invalid_numbers(store, field, value):
    state = PositionState(symbol="SPY", strategy="trend", qty=1.0, entry_price=500.0, entry_ts=NOW, stop_price=480.0)
    setattr(state, field, value)
    with pytest.raises(ValueError):
        store.put_position(state)
    assert store.get_positions() == {}


# --------------------------------------------------------------------------- approvals


def test_approval_lifecycle(store):
    signal_id = store.insert_signal(make_signal(), date(2026, 9, 28))
    approval_id = store.create_approval(signal_id, 1500.0, NOW + timedelta(hours=12))
    store.set_approval_message_id(approval_id, 42)
    pending = store.pending_approvals()
    assert [a["id"] for a in pending] == [approval_id]
    assert pending[0]["message_id"] == 42 and pending[0]["ts_requested"] == to_iso(NOW)
    assert pending[0]["expires_ts"] == to_iso(NOW + timedelta(hours=12))

    assert store.set_approval(approval_id, "expired") is True
    # A late click cannot flip a decided approval.
    assert store.set_approval(approval_id, "approved") is False
    assert store.get_approval(approval_id)["status"] == "expired"
    assert store.get_approval(approval_id)["decided_ts"] == to_iso(NOW)
    assert store.set_approval(999, "approved") is False
    assert store.pending_approvals() == []
    assert [a["id"] for a in store.approvals_by_status("expired")] == [approval_id]
    assert store.approval_counts() == {"approved": 0, "expired": 1, "pending": 0, "rejected": 0}


@pytest.mark.parametrize("status", ["pending", "maybe"])
def test_set_approval_rejects_invalid_status(store, status):
    approval_id = store.create_approval(1, 1500.0, NOW)
    with pytest.raises(ValueError):
        store.set_approval(approval_id, status)


# --------------------------------------------------------------------------- events, equity, kv


def test_events_equity_and_kv(store):
    store.log_event("INFO", "start", "bot started", ts=utc(2026, 9, 29, 12))
    store.log_event("error", "tick", "boom", data={"n": 1, "when": NOW, "bad": math.nan}, ts=utc(2026, 9, 29, 13))
    store.log_event("CRITICAL", "kill", "tripped", ts=utc(2026, 9, 30, 13))
    events = store.recent_events(limit=2)
    assert [e["kind"] for e in events] == ["kill", "tick"]
    assert events[1]["data"] == {"n": 1, "when": to_iso(NOW), "bad": None}
    assert events[1]["level"] == "error"
    assert store.count_events() == 2
    assert store.count_events(since=utc(2026, 9, 29), until=utc(2026, 9, 30)) == 1
    assert store.count_events(levels=("info",)) == 1

    store.record_equity(100_000.0, 10_050.0, 95_000.0, 2_500.0, ts=utc(2026, 9, 29, 14))
    store.record_equity(100_100.0, 10_150.0, 95_000.0, 2_600.0, ts=utc(2026, 9, 29, 15))
    store.record_equity(100_200.0, 10_250.0, 95_000.0, 2_700.0, ts=utc(2026, 9, 29, 15))  # same ts replaces
    series = store.equity_series()
    assert [row["bot_equity"] for row in series] == [10_050.0, 10_250.0]
    assert store.equity_series(since=utc(2026, 9, 29, 15))[0]["exposure"] == 2_700.0
    assert store.latest_equity()["bot_equity"] == 10_250.0
    assert store.latest_equity(before=utc(2026, 9, 29, 15))["bot_equity"] == 10_050.0
    assert store.latest_equity(before=utc(2026, 9, 29)) is None

    assert store.kv_get("missing") is None and store.kv_get("missing", "x") == "x"
    store.kv_set("last_bar:SPY", "2026-09-28")
    store.kv_set("last_bar:SPY", "2026-09-29")
    assert store.kv_get("last_bar:SPY") == "2026-09-29"


# --------------------------------------------------------------------------- concurrency


def test_concurrent_writers_and_readers(store):
    errors: list[BaseException] = []

    def write(worker: int) -> None:
        try:
            for i in range(50):
                store.log_event("info", "load", f"{worker}-{i}")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    def read() -> None:
        try:
            for _ in range(50):
                store.recent_events(limit=20)
                store.jev_stats()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(w,)) for w in range(4)] + [threading.Thread(target=read) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.count_events(levels=("info",)) == 200


def test_second_connection_sees_committed_writes(tmp_path):
    path = tmp_path / "bot.sqlite3"
    with Store(path) as writer, Store(path) as reader:
        writer.kv_set("k", "v")
        assert reader.kv_get("k") == "v"
        raw = sqlite3.connect(path)
        try:
            assert raw.execute("SELECT value FROM kv WHERE key = 'k'").fetchone() == ("v",)
        finally:
            raw.close()


def test_orders_today_counts_from_a_later_resume(tmp_path):
    from bot.models import OrderIntent, OrderPurpose, OrderResult, Side

    store = Store(tmp_path / "s.sqlite3")
    day = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    for i, hour in enumerate((14, 15, 16)):
        cid = f"entry-SPY-{i}"
        store.upsert_order(
            OrderIntent("SPY", Side.BUY, 1.0, 100.0, OrderPurpose.ENTRY, "t", cid),
            OrderResult(cid, f"b{i}", "filled", 1.0, 100.0),
            ts=day.replace(hour=hour),
        )
    now = day.replace(hour=18)
    assert store.orders_today(now) == 3
    assert store.orders_today(now, since=day.replace(hour=15, minute=30)) == 1
    assert store.orders_today(now, since=day.replace(hour=2)) == 3  # a reset before today changes nothing
