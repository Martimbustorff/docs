import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestTradeRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.trading.enums import AccountStatus, OrderSide, OrderStatus, PositionSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import bot.broker as broker_mod
from bot.broker import (
    AlpacaBroker,
    OrderGateway,
    SimBroker,
    _as_result,
    assert_trading_allowed,
    floor_qty,
    make_client_order_id,
)
from bot.config import LIVE_ACK_PHRASE, ConfigError, RiskConfig, load_settings
from bot.models import OrderIntent, OrderPurpose, OrderResult, RiskAction, Side
from bot.risk import KillSwitch, RiskContext, RiskManager

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
SECRET = "sk-super-secret-value"


# --------------------------------------------------------------------------- fakes


class FakeStore:
    """The subset of bot.store.Store used by risk.py and broker.py. Orders come back as dict rows."""

    def __init__(self):
        self.kv = {}
        self.events = []
        self.orders = {}

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = value

    def log_event(self, level, kind, message, data=None):
        self.events.append((level, kind, message, data))

    def upsert_order(self, intent, result):
        self.orders[intent.client_order_id] = (intent, result)

    def get_order_by_client_id(self, client_order_id):
        row = self.orders.get(client_order_id)
        return None if row is None else asdict(row[1])


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def risk_config():
    return RiskConfig(
        capital_usd=10_000,
        risk_per_trade_pct=1.0,
        max_position_pct=33.0,
        max_position_usd=3_500,
        max_total_exposure_pct=100,
        daily_loss_limit_pct=2.0,
        max_drawdown_kill_pct=15.0,
        max_orders_per_day=10,
        max_consecutive_errors=5,
        approval_threshold_usd=1_000,
        approval_timeout_minutes=720,
        approval_max_price_drift_pct=2.0,
    )


def intent(symbol="SPY", side=Side.BUY, qty=5.0, price=100.0, purpose=OrderPurpose.ENTRY, key="k1"):
    return OrderIntent(symbol, side, qty, price, purpose, "test", make_client_order_id(purpose, symbol, key))


def live_settings(settings, gate_age=timedelta(days=1), passed=True):
    live = settings.model_copy(update={"trading_mode": "live", "alpaca_paper": False, "live_trading_ack": LIVE_ACK_PHRASE})
    if gate_age is not None:
        write_gate(live.live_gate_path, passed=passed, ts=(datetime.now(UTC) - gate_age).isoformat())
    return live


def write_gate(path, **data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class Rig:
    def __init__(self, settings, sim=None):
        self.settings = settings
        self.store = FakeStore()
        self.notifier = FakeNotifier()
        self.kill = KillSwitch(settings, self.store)
        self.risk = RiskManager(risk_config(), self.store, self.kill)
        self.sim = sim or SimBroker(cash=100_000)
        self.sim.set_price("SPY", 100.0)
        self.sim.set_price("BTC/USD", 50_000.0)
        self.gateway = OrderGateway(self.sim, self.risk, self.kill, self.store, settings, self.notifier)


@pytest.fixture
def rig(tmp_settings):
    return Rig(tmp_settings)


# --------------------------------------------------------------------------- helpers


def test_make_client_order_id_is_deterministic_and_short():
    cid = make_client_order_id(OrderPurpose.TAKE_PROFIT, "BTC/USD", "trend|2026-09-29")
    assert cid.startswith("take_profit-BTCUSD-") and len(cid.split("-")[-1]) == 12
    assert len(cid) <= 48
    assert cid == make_client_order_id(OrderPurpose.TAKE_PROFIT, "BTC/USD", "trend|2026-09-29")
    assert cid != make_client_order_id(OrderPurpose.TAKE_PROFIT, "BTC/USD", "trend|2026-09-30")


@pytest.mark.parametrize(
    "qty, expected",
    [(0.3, 0.3), (1.23456789012, 1.234567890), (1e-10, 0.0), (0.0, 0.0), (-1.0, 0.0), (float("nan"), 0.0), (1e12, 1e12)],
)
def test_floor_qty(qty, expected):
    assert floor_qty(qty) == expected


def test_as_result_accepts_dicts_rows_and_results():
    result = OrderResult("c1", "b1", "filled", 2.0, 10.0, "ok")
    assert _as_result(result) is result
    assert _as_result(asdict(result)) == result
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 'c1' AS client_order_id, 'b1' AS broker_order_id, 'filled' AS status, 2.0 AS filled_qty, "
        "10.0 AS filled_avg_price, 'ok' AS message, 'SPY' AS symbol"
    ).fetchone()
    assert _as_result(row) == result


