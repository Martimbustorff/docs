"""Read-only strategy-tester dashboard: FastAPI + Jinja2, server-rendered inline SVG, no JavaScript.

Every route except /healthz needs HTTP Basic auth (DASHBOARD_USER / DASHBOARD_PASSWORD). With no
password set, only loopback clients are served and `create_app` refuses a non-loopback bind.
Pages only read: the SQLite store, var/live_gate.json, var/kill_switch_drill.json, the kill-switch
file, results/tournament.json and the FINAL_CHECK section of strategy.md. Text from those sources
(signal reasons, event messages, kill-switch reasons, tournament strings) is untrusted and always
autoescaped. No setting other than the mode flags is ever rendered.

Runner state read from kv: its heartbeat and start-of-day equity (the runner's own keys), and
optionally `last_price:<symbol>` = JSON `{"price": float, "ts": ISO-8601}` for an open position's
distance to its stop. Without a price the distance is measured from the entry price.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import math
import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup
from pydantic import SecretStr

from bot.broker import LIVE_GATE_MAX_AGE, assert_trading_allowed
from bot.config import ROOT, STRATEGY_PATH, ConfigError, Settings, StrategyConfig
from bot.dashboard import charts
from bot.risk import DAILY_LOSS_KEY, ERRORS_KEY, PEAK_KEY, RESET_TS_KEY, KillSwitch
from bot.runner import HEARTBEAT_KEY, SOD_KEY
from bot.store import SIGNAL_GATES, Store, ny_day_bounds, parse_ts
from bot.timeutil import NY, ny_trading_day

log = logging.getLogger(__name__)

RESULTS_PATH = ROOT / "results" / "tournament.json"
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

LAST_PRICE_PREFIX = "last_price:"
EQUITY_INTERVAL = timedelta(minutes=15)  # how often the runner records equity
PAGE_SIZE = 50
SIGNAL_SCAN_LIMIT = 100_000  # newest signals read for filtering and Jev statistics
EVENTS_SHOWN = 15
VETOES_SHOWN = 20
EXPECTATION_POINTS = 60
BLOW_UP_HEADING = "WHAT COULD BLOW UP THIS ACCOUNT?"
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'none'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
}
NAV = (
    ("/", "Overview"),
    ("/signals", "Signals"),
    ("/backtest", "Backtest"),
    ("/jev", "Jev"),
    ("/live-gate", "Live gate"),
)

Row = dict[str, Any]


# --------------------------------------------------------------------------- formatting


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def fmt_usd(value: Any, signed: bool = False) -> str:
    number = _finite(value)
    if number is None:
        return "n/a"
    number = round(number, 2)
    sign = "-" if number < 0 else ("+" if signed and number > 0 else "")
    return f"{sign}${abs(number):,.2f}"


def fmt_pct(value: Any, signed: bool = False, digits: int = 2) -> str:
    """`value` is already in percent (12.5 means 12.5%)."""
    number = _finite(value)
    if number is None:
        return "n/a"
    return f"{number:+,.{digits}f}%" if signed else f"{number:,.{digits}f}%"


def fmt_frac(value: Any) -> str:
    """A fraction (0.05 = 5%), like `Trade.pnl_pct`, as a signed percentage."""
    number = _finite(value)
    return "n/a" if number is None else fmt_pct(number * 100, signed=True)


def fmt_rate(value: Any) -> str:
    """A 0-1 rate (win rate, share) as a percentage."""
    number = _finite(value)
    return "n/a" if number is None else f"{number * 100:.1f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{number:,.{digits}f}"


def fmt_int(value: Any) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{int(number):,}"


def fmt_price(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "n/a"
    return f"${number:,.2f}" if abs(number) >= 1 else f"${number:.4f}"


def fmt_qty(value: Any) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{number:,.6f}".rstrip("0").rstrip(".")


def fmt_cost(value: Any) -> str:
    """Jev costs are fractions of a cent."""
    number = _finite(value)
    return "n/a" if number is None else f"${number:,.6f}"


def _whole_usd(value: float) -> str:
    return f"${value:,.0f}"


def fmt_ms(value: Any) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{number:,.0f} ms"


def fmt_ts(value: Any) -> str:
    """New York time, minute precision; unparseable text is shown as written."""
    if value is None or value == "":
        return "n/a"
    try:
        ts = parse_ts(value) if isinstance(value, str) else value
        return ts.astimezone(NY).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, AttributeError):
        return str(value)


def fmt_age(delta: timedelta | None) -> str:
    if delta is None:
        return "n/a"
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future (clock skew?)"
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def _signed_class(value: Any) -> str:
    number = _finite(value)
    return "" if number is None or number == 0 else ("pos" if number > 0 else "neg")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_ts(value)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks; `q` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100
    lo = math.floor(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _mean(values: Iterable[float | None]) -> float | None:
    numbers = [v for v in values if v is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# --------------------------------------------------------------------------- templates


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        usd=fmt_usd, pct=fmt_pct, frac=fmt_frac, rate=fmt_rate, num=fmt_num, int=fmt_int, price=fmt_price,
        qty=fmt_qty, cost=fmt_cost, ms=fmt_ms, ts=fmt_ts, signed_class=_signed_class,
    )  # fmt: skip
    return env


_ENV = _environment()


# --------------------------------------------------------------------------- auth


def _password(settings: Settings) -> str:
    secret = settings.dashboard_password
    return secret.get_secret_value() if secret is not None else ""


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def _basic_credentials(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


def _credentials_match(given: tuple[str, str], user: str, password: str) -> bool:
    # Both comparisons always run, so the response time does not reveal which one failed.
    user_ok = secrets.compare_digest(given[0].encode(), user.encode())
    password_ok = secrets.compare_digest(given[1].encode(), password.encode())
    return user_ok and password_ok


def redact_secrets(html: str, settings: Settings) -> str:
    """Defense in depth: no secret value (raw or HTML-escaped) survives into a page, even if one
    leaked into a stored event message or error text."""
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        secret = value.get_secret_value() if isinstance(value, SecretStr) else ""
        if len(secret) >= 4:  # a tiny value would blank out ordinary text
            html = html.replace(secret, "***").replace(escape(secret), "***")
    return html


def _auth_refusal(request: Request, settings: Settings) -> Response | None:
    """None when the request may proceed, else the 401/403 response."""
    if request.url.path == "/healthz":
        return None
    password = _password(settings)
    if not password:
        if is_loopback(request.client.host if request.client else None):
            return None
        return PlainTextResponse("DASHBOARD_PASSWORD is not set, so only loopback clients are served.", 403)
    given = _basic_credentials(request.headers.get("authorization"))
    if given is not None and _credentials_match(given, settings.dashboard_user, password):
        return None
    return PlainTextResponse(
        "Authentication required.", 401, headers={"WWW-Authenticate": 'Basic realm="trading-bot", charset="UTF-8"'}
    )


# --------------------------------------------------------------------------- shared state


def _mode(settings: Settings, cfg: StrategyConfig) -> dict[str, Any]:
    """Mode flags only, plus what the live-trading guard currently allows."""
    try:
        assert_trading_allowed(settings, settings.live_gate_path, risk_increasing=False)
    except ConfigError:
        guard, guard_note = "bad", "Mode flags disagree: the live-trading guard blocks every order."
    else:
        try:
            assert_trading_allowed(settings, settings.live_gate_path)
        except ConfigError:
            guard = "warn"
            guard_note = "The live gate is missing, failed or older than 7 days: entries are blocked, exits still run."
        else:
            guard, guard_note = "ok", "The live-trading guard allows trading."
    return {
        "label": settings.trading_mode.upper(),
        "css": settings.trading_mode,
        "endpoint": "Alpaca paper endpoint" if settings.alpaca_paper else "Alpaca LIVE endpoint",
        "jev_mode": cfg.jev.mode,
        "guard": guard,
        "guard_note": guard_note,
    }


def _kill_state(settings: Settings) -> dict[str, Any]:
    kill = KillSwitch(settings)  # no store: this instance never writes
    status = kill.status() or {}
    return {
        "tripped": kill.is_tripped(),
        "reason": _text(status.get("reason")),
        "source": _text(status.get("source")),
        "ts": status.get("ts"),
    }


def _read_json(path: Path) -> tuple[Any, str | None]:
    """(data, None), (None, None) when the file does not exist, or (None, error)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None, f"{path.name} could not be read ({type(exc).__name__})"


