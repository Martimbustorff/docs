import base64
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from bot.config import ConfigError, load_settings
from bot.dashboard import charts
from bot.dashboard.app import SECURITY_HEADERS, create_app, redact_secrets, render_static_report
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
from bot.risk import DAILY_LOSS_KEY, PEAK_KEY, KillSwitch
from bot.runner import HEARTBEAT_KEY, SOD_KEY
from bot.store import Store, to_iso

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)  # 11:00 New York; the NY day began at 04:00 UTC
USER, PASSWORD = "admin", "correct horse battery staple"
XSS = '<script>alert("pwned")</script>'
SECRETS = {
    "ALPACA_API_KEY": "PKDONOTRENDER0001",
    "ALPACA_SECRET_KEY": "alpaca-secret-do-not-render",
    "TYPESAFE_API_KEY": "typesafe-secret-do-not-render",
    "TELEGRAM_BOT_TOKEN": "12345:telegram-secret-do-not-render",
    "TELEGRAM_CHAT_ID": "987654321",
}
PAGES = ("/", "/signals", "/backtest", "/jev", "/live-gate")
ROW_IDS = re.compile(r'<th scope="row">(\d+)</th>')


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------- tournament fixture


def metrics(ret, cagr, dd, win, pf, n, sharpe=0.9):
    return {
        "total_return_pct": ret, "cagr_pct": cagr, "max_drawdown_pct": dd, "win_rate": win, "profit_factor": pf,
        "n_trades": n, "avg_win_pct": 4.1, "avg_loss_pct": -1.9, "expectancy_pct": 0.8, "sharpe": sharpe,
        "sortino": 1.3, "exposure_pct": 41.5, "largest_loss_usd": -212.4, "largest_loss_pct": -6.2,
        "max_dd_duration_days": 240, "buy_hold_return_pct": 88.0, "buy_hold_max_dd_pct": 33.7,
    }  # fmt: skip


def regimes(with_null_stress=True):
    stress = {
        "covid_crash_2020": {"start": "2020-02-19", "end": "2020-03-23", "return_pct": -4.2, "max_dd_pct": 6.1,
                             "n_trades": 1, "n_bars": 23},
        "bear_market_2022": {"start": "2022-01-03", "end": "2022-10-12", "return_pct": -2.5, "max_dd_pct": 7.9,
                             "n_trades": 3, "n_bars": 195},
        "tariff_shock_2025": {"start": "2025-02-19", "end": "2025-04-08", "return_pct": None, "max_dd_pct": None,
                              "n_trades": 0, "n_bars": 0},
    }  # fmt: skip
    if with_null_stress:
        stress["crypto_winter_2018"] = None
    return {
        "trend": {
            "bull": {"return_pct": 31.2, "max_dd_pct": 5.5, "n_trades": 9, "n_bars": 1400},
            "bear": {"return_pct": -3.1, "max_dd_pct": 8.2, "n_trades": 2, "n_bars": 300},
            "sideways": {"return_pct": 1.4, "max_dd_pct": 4.0, "n_trades": 4, "n_bars": 700},
        },
        "vol": {
            "high": {"return_pct": -1.0, "max_dd_pct": 9.0, "n_trades": 6, "n_bars": 1200},
            "low": {"return_pct": 27.0, "max_dd_pct": 3.1, "n_trades": 9, "n_bars": 1200},
        },
        "stress": stress,
    }


def run(symbol, strategy, label, passed, score, fail_reasons=(), is_pf=1.8, oos_pf=1.5, neighbors=0.5):
    return {
        "symbol": symbol, "strategy": strategy, "label": label, "params": {"atr_mult": 3},
        "in_sample": metrics(42.0, 5.1, 9.8, 0.47, is_pf, 15),
        "out_of_sample": metrics(12.0, 2.9, 7.7, 0.44, oos_pf, 9),
        "full": metrics(58.0, 3.9, 11.2, 0.45, 1.6, 24), "passed": passed, "fail_reasons": list(fail_reasons),
        "score": score, "neighbors_passing": neighbors, "n_neighbors": 4, "regimes": regimes(),
        "approvals": {"entry_orders": 24, "needing_approval": 18},
    }  # fmt: skip


def weekly(start: str, n: int, base: float = 10_000.0) -> list[list]:
    day = datetime.fromisoformat(start)
    return [[(day + timedelta(weeks=i)).date().isoformat(), round(base * (1 + 0.002 * i), 4)] for i in range(n)]