# --------------------------------------------------------------------------- live guard matrix


def test_guard_paper_ok(tmp_settings):
    assert_trading_allowed(tmp_settings, tmp_settings.live_gate_path)


def test_guard_paper_mode_with_live_endpoint_raises(tmp_settings):
    with pytest.raises(ConfigError, match="ALPACA_PAPER=false"):
        assert_trading_allowed(tmp_settings.model_copy(update={"alpaca_paper": False}), tmp_settings.live_gate_path)


def test_guard_live_fully_configured_ok(tmp_settings):
    live = live_settings(tmp_settings)
    assert_trading_allowed(live, live.live_gate_path)


@pytest.mark.parametrize(
    "update",
    [
        {"alpaca_paper": True},
        {"live_trading_ack": None},
        {"live_trading_ack": LIVE_ACK_PHRASE.lower()},
        {"live_trading_ack": LIVE_ACK_PHRASE + " "},
    ],
)
def test_guard_live_misconfigured_raises(tmp_settings, update):
    live = live_settings(tmp_settings).model_copy(update=update)
    with pytest.raises(ConfigError):
        assert_trading_allowed(live, live.live_gate_path)


@pytest.mark.parametrize(
    "gate",
    [
        None,  # missing file
        {"passed": False, "ts": NOW.isoformat()},
        {"passed": "true", "ts": NOW.isoformat()},
        {"passed": True},
        {"passed": True, "ts": "yesterday"},
        {"passed": True, "ts": (NOW - timedelta(days=7)).isoformat()},
        {"passed": True, "ts": (NOW + timedelta(hours=1)).isoformat()},
        "not json",
    ],
)
def test_guard_live_gate_file_must_be_passed_and_fresh(tmp_settings, gate):
    live = live_settings(tmp_settings, gate_age=None)
    if isinstance(gate, dict):
        write_gate(live.live_gate_path, **gate)
    elif gate is not None:
        live.live_gate_path.write_text(gate)
    with pytest.raises(ConfigError):
        assert_trading_allowed(live, live.live_gate_path, now=NOW)


def test_guard_live_gate_boundaries(tmp_settings):
    live = live_settings(tmp_settings, gate_age=None)
    write_gate(live.live_gate_path, passed=True, ts=(NOW - timedelta(days=6, hours=23)).isoformat())
    assert_trading_allowed(live, live.live_gate_path, now=NOW)
    write_gate(live.live_gate_path, passed=True, ts="2026-09-30T14:00:00Z")
    assert_trading_allowed(live, live.live_gate_path, now=NOW)


# --------------------------------------------------------------------------- SimBroker


def test_sim_fills_with_slippage_and_tracks_cash():
    sim = SimBroker(cash=10_000, slippage_bps=10)
    sim.set_price("SPY", 100.0)
    buy = sim.submit(intent(qty=10))
    assert buy.status == "filled" and buy.filled_qty == 10 and buy.filled_avg_price == pytest.approx(100.1)
    assert sim.cash == pytest.approx(10_000 - 1_001)
    pos = sim.positions()["SPY"]
    assert pos.qty == 10 and pos.avg_entry_price == pytest.approx(100.1)
    assert pos.market_value == pytest.approx(1_000) and pos.unrealized_pl == pytest.approx(-1.0)
    assert sim.account().equity == pytest.approx(9_999)
    sell = sim.submit(intent(side=Side.SELL, qty=10, purpose=OrderPurpose.EXIT))
    assert sell.filled_avg_price == pytest.approx(99.9)
    assert sim.positions() == {}
    assert sim.get_order(buy.client_order_id) == buy
    assert sim.get_order("nope") is None