def _all_signals(store: Store) -> list[Row]:
    return store.signal_with_jev(limit=SIGNAL_SCAN_LIMIT)


def _answers(row: Row) -> list[Mapping[str, Any]]:
    return [a for a in _list(row.get("jev_answers")) if isinstance(a, Mapping)]


def _jev_called(row: Row) -> bool:
    return row.get("jev_model") is not None or row.get("jev_error") is not None


def _jev_said_no(row: Row) -> bool:
    """Jev's own verdict, which a shadow decision records without enforcing (as the store counts it)."""
    answers = _answers(row)
    return row.get("jev_passed") is False or not answers or any(a.get("passed") is not True for a in answers)


def _result(row: Row) -> tuple[float | None, bool]:
    """(P&L fraction, is_counterfactual): the realized outcome, else the runner's counterfactual."""
    realized = _finite(row.get("outcome_pnl_pct"))
    if realized is not None:
        return realized, False
    return _finite(row.get("counterfactual_pnl_pct")), True


# --------------------------------------------------------------------------- overview


def _heartbeat(store: Store, cfg: StrategyConfig, now: datetime) -> dict[str, Any]:
    """The runner's heartbeat when it writes one, else the newest equity snapshot."""
    beat = _parse_ts(store.kv_get(HEARTBEAT_KEY))
    latest = store.latest_equity() if beat is None else None
    snapshot = _parse_ts(latest["ts"]) if latest else None
    if beat is not None:
        ts, source, stale_after = beat, "runner heartbeat", timedelta(seconds=3 * cfg.execution.poll_seconds)
    elif snapshot is not None:
        ts, source, stale_after = snapshot, "latest equity snapshot", 2 * EQUITY_INTERVAL
    else:
        return {"ts": None, "age": "never", "source": None, "stale": True}
    age = now - ts
    return {"ts": ts, "age": fmt_age(age), "source": source, "stale": age > stale_after}