def tournament_fixture() -> dict:
    rules = {"entry": "Buy at the next open when the close is above its 200-day average.",
             "exit": "Sell at the next open when the close falls below its 200-day average.",
             "stop_loss": "Trailing stop at the highest close minus 3 x ATR(14).", "take_profit": "None.",
             "timeframe": "Daily bars."}  # fmt: skip
    return {
        "generated_at": "2026-09-30T20:00:00Z",
        "data": {
            "SPY": {"first": "2014-09-17", "last": "2026-09-29", "bars": 3026},
            "QQQ": {"first": "2014-09-17", "last": "2026-09-29", "bars": 3026},
            "BTC/USD": {"first": "2014-09-17", "last": "2026-09-29", "bars": 4396},
        },
        "config": {
            "in_sample": ["2014-09-17", "2021-12-31"],
            "out_of_sample": ["2022-01-01", "2026-09-29"],
            "filters": {
                "max_drawdown_pct": 15.0,
                "min_win_rate": 0.4,
                "min_profit_factor": 1.2,
                "min_trades": 8,
                "require_positive_in_sample": True,
                "require_positive_out_of_sample": True,
            },
            "risk": {"capital_usd": 10000, "risk_per_trade_pct": 1.0},
            "slippage_bps": {"stock": 5, "crypto": 10},
            "fee_bps": {"stock": 0, "crypto": 25},
        },  # fmt: skip
        "n_configs": 90,
        "runs": [
            run("SPY", "trend", "trend(atr_mult=3,fast=50,slow=200)", True, 0.8123),
            run("SPY", "breakout", "breakout(atr_mult=2,entry_n=55,exit_n=20)", True, 0.6011),
            run(
                "SPY",
                "meanrev",
                "meanrev(atr_mult=2,entry_rsi=5,max_hold=5)",
                False,
                0.9,
                ["OOS win_rate 0.35 < 0.40", f"IS note {XSS}"],
            ),
            run("QQQ", "momentum", "momentum(atr_mult=3,lookback=120)", True, 0.7002, is_pf=None),
            run("QQQ", "trend", "trend(atr_mult=4,fast=20,slow=100)", False, 0.1, ["IS max_drawdown_pct 18.2 > 15.0"]),
            run(
                "BTC/USD",
                "trend",
                "trend(atr_mult=3,fast=20,slow=100)",
                False,
                0.4,
                ["OOS profit_factor 0.90 < 1.20"],
                oos_pf=None,
            ),
            run(
                "BTC/USD",
                "breakout",
                "breakout(atr_mult=3,entry_n=20,exit_n=10)",
                False,
                None,
                ["OOS total_return_pct -12.0 <= 0"],
            ),
        ],
        "winners": {
            "SPY": {
                "label": "trend(atr_mult=3,fast=50,slow=200)",
                "strategy": "trend",
                "title": "Trend following",
                "params": {"atr_mult": 3, "fast": 50, "slow": 200},
                "rules": rules,
                "score": 0.8123,
            },
            "QQQ": {
                "label": "momentum(atr_mult=3,lookback=120)",
                "strategy": "momentum",
                "params": {"atr_mult": 3, "lookback": 120},
                "rules": rules,
                "score": 0.7002,
            },
            "BTC/USD": None,
        },  # fmt: skip
        "portfolio": {
            "window": "out_of_sample",
            "start": "2022-01-01",
            "end": "2026-09-29",
            "symbols": ["SPY", "QQQ"],
            "metrics": metrics(18.0, 8.0, 12.0, 0.46, None, 17, sharpe=0.7),
            "regimes": regimes(),
            "regime_proxy": "SPY",
            "kill_switch_would_fire": [{"date": "2022-06-13", "reason": "drawdown 15.2% from peak >= 15.0%"}],
            "daily_loss_limit_hits": 3,
            "daily_loss_limit_days": ["2022-05-05", "2022-06-13", "2024-08-05"],
            "approvals": {"entry_orders": 40, "needing_approval": 30},
            "shrunk_entries": 2,
            "blocked_entries": [],
            "trades_by_symbol": {"SPY": 9, "QQQ": 8},
            "equity": weekly("2022-01-03", 150),
        },
        "portfolio_full": {
            "window": "full",
            "start": "2014-09-17",
            "end": "2026-09-29",
            "metrics": metrics(90.0, 5.6, 13.0, 0.45, 2.1, 60),
            "regimes": regimes(with_null_stress=False),
            "kill_switch_would_fire": [],
            "daily_loss_limit_hits": 5,
            "equity": weekly("2014-09-19", 600),
        },
        "winner_equity": {"SPY": weekly("2014-09-19", 600), "QQQ": weekly("2014-09-19", 600, 9_800.0)},
    }


# --------------------------------------------------------------------------- store seed