def test_sim_rejects_duplicates_oversells_and_overspending():
    sim = SimBroker(cash=1_000)
    sim.set_price("SPY", 100.0)
    sim.submit(intent(qty=5))
    with pytest.raises(ValueError, match="unique"):
        sim.submit(intent(qty=5))
    with pytest.raises(ValueError, match="insufficient qty"):
        sim.submit(intent(side=Side.SELL, qty=6, purpose=OrderPurpose.EXIT))
    with pytest.raises(ValueError, match="buying power"):
        sim.submit(intent(qty=6, key="k2"))
    with pytest.raises(ValueError, match="no price"):
        sim.last_price("QQQ")


def test_sim_failure_injection():
    sim = SimBroker(fail_next=2)
    sim.set_price("SPY", 100.0)
    for key in ("a", "b"):
        with pytest.raises(RuntimeError, match="injected"):
            sim.submit(intent(key=key))
    assert sim.submit(intent(key="c")).status == "filled"


def test_sim_partial_fill_then_cancel():
    sim = SimBroker(fill_ratio=0.5)
    sim.set_price("SPY", 100.0)
    result = sim.submit(intent(qty=10))
    assert result.status == "partially_filled" and result.filled_qty == 5
    sim.cancel_all()
    assert sim.get_order(result.client_order_id).status == "canceled"
    assert sim.positions()["SPY"].qty == 5


def test_sim_close_position_and_bars(bars_factory):
    sim = SimBroker()
    sim.set_price("BTC/USD", 50_000.0)
    sim.submit(intent("BTC/USD", qty=0.01))
    closed = sim.close_position("BTC/USD")
    assert closed.status == "filled" and closed.filled_qty == 0.01
    assert sim.close_position("BTC/USD") is None
    sim.set_bars("SPY", bars_factory(range(100, 110), start="2026-09-01"))
    frame = sim.daily_bars("SPY", date(2026, 9, 3), date(2026, 9, 5))
    assert list(frame.index.strftime("%Y-%m-%d")) == ["2026-09-03", "2026-09-04", "2026-09-05"]
    assert sim.daily_bars("QQQ", date(2026, 9, 1)).empty


# --------------------------------------------------------------------------- OrderGateway: submit


def test_gateway_submits_and_records(rig):
    result = rig.gateway.submit(intent())
    assert result.status == "filled"
    assert len(rig.sim.submitted) == 1
    stored_intent, stored_result = rig.store.orders[result.client_order_id]
    assert stored_result == result and stored_intent.purpose is OrderPurpose.ENTRY
    assert rig.notifier.sent == []


def test_kill_switch_blocks_entry_even_after_risk_allowed(rig):
    entry = intent()
    ctx = RiskContext(10_000, 10_000, 0, 0, 0, NOW)
    assert rig.risk.check(entry, ctx).action is RiskAction.ALLOW
    rig.kill.trip("tripped between check and submit", "telegram")
    result = rig.gateway.submit(entry)
    assert result.status == "refused" and "kill switch" in result.message
    assert rig.sim.submitted == []
    assert rig.store.orders[entry.client_order_id][1].status == "refused"
    assert any("refused" in text for text in rig.notifier.sent)


def test_exits_pass_while_kill_switch_is_tripped(rig):
    rig.gateway.submit(intent(qty=5))
    rig.kill.trip("stop everything", "cli")
    result = rig.gateway.submit(intent(side=Side.SELL, qty=5, purpose=OrderPurpose.STOP))
    assert result.status == "filled"
    assert rig.sim.positions() == {}


def test_resubmission_is_idempotent(rig):
    first = rig.gateway.submit(intent())
    again = rig.gateway.submit(intent())
    assert again == first
    assert len(rig.sim.submitted) == 1
    restarted = OrderGateway(rig.sim, rig.risk, rig.kill, rig.store, rig.settings, rig.notifier)
    assert restarted.submit(intent()) == first
    assert len(rig.sim.submitted) == 1


def test_known_order_is_returned_even_when_kill_switch_is_tripped(rig):
    first = rig.gateway.submit(intent())
    rig.kill.trip("later", "cli")
    assert rig.gateway.submit(intent()) == first
    assert rig.store.orders[first.client_order_id][1].status == "filled"  # not overwritten with "refused"


def test_adopts_order_the_broker_has_after_a_crash(rig):
    entry = intent()
    pre_crash = rig.sim.submit(entry)  # reached the broker, never recorded
    assert rig.store.orders == {}
    result = rig.gateway.submit(entry)
    assert result == pre_crash
    assert len(rig.sim.submitted) == 1
    assert rig.store.orders[entry.client_order_id][1] == pre_crash


