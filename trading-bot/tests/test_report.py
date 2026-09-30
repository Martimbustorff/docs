import json
from datetime import date, datetime, timedelta, timezone

import pytest

from bot.config import AssetRule, load_settings
from bot.models import JevDecision, OrderIntent, OrderPurpose, OrderResult, PositionState, Side, Trade
from bot.report import daily_report, report_path
from bot.store import Store

UTC = timezone.utc
DAY = date(2026, 9, 29)  # New York day: 2026-09-29 04:00 UTC to 2026-09-30 04:00 UTC (EDT)
SECRET = "sk-test-secret-do-not-render"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def section(text: str, title: str) -> str:
    """The body of the `## title` section."""
    body = text.split(f"## {title}\n", 1)[1]
    return body.split("\n## ", 1)[0]


@pytest.fixture
def settings(tmp_path):
    return load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path / "var"), "TYPESAFE_API_KEY": SECRET})


@pytest.fixture
def store(settings):
    with Store(settings.db_path, clock=lambda: utc(2026, 9, 29, 21, 15)) as s:
        yield s


def trade(symbol, exit_ts, pnl, pnl_pct, exit_reason="signal") -> Trade:
    return Trade(
        symbol=symbol, strategy="trend", entry_ts=exit_ts - timedelta(days=4), entry_price=100.0,
        exit_ts=exit_ts, exit_price=100.0 * (1 + pnl_pct), qty=10.0, pnl=pnl, pnl_pct=pnl_pct,
        exit_reason=exit_reason,
    )  # fmt: skip


def decision(latency_ms, cost, passed=True, error=None) -> JevDecision:
    return JevDecision(
        passed=passed, answers=(), model="jev-latest", latency_ms=latency_ms, input_tokens=2000,
        cost_usd=cost, error=error,
    )  # fmt: skip


def seed(store: Store, settings) -> None:
    store.insert_trade(trade("SPY", utc(2026, 9, 29, 14), 150.0, 0.05))
    store.insert_trade(trade("BTC/USD", utc(2026, 9, 30, 2), -80.0, -0.04, "stop"))  # 22:00 NY on the 29th
    store.insert_trade(trade("QQQ", utc(2026, 9, 28, 15), 40.0, 0.02))
    store.insert_trade(trade("QQQ", utc(2026, 9, 25, 15), -30.0, -0.06))
    store.insert_trade(trade("SPY", utc(2026, 9, 30, 15), -500.0, -0.10))  # next NY day: excluded

    store.put_position(
        PositionState(symbol="SPY", strategy="trend", qty=5.5, entry_price=500.0, entry_ts=utc(2026, 9, 29, 13, 31),
                      stop_price=480.0, take_profit=None, bars_held=0, highest_close=500.0)
    )  # fmt: skip
    store.put_position(
        PositionState(symbol="BTC/USD", strategy="breakout", qty=0.0123, entry_price=65000.0,
                      entry_ts=utc(2026, 9, 28, 0, 1), stop_price=61000.0, take_profit=72000.0, bars_held=1)
    )  # fmt: skip

    # Realized by 20:00 UTC on the 29th: 150 + 40 - 30 = 160, so 10,185 means +25 unrealized.
    store.record_equity(100_000.0, 10_185.0, 90_000.0, 2_775.0, ts=utc(2026, 9, 29, 20))
    store.record_equity(100_500.0, 10_999.0, 90_000.0, 2_775.0, ts=utc(2026, 9, 30, 12))  # after the day

    store.insert_jev(decision(100.0, 0.0001), None, "SPY", ts=utc(2026, 9, 29, 20, 5))
    store.insert_jev(decision(300.0, 0.0003, passed=False), None, "QQQ", ts=utc(2026, 9, 29, 20, 5))
    store.insert_jev(decision(500.0, 0.0, passed=False, error="timeout"), None, "SPY", ts=utc(2026, 9, 28, 20, 5))
    uncalled = JevDecision(passed=True, answers=(), model=None, latency_ms=0.0, input_tokens=0, cost_usd=0.0)
    store.insert_jev(uncalled, None, "SPY", ts=utc(2026, 9, 29, 20, 6))  # mode off: not counted

    approved = store.create_approval(1, 1500.0, utc(2026, 9, 30, 3), now=utc(2026, 9, 29, 15))
    store.set_approval(approved, "approved", now=utc(2026, 9, 29, 15, 5))
    store.create_approval(2, 1200.0, utc(2026, 9, 30, 9), now=utc(2026, 9, 29, 21))
    old = store.create_approval(3, 1100.0, utc(2026, 9, 29, 3), now=utc(2026, 9, 28, 15))
    store.set_approval(old, "expired", now=utc(2026, 9, 29, 3))

    store.log_event("error", "tick", "broker timeout", ts=utc(2026, 9, 29, 15))
    store.log_event("critical", "tick", "late failure", ts=utc(2026, 9, 30, 3))  # 23:00 NY on the 29th
    store.log_event("info", "fill", "filled", ts=utc(2026, 9, 29, 16))
    store.log_event("error", "tick", "next day", ts=utc(2026, 9, 30, 5))

    intent = OrderIntent("SPY", Side.BUY, 5.5, 500.0, OrderPurpose.ENTRY, "trend entry", "entry-SPY-1")
    store.upsert_order(intent, OrderResult("entry-SPY-1", "b-1", "filled", 5.5, 500.0), ts=utc(2026, 9, 29, 13, 31))

    settings.kill_switch_path.write_text(
        json.dumps({"reason": "max orders\nper day", "source": "risk", "ts": "2026-09-29T19:00:00+00:00"})
    )