def answer(name, value, passed, rule):
    top = "yes" if value >= 0.5 else "no"
    return JevAnswer(name=name, type="noul", probabilities={"yes": value, "no": 1 - value}, top=top,
                     gate_value=value, passed=passed, rule=rule)  # fmt: skip


def decision(latency, passed, answers=(), error=None, shadow=False):
    model = None if error else "jev-latest"
    return JevDecision(passed=passed, answers=tuple(answers), model=model, latency_ms=latency, input_tokens=2000,
                       cost_usd=0.000084, error=error, shadow=shadow)  # fmt: skip


def add_signal(store, symbol, day, reason="close crossed above the 200-day average", kind=SignalKind.ENTRY, **fields):
    ts = utc(2026, 9, day, 20)
    signal = Signal(ts=ts, symbol=symbol, strategy="trend", kind=kind, reason=reason, price=500.0,
                    stop_price=480.0 if kind is SignalKind.ENTRY else None, features={"atr": 5.0})  # fmt: skip
    signal_id = store.insert_signal(signal, ts.date())
    if fields:
        store.update_signal(signal_id, **fields)
    return signal_id


def seed(store: Store) -> None:
    passing = [answer("buying_pressure", 0.62, True, "P(yes) >= 0.45")]
    failing = [
        answer("buying_pressure", 0.31, False, "P(yes) >= 0.45"),
        answer("event_risk", 0.2, True, "P(yes) <= 0.50"),
    ]

    sid = add_signal(store, "SPY", 25, gate="passed", risk_action="allow", risk_reason="within limits ($950.00)",
                     status="filled", order_client_id="entry-SPY-aaaaaaaaaaaa", outcome_pnl_pct=0.05)  # fmt: skip
    store.insert_jev(decision(100.0, True, passing), sid, "SPY", ts=utc(2026, 9, 25, 20, 1))
    intent = OrderIntent(symbol="SPY", side=Side.BUY, qty=1.9, ref_price=500.0, purpose=OrderPurpose.ENTRY,
                         reason="trend entry", client_order_id="entry-SPY-aaaaaaaaaaaa", signal_id=sid)  # fmt: skip
    store.upsert_order(
        intent, OrderResult("entry-SPY-aaaaaaaaaaaa", "b-1", "filled", 1.9, 500.25), ts=utc(2026, 9, 26, 13, 31)
    )

    sid = add_signal(store, "QQQ", 26, reason=f"breakout {XSS}", gate="vetoed", risk_action=None, status="blocked",
                     counterfactual_pnl_pct=-0.02)  # fmt: skip
    store.insert_jev(decision(200.0, False, failing), sid, "QQQ", ts=utc(2026, 9, 26, 20, 1))

    sid = add_signal(store, "BTC/USD", 27, gate="error", status="blocked", counterfactual_pnl_pct=0.03)
    store.insert_jev(decision(300.0, False, error="timeout after 3.0s"), sid, "BTC/USD", ts=utc(2026, 9, 27, 20, 1))

    reason = "notional $1,500.00 > approval threshold $1,000.00"
    sid = add_signal(store, "SPY", 28, gate="shadow", risk_action="needs_approval", risk_reason=reason,
                     status="awaiting_approval")  # fmt: skip
    store.insert_jev(decision(400.0, True, failing, shadow=True), sid, "SPY", ts=utc(2026, 9, 28, 20, 1))
    approval = store.create_approval(sid, 1500.0, utc(2026, 9, 30, 20), now=utc(2026, 9, 30, 8))
    store.update_signal(sid, approval_id=approval)

    add_signal(store, "SPY", 29, reason="close fell below the 200-day average", kind=SignalKind.EXIT, gate="n/a",
               status="submitted")  # fmt: skip

    store.insert_trade(Trade("SPY", "trend", utc(2026, 9, 20, 13, 31), 480.0, utc(2026, 9, 29, 14), 500.0, 7.5,
                             150.0, 0.0417, "signal"))  # fmt: skip
    store.insert_trade(Trade("QQQ", "trend", utc(2026, 9, 28, 13, 31), 400.0, utc(2026, 9, 30, 14), 398.0, 10.0,
                             -20.0, -0.005, "stop"))  # fmt: skip
    store.record_equity(100_000.0, 10_050.0, 90_000.0, 0.0, ts=utc(2026, 9, 28, 20))
    store.record_equity(100_100.0, 10_100.0, 90_000.0, 950.0, ts=utc(2026, 9, 29, 20))
    store.record_equity(100_180.0, 10_180.0, 89_000.0, 2_900.0, ts=utc(2026, 9, 30, 14, 45))

    store.put_position(PositionState("SPY", "trend", 1.9, 500.25, utc(2026, 9, 26, 13, 31), 480.0, None, 3, 510.0))
    store.put_position(PositionState("BTC/USD", "breakout", 0.02, 65_000.0, utc(2026, 9, 29, 0, 1), 61_000.0, 72_000.0))
    store.kv_set("last_price:SPY", json.dumps({"price": 505.0, "ts": to_iso(utc(2026, 9, 30, 14, 59))}))
    store.kv_set(HEARTBEAT_KEY, to_iso(NOW - timedelta(seconds=30)))
    store.kv_set(PEAK_KEY, "10200.0")

    store.log_event("error", "broker_error", "Alpaca timed out", data={"attempt": 1}, ts=utc(2026, 9, 30, 14))
    store.log_event("info", "news", f"headline: {XSS}", ts=utc(2026, 9, 30, 14, 30))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path):
    environ = {"BOT_DATA_DIR": str(tmp_path / "var"), "DASHBOARD_USER": USER, "DASHBOARD_PASSWORD": PASSWORD, **SECRETS}
    return load_settings(env_file=None, environ=environ)