def test_broker_rejected_order_is_not_resubmitted(rig):
    entry = intent()
    rig.store.upsert_order(entry, OrderResult(entry.client_order_id, "b-9", "rejected", message="halted"))
    assert rig.gateway.submit(entry).broker_order_id == "b-9"
    assert rig.sim.submitted == []


def test_local_failure_and_refusal_may_retry(rig):
    rig.sim.fail_next = 1
    failed = rig.gateway.submit(intent())
    assert failed.status == "rejected" and failed.broker_order_id is None
    assert rig.risk.consecutive_errors == 1
    retried = rig.gateway.submit(intent())
    assert retried.status == "filled"
    assert rig.risk.consecutive_errors == 0
    rig.kill.trip("x", "cli")
    refused = rig.gateway.submit(intent(key="k2"))
    assert refused.status == "refused"
    rig.kill.reset(confirm=True)
    assert rig.gateway.submit(intent(key="k2")).status == "filled"


def test_exception_is_rejected_alerted_counted_and_never_raised(rig):
    rig.sim.fail_next = 5
    for n in range(5):
        result = rig.gateway.submit(intent(key=f"e{n}"))
        assert result.status == "rejected" and "injected failure" in result.message
    assert len(rig.notifier.sent) == 5
    assert rig.kill.is_tripped()
    assert "5 consecutive errors" in rig.kill.status()["reason"]


def test_error_messages_never_carry_secrets(tmp_path, caplog):
    settings = load_settings(
        env_file=None,
        environ={"BOT_DATA_DIR": str(tmp_path / "var"), "ALPACA_API_KEY": "PKKEY", "ALPACA_SECRET_KEY": SECRET},
    )

    class LeakyBroker(SimBroker):
        def submit(self, order):
            raise RuntimeError(f"401 for key PKKEY secret {SECRET}")

    rig = Rig(settings, sim=LeakyBroker())
    with caplog.at_level(logging.DEBUG):
        result = rig.gateway.submit(intent())
    assert result.status == "rejected"
    everything = [result.message, *rig.notifier.sent, *(str(e) for e in rig.store.events), caplog.text]
    assert all(SECRET not in text and "PKKEY" not in text for text in everything)
    assert "***" in result.message


@pytest.mark.parametrize(
    "bad",
    [
        intent(side=Side.SELL, purpose=OrderPurpose.ENTRY),
        intent(side=Side.BUY, purpose=OrderPurpose.EXIT),
        intent(qty=0.0),
        intent(qty=float("nan")),
        OrderIntent("SPY", Side.BUY, 1.0, 100.0, OrderPurpose.ENTRY, "x", "x" * 49),
    ],
)
def test_malformed_orders_are_refused(rig, bad):
    assert rig.gateway.submit(bad).status == "refused"
    assert rig.sim.submitted == []


def test_sell_is_clamped_to_the_long_position(rig):
    rig.gateway.submit(intent(qty=5))
    result = rig.gateway.submit(intent(side=Side.SELL, qty=8, purpose=OrderPurpose.EXIT))
    assert result.status == "filled" and result.filled_qty == 5
    assert rig.store.orders[result.client_order_id][0].qty == 5
    assert rig.sim.positions() == {}
    flat = rig.gateway.submit(intent(side=Side.SELL, qty=1, purpose=OrderPurpose.EXIT, key="k2"))
    assert flat.status == "refused" and "no long SPY position" in flat.message


def test_live_guard_refuses_entries_and_exits(rig):
    rig.gateway.submit(intent(qty=5))
    rig.gateway.settings = rig.settings.model_copy(update={"alpaca_paper": False})
    entry = rig.gateway.submit(intent(key="k2"))
    exit_ = rig.gateway.submit(intent(side=Side.SELL, qty=5, purpose=OrderPurpose.STOP))
    assert entry.status == exit_.status == "refused"
    assert "live-trading guard" in exit_.message
    assert len(rig.sim.submitted) == 1


def test_live_broker_needs_live_mode(tmp_settings):
    rig = Rig(tmp_settings, sim=SimBroker(is_paper=False))
    result = rig.gateway.submit(intent())
    assert result.status == "refused" and "broker is live" in result.message


