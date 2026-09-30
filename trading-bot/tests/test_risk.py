import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.config import LIVE_ACK_PHRASE, ConfigError, RiskConfig
from bot.models import OrderIntent, OrderPurpose, RiskAction, Side, Signal, SignalKind
from bot.risk import DAILY_LOSS_KEY, ERRORS_KEY, PEAK_KEY, KillSwitch, RiskContext, RiskManager

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)  # 11:00 in New York


class FakeStore:
    """The subset of bot.store.Store that risk.py uses."""

    def __init__(self):
        self.kv = {}
        self.events = []

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = value

    def log_event(self, level, kind, message, data=None):
        self.events.append((level, kind, message, data))


def risk_config(**overrides):
    values = dict(
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
    values.update(overrides)
    return RiskConfig(**values)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def kill(tmp_settings, store):
    return KillSwitch(tmp_settings, store)


@pytest.fixture
def rm(kill, store):
    return RiskManager(risk_config(), store, kill)


def entry(qty=5.0, price=100.0, symbol="SPY", side=Side.BUY, purpose=OrderPurpose.ENTRY):
    return OrderIntent(symbol, side, qty, price, purpose, "test", f"{purpose.value}-{symbol}-abc")


def ctx(**overrides):
    values = dict(
        bot_equity=10_000.0,
        start_of_day_equity=10_000.0,
        open_exposure_usd=0.0,
        symbol_exposure_usd=0.0,
        orders_today=0,
        now=NOW,
    )
    values.update(overrides)
    return RiskContext(**values)


def signal(price=100.0, stop=95.0, kind=SignalKind.ENTRY):
    return Signal(NOW, "SPY", "trend", kind, "test", price, stop_price=stop)


# --------------------------------------------------------------------------- KillSwitch


def test_trip_writes_json_atomically_and_logs(tmp_settings, kill, store):
    assert not kill.is_tripped()
    assert kill.status() is None
    kill.trip("manual test", source="telegram")
    assert kill.is_tripped()
    data = json.loads(tmp_settings.kill_switch_path.read_text())
    assert data["reason"] == "manual test" and data["source"] == "telegram"
    assert datetime.fromisoformat(data["ts"]).tzinfo is not None
    assert kill.status() == data
    assert [p.name for p in tmp_settings.kill_switch_path.parent.iterdir()] == ["KILL_SWITCH"]  # no temp files
    assert store.events[-1][:2] == ("critical", "kill_switch_tripped")


def test_trip_is_idempotent_and_keeps_first_reason(kill, store):
    kill.trip("first", "risk")
    kill.trip("second", "cli")
    assert kill.status()["reason"] == "first"
    assert len([e for e in store.events if e[1] == "kill_switch_tripped"]) == 1


def test_trip_survives_a_new_instance(tmp_settings, kill):
    kill.trip("restart me", "risk")
    assert KillSwitch(tmp_settings).is_tripped()


def test_reset_needs_confirm(kill, store):
    kill.trip("x", "cli")
    with pytest.raises(ValueError):
        kill.reset(confirm=False)
    assert kill.is_tripped()
    kill.reset(confirm=True)
    assert not kill.is_tripped()
    assert kill.status() is None
    assert store.events[-1][1] == "kill_switch_reset"


def test_env_forces_kill_switch_and_blocks_reset(tmp_settings):
    forced = KillSwitch(tmp_settings.model_copy(update={"kill_switch": True}))
    assert forced.is_tripped()
    assert forced.status()["source"] == "env"
    forced.trip("also on file", "cli")
    with pytest.raises(ConfigError):
        forced.reset(confirm=True)
    assert forced.path.exists() and forced.is_tripped()


def test_unreadable_file_still_trips(tmp_settings, kill):
    tmp_settings.kill_switch_path.write_text("{not json")
    assert kill.is_tripped()
    assert kill.status()["source"] == "unknown"


def test_write_failure_keeps_switch_tripped_in_memory(kill, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("bot.risk.os.replace", boom)
    kill.trip("cannot persist", "risk")
    assert not kill.path.exists()
    assert kill.is_tripped()
    assert kill.status()["reason"] == "cannot persist"
    assert list(kill.path.parent.iterdir()) == []  # temp file cleaned up
    kill.reset(confirm=True)
    assert not kill.is_tripped()


# --------------------------------------------------------------------------- sizing


def test_size_entry_risk_budget_binds(rm):
    # 1% of 10k = $100 risk / $5 stop distance = 20 units ($2,000 < 33% and < $3,500)
    assert rm.size_entry(signal(100, 95), 10_000) == pytest.approx(20)


def test_size_entry_pct_cap_binds(rm):
    # $100 / $0.5 = 200 units, but 33% of 10k / $100 = 33 units
    assert rm.size_entry(signal(100, 99.5), 10_000) == pytest.approx(33)


def test_size_entry_usd_cap_binds(rm):
    # equity 50k: risk -> 500 units, pct -> 165 units, $3,500 / $100 = 35 units
    assert rm.size_entry(signal(100, 99), 50_000) == pytest.approx(35)


@pytest.mark.parametrize(
    "sig, equity",
    [
        (signal(100, None), 10_000),
        (signal(100, 100), 10_000),
        (signal(100, 101), 10_000),
        (signal(float("nan"), 95), 10_000),
        (signal(100, float("nan")), 10_000),
        (signal(100, 95), 0),
        (signal(100, 95), float("nan")),
        (signal(100, 95, kind=SignalKind.EXIT), 10_000),
    ],
)
def test_size_entry_invalid_is_zero(rm, sig, equity):
    assert rm.size_entry(sig, equity) == 0.0


def test_size_entry_rounds_down_to_broker_precision(rm):
    # 3.0 - 2.9 is 0.10000000000000009 in floating point: the risk budget must not be exceeded
    qty = rm.size_entry(signal(3.0, 2.9), 10_000)
    assert qty == 999.999999999
    assert rm.size_entry(signal(7.0, 6.0), 10_000) == 100.0
    btc = rm.size_entry(signal(61_234.5, 58_000.0), 10_000)
    assert btc * 61_234.5 <= 3_300 and round(btc, 9) == btc


# --------------------------------------------------------------------------- check: exits


@pytest.mark.parametrize("purpose", [OrderPurpose.EXIT, OrderPurpose.STOP, OrderPurpose.TAKE_PROFIT, OrderPurpose.KILL])
def test_risk_reducing_orders_always_allowed(rm, kill, purpose):
    kill.trip("tripped", "test")
    bad_day = ctx(
        bot_equity=5_000, orders_today=999, open_exposure_usd=1e9, symbol_exposure_usd=1e9, start_of_day_equity=10_000
    )
    verdict = rm.check(entry(qty=1_000, side=Side.SELL, purpose=purpose), bad_day)
    assert verdict.action is RiskAction.ALLOW
    assert verdict.adjusted_qty is None


def test_risk_reducing_blocked_only_by_live_guard(tmp_settings, store):
    live_without_gate = tmp_settings.model_copy(
        update={"trading_mode": "live", "alpaca_paper": False, "live_trading_ack": LIVE_ACK_PHRASE}
    )
    rm = RiskManager(risk_config(), store, KillSwitch(live_without_gate, store))
    verdict = rm.check(entry(side=Side.SELL, purpose=OrderPurpose.STOP), ctx())
    assert verdict.action is RiskAction.BLOCK
    assert "live-trading guard" in verdict.reason


# --------------------------------------------------------------------------- check: entry rules in order


def test_rule1_kill_switch_blocks(rm, kill):
    kill.trip("x", "test")
    verdict = rm.check(entry(), ctx())
    assert verdict.action is RiskAction.BLOCK and "kill switch" in verdict.reason


def test_rule1_live_guard_blocks_mixed_config(tmp_settings, store):
    mixed = tmp_settings.model_copy(update={"alpaca_paper": False})  # paper mode, live endpoint
    rm = RiskManager(risk_config(), store, KillSwitch(mixed, store))
    verdict = rm.check(entry(), ctx())
    assert verdict.action is RiskAction.BLOCK and "live-trading guard" in verdict.reason


def test_rule2_max_orders_trips_kill_switch(rm, kill):
    assert rm.check(entry(), ctx(orders_today=9)).action is RiskAction.ALLOW
    assert not kill.is_tripped()
    verdict = rm.check(entry(), ctx(orders_today=10))
    assert verdict.action is RiskAction.BLOCK and "max_orders_per_day" in verdict.reason
    assert kill.is_tripped()
    assert kill.status()["source"] == "risk"


def test_rule2_runs_before_daily_loss(rm, kill):
    verdict = rm.check(entry(), ctx(orders_today=10, bot_equity=9_000))
    assert "max_orders_per_day" in verdict.reason
    assert kill.is_tripped()


def test_rule3_daily_loss_blocks_at_limit(rm, store):
    assert rm.check(entry(), ctx(bot_equity=9_800.01)).action is RiskAction.ALLOW
    verdict = rm.check(entry(), ctx(bot_equity=9_800.0))  # exactly 2% down
    assert verdict.action is RiskAction.BLOCK and "daily loss" in verdict.reason
    assert store.kv[DAILY_LOSS_KEY] == "2026-09-30"
    assert any(e[1] == "daily_loss_limit" for e in store.events)


def test_rule3_block_lasts_for_the_new_york_day(rm):
    assert rm.check(entry(), ctx(bot_equity=9_000)).action is RiskAction.BLOCK
    # equity recovers the same NY day (23:30 NY is already 03:30 UTC next day): still blocked
    late = datetime(2026, 10, 1, 3, 30, tzinfo=UTC)
    verdict = rm.check(entry(), ctx(bot_equity=10_000, now=late))
    assert verdict.action is RiskAction.BLOCK and "earlier today" in verdict.reason
    next_day = datetime(2026, 10, 1, 13, 35, tzinfo=UTC)
    assert rm.check(entry(), ctx(bot_equity=10_000, now=next_day)).action is RiskAction.ALLOW


def test_rule3_runs_before_caps(rm):
    verdict = rm.check(entry(qty=1_000), ctx(bot_equity=9_000))
    assert "daily loss" in verdict.reason


def test_rule4_per_symbol_pct_cap_shrinks(rm):
    # 33% of 10k = $3,300 cap, $2,500 already held -> $800 room
    verdict = rm.check(entry(qty=20, price=100), ctx(symbol_exposure_usd=2_500, open_exposure_usd=2_500))
    assert verdict.action is RiskAction.ALLOW
    assert verdict.adjusted_qty == pytest.approx(8)
    assert "per-symbol pct cap" in verdict.reason


def test_rule4_usd_cap_shrinks(rm):
    # equity 20k: pct cap $6,600, USD cap $3,500 binds
    verdict = rm.check(entry(qty=50, price=100), ctx(bot_equity=20_000, start_of_day_equity=20_000))
    assert verdict.adjusted_qty == pytest.approx(35)
    assert "per-symbol USD cap" in verdict.reason
    assert verdict.action is RiskAction.NEEDS_APPROVAL  # $3,500 > $1,000


def test_rule4_total_exposure_shrinks(rm):
    verdict = rm.check(entry(qty=5, price=100, symbol="QQQ"), ctx(open_exposure_usd=9_800))
    assert verdict.adjusted_qty == pytest.approx(2)
    assert "total exposure cap" in verdict.reason


def test_rule4_blocks_below_one_dollar(rm):
    verdict = rm.check(entry(qty=5, price=100), ctx(open_exposure_usd=9_999.5))
    assert verdict.action is RiskAction.BLOCK and "< $1" in verdict.reason
    tiny = rm.check(entry(qty=0.005, price=100), ctx())
    assert tiny.action is RiskAction.BLOCK


def test_rule4_full_symbol_blocks(rm):
    verdict = rm.check(entry(), ctx(symbol_exposure_usd=3_500, open_exposure_usd=3_500))
    assert verdict.action is RiskAction.BLOCK


def test_rule5_needs_approval_above_threshold(rm):
    verdict = rm.check(entry(qty=10.01, price=100), ctx())
    assert verdict.action is RiskAction.NEEDS_APPROVAL
    assert verdict.adjusted_qty is None
    assert rm.check(entry(qty=10, price=100), ctx()).action is RiskAction.ALLOW  # exactly $1,000


def test_rule5_uses_shrunk_notional(rm):
    verdict = rm.check(entry(qty=20, price=100), ctx(symbol_exposure_usd=2_500, open_exposure_usd=2_500))
    assert verdict.action is RiskAction.ALLOW  # $2,000 asked, $800 fits, under the $1,000 threshold


def test_rule6_allow(rm):
    verdict = rm.check(entry(qty=5, price=100), ctx())
    assert verdict.action is RiskAction.ALLOW and verdict.adjusted_qty is None


@pytest.mark.parametrize(
    "intent, context",
    [
        (entry(side=Side.SELL), ctx()),
        (entry(qty=float("nan")), ctx()),
        (entry(price=0.0), ctx()),
        (entry(), ctx(bot_equity=float("nan"))),
        (entry(), ctx(start_of_day_equity=0.0)),
        (entry(), ctx(open_exposure_usd=float("nan"))),
        (entry(), ctx(symbol_exposure_usd=-1.0)),
    ],
)
def test_invalid_entries_fail_closed(rm, intent, context):
    assert rm.check(intent, context).action is RiskAction.BLOCK


def test_store_failure_fails_closed(rm, store, monkeypatch):
    def broken(key):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "kv_get", broken)
    verdict = rm.check(entry(), ctx())
    assert verdict.action is RiskAction.BLOCK and "risk check error" in verdict.reason


# --------------------------------------------------------------------------- consecutive errors


def test_consecutive_errors_trip_kill_switch(rm, kill, store):
    for _ in range(4):
        rm.after_error()
    assert not kill.is_tripped()
    assert store.kv[ERRORS_KEY] == "4"
    rm.after_error()
    assert kill.is_tripped()
    assert "5 consecutive errors" in kill.status()["reason"]
    assert rm.consecutive_errors == 0


def test_success_resets_error_count(rm, kill, store):
    for _ in range(4):
        rm.after_error()
    rm.after_success()
    assert store.kv[ERRORS_KEY] == "0"
    for _ in range(4):
        rm.after_error()
    assert not kill.is_tripped()


def test_error_count_survives_restart(kill, store):
    first = RiskManager(risk_config(), store, kill)
    for _ in range(3):
        first.after_error()
    second = RiskManager(risk_config(), store, kill)
    assert second.consecutive_errors == 3
    second.after_error()
    second.after_error()
    assert kill.is_tripped()


# --------------------------------------------------------------------------- drawdown


def test_drawdown_tracks_peak_in_kv(rm, kill, store):
    rm.check_drawdown(10_000)
    rm.check_drawdown(12_000)
    rm.check_drawdown(11_000)
    assert float(store.kv[PEAK_KEY]) == 12_000
    assert not kill.is_tripped()


def test_drawdown_trips_at_limit(rm, kill, store):
    rm.check_drawdown(12_000)
    rm.check_drawdown(10_201)  # 14.99%
    assert not kill.is_tripped()
    rm.check_drawdown(10_200)  # exactly 15%
    assert kill.is_tripped()
    assert "drawdown" in kill.status()["reason"]
    assert float(store.kv[PEAK_KEY]) == 10_200  # re-based so a manual resume does not re-trip
    kill.reset(confirm=True)
    rm.check_drawdown(10_150)
    assert not kill.is_tripped()


def test_drawdown_uses_persisted_peak_after_restart(kill, store):
    RiskManager(risk_config(), store, kill).check_drawdown(20_000)
    RiskManager(risk_config(), store, kill).check_drawdown(16_000)  # 20% from the stored peak
    assert kill.is_tripped()


def test_drawdown_rejects_nan_and_trips_on_non_positive(rm, kill):
    with pytest.raises(ValueError):
        rm.check_drawdown(float("nan"))
    assert not kill.is_tripped()
    rm.check_drawdown(0.0)
    assert kill.is_tripped()


def test_drawdown_ignores_corrupt_peak(rm, kill, store):
    store.kv[PEAK_KEY] = "garbage"
    rm.check_drawdown(9_000)
    assert float(store.kv[PEAK_KEY]) == 9_000
    assert not kill.is_tripped()


def test_now_boundary_uses_new_york_day(rm, store):
    before_midnight_ny = datetime(2026, 10, 1, 3, 59, tzinfo=UTC)  # 23:59 NY on 09-30
    rm.check(entry(), ctx(bot_equity=9_000, now=before_midnight_ny))
    assert store.kv[DAILY_LOSS_KEY] == "2026-09-30"
    after = before_midnight_ny + timedelta(minutes=2)
    assert rm.check(entry(), ctx(bot_equity=9_900, start_of_day_equity=9_900, now=after)).action is RiskAction.ALLOW