@pytest.fixture
def store(settings):
    with Store(settings.db_path, clock=lambda: NOW) as s:
        yield s


@pytest.fixture
def results_path(tmp_path):
    path = tmp_path / "results" / "tournament.json"
    path.parent.mkdir()
    path.write_text(json.dumps(tournament_fixture()))
    return path


@pytest.fixture
def make_client(settings, strategy_cfg, store, tmp_path):
    def make(results_path=tmp_path / "missing.json", *, auth=True, strategy_path=tmp_path / "strategy.md", **kwargs):
        app = create_app(settings, strategy_cfg, store, results_path, strategy_path=strategy_path)
        headers = basic(USER, PASSWORD) if auth else {}
        return TestClient(app, headers=headers, **kwargs)

    return make


@pytest.fixture
def client(make_client, store, results_path):
    seed(store)
    return make_client(results_path)


# --------------------------------------------------------------------------- auth and headers


@pytest.mark.parametrize("path", [*PAGES, "/static/app.css", "/no-such-page"])
def test_every_route_but_healthz_needs_auth(make_client, path):
    response = make_client(auth=False).get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert "<html" not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        basic(USER, "wrong password"),
        basic("root", PASSWORD),
        basic(USER, PASSWORD + " "),
        {"Authorization": "Basic not-base64!!"},
        {"Authorization": "Bearer " + PASSWORD},
        {"Authorization": "Basic " + base64.b64encode(PASSWORD.encode()).decode()},  # no colon
    ],
)
def test_wrong_credentials_are_refused(make_client, headers):
    response = make_client(auth=False).get("/", headers=headers)
    assert response.status_code == 401


def test_correct_credentials_are_accepted(make_client):
    client = make_client()
    for path in (*PAGES, "/static/app.css"):
        assert client.get(path).status_code == 200, path


def test_healthz_is_open_and_trivial(make_client, store):
    store.close()  # no database access: liveness must not depend on SQLite
    response = make_client(auth=False).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_without_a_password_only_loopback_clients_are_served(tmp_path, strategy_cfg, store):
    settings = load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path / "var2")})
    app = create_app(settings, strategy_cfg, store, tmp_path / "missing.json")
    local = "http://127.0.0.1:8080"
    assert TestClient(app, base_url=local, client=("127.0.0.1", 50000)).get("/").status_code == 200
    assert TestClient(app, base_url="http://localhost:8080", client=("::1", 50000)).get("/").status_code == 200
    remote = TestClient(app, base_url=local, client=("203.0.113.9", 50000))
    assert remote.get("/").status_code == 403
    assert remote.get("/", headers=basic("admin", "")).status_code == 403
    assert remote.get("/healthz").status_code == 200


def test_create_app_refuses_a_public_bind_without_a_password(tmp_path, strategy_cfg, store):
    settings = load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path / "v"), "DASHBOARD_HOST": "0.0.0.0"})
    with pytest.raises(ConfigError, match="DASHBOARD_PASSWORD"):
        create_app(settings, strategy_cfg, store)


@pytest.mark.parametrize("auth,path", [(True, "/"), (True, "/static/app.css"), (False, "/"), (False, "/healthz")])
def test_security_headers_on_every_response(make_client, auth, path):
    response = make_client(auth=auth).get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_the_dashboard_is_read_only(client):
    assert client.post("/", data={"x": "1"}).status_code == 405
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_an_internal_error_is_a_plain_500_with_headers(make_client, store):
    client = make_client()
    store.close()
    response = client.get("/")
    assert response.status_code == 500
    assert response.headers["x-frame-options"] == "DENY"
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------- pages