def pinned_assets(cfg, enabled=()):
    """A copy of cfg whose assets don't depend on what the last tournament wrote to strategy.md."""
    cfg = cfg.model_copy(deep=True)
    cfg.assets = {s: AssetRule(enabled=s in enabled, strategy="trend") for s in ("SPY", "QQQ", "BTC/USD")}
    return cfg


def test_report_with_seeded_data(store, settings, strategy_cfg):
    cfg = pinned_assets(strategy_cfg, enabled=("SPY",))
    seed(store, settings)

    text = daily_report(store, settings, cfg, DAY)

    assert text.startswith("# Daily report for 2026-09-29\n")
    assert "Generated 2026-09-29 21:15 UTC" in text
    assert "- Mode: **PAPER** (Alpaca paper account)" in text
    assert "- Enabled assets: SPY (trend)" in text

    trades = section(text, "Trades today")
    assert "2 closed: 1 won, 1 lost." in trades
    assert "| SPY | trend | 2026-09-25 10:00 | 2026-09-29 10:00 | 10 | $100.00 | $105.00 | +$150.00 | +5.00% | signal |" in trades
    assert "| BTC/USD | trend |" in trades and "2026-09-29 22:00 | 10 | $100.00 | $96.00 | -$80.00 | -4.00% | stop |" in trades
    assert "QQQ" not in trades and "-$500.00" not in trades

    positions = section(text, "Open positions (now)")
    assert "| SPY | trend | 5.5 | $500.00 | $480.00 | none | 2026-09-29 09:31 | 0 |" in positions
    assert "| BTC/USD | breakout | 0.0123 | $65,000.00 | $61,000.00 | $72,000.00 | 2026-09-27 20:01 | 1 |" in positions

    pnl = section(text, "P&L")
    assert "- Realized today: +$70.00" in pnl
    assert "- Realized, cumulative: +$80.00" in pnl
    assert "- Unrealized: +$25.00 (as of 2026-09-29 16:00 NY)" in pnl
    assert "- Bot equity: $10,185.00, exposure $2,775.00, account equity $100,000.00" in pnl
    assert "- Win rate, cumulative: 50.0% (2 of 4 trades)" in pnl
    assert (
        "- Largest loss: -$80.00, -4.00% (BTC/USD, closed 2026-09-29); "
        "largest loss by return: -6.00% (QQQ, closed 2026-09-25)"
    ) in pnl

    jev = section(text, "Jev")
    assert "| Today | 2 | 200 ms | $0.000200 | $0.000400 |" in jev
    assert "| Cumulative | 3 | 300 ms | $0.000133 | $0.000400 |" in jev

    activity = section(text, "Risk and activity")
    assert "- Entry orders: 1 (daily limit 10)" in activity
    assert "- Jev vetoes: 1\n" in activity
    assert "- Jev errors: 0" in activity
    assert "- Approvals requested: 2 (approved 1, rejected 0, expired 0, pending 1)" in activity
    assert "- Errors logged: 2" in activity
    assert (
        '- Kill switch (now): **TRIPPED**: "max orders per day" (source: risk, at 2026-09-29 15:00 NY)'
    ) in activity

    assert SECRET not in text
    saved = report_path(settings, DAY)
    assert saved == settings.reports_dir / "2026-09-29.md"
    assert saved.read_text(encoding="utf-8") == text