def _pnl(store: Store, cfg: StrategyConfig, now: datetime) -> dict[str, Any]:
    day_start, day_end = ny_day_bounds(ny_trading_day(now))
    realized_today = sum(_finite(t["pnl"]) or 0.0 for t in store.trades(since=day_start, until=day_end))
    latest = store.latest_equity()
    bot_equity = _finite(latest["bot_equity"]) if latest else None
    unrealized = None
    if latest is not None and bot_equity is not None:
        realized_then = sum(_finite(t["pnl"]) or 0.0 for t in store.trades() if t["exit_ts"] <= latest["ts"])
        unrealized = bot_equity - cfg.risk.capital_usd - realized_then
    start_equity = _start_of_day_equity(store, now, day_start)
    change = bot_equity - start_equity if bot_equity is not None and start_equity is not None else None
    return {
        "day": ny_trading_day(now).isoformat(),
        "realized_today": realized_today,
        "equity_change_today": change,
        "unrealized": unrealized,
        "bot_equity": bot_equity,
        "exposure": _finite(latest["exposure"]) if latest else None,
        "as_of": latest["ts"] if latest else None,
    }


def _start_of_day_equity(store: Store, now: datetime, day_start: datetime) -> float | None:
    """The runner's start-of-day bot equity (what the daily loss limit measures from), else the
    last snapshot before the New York day began, else the day's first snapshot."""
    try:
        runner_sod = _dict(json.loads(store.kv_get(SOD_KEY) or "{}"))
    except ValueError:
        runner_sod = {}
    if runner_sod.get("day") == ny_trading_day(now).isoformat() and _finite(runner_sod.get("equity")) is not None:
        return _finite(runner_sod["equity"])
    start = store.latest_equity(before=day_start) or next(iter(store.equity_series(since=day_start)), None)
    return _finite(start["bot_equity"]) if start else None


def _risk_state(store: Store, cfg: StrategyConfig, now: datetime, bot_equity: float | None) -> dict[str, Any]:
    peak = _finite(store.kv_get(PEAK_KEY))
    drawdown = (peak - bot_equity) / peak * 100 if peak is not None and peak > 0 and bot_equity is not None else None
    reset = _parse_ts(store.kv_get(RESET_TS_KEY))
    errors = _finite(store.kv_get(ERRORS_KEY))
    return {
        "drawdown_pct": drawdown,
        "kill_pct": cfg.risk.max_drawdown_kill_pct,
        "daily_block": store.kv_get(DAILY_LOSS_KEY) == ny_trading_day(now).isoformat(),
        "daily_limit_pct": cfg.risk.daily_loss_limit_pct,
        "orders_today": store.orders_today(now, since=reset),
        "max_orders": cfg.risk.max_orders_per_day,
        "errors": int(errors) if errors is not None else 0,
        "max_errors": cfg.risk.max_consecutive_errors,
    }


def _mark(store: Store, symbol: str) -> tuple[float, datetime | None] | None:
    raw = store.kv_get(LAST_PRICE_PREFIX + symbol)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    price = _finite(_dict(data).get("price"))
    return (price, _parse_ts(_dict(data).get("ts"))) if price is not None and price > 0 else None


def _positions(store: Store) -> tuple[list[Row], str | None]:
    try:
        states = store.get_positions()
    except Exception:
        log.exception("could not read positions")
        return [], "The positions table could not be read; see the dashboard log."
    rows = []
    for p in states.values():
        mark = _mark(store, p.symbol)
        ref = mark[0] if mark else p.entry_price
        distance = (ref - p.stop_price) / ref * 100 if ref > 0 else None
        rows.append(
            {
                "symbol": p.symbol, "strategy": p.strategy, "qty": p.qty, "entry_price": p.entry_price,
                "entry_ts": p.entry_ts, "stop_price": p.stop_price, "take_profit": p.take_profit,
                "bars_held": p.bars_held, "mark": mark[0] if mark else None, "mark_ts": mark[1] if mark else None,
                "distance_pct": distance, "at_risk": (ref - p.stop_price) * p.qty, "from_entry": mark is None,
            }
        )  # fmt: skip
    return rows, None


def _pending_approvals(store: Store, now: datetime) -> list[Row]:
    rows = []
    for approval in store.pending_approvals():
        signal = store.get_signal(approval["signal_id"]) if approval["signal_id"] is not None else None
        expires = _parse_ts(approval["expires_ts"])
        rows.append(
            {
                "id": approval["id"],
                "symbol": signal["symbol"] if signal else "?",
                "strategy": signal["strategy"] if signal else "?",
                "notional": approval["notional"],
                "requested": approval["ts_requested"],
                "expires": approval["expires_ts"],
                "expires_in": _until(expires - now) if expires else "n/a",
            }
        )
    return rows