def test_every_page_renders_with_an_empty_db_and_no_results(make_client):
    client = make_client()
    bodies = {path: client.get(path) for path in PAGES}
    assert all(r.status_code == 200 for r in bodies.values())
    assert "No open positions." in bodies["/"].text
    assert "No equity snapshots yet" in bodies["/"].text
    assert "never" in bodies["/"].text  # heartbeat
    assert "No signals yet" in bodies["/signals"].text
    assert "No tournament results yet." in bodies["/backtest"].text
    assert "No Jev calls yet" in bodies["/jev"].text
    assert "No final check has run yet." in bodies["/live-gate"].text
    assert "No kill-switch drill has run yet." in bodies["/live-gate"].text


def test_every_page_renders_with_data(client):
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        assert '<html lang="en">' in response.text
        assert "<script" not in response.text
        assert 'style="' not in response.text  # the CSP forbids inline styles


def test_overview_shows_mode_kill_switch_pnl_positions_and_approvals(client):
    html = client.get("/").text
    assert 'class="badge mode-paper"' in html and ">PAPER<" in html
    assert "&#10003; Off" in html  # kill switch
    assert "30 s ago" in html and "runner heartbeat" in html
    assert "+$80.00" in html  # bot equity change today: 10,180 - 10,100
    assert "-$20.00" in html  # realized today
    assert "+$50.00" in html  # unrealized: 10,180 - 10,000 - (150 - 20)
    assert "4.95%" in html  # SPY: (505 - 480) / 505
    assert "6.15%" in html  # BTC/USD from entry: (65,000 - 61,000) / 65,000
    assert "measured from the entry price" in html
    assert "$1,500.00" in html and "in 5.0 h" in html  # pending approval, expires 20:00 UTC
    assert "Alpaca timed out" in html
    assert "Backtest pace (8.0%/yr)" in html and "Pace less worst drawdown (12.0%)" in html
    assert "0.20%" in html  # drawdown from the 10,200 peak


def test_todays_pnl_starts_from_the_runners_start_of_day_equity(client, store):
    store.kv_set(SOD_KEY, json.dumps({"day": "2026-09-29", "equity": 9_000.0}))  # stale: ignored
    assert "+$80.00" in client.get("/").text
    store.kv_set(SOD_KEY, json.dumps({"day": "2026-09-30", "equity": 10_150.0}))
    assert "+$30.00" in client.get("/").text


def test_overview_shows_a_tripped_kill_switch_and_the_daily_block(client, settings, store):
    KillSwitch(settings).trip(f"manual stop {XSS}", source="telegram")
    store.kv_set(DAILY_LOSS_KEY, "2026-09-30")
    html = client.get("/").text
    assert "Kill switch tripped" in html and "Tripped" in html
    assert "manual stop &lt;script&gt;" in html
    assert "entries blocked until tomorrow" in html


def test_overview_flags_a_stale_heartbeat(client, store):
    store.kv_set(HEARTBEAT_KEY, to_iso(NOW - timedelta(minutes=20)))
    html = client.get("/").text
    assert "20 min ago" in html and "The bot may be down" in html


def test_a_mode_mismatch_is_shown_without_settings_values(tmp_path, strategy_cfg, store):
    environ = {"BOT_DATA_DIR": str(tmp_path / "v"), "DASHBOARD_PASSWORD": PASSWORD, "ALPACA_PAPER": "false"}
    settings = load_settings(env_file=None, environ=environ)
    client = TestClient(
        create_app(settings, strategy_cfg, store, tmp_path / "none.json"), headers=basic("admin", PASSWORD)
    )
    html = client.get("/").text
    assert "Alpaca LIVE endpoint" in html
    assert "the live-trading guard blocks every order" in html


def test_signals_page_shows_jev_probabilities_risk_and_results(client):
    html = client.get("/signals").text
    assert "buying_pressure: <strong>0.62</strong>" in html
    assert "&#10007; fail" in html and "&#10003; pass" in html
    assert "error: timeout after 3.0s" in html
    assert "needs_approval" in html and "pending" in html  # risk action and approval status
    assert "filled" in html and "1.9 @ $500.25" in html  # order status and fill
    assert "+5.00%" in html  # realized outcome
    assert "-2.00%" in html and "counterfactual" in html