def test_expired_live_gate_blocks_entries_but_not_exits_or_flatten(tmp_settings):
    fresh = live_settings(tmp_settings)
    rig = Rig(fresh, sim=SimBroker(is_paper=False))
    assert rig.gateway.submit(intent(qty=5)).status == "filled"
    rig.gateway.settings = live_settings(fresh, gate_age=timedelta(days=8))
    assert rig.gateway.submit(intent(key="k2")).status == "refused"
    stop = rig.gateway.submit(intent(side=Side.SELL, qty=2, purpose=OrderPurpose.STOP))
    assert stop.status == "filled"
    results = rig.gateway.flatten_all("kill")
    assert [r.status for r in results] == ["filled"] and rig.sim.positions() == {}


def test_live_mode_with_passed_gate_trades(tmp_settings):
    rig = Rig(live_settings(tmp_settings), sim=SimBroker(is_paper=False))
    assert rig.gateway.submit(intent()).status == "filled"


def test_store_write_failure_after_submit_is_alerted(rig, monkeypatch):
    def broken(intent, result):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rig.store, "upsert_order", broken)
    result = rig.gateway.submit(intent())
    assert result.status == "filled"
    assert rig.risk.consecutive_errors == 1
    assert any("could not record" in text for text in rig.notifier.sent)


def test_store_read_failure_falls_back_to_broker_lookup(rig, monkeypatch):
    entry = intent()
    rig.sim.submit(entry)

    def broken(cid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rig.store, "get_order_by_client_id", broken)
    assert rig.gateway.submit(entry).status == "filled"
    assert len(rig.sim.submitted) == 1


# --------------------------------------------------------------------------- OrderGateway: flatten_all


def test_flatten_all_cancels_and_closes_everything(rig):
    rig.gateway.submit(intent("SPY", qty=5))
    rig.gateway.submit(intent("BTC/USD", qty=0.02))
    results = rig.gateway.flatten_all("drill")
    assert rig.sim.cancel_all_calls == 1
    assert rig.sim.positions() == {}
    assert [r.status for r in results] == ["filled", "filled"]
    kills = [i for i, _ in rig.store.orders.values() if i.purpose is OrderPurpose.KILL]
    assert {i.symbol for i in kills} == {"SPY", "BTC/USD"} and all(i.side is Side.SELL for i in kills)
    assert "2 position(s) closed" in rig.notifier.sent[-1]
    assert rig.gateway.flatten_all("again") == []


def test_flatten_all_keeps_going_after_a_failure(rig):
    rig.gateway.submit(intent("SPY", qty=5))
    rig.gateway.submit(intent("BTC/USD", qty=0.02))
    rig.sim.fail_next = 1  # BTC/USD closes first (sorted)
    results = rig.gateway.flatten_all("kill")
    assert [r.status for r in results] == ["rejected", "filled"]
    assert results[0].client_order_id.startswith("kill-BTCUSD-")
    assert set(rig.sim.positions()) == {"BTC/USD"}
    assert "FAILURES" in rig.notifier.sent[-1]
    assert rig.risk.consecutive_errors == 1


def test_flatten_all_survives_cancel_failure(rig, monkeypatch):
    rig.gateway.submit(intent("SPY", qty=5))

    def broken():
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(rig.sim, "cancel_all", broken)
    results = rig.gateway.flatten_all("kill")
    assert [r.status for r in results] == ["filled"]
    assert "cancel_all" in rig.notifier.sent[-1]


def test_flatten_all_refused_by_live_guard(rig):
    rig.gateway.submit(intent("SPY", qty=5))
    rig.gateway.settings = rig.settings.model_copy(update={"alpaca_paper": False})
    results = rig.gateway.flatten_all("kill")
    assert [r.status for r in results] == ["refused"]
    assert rig.sim.cancel_all_calls == 0 and "SPY" in rig.sim.positions()
    assert "REFUSED" in rig.notifier.sent[-1]


# --------------------------------------------------------------------------- AlpacaBroker (no network)


def api_error(status):
    return APIError(json.dumps({"code": status * 100000, "message": "err"}), SimpleNamespace(response=SimpleNamespace(status_code=status)))


