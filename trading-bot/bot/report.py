"""Daily Markdown report for one New York trading day.

It covers the day's closed trades, open positions, realized and unrealized P&L, cumulative win
rate and largest loss, Jev latency and cost, counts of vetoes, approvals and errors, and the
kill switch. Cumulative figures run to the end of `day`. Open positions and the kill switch are
shown as they are when the report is generated. The file goes to
`settings.reports_dir/YYYY-MM-DD.md`, and the runner sends the returned text to Telegram.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bot.config import Settings, StrategyConfig
from bot.store import Store, ny_day_bounds, parse_ts
from bot.timeutil import NY, UTC, ny_trading_day

log = logging.getLogger(__name__)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")

Row = dict[str, Any]


def report_path(settings: Settings, day: date) -> Path:
    return settings.reports_dir / f"{day.isoformat()}.md"


def daily_report(store: Store, settings: Settings, cfg: StrategyConfig, day: date | str) -> str:
    """Build the report for New York calendar day `day` (a date or "YYYY-MM-DD"), save it and
    return the Markdown."""
    day = _as_day(day)
    start, end = ny_day_bounds(day)
    day_trades = store.trades(since=start, until=end)
    history = store.trades(until=end)
    sections = [
        _header(store, settings, cfg, day),
        _trades_section(day_trades),
        _positions_section(store),
        _pnl_section(store, cfg, day_trades, history, end),
        _jev_section(store, start, end),
        _activity_section(store, settings, cfg, start, end),
    ]
    text = "\n\n".join(sections) + "\n"
    path = report_path(settings, day)
    _write_atomic(path, text)
    log.info("daily report for %s written to %s", day, path)
    return text


# --------------------------------------------------------------------------- sections


def _header(store: Store, settings: Settings, cfg: StrategyConfig, day: date) -> str:
    mode = "PAPER" if settings.trading_mode == "paper" else "LIVE"
    account = "Alpaca paper account" if settings.alpaca_paper else "Alpaca live account"
    assets = ", ".join(f"{symbol} ({rule.strategy})" for symbol, rule in cfg.enabled_assets.items())
    generated = store.now().astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            f"# Daily report for {day.isoformat()}",
            "",
            f"New York trading day. Generated {generated}.",
            "",
            f"- Mode: **{mode}** ({account})",
            f"- Jev mode: {cfg.jev.mode}",
            f"- Enabled assets: {assets or 'none'}",
        ]
    )


def _trades_section(trades: list[Row]) -> str:
    if not trades:
        return "## Trades today\n\nNo trades today."
    won = sum(1 for t in trades if _num(t["pnl"]) > 0)
    lost = sum(1 for t in trades if _num(t["pnl"]) < 0)
    flat = len(trades) - won - lost
    summary = f"{len(trades)} closed: {won} won, {lost} lost" + (f", {flat} flat." if flat else ".")
    lines = [
        "## Trades today",
        "",
        summary,
        "",
        "| Symbol | Strategy | Entered (NY) | Exited (NY) | Qty | Entry | Exit | P&L | P&L % | Exit reason |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    lines += [
        _row(
            t["symbol"], t["strategy"], _ny_time(t["entry_ts"]), _ny_time(t["exit_ts"]), _qty(t["qty"]),
            _price(t["entry_price"]), _price(t["exit_price"]), _usd(t["pnl"], signed=True),
            _pct(t["pnl_pct"]), t["exit_reason"],
        )
        for t in trades
    ]  # fmt: skip
    return "\n".join(lines)


def _positions_section(store: Store) -> str:
    positions = store.get_positions()
    if not positions:
        return "## Open positions (now)\n\nNo open positions."
    lines = [
        "## Open positions (now)",
        "",
        "| Symbol | Strategy | Qty | Entry | Stop | Take profit | Entered (NY) | Bars held |",
        "|---|---|---:|---:|---:|---:|---|---:|",
    ]
    lines += [
        _row(
            p.symbol, p.strategy, _qty(p.qty), _price(p.entry_price), _price(p.stop_price),
            "none" if p.take_profit is None else _price(p.take_profit), _ny_time(p.entry_ts), p.bars_held,
        )
        for p in positions.values()
    ]  # fmt: skip
    return "\n".join(lines)


def _pnl_section(store: Store, cfg: StrategyConfig, day_trades: list[Row], history: list[Row], end: datetime) -> str:
    lines = [
        "## P&L",
        "",
        f"- Realized today: {_usd(sum(_num(t['pnl']) for t in day_trades), signed=True)}",
        f"- Realized, cumulative: {_usd(sum(_num(t['pnl']) for t in history), signed=True)}",
        *_equity_lines(store, cfg, history, end),
        _win_rate_line(history),
        _largest_loss_line(history),
    ]
    return "\n".join(lines)


def _equity_lines(store: Store, cfg: StrategyConfig, history: list[Row], end: datetime) -> list[str]:
    """Unrealized P&L from the newest equity snapshot before `end`, assuming
    bot_equity = capital + realized + unrealized."""
    snap = store.latest_equity(before=end)
    if snap is None or snap["bot_equity"] is None:
        return ["- Unrealized: n/a (no equity snapshot yet)"]
    realized_then = sum(_num(t["pnl"]) for t in history if t["exit_ts"] <= snap["ts"])
    unrealized = snap["bot_equity"] - cfg.risk.capital_usd - realized_then
    return [
        f"- Unrealized: {_usd(unrealized, signed=True)} (as of {_ny_time(snap['ts'])} NY)",
        f"- Bot equity: {_usd(snap['bot_equity'])}, exposure {_usd(snap['exposure'])}, "
        f"account equity {_usd(snap['account_equity'])}",
    ]


def _win_rate_line(history: list[Row]) -> str:
    if not history:
        return "- Win rate, cumulative: n/a (no closed trades)"
    wins = sum(1 for t in history if _num(t["pnl"]) > 0)
    return f"- Win rate, cumulative: {wins / len(history):.1%} ({wins} of {len(history)} trades)"


def _largest_loss_line(history: list[Row]) -> str:
    losses = [t for t in history if _num(t["pnl"]) < 0]
    if not losses:
        return "- Largest loss: none"
    by_usd = min(losses, key=lambda t: t["pnl"])
    line = f"- Largest loss: {_usd(by_usd['pnl'], signed=True)}, {_pct(by_usd['pnl_pct'])} ({_trade_label(by_usd)})"
    with_pct = [t for t in losses if t["pnl_pct"] is not None]
    by_pct = min(with_pct, key=lambda t: t["pnl_pct"]) if with_pct else by_usd
    if by_pct["id"] != by_usd["id"]:
        line += f"; largest loss by return: {_pct(by_pct['pnl_pct'])} ({_trade_label(by_pct)})"
    return line


def _jev_section(store: Store, start: datetime, end: datetime) -> str:
    return "\n".join(
        [
            "## Jev",
            "",
            "| Window | Decisions | Avg latency | Avg cost per decision | Total cost |",
            "|---|---:|---:|---:|---:|",
            _jev_row("Today", store.jev_stats(since=start, until=end)),
            _jev_row("Cumulative", store.jev_stats(until=end)),
        ]
    )


def _jev_row(label: str, stats: dict[str, Any]) -> str:
    if stats["n"] == 0:
        return _row(label, 0, "n/a", "n/a", _cost(0.0))
    return _row(
        label, stats["n"], f"{stats['avg_latency_ms']:,.0f} ms", _cost(stats["avg_cost_usd"]),
        _cost(stats["total_cost_usd"]),
    )


def _activity_section(store: Store, settings: Settings, cfg: StrategyConfig, start: datetime, end: datetime) -> str:
    jev = store.jev_outcome_counts(since=start, until=end)
    approvals = store.approval_counts(since=start, until=end)
    vetoes = str(jev["vetoed"])
    if jev["shadow_vetoed"]:
        vetoes += f" (plus {jev['shadow_vetoed']} in shadow mode, not enforced)"
    breakdown = ", ".join(f"{status} {approvals[status]}" for status in ("approved", "rejected", "expired", "pending"))
    return "\n".join(
        [
            "## Risk and activity",
            "",
            f"- Entry orders: {store.orders_today(start)} (daily limit {cfg.risk.max_orders_per_day})",
            f"- Jev vetoes: {vetoes}",
            f"- Jev errors: {jev['errors']}",
            f"- Approvals requested: {sum(approvals.values())} ({breakdown})",
            f"- Errors logged: {store.count_events(since=start, until=end)}",
            f"- Kill switch (now): {_kill_switch_state(settings)}",
        ]
    )


def _kill_switch_state(settings: Settings) -> str:
    details: list[str] = []
    if settings.kill_switch:
        details.append("forced on by KILL_SWITCH=1")
    if settings.kill_switch_path.exists():
        details.append(_kill_file_detail(settings.kill_switch_path))
    return "**TRIPPED**: " + "; ".join(details) if details else "off"


def _kill_file_detail(path: Path) -> str:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("kill switch file %s exists but is unreadable", path)
        return "switch file present (unreadable)"
    if not isinstance(info, dict):
        return "switch file present"
    reason = _clean(info.get("reason")) or "no reason given"
    source = _clean(info.get("source")) or "unknown"
    when = _clean(info.get("ts"))
    try:
        when = f"{_ny_time(when)} NY" if when else ""
    except ValueError:
        pass  # not ISO-8601: show it as written
    return f'"{reason}" (source: {source}' + (f", at {when})" if when else ")")


# --------------------------------------------------------------------------- formatting


def _as_day(day: date | str) -> date:
    if isinstance(day, datetime):
        return ny_trading_day(day) if day.tzinfo else day.date()
    if isinstance(day, date):
        return day
    return date.fromisoformat(day)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _num(value: float | None) -> float:
    return 0.0 if value is None or not math.isfinite(value) else float(value)


def _usd(value: float | None, signed: bool = False) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    value = round(value, 2)
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    return f"{sign}${abs(value):,.2f}"


def _cost(value: float) -> str:
    """Jev costs are fractions of a cent, so they get six decimals."""
    return f"${value:,.6f}"


def _price(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"${value:,.2f}" if abs(value) >= 1 else f"${value:.4f}"


def _qty(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _pct(fraction: float | None) -> str:
    """`Trade.pnl_pct` is a fraction (pnl / cost basis); shown as a signed percentage."""
    if fraction is None or not math.isfinite(fraction):
        return "n/a"
    return f"{fraction * 100:+.2f}%"


def _ny_time(value: datetime | str) -> str:
    ts = parse_ts(value) if isinstance(value, str) else value
    return ts.astimezone(NY).strftime("%Y-%m-%d %H:%M")


def _trade_label(trade: Row) -> str:
    return f"{trade['symbol']}, closed {_ny_time(trade['exit_ts'])[:10]}"


def _clean(value: object, limit: int = 200) -> str:
    """Single-line, length-capped text from a file another process wrote."""
    if value is None:
        return ""
    text = " ".join(_CONTROL_CHARS.sub(" ", str(value)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row(*cells: object) -> str:
    return "| " + " | ".join(_clean(cell, limit=500).replace("|", "\\|") for cell in cells) + " |"