def test_signals_pagination_and_filters(make_client, store):
    for i in range(120):
        day = datetime(2025, 1, 1, 21, tzinfo=UTC) + timedelta(days=i)
        store.insert_signal(Signal(day, "SPY", "trend", SignalKind.ENTRY, "r", 500.0, 480.0), day.date())
    for i in range(5):
        day = datetime(2024, 1, 1, 21, tzinfo=UTC) + timedelta(days=i)
        sid = store.insert_signal(Signal(day, "QQQ", "trend", SignalKind.ENTRY, "r", 400.0, 390.0), day.date())
        store.update_signal(sid, gate="vetoed")
    client = make_client()

    first = client.get("/signals").text
    assert "125 signals" in first and "Page 1 of 3" in first
    assert len(ROW_IDS.findall(first)) == 50
    assert 'href="?page=2"' in first and 'rel="prev"' not in first
    last = client.get("/signals", params={"page": 3}).text
    ids = ROW_IDS.findall(last)
    assert len(ids) == 25 and "Page 3 of 3" in last and 'rel="next"' not in last
    assert client.get("/signals", params={"page": 99}).text.count("Page 3 of 3") == 1  # clamped
    assert "Page 1 of 3" in client.get("/signals", params={"page": "abc"}).text

    qqq = client.get("/signals", params={"symbol": "QQQ"}).text
    assert "5 signals match" in qqq and len(ROW_IDS.findall(qqq)) == 5
    vetoed = client.get("/signals", params={"gate": "vetoed", "symbol": "SPY"}).text
    assert "No signals match these filters" in vetoed
    page2 = client.get("/signals", params={"symbol": "SPY", "page": 2}).text
    assert 'href="?symbol=SPY&amp;page=1"' in page2 and 'href="?symbol=SPY&amp;page=3"' in page2
    unknown = client.get("/signals", params={"symbol": XSS, "gate": "bogus"}).text
    assert "125 signals." in unknown and XSS not in unknown


def test_backtest_page_renders_the_tournament(client):
    html = client.get("/backtest").text
    assert "<strong>90</strong> configurations tested" in html
    assert "2014-09-17 to 2021-12-31" in html and "2022-01-01 to 2026-09-29" in html
    assert "min win rate" in html and "min trades" in html
    assert html.count('<tr class="winner">') == 2 and html.count('<tr class="survivor">') == 1
    assert "SPY: 2 of 3 survived" in html and "BTC/USD: 0 of 2 survived" in html
    assert "OOS win_rate 0.35 &lt; 0.40" in html
    assert "No strategy survived for BTC/USD, so it stays disabled." in html
    assert "Buy at the next open when the close is above its 200-day average." in html
    assert "∞" in html  # null profit factor
    assert 'aria-labelledby="winner-equity-title winner-equity-desc"' in html
    assert "No data in this window" in html  # null stress entries
    assert "2022-06-13" in html and "drawdown 15.2% from peak" in html
    assert "<strong>3</strong> (2022-05-05, 2022-06-13, 2024-08-05)" in html  # daily loss limit days
    assert "18 of 24 entry orders (75.0%)" in html  # winner approvals
    assert "30 of 40 entry orders (75.0%) would have waited" in html  # portfolio approvals
    assert "SPY 9, QQQ 8" in html and "Trend following" in html and "50.0% of 4" in html
    assert "Portfolio, full period" in html and 'aria-labelledby="portfolio-full-title' in html
    assert "The kill switch would not have fired in this window." in html  # the full-period run
    assert "labelled from SPY" in html
    assert "Sharpe" in html and "+8.00%" in html  # portfolio metrics
    assert "90 configurations were tested" in html  # caveat


def test_backtest_page_handles_a_corrupt_or_partial_file(make_client, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    html = make_client(broken).get("/backtest").text
    assert "could not be read" in html
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"generated_at": "2026-09-30T20:00:00Z", "runs": [{"symbol": "SPY"}], "winners": {}}))
    response = make_client(partial).get("/backtest")
    assert response.status_code == 200 and "No portfolio backtest recorded" in response.text
    listing = tmp_path / "list.json"
    listing.write_text("[1, 2]")
    assert "not a JSON object" in make_client(listing).get("/backtest").text


def test_jev_page_reports_latency_cost_and_outcomes(client):
    html = client.get("/jev").text
    assert '<p class="stat">4</p>' in html  # decisions (the timeout counts)
    assert "250 ms" in html and "385 ms" in html  # p50, p95 of 100/200/300/400
    assert "$0.000084" in html and "$0.000336" in html  # per decision, total
    assert 'aria-labelledby="latency-title latency-desc"' in html and 'class="bar"' in html
    assert "Vetoed by Jev" in html and "-2.00%" in html  # counterfactual of the veto
    assert "Shadow mode, Jev said no (not enforced)" in html
    assert "buying_pressure" in html and "P(yes) &gt;= 0.45" in html