def order_ns(cid, status=OrderStatus.ACCEPTED, filled_qty="0", avg=None):
    return SimpleNamespace(id=uuid4(), client_order_id=cid, status=status, filled_qty=filled_qty, filled_avg_price=avg)


def bar(ts, close):
    return SimpleNamespace(timestamp=ts, open=close - 1, high=close + 1, low=close - 2, close=close, volume=1000.0)


class FakeTrading:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.requests = []
        self.orders = {}
        self.positions = []
        self.closed = []
        self.cancel_responses = []

    def submit_order(self, request):
        self.requests.append(request)
        order = order_ns(request.client_order_id)
        self.orders[request.client_order_id] = order
        return order

    def get_order_by_client_id(self, cid):
        if cid == "boom":
            raise api_error(500)
        if cid not in self.orders:
            raise api_error(404)
        return self.orders[cid]

    def get_all_positions(self):
        return self.positions

    def close_position(self, symbol):
        self.closed.append(symbol)
        if symbol not in {p.symbol for p in self.positions}:
            raise api_error(404)
        return order_ns("alpaca-generated", OrderStatus.PENDING_NEW)

    def cancel_orders(self):
        return self.cancel_responses

    def get_clock(self):
        return SimpleNamespace(is_open=False, next_open=datetime(2026, 10, 1, 9, 30, tzinfo=timezone(timedelta(hours=-4))))

    def get_account(self):
        return SimpleNamespace(
            equity="10250.5", cash="7000", buying_power="14000", trading_blocked=False, account_blocked=False,
            trade_suspended_by_user=None, status=AccountStatus.ACTIVE,
        )


class FakeData:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.requests = []
        self.bars = {}

    def _latest(self, request):
        self.requests.append(request)
        return {request.symbol_or_symbols: SimpleNamespace(price=123.45)}

    def _bars(self, request):
        self.requests.append(request)
        return SimpleNamespace(data=self.bars)

    get_stock_latest_trade = get_crypto_latest_trade = _latest
    get_stock_bars = get_crypto_bars = _bars


@pytest.fixture
def alpaca(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_mod, "TradingClient", FakeTrading)
    monkeypatch.setattr(broker_mod, "StockHistoricalDataClient", FakeData)
    monkeypatch.setattr(broker_mod, "CryptoHistoricalDataClient", FakeData)
    settings = load_settings(
        env_file=None,
        environ={"BOT_DATA_DIR": str(tmp_path / "var"), "ALPACA_API_KEY": "PKKEY", "ALPACA_SECRET_KEY": SECRET},
    )
    return AlpacaBroker(settings, clock=lambda: NOW)


def test_alpaca_needs_keys(tmp_settings):
    with pytest.raises(ConfigError):
        AlpacaBroker(tmp_settings)


def test_alpaca_clients_get_keys_and_paper_flag(alpaca):
    assert alpaca.is_paper is True
    assert alpaca.trading.init_kwargs == {"api_key": "PKKEY", "secret_key": SECRET, "paper": True}
    assert alpaca.stock_data.init_kwargs == {"api_key": "PKKEY", "secret_key": SECRET}
    assert alpaca.crypto_data.init_kwargs == {"api_key": "PKKEY", "secret_key": SECRET}


def test_alpaca_live_flag_follows_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_mod, "TradingClient", FakeTrading)
    monkeypatch.setattr(broker_mod, "StockHistoricalDataClient", FakeData)
    monkeypatch.setattr(broker_mod, "CryptoHistoricalDataClient", FakeData)
    settings = load_settings(
        env_file=None,
        environ={"BOT_DATA_DIR": str(tmp_path), "ALPACA_API_KEY": "AK", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "false"},
    )
    broker = AlpacaBroker(settings)
    assert broker.is_paper is False and broker.trading.init_kwargs["paper"] is False


def test_alpaca_crypto_order_is_gtc_fractional(alpaca):
    cid = make_client_order_id(OrderPurpose.ENTRY, "BTC/USD", "k")
    result = alpaca.submit(OrderIntent("BTC/USD", Side.BUY, 0.0123456789123, 50_000, OrderPurpose.ENTRY, "t", cid))
    request = alpaca.trading.requests[-1]
    assert isinstance(request, MarketOrderRequest)
    assert request.symbol == "BTC/USD" and request.qty == 0.012345678
    assert request.side is OrderSide.BUY and request.time_in_force is TimeInForce.GTC
    assert request.client_order_id == cid
    assert result.client_order_id == cid and result.status == "accepted" and result.filled_avg_price is None