def test_report_empty_day(store, settings, strategy_cfg):
    text = daily_report(store, settings, pinned_assets(strategy_cfg), "2026-09-29")

    assert "No trades today." in section(text, "Trades today")
    assert "No open positions." in section(text, "Open positions (now)")
    pnl = section(text, "P&L")
    assert "- Realized today: $0.00" in pnl
    assert "- Realized, cumulative: $0.00" in pnl
    assert "- Unrealized: n/a (no equity snapshot yet)" in pnl
    assert "- Win rate, cumulative: n/a (no closed trades)" in pnl
    assert "- Largest loss: none" in pnl
    assert "| Today | 0 | n/a | n/a | $0.000000 |" in text
    assert "- Enabled assets: none" in text
    assert "- Jev vetoes: 0" in text
    assert "- Approvals requested: 0 (approved 0, rejected 0, expired 0, pending 0)" in text
    assert "- Kill switch (now): off" in text
    assert report_path(settings, DAY).read_text(encoding="utf-8") == text


def test_report_day_from_aware_datetime_and_forced_kill_switch(tmp_path, strategy_cfg):
    settings = load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path / "var"), "KILL_SWITCH": "1"})
    settings.kill_switch_path.write_text("{not json")
    with Store(settings.db_path) as store:
        store.insert_trade(trade("SPY", utc(2026, 9, 30, 2), 12.0, 0.012))
        store.log_event("info", "fill", "x", data={"shadow": True})
        text = daily_report(store, settings, strategy_cfg, utc(2026, 9, 30, 3, 30))  # 23:30 NY on the 29th

    assert text.startswith("# Daily report for 2026-09-29\n")
    assert "+$12.00" in section(text, "Trades today")
    assert "- Largest loss: none" in text
    assert "**TRIPPED**: forced on by KILL_SWITCH=1; switch file present (unreadable)" in text
    assert report_path(settings, DAY).exists()


def test_report_shows_shadow_vetoes_and_jev_errors(store, settings, strategy_cfg):
    shadow = JevDecision(passed=False, answers=(), model="jev-latest", latency_ms=50.0, input_tokens=10,
                         cost_usd=0.00001, shadow=True)  # fmt: skip
    store.insert_jev(shadow, None, "SPY", ts=utc(2026, 9, 29, 20))
    store.insert_jev(decision(3000.0, 0.0, passed=False, error="timeout"), None, "SPY", ts=utc(2026, 9, 29, 20))
    text = daily_report(store, settings, strategy_cfg, DAY)
    assert "- Jev vetoes: 0 (plus 1 in shadow mode, not enforced)" in text
    assert "- Jev errors: 1" in text