def live_gate_record(ts: datetime) -> dict:
    """Shaped like bot.live_gate.final_check's var/live_gate.json."""
    return {
        "passed": False, "ts": to_iso(ts), "summary": "1 of 2 required checks failed.",
        "checks": [
            {"name": "signal_match_rate", "passed": True, "required": True, "detail": "19 of 20 signals match.",
             "value": 0.9523809523809523, "threshold": 0.9},
            {"name": "return_gap_pct", "passed": False, "required": True, "detail": "Paper trails the replay.",
             "value": 4.2, "threshold": 3.0},
            {"name": "fill_slippage_bps", "passed": True, "required": False, "detail": "Advisory.", "value": None,
             "threshold": None},
        ],
        "blow_up": [{"title": "A gap through the stop", "detail": "while the bot is down."},
                    {"title": "Crypto weekend crash", "detail": f"BTC can fall 20% overnight {XSS}"}],
    }  # fmt: skip


def drill_record(settings, ts: datetime) -> dict:
    """Shaped like bot.live_gate.kill_switch_drill's var/kill_switch_drill.json."""
    steps = [{"name": "orders_cancelled", "ok": True, "detail": "0 open orders"},
             {"name": "alert_sent", "ok": False, "detail": "no alert recorded"}]  # fmt: skip
    return {"passed": False, "ts": to_iso(ts), "mode": "sim", "state_dir": str(settings.data_dir / "drill" / "x"),
            "steps": steps}  # fmt: skip


def test_live_gate_page_lists_checks_and_blow_up_risks(make_client, settings):
    settings.live_gate_path.write_text(json.dumps(live_gate_record(NOW - timedelta(days=2))))
    settings.drill_path.write_text(json.dumps(drill_record(settings, NOW - timedelta(days=45))))
    html = make_client().get("/live-gate").text
    assert "signal match rate" in html and "19 of 20 signals match." in html and ">0.9524<" in html
    assert 'fill slippage bps <span class="muted">(advisory)</span>' in html
    assert "1 of 2 required checks failed." in html
    assert html.count("&#10007; fail") == 4  # the gate, return_gap_pct, the drill and alert_sent
    assert "Fresh: younger than 7 days." in html
    assert "Older than 30 days" in html  # the drill is 45 days old
    assert "Ran against: sim." in html and "no alert recorded" in html
    assert "A gap through the stop: while the bot is down." in html
    assert "Crypto weekend crash: BTC can fall 20% overnight &lt;script&gt;" in html
    assert "From var/live_gate.json." in html
    assert str(settings.data_dir) not in html  # the drill's state_dir is not rendered


def test_live_gate_falls_back_to_the_strategy_md_risk_list(make_client, tmp_path):
    strategy = tmp_path / "strategy.md"
    strategy.write_text("## Final check\n\n### WHAT COULD BLOW UP THIS ACCOUNT?\n\n- Overnight gaps.\n"
                        "  continued on a second line\n1. Exchange outage.\n\nThat is all.\n\n- unrelated bullet\n"
                        "<!-- END FINAL_CHECK -->\n")  # fmt: skip
    html = make_client(strategy_path=strategy).get("/live-gate").text
    assert "<li>Overnight gaps.</li>" in html and "<li>Exchange outage.</li>" in html
    assert "unrelated bullet" not in html
    assert "the Final check section of strategy.md" in html


def test_unreadable_gate_files_are_reported(make_client, settings):
    settings.live_gate_path.write_text("{oops")
    html = make_client().get("/live-gate").text
    assert "live_gate.json could not be read" in html


# --------------------------------------------------------------------------- untrusted text and secrets


def test_untrusted_text_is_escaped_everywhere(client, settings):
    KillSwitch(settings).trip(XSS, source="test")
    for path in PAGES:
        html = client.get(path).text
        assert XSS not in html, path
        assert "<script" not in html, path
    assert "&lt;script&gt;" in client.get("/signals").text  # signal reason
    assert "headline: &lt;script&gt;" in client.get("/").text  # event message
    assert "IS note &lt;script&gt;" in client.get("/backtest").text  # tournament fail reason


def test_no_secret_or_settings_value_is_rendered(client, settings):
    settings.live_gate_path.write_text(json.dumps(live_gate_record(NOW)))
    settings.drill_path.write_text(json.dumps(drill_record(settings, NOW)))
    for path in (*PAGES, "/healthz"):
        html = client.get(path).text
        for secret in (*SECRETS.values(), PASSWORD, str(settings.data_dir)):
            assert secret not in html, (path, secret)


def test_a_secret_leaked_into_stored_text_is_redacted(client, store):
    store.log_event("error", "jev_error", f"401 Unauthorized for key {SECRETS['TYPESAFE_API_KEY']}")
    html = client.get("/").text
    assert SECRETS["TYPESAFE_API_KEY"] not in html
    assert "401 Unauthorized for key ***" in html