def test_alpaca_stock_order_is_day(alpaca):
    alpaca.submit(intent(side=Side.SELL, qty=2.5, purpose=OrderPurpose.STOP))
    request = alpaca.trading.requests[-1]
    assert request.symbol == "SPY" and request.qty == 2.5
    assert request.side is OrderSide.SELL and request.time_in_force is TimeInForce.DAY


def test_alpaca_get_order_maps_404_to_none(alpaca):
    assert alpaca.get_order("unknown") is None
    with pytest.raises(APIError):
        alpaca.get_order("boom")
    alpaca.trading.orders["c"] = order_ns("c", OrderStatus.FILLED, "3", "101.5")
    assert alpaca.get_order("c") == OrderResult("c", str(alpaca.trading.orders["c"].id), "filled", 3.0, 101.5, "alpaca status: filled")


@pytest.mark.parametrize(
    "raw, mapped",
    [
        (OrderStatus.NEW, "new"),
        (OrderStatus.PENDING_NEW, "accepted"),
        (OrderStatus.PARTIALLY_FILLED, "partially_filled"),
        (OrderStatus.EXPIRED, "canceled"),
        (OrderStatus.CANCELED, "canceled"),
        (OrderStatus.REJECTED, "rejected"),
        (OrderStatus.DONE_FOR_DAY, "accepted"),
    ],
)
def test_alpaca_status_mapping(alpaca, raw, mapped):
    alpaca.trading.orders["c"] = order_ns("c", raw)
    result = alpaca.get_order("c")
    assert result.status == mapped and raw.value in result.message


def test_alpaca_positions_normalise_crypto_symbols(alpaca):
    alpaca.trading.positions = [
        SimpleNamespace(symbol="BTCUSD", asset_class="crypto", qty="0.5", side=PositionSide.LONG,
                        avg_entry_price="60000", market_value="31000", unrealized_pl="1000", current_price="62000"),
        SimpleNamespace(symbol="SPY", asset_class="us_equity", qty="3", side=PositionSide.LONG,
                        avg_entry_price="500", market_value=None, unrealized_pl=None, current_price="510"),
    ]
    positions = alpaca.positions()
    assert set(positions) == {"BTC/USD", "SPY"}
    assert positions["BTC/USD"].qty == 0.5 and positions["BTC/USD"].market_value == 31_000
    assert positions["SPY"].market_value == pytest.approx(1_530) and positions["SPY"].unrealized_pl == 0.0


def test_alpaca_close_position_uses_slashless_symbol(alpaca):
    alpaca.trading.positions = [SimpleNamespace(symbol="BTCUSD")]
    result = alpaca.close_position("BTC/USD")
    assert alpaca.trading.closed == ["BTCUSD"]
    assert result.status == "accepted"
    assert alpaca.close_position("SPY") is None


def test_alpaca_cancel_all_raises_on_failures(alpaca):
    alpaca.trading.cancel_responses = [SimpleNamespace(id="a", status=200)]
    alpaca.cancel_all()
    alpaca.trading.cancel_responses.append(SimpleNamespace(id="b", status=500))
    with pytest.raises(RuntimeError, match="b"):
        alpaca.cancel_all()


def test_alpaca_last_price_requests(alpaca):
    assert alpaca.last_price("SPY") == 123.45
    stock_request = alpaca.stock_data.requests[-1]
    assert isinstance(stock_request, StockLatestTradeRequest) and stock_request.feed is DataFeed.IEX
    assert alpaca.last_price("BTC/USD") == 123.45
    crypto_request = alpaca.crypto_data.requests[-1]
    assert isinstance(crypto_request, CryptoLatestTradeRequest) and crypto_request.symbol_or_symbols == "BTC/USD"


def test_alpaca_account_and_clock(alpaca):
    snap = alpaca.account()
    assert snap.equity == 10_250.5 and snap.cash == 7_000 and snap.buying_power == 14_000
    assert snap.is_paper and not snap.trading_blocked and snap.status == "ACTIVE"
    assert alpaca.is_market_open() is False
    assert alpaca.next_open() == datetime(2026, 10, 1, 13, 30, tzinfo=UTC)