def _until(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        return "overdue"
    return f"in {minutes} min" if minutes < 120 else f"in {minutes / 60:.1f} h"


def _expectation_series(
    bot: list[tuple[datetime, float]], portfolio: Mapping[str, Any], now: datetime
) -> tuple[dict[str, list[tuple[datetime, float]]], str]:
    """The bot's equity next to the pace the backtest portfolio compounded at, from the bot's
    first snapshot, and that pace less the backtest's worst drawdown."""
    metrics = _dict(portfolio.get("metrics"))
    cagr, max_dd = _finite(metrics.get("cagr_pct")), _finite(metrics.get("max_drawdown_pct"))
    series: dict[str, list[tuple[datetime, float]]] = {"Bot equity": bot}
    if not bot or cagr is None or cagr <= -100:
        return series, "No backtest expectation: run the tournament to compare against the backtest portfolio."
    (t0, e0), t1 = bot[0], max(bot[-1][0], now)
    step = (t1 - t0) / EXPECTATION_POINTS
    times = [t0 + step * i for i in range(EXPECTATION_POINTS + 1)] if step else [t0]
    pace = [(t, e0 * (1 + cagr / 100) ** ((t - t0).total_seconds() / 86400 / 365.25)) for t in times]
    window = _text(portfolio.get("window")) or "backtest"
    series[f"Backtest pace ({fmt_pct(cagr, digits=1)}/yr)"] = pace
    note = f"Backtest pace compounds the portfolio's {window} CAGR from the bot's first equity snapshot."
    if max_dd is not None and 0 < max_dd < 100:
        series[f"Pace less worst drawdown ({fmt_pct(max_dd, digits=1)})"] = [
            (t, v * (1 - max_dd / 100)) for t, v in pace
        ]
        note += " Equity below the lowest line is worse than anything the backtest saw."
    return series, note


def _equity_chart(store: Store, results: Mapping[str, Any] | None, now: datetime) -> tuple[Markup, str]:
    bot = [(parse_ts(r["ts"]), v) for r in store.equity_series() if (v := _finite(r["bot_equity"])) is not None]
    series, note = _expectation_series(bot, _dict(_dict(results).get("portfolio")), now)
    svg = charts.multi_line_chart(
        series, title="Bot equity versus the backtest expectation",
        desc="Bot equity in US dollars over time, with the backtest portfolio's compounded pace for comparison.",
        y_label="Equity (USD)", x_label="Date (UTC)", y_format=_whole_usd,
        empty_message="No equity snapshots yet", chart_id="equity",
    )  # fmt: skip
    return Markup(svg), note


def _overview(settings: Settings, cfg: StrategyConfig, store: Store, results: Mapping[str, Any] | None) -> Row:
    now = store.now()
    pnl = _pnl(store, cfg, now)
    positions, positions_error = _positions(store)
    chart, chart_note = _equity_chart(store, results, now)
    return {
        "kill": _kill_state(settings),
        "heartbeat": _heartbeat(store, cfg, now),
        "pnl": pnl,
        "risk": _risk_state(store, cfg, now, pnl["bot_equity"]),
        "positions": positions,
        "positions_error": positions_error,
        "approvals": _pending_approvals(store, now),
        "events": store.recent_events(EVENTS_SHOWN),
        "equity_chart": chart,
        "equity_note": chart_note,
        "capital": cfg.risk.capital_usd,
    }


# --------------------------------------------------------------------------- signals


def _query_int(value: str | None, default: int = 1) -> int:
    try:
        return max(1, int(value)) if value is not None else default
    except ValueError:
        return default


def _answer_views(row: Row) -> list[Row]:
    return [
        {
            "name": _text(a.get("name")),
            "value": _finite(a.get("gate_value")),
            "passed": a.get("passed") if isinstance(a.get("passed"), bool) else None,
            "rule": _text(a.get("rule")),
        }
        for a in _answers(row)
    ]


def _signal_view(row: Row, store: Store) -> Row:
    order = store.get_order_by_client_id(row["order_client_id"]) if row.get("order_client_id") else None
    result, counterfactual = _result(row)
    return {
        **{k: row.get(k) for k in ("id", "ts", "bar_date", "symbol", "strategy", "kind", "reason", "price",
                                   "stop_price", "gate", "risk_action", "risk_reason", "approval_status", "status")},
        "answers": _answer_views(row),
        "jev_error": row.get("jev_error"),
        "order": order,
        "result": result,
        "counterfactual": counterfactual,
    }  # fmt: skip


def _signals(cfg: StrategyConfig, store: Store, params: Mapping[str, str]) -> Row:
    rows = _all_signals(store)
    symbols = sorted(set(cfg.assets) | {r["symbol"] for r in rows})
    symbol = params.get("symbol") if params.get("symbol") in symbols else None
    gate = params.get("gate") if params.get("gate") in SIGNAL_GATES else None
    matching = [r for r in rows if (symbol is None or r["symbol"] == symbol) and (gate is None or r["gate"] == gate)]
    pages = max(1, math.ceil(len(matching) / PAGE_SIZE))
    page = min(_query_int(params.get("page")), pages)
    shown = matching[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    filters = {k: v for k, v in (("symbol", symbol), ("gate", gate)) if v}

    def link(target: int) -> str | None:
        return "?" + urlencode({**filters, "page": target}) if 1 <= target <= pages and target != page else None

    return {
        "rows": [_signal_view(r, store) for r in shown],
        "total": len(matching),
        "scanned_all": len(rows) < SIGNAL_SCAN_LIMIT,
        "symbols": symbols,
        "gates": sorted(SIGNAL_GATES),
        "symbol": symbol,
        "gate": gate,
        "page": page,
        "pages": pages,
        "prev": link(page - 1),
        "next": link(page + 1),
    }


# --------------------------------------------------------------------------- Jev


_OUTCOME_GROUPS: tuple[tuple[str, Callable[[Row], bool]], ...] = (
    ("Passed Jev", lambda r: r["gate"] == "passed"),
    ("Vetoed by Jev", lambda r: r["gate"] == "vetoed"),
    ("Jev error (blocked, fail-closed)", lambda r: r["gate"] == "error"),
    ("Shadow mode, Jev said yes", lambda r: r["gate"] == "shadow" and not _jev_said_no(r)),
    ("Shadow mode, Jev said no (not enforced)", lambda r: r["gate"] == "shadow" and _jev_said_no(r)),
)


def _outcome_row(label: str, rows: list[Row]) -> Row:
    results = [_result(r) for r in rows]
    values = [v for v, _ in results if v is not None]
    return {
        "label": label,
        "signals": len(rows),
        "realized": sum(1 for v, cf in results if v is not None and not cf),
        "counterfactual": sum(1 for v, cf in results if v is not None and cf),
        "avg": _mean(values),
        "win_rate": sum(1 for v in values if v > 0) / len(values) if values else None,
    }


def _question_rows(rows: list[Row]) -> list[Row]:
    stats: dict[str, Row] = {}
    for row in rows:
        result, _ = _result(row)
        for answer in _answers(row):
            name = _text(answer.get("name"))
            entry = stats.setdefault(
                name, {"name": name, "rule": "", "values": [], "failed": 0, "pass_r": [], "fail_r": []}
            )
            entry["rule"] = _text(answer.get("rule")) or entry["rule"]
            entry["values"].append(_finite(answer.get("gate_value")))
            passed = answer.get("passed") is True
            entry["failed"] += 0 if passed else 1
            if result is not None:
                entry["pass_r" if passed else "fail_r"].append(result)
    return [
        {
            "name": s["name"], "rule": s["rule"], "answered": len(s["values"]), "failed": s["failed"],
            "avg_value": _mean(s["values"]), "avg_pass": _mean(s["pass_r"]), "avg_fail": _mean(s["fail_r"]),
            "n_pass": len(s["pass_r"]), "n_fail": len(s["fail_r"]),
        }
        for s in sorted(stats.values(), key=lambda s: s["name"])
    ]  # fmt: skip


def _jev(cfg: StrategyConfig, store: Store) -> Row:
    rows = [r for r in _all_signals(store) if r["kind"] == "entry"]
    called = [r for r in rows if _jev_called(r)]
    latencies = [v for r in called if (v := _finite(r.get("jev_latency_ms"))) is not None]
    stats = store.jev_stats()
    histogram = charts.bar_histogram(
        latencies, title="Jev latency distribution",
        desc="How many Jev calls took each amount of time, in milliseconds.",
        x_label="Latency (ms)", y_label="Calls", x_format=lambda v: f"{v:,.0f}", empty_message="No Jev calls yet",
        chart_id="latency",
    )  # fmt: skip
    gate_counts = Counter(r["gate"] or "not evaluated" for r in rows)
    return {
        "mode": cfg.jev.mode,
        "model": cfg.jev.model,
        "stats": stats,
        "p50": _percentile(latencies, 50),
        "p95": _percentile(latencies, 95),
        "n_latencies": len(latencies),
        "counts": store.jev_outcome_counts(),
        "gate_counts": sorted(gate_counts.items()),
        "outcomes": [_outcome_row(label, [r for r in rows if test(r)]) for label, test in _OUTCOME_GROUPS],
        "questions": _question_rows(called),
        "histogram": Markup(histogram),
        "vetoes": [_signal_view(r, store) for r in store.vetoed_signals_with_counterfactual()[:VETOES_SHOWN]],
    }


# --------------------------------------------------------------------------- backtest


def _pf(metrics: Mapping[str, Any]) -> str:
    """Profit factor; the tournament writes inf (no losing trades) as null."""
    if "profit_factor" not in metrics:
        return "n/a"
    value = metrics["profit_factor"]
    return "∞" if value is None else fmt_num(value)


_METRICS: tuple[tuple[str, str, Callable[[Any], str]], ...] = (
    ("total_return_pct", "Total return", lambda v: fmt_pct(v, signed=True)),
    ("cagr_pct", "CAGR", lambda v: fmt_pct(v, signed=True)),
    ("max_drawdown_pct", "Max drawdown", fmt_pct),
    ("win_rate", "Win rate", fmt_rate),
    ("n_trades", "Trades", fmt_int),
    ("avg_win_pct", "Average win", lambda v: fmt_pct(v, signed=True)),
    ("avg_loss_pct", "Average loss", lambda v: fmt_pct(v, signed=True)),
    ("expectancy_pct", "Expectancy per trade", lambda v: fmt_pct(v, signed=True)),
    ("sharpe", "Sharpe", fmt_num),
    ("sortino", "Sortino", fmt_num),
    ("exposure_pct", "Time in the market", fmt_pct),
    ("largest_loss_usd", "Largest loss", lambda v: fmt_usd(v, signed=True)),
    ("largest_loss_pct", "Largest loss (per trade)", lambda v: fmt_pct(v, signed=True)),
    ("max_dd_duration_days", "Longest drawdown (days)", fmt_int),
    ("buy_hold_return_pct", "Buy and hold return", lambda v: fmt_pct(v, signed=True)),
    ("buy_hold_max_dd_pct", "Buy and hold max drawdown", fmt_pct),
)


def _metric_rows(metrics: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Known metrics in a fixed order with their units, then any others as plain numbers."""
    rows = [(label, fmt(metrics.get(key))) for key, label, fmt in _METRICS if key in metrics]
    if "profit_factor" in metrics:
        rows.append(("Profit factor (∞: no losing trades)", _pf(metrics)))
    known = {key for key, _, _ in _METRICS} | {"profit_factor"}
    return rows + [(key, fmt_num(v)) for key, v in metrics.items() if key not in known]


def _window_metrics(metrics: Any) -> Row:
    m = _dict(metrics)
    return {
        "ret": fmt_pct(m.get("total_return_pct"), signed=True), "cagr": fmt_pct(m.get("cagr_pct"), signed=True),
        "dd": fmt_pct(m.get("max_drawdown_pct")), "win": fmt_rate(m.get("win_rate")), "pf": _pf(m),
        "trades": fmt_int(m.get("n_trades")),
    }  # fmt: skip


def _neighbors(run: Mapping[str, Any]) -> str:
    share, count = fmt_rate(run.get("neighbors_passing")), _finite(run.get("n_neighbors"))
    return share if count is None or share == "n/a" else f"{share} of {int(count)}"


def _run_view(run: Mapping[str, Any], rank: int | None, winner_label: str | None) -> Row:
    label = _text(run.get("label"))
    return {
        "rank": rank,
        "label": label,
        "passed": run.get("passed") is True,
        "winner": winner_label is not None and label == winner_label,
        "is_": _window_metrics(run.get("in_sample")),
        "oos": _window_metrics(run.get("out_of_sample")),
        "neighbors": _neighbors(run),
        "score": fmt_num(run.get("score"), 3),
        "fail_reasons": [_text(r) for r in _list(run.get("fail_reasons"))],
    }


def _regime_view(regimes: Any) -> Row | None:
    regimes = _dict(regimes)
    if not regimes:
        return None

    def row(name: str, entry: Any) -> Row:
        e = _dict(entry)
        trades = e.get("n_trades", e.get("trades"))
        window = f"{e['start']} to {e['end']}" if e.get("start") and e.get("end") else ""
        return {
            "name": name.replace("_", " "), "window": window, "ret": fmt_pct(e.get("return_pct"), signed=True),
            "dd": fmt_pct(e.get("max_dd_pct")), "trades": fmt_int(trades), "bars": fmt_int(e.get("n_bars")),
            "no_data": entry is None or e.get("return_pct") is None,
        }  # fmt: skip

    return {
        group: [row(name, entry) for name, entry in _dict(regimes.get(group)).items()]
        for group in ("trend", "vol", "stress")
    }


def _equity_points(values: Any) -> list[tuple[date, float]]:
    points = []
    for item in _list(values):
        if isinstance(item, list) and len(item) == 2:
            day, value = _parse_date(item[0]), _finite(item[1])
            if day is not None and value is not None:
                points.append((day, value))
    return points


def _score_key(run: Mapping[str, Any]) -> float:
    score = _finite(run.get("score"))
    return -score if score is not None else math.inf


def _symbol_view(symbol: str, runs: list[Mapping[str, Any]], winner: Any) -> Row:
    winner = _dict(winner) or None
    winner_label = _text(winner.get("label")) if winner else None
    survivors = sorted((r for r in runs if r.get("passed") is True), key=_score_key)
    others = sorted((r for r in runs if r.get("passed") is not True), key=_score_key)
    views = [_run_view(r, rank, winner_label) for rank, r in enumerate(survivors, 1)]
    views += [_run_view(r, None, winner_label) for r in others]
    winning_run = next((r for r in runs if winner_label and _text(r.get("label")) == winner_label), None)
    return {
        "symbol": symbol,
        "runs": views,
        "survivors": sum(1 for v in views if v["passed"]),
        "winner": None if winner is None else {
            "label": winner_label, "title": _text(winner.get("title")), "score": fmt_num(winner.get("score"), 3),
            "params": sorted(_dict(winner.get("params")).items()),
            "rules": [(k.replace("_", " "), _text(v)) for k, v in _dict(winner.get("rules")).items()],
        },
        "regimes": _regime_view(_dict(winning_run).get("regimes")) if winning_run else None,
        "approvals": _approval_share(_dict(winning_run).get("approvals")),
    }  # fmt: skip


def _symbols(results: Mapping[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for source in (_dict(results.get("data")), _dict(results.get("winners"))):
        seen.update(dict.fromkeys(source))
    for run in _list(results.get("runs")):
        if isinstance(run, Mapping) and run.get("symbol"):
            seen[_text(run["symbol"])] = None
    return list(seen)


def _caveats(results: Mapping[str, Any]) -> list[str]:
    n = fmt_int(results.get("n_configs"))
    return [
        f"{n} configurations were tested. With that many tries some will look good by luck, so treat "
        "every survivor as a hypothesis, not a proven edge.",
        "Survivors are ranked on the in-sample window only; the out-of-sample window is a pass/fail check.",
        "The backtest cannot include Jev, news or manual approvals: live results will differ.",
        "Fills assume the configured slippage and fees on daily bars; real fills can be worse, "
        "especially through gaps.",
        "Daily bars on three assets give few trades per year, so win rates carry wide error bars.",
        "Past results do not predict future returns. Not financial advice.",
        *[_text(c) for c in _list(results.get("caveats"))],
    ]


def backtest_view(results: Mapping[str, Any]) -> Row:
    """Everything the backtest page and the static report show, from tournament.json's dict.
    Tolerates missing or mistyped keys: a partial file renders what it has."""
    config = _dict(results.get("config"))
    runs = [r for r in _list(results.get("runs")) if isinstance(r, Mapping)]
    winners = _dict(results.get("winners"))
    symbols = [
        _symbol_view(s, [r for r in runs if _text(r.get("symbol")) == s], winners.get(s)) for s in _symbols(results)
    ]
    winner_equity = {s: pts for s, v in _dict(results.get("winner_equity")).items() if (pts := _equity_points(v))}
    return {
        "generated_at": _text(results.get("generated_at")),
        "n_configs": fmt_int(results.get("n_configs")),
        "data": [(s, _dict(d)) for s, d in _dict(results.get("data")).items()],
        "in_sample": " to ".join(map(_text, _list(config.get("in_sample")))) or "n/a",
        "out_of_sample": " to ".join(map(_text, _list(config.get("out_of_sample")))) or "n/a",
        "filters": sorted(_dict(config.get("filters")).items()),
        "costs": [(name, sorted(_dict(config.get(name)).items())) for name in ("slippage_bps", "fee_bps")],
        "symbols": symbols,
        "winner_chart": Markup(charts.multi_line_chart(
            dict(list(winner_equity.items())[: charts.MAX_SERIES]), title="Winner equity curves",
            desc="Weekly equity of each symbol's winning strategy, each run on its own capital.",
            y_label="Equity (USD)", x_label="Date", y_format=_whole_usd, empty_message="No winners",
            chart_id="winner-equity",
        )),
        "portfolios": [
            _portfolio_view(_dict(results.get(key)), chart_id=key.replace("_", "-"))
            for key in ("portfolio", "portfolio_full")
            if _dict(results.get(key))
        ],
        "caveats": _caveats(results),
    }  # fmt: skip


def _approval_share(approvals: Any) -> Row | None:
    counts = _dict(approvals)
    entries, needing = _finite(counts.get("entry_orders")), _finite(counts.get("needing_approval"))
    if entries is None:
        return None
    share = fmt_rate(needing / entries) if needing is not None and entries else "n/a"
    return {"entries": fmt_int(entries), "needing": fmt_int(needing), "share": share}


def _portfolio_view(portfolio: Mapping[str, Any], chart_id: str) -> Row:
    """One shared-capital backtest of every winner (`portfolio` or `portfolio_full`)."""
    start, end = _text(portfolio.get("start")), _text(portfolio.get("end"))
    window = _text(portfolio.get("window")).replace("_", "-") or "unknown window"
    title = "Portfolio, " + ("full period" if window == "full" else window)
    return {
        "title": title,
        "window": window,
        "dates": f"{start} to {end}" if start and end else "",
        "metrics": _metric_rows(_dict(portfolio.get("metrics"))),
        "regimes": _regime_view(portfolio.get("regimes")),
        "regime_proxy": _text(portfolio.get("regime_proxy")),
        "kill_fires": [(_text(_dict(k).get("date")), _text(_dict(k).get("reason")))
                       for k in _list(portfolio.get("kill_switch_would_fire"))],
        "daily_loss_hits": fmt_int(portfolio.get("daily_loss_limit_hits")),
        "daily_loss_days": [_text(d) for d in _list(portfolio.get("daily_loss_limit_days"))],
        "approvals": _approval_share(portfolio.get("approvals")),
        "trades_by_symbol": list(_dict(portfolio.get("trades_by_symbol")).items()),
        "shrunk": fmt_int(portfolio.get("shrunk_entries")),
        "blocked": len(_list(portfolio.get("blocked_entries"))),
        "chart": Markup(charts.line_chart(
            _equity_points(portfolio.get("equity")), title=f"{title} equity", name=title,
            desc="Weekly equity of the combined backtest of every winner on shared capital.",
            y_label="Equity (USD)", x_label="Date", y_format=_whole_usd, empty_message="No portfolio equity",
            chart_id=chart_id,
        )),
    }  # fmt: skip


def _load_results(path: Path) -> tuple[Mapping[str, Any] | None, str | None]:
    data, error = _read_json(path)
    if data is not None and not isinstance(data, Mapping):
        return None, f"{path.name} is not a JSON object"
    return data, error


def render_static_report(results: dict) -> str:
    """The /backtest view as one self-contained HTML page (inline CSS and SVG, no scripts, no
    external references), for sharing. The CLI writes it to results/tournament.html."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    return _ENV.get_template("report.html").render(bt=backtest_view(results), css=Markup(css))


# --------------------------------------------------------------------------- live gate


def _compact(value: Any) -> Any:
    """Floats to at most four decimals (0.95238 -> "0.9524"); anything else unchanged."""
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return value


def _checks(items: Any) -> list[Row]:
    """Final-check records `{name, passed, required, detail, value, threshold}` or drill steps
    `{name, ok, detail}`."""
    out = []
    for item in (i for i in _list(items) if isinstance(i, Mapping)):
        passed = item.get("passed", item.get("ok"))
        out.append(
            {
                "name": _text(item.get("name")).replace("_", " ") or "check",
                "passed": passed if isinstance(passed, bool) else None,
                "required": item.get("required") is not False,
                "value": _compact(item.get("value")),
                "threshold": _compact(item.get("threshold")),
                "detail": _text(item.get("detail")),
            }
        )
    return out


def _gate_doc(data: Any, error: str | None, max_age: timedelta, now: datetime, checks_key: str) -> Row:
    """What the page shows of var/live_gate.json or var/kill_switch_drill.json. Other keys (such
    as the drill's state directory) are deliberately not rendered."""
    doc = _dict(data)
    ts = _parse_ts(doc.get("ts"))
    return {
        "exists": data is not None,
        "error": error or (None if data is None or isinstance(data, Mapping) else "the file is not a JSON object"),
        "passed": doc.get("passed") if isinstance(doc.get("passed"), bool) else None,
        "ts": ts,
        "age": fmt_age(now - ts) if ts else "n/a",
        "fresh": ts is not None and now - ts < max_age,
        "max_age_days": max_age.days,
        "checks": _checks(doc.get(checks_key)),
        "summary": _text(doc.get("summary")),
        "mode": _text(doc.get("mode")),
    }


def _blow_up_items(value: Any) -> list[str]:
    """live_gate.json's `blow_up` records `{title, detail}`, or Markdown bullet lines."""
    items = []
    for item in _list(value):
        if isinstance(item, Mapping):
            head, body = _text(item.get("title")), _text(item.get("detail"))
            text = f"{head}: {body}" if head and body else head or body
        else:
            text = _BULLET.sub("", _text(item)).strip()
        if text:
            items.append(text)
    return items


def _blow_up_from_strategy(path: Path) -> list[str]:
    """Bullets under the WHAT COULD BLOW UP THIS ACCOUNT? heading of strategy.md's final check."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    start = text.find(BLOW_UP_HEADING)
    if start < 0:
        return []
    lines: list[str] = []
    for line in text[start + len(BLOW_UP_HEADING) :].splitlines()[1:]:
        if line.lstrip().startswith(("#", "<!--")):
            break
        if _BULLET.match(line):
            lines.append(line)
        elif lines and line.strip() and not line[0].isspace():
            break  # the first paragraph after the list ends it
    return _blow_up_items(lines)


def _live_gate(settings: Settings, cfg: StrategyConfig, store: Store, strategy_path: Path) -> Row:
    now = store.now()
    gate, gate_error = _read_json(settings.live_gate_path)
    drill, drill_error = _read_json(settings.drill_path)
    drill_age = timedelta(days=cfg.live_gate.kill_switch_drill_max_age_days)
    items, source = _blow_up_items(_dict(gate).get("blow_up")), "var/live_gate.json"
    if not items:
        items, source = _blow_up_from_strategy(strategy_path), "the Final check section of strategy.md"
    return {
        "gate": _gate_doc(gate, gate_error, LIVE_GATE_MAX_AGE, now, "checks"),
        "drill": _gate_doc(drill, drill_error, drill_age, now, "steps"),
        "blow_up": items,
        "blow_up_source": source,
        "thresholds": cfg.live_gate.model_dump(),
    }


# --------------------------------------------------------------------------- app


def create_app(
    settings: Settings,
    cfg: StrategyConfig,
    store: Store,
    results_path: Path = RESULTS_PATH,
    *,
    strategy_path: Path = STRATEGY_PATH,
) -> FastAPI:
    """The dashboard app. Raises ConfigError when there is no password and DASHBOARD_HOST is not
    a loopback address (the CLI binds to DASHBOARD_HOST)."""
    if not _password(settings) and not is_loopback(settings.dashboard_host):
        raise ConfigError("DASHBOARD_PASSWORD must be set when DASHBOARD_HOST is not a loopback address")
    app = FastAPI(title="Trading bot dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response = _auth_refusal(request, settings)
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("dashboard error on %s", request.url.path)
                response = PlainTextResponse("Internal error; see the dashboard log.", 500)
        response.headers.update(SECURITY_HEADERS)
        return response

    def page(name: str, path: str, title: str, **context: Any) -> HTMLResponse:
        base = {"nav": NAV, "active": path, "page_title": title, "mode": _mode(settings, cfg),
                "kill_tripped": KillSwitch(settings).is_tripped()}  # fmt: skip
        return HTMLResponse(redact_secrets(_ENV.get_template(name).render({**base, **context}), settings))

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    def overview() -> HTMLResponse:
        results, _ = _load_results(results_path)
        return page("overview.html", "/", "Overview", o=_overview(settings, cfg, store, results))

    @app.get("/signals", response_class=HTMLResponse)
    def signals(request: Request) -> HTMLResponse:
        return page("signals.html", "/signals", "Signals", s=_signals(cfg, store, request.query_params))

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest() -> HTMLResponse:
        results, error = _load_results(results_path)
        view = backtest_view(results) if results is not None else None
        return page("backtest.html", "/backtest", "Backtest", bt=view, error=error)

    @app.get("/jev", response_class=HTMLResponse)
    def jev() -> HTMLResponse:
        return page("jev.html", "/jev", "Jev", j=_jev(cfg, store))

    @app.get("/live-gate", response_class=HTMLResponse)
    def live_gate() -> HTMLResponse:
        return page("live_gate.html", "/live-gate", "Live gate", g=_live_gate(settings, cfg, store, strategy_path))

    return app