def test_redact_secrets_covers_the_html_escaped_form(tmp_path):
    settings = load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path), "DASHBOARD_PASSWORD": "p&ss<word>"})
    assert redact_secrets("a p&amp;ss&lt;word&gt; b p&ss<word>", settings) == "a *** b ***"


# --------------------------------------------------------------------------- static report


def test_static_report_is_self_contained():
    html = render_static_report(tournament_fixture())
    assert html.startswith("<!doctype html>")
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html and XSS not in html
    assert "<link" not in html and "url(" not in html and "@import" not in html
    assert "<style>" in html and "--series-1" in html and "prefers-color-scheme: dark" in html
    assert "<svg" in html and "Tournament report" in html
    assert "No strategy survived for BTC/USD" in html


def test_static_report_of_minimal_results():
    html = render_static_report({})
    assert "No symbols in the results." in html and "No winners" in html


# --------------------------------------------------------------------------- charts


def test_charts_handle_empty_and_single_point_series():
    empty = charts.line_chart([], title="Equity", desc="nothing", y_label="USD", empty_message="Nothing yet")
    assert "Nothing yet" in empty and "<path" not in empty
    one = charts.line_chart([(datetime(2026, 9, 30, tzinfo=UTC), 10_000.0)], title="Equity", desc="d", y_label="USD")
    assert one.count("<circle") == 1 and "<path" not in one and "2026-09-30" in one
    nan = charts.line_chart([(1, float("nan")), (2, None)], title="t", desc="d", y_label="y")
    assert "No data yet" in nan


def test_line_chart_is_accessible_and_escaped():
    svg = charts.multi_line_chart(
        {"A <b>": [(1, 1.0), (2, 3.0)], "B": [(1, 2.0), (3, 2.5)]},
        title='Title "<x>"', desc="Desc & more", y_label="Return (%)", x_label="Day",
    )  # fmt: skip
    assert svg.startswith('<svg class="chart"') and 'role="img"' in svg
    assert "<title id=" in svg and "<desc id=" in svg
    assert "Title &quot;&lt;x&gt;&quot;" in svg and "Desc &amp; more" in svg and "A &lt;b&gt;" in svg
    assert 'class="legend"' in svg and 'class="line s1"' in svg and 'class="line s2"' in svg
    assert "Return (%)" in svg and "Day" in svg
    assert "xmlns" not in svg and "http" not in svg and "style=" not in svg
    single = charts.line_chart([(1, 1.0), (2, 2.0)], title="t", desc="d", y_label="y")
    assert 'class="legend"' not in single


def test_line_chart_thins_long_series_and_rejects_too_many():
    svg = charts.line_chart([(i, float(i)) for i in range(5000)], title="t", desc="d", y_label="y", max_points=100)
    path = re.search(r' d="([^"]+)"', svg).group(1)
    assert path.count("L") <= 101
    with pytest.raises(ValueError):
        charts.multi_line_chart({str(i): [(0, 1.0)] for i in range(7)}, title="t", desc="d", y_label="y")


def test_bar_histogram_counts_and_edges():
    edges, counts = charts.histogram_bins([100, 120, 130, 900], bins=4)
    assert counts == [3, 0, 0, 1] and edges[0] == 100 and edges[-1] == 900
    assert charts.histogram_bins([5, 5, 5], bins=4) == ([4.5, 5.5], [3])
    svg = charts.bar_histogram([100, 120, 130, 900], title="Latency", desc="d", x_label="ms", bins=4)
    assert svg.count('class="bar"') == 2 and "<title>" in svg
    assert "No calls" in charts.bar_histogram([], title="t", desc="d", x_label="x", empty_message="No calls")


def test_nice_ticks_cover_the_range():
    assert charts.nice_ticks(9_800, 10_450) == [9_800, 10_000, 10_200, 10_400, 10_600]
    assert charts.nice_ticks(0, 7, integer=True) == [0, 2, 4, 6, 8]
    ticks = charts.nice_ticks(5, 5)
    assert ticks[0] < 5 < ticks[-1]


def test_without_a_password_a_rebound_host_name_is_refused(tmp_path, strategy_cfg, store):
    """DNS rebinding: the browser connects from loopback but sends the attacker's host name."""
    settings = load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path / "var3")})
    app = create_app(settings, strategy_cfg, store, tmp_path / "missing.json")
    rebound = TestClient(app, base_url="http://evil.example:8080", client=("127.0.0.1", 50000))
    assert rebound.get("/").status_code == 403
    assert rebound.get("/signals").status_code == 403
    assert rebound.get("/healthz").status_code == 200