def test_alpaca_stock_bars_request_and_frame(alpaca):
    alpaca.stock_data.bars = {
        "SPY": [
            bar(datetime(2026, 9, 25, 4, 0, tzinfo=UTC), 500.0),
            bar(datetime(2026, 9, 28, 4, 0, tzinfo=UTC), 501.0),
            bar(datetime(2026, 9, 29, 4, 0, tzinfo=UTC), 502.0),
            bar(datetime(2026, 9, 30, 4, 0, tzinfo=UTC), 503.0),  # still trading at NOW (11:00 NY)
        ]
    }
    frame = alpaca.daily_bars("SPY", date(2026, 9, 28))
    request = alpaca.stock_data.requests[-1]
    assert isinstance(request, StockBarsRequest)
    assert request.timeframe.value == "1Day" and request.feed is DataFeed.SIP and request.adjustment is Adjustment.ALL
    assert request.end == (NOW - timedelta(minutes=16)).replace(tzinfo=None)  # free plans: SIP older than 15 min
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "date" and frame.index.tz is None
    assert list(frame.index.strftime("%Y-%m-%d")) == ["2026-09-28", "2026-09-29"]
    assert frame["close"].tolist() == [501.0, 502.0] and all(t == "float64" for t in frame.dtypes)


def hourly(day, closes):
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return [bar(start + timedelta(hours=h), c) for h, c in enumerate(closes)]


def test_alpaca_crypto_days_are_built_from_hourly_bars_on_utc_days(alpaca):
    alpaca._clock = lambda: datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
    alpaca.crypto_data.bars = {
        "BTC/USD": hourly(date(2026, 9, 28), [60_000.0 + h for h in range(24)])
        + hourly(date(2026, 9, 29), [61_000.0 - h for h in range(24)])
        + hourly(date(2026, 9, 30), [62_000.0, 62_100.0, 62_200.0])  # still forming at 03:00 UTC
    }
    frame = alpaca.daily_bars("BTC/USD", date(2026, 9, 1), date(2026, 9, 30))
    request = alpaca.crypto_data.requests[-1]
    assert isinstance(request, CryptoBarsRequest) and request.symbol_or_symbols == "BTC/USD"
    assert request.timeframe.value == "1Hour"
    assert request.end == datetime(2026, 10, 1)  # alpaca-py stores naive UTC
    assert list(frame.index.strftime("%Y-%m-%d")) == ["2026-09-28", "2026-09-29"]
    sep28 = frame.loc["2026-09-28"]
    assert sep28["open"] == 59_999.0 and sep28["close"] == 60_023.0  # first hour's open, last hour's close
    assert sep28["high"] == 60_024.0 and sep28["low"] == 59_998.0 and sep28["volume"] == 24_000.0


def test_alpaca_crypto_day_is_open_until_utc_midnight(alpaca):
    alpaca._clock = lambda: datetime(2026, 9, 29, 23, 59, tzinfo=UTC)
    alpaca.crypto_data.bars = {"BTC/USD": hourly(date(2026, 9, 29), [61_000.0] * 24)}
    assert alpaca.daily_bars("BTC/USD", date(2026, 9, 1)).empty


def test_alpaca_stock_bars_fall_back_to_iex_when_sip_is_refused(alpaca):
    calls = []

    def bars(request):
        calls.append(request.feed)
        if request.feed is DataFeed.SIP:
            raise api_error(403)
        return SimpleNamespace(data={"SPY": [bar(datetime(2026, 9, 29, 4, 0, tzinfo=UTC), 502.0)]})

    alpaca.stock_data.get_stock_bars = bars
    frame = alpaca.daily_bars("SPY", date(2026, 9, 28))
    assert calls == [DataFeed.SIP, DataFeed.IEX]
    assert frame["close"].tolist() == [502.0]


def test_alpaca_empty_bars_have_the_frame_shape(alpaca):
    frame = alpaca.daily_bars("QQQ", date(2026, 9, 1))
    assert frame.empty and list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "date" and isinstance(frame.index, pd.DatetimeIndex)
