"""Strategy tournament: every strategy x grid point x symbol, judged by fixed filters.

Each configuration is backtested on the in-sample window, the out-of-sample window and the full
period (the engine prepares indicators on the full history and trades only inside the window).
A configuration survives when it passes every filter in both windows, `min_trades` per window.

Survivors are ranked by in-sample Calmar ratio (CAGR / max drawdown), then in-sample win rate.
The out-of-sample window is only a pass/fail check: ranking on it would make it training data.
Each symbol gets one winner or none, and a symbol without a winner stays disabled. Every run also
reports `neighbors_passing`, the share of its grid neighbours (same strategy and symbol, exactly
one parameter different) that survive too: a robustness hint, never a ranking input.

`run_and_write` is the entry point behind `python -m bot tournament [--apply]`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bot import data
from bot.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from bot.backtest.metrics import compute_metrics
from bot.backtest.portfolio import PortfolioResult, regimes_on_proxy, run_portfolio
from bot.backtest.regimes import regime_breakdown
from bot.config import (
    ROOT,
    STRATEGY_PATH,
    ConfigError,
    StrategyConfig,
    TournamentFilters,
    load_strategy,
    read_config_block,
    replace_section,
    write_config_block,
)
from bot.strategies import REGISTRY, Strategy, build
from bot.timeutil import periods_per_year, utcnow

log = logging.getLogger(__name__)

RESULTS_DIR = ROOT / "results"
JSON_NAME = "tournament.json"
MD_NAME = "tournament.md"
WINDOW_TAGS = {"in_sample": "IS", "out_of_sample": "OOS"}
KEY_METRICS = (
    "total_return_pct",
    "cagr_pct",
    "max_drawdown_pct",
    "win_rate",
    "profit_factor",
    "n_trades",
    "sharpe",
    "exposure_pct",
    "buy_hold_return_pct",
)
LEADERBOARD_SIZE = 5
REGIME_PROXY = "SPY"


# --------------------------------------------------------------------------- filters and ranking


def _shown(value: float, threshold: float, digits: int) -> tuple[str, str]:
    """Format a value and its threshold with enough digits that they never print the same."""
    for d in range(digits, 7):
        a, b = f"{value:.{d}f}", f"{threshold:.{d}f}"
        if a != b:
            break
    return a, b


def fail_reasons(
    in_sample: Mapping[str, Any], out_of_sample: Mapping[str, Any], filters: TournamentFilters
) -> list[str]:
    """Human-readable filter failures, naming the window and the numbers. Undefined (NaN)
    metrics fail."""
    reasons: list[str] = []
    for window, m in (("IS", in_sample), ("OOS", out_of_sample)):
        dd = _float(m.get("max_drawdown_pct"))
        if not dd <= filters.max_drawdown_pct:
            a, b = _shown(dd, filters.max_drawdown_pct, 1)
            reasons.append(f"{window} max_drawdown {a}% > {b}%")
        win = _float(m.get("win_rate"))
        if not win >= filters.min_win_rate:
            a, b = _shown(win, filters.min_win_rate, 2)
            reasons.append(f"{window} win_rate {a} < {b}")
        pf = _float(m.get("profit_factor"))
        if not pf >= filters.min_profit_factor:
            a, b = _shown(pf, filters.min_profit_factor, 2)
            reasons.append(f"{window} profit_factor {a} < {b}")
        trades = int(m.get("n_trades") or 0)
        if trades < filters.min_trades:
            reasons.append(f"{window} n_trades {trades} < {filters.min_trades}")
        required = filters.require_positive_in_sample if window == "IS" else filters.require_positive_out_of_sample
        ret = _float(m.get("total_return_pct"))
        if required and not ret > 0:
            reasons.append(f"{window} total_return {ret:.2f}% is not positive")
    return reasons


def calmar(metrics: Mapping[str, Any]) -> float:
    """CAGR / max drawdown; inf for a positive CAGR without drawdown, NaN when undefined."""
    cagr, dd = _float(metrics.get("cagr_pct")), _float(metrics.get("max_drawdown_pct"))
    if not (math.isfinite(cagr) and math.isfinite(dd)):
        return math.nan
    if dd > 0:
        return cagr / dd
    return math.inf if cagr > 0 else 0.0


def rank_key(run: Mapping[str, Any]) -> tuple[float, float, str]:
    """Sort key, best first: in-sample Calmar, then in-sample win rate. Out-of-sample numbers
    are deliberately absent."""
    score, win = _float(run.get("score")), _float(run["in_sample"].get("win_rate"))
    return (-score if not math.isnan(score) else math.inf, -win if not math.isnan(win) else math.inf, run["label"])


def pick_winners(runs: list[dict[str, Any]], symbols: Iterable[str]) -> dict[str, dict[str, Any] | None]:
    """The best-ranked survivor per symbol, or None when nothing survived."""
    winners: dict[str, dict[str, Any] | None] = {}
    for symbol in symbols:
        survivors = sorted((r for r in runs if r["symbol"] == symbol and r["passed"]), key=rank_key)
        winners[symbol] = _winner(survivors[0]) if survivors else None
    return winners


def add_neighbors(runs: list[dict[str, Any]]) -> None:
    """Set `neighbors_passing` (share of grid neighbours that survive, None without neighbours)
    and `n_neighbors` on every run."""
    for run in runs:
        neighbors = [
            other
            for other in runs
            if other is not run
            and other["symbol"] == run["symbol"]
            and other["strategy"] == run["strategy"]
            and _differ_in_one(run["params"], other["params"])
        ]
        run["n_neighbors"] = len(neighbors)
        run["neighbors_passing"] = sum(o["passed"] for o in neighbors) / len(neighbors) if neighbors else None


def _differ_in_one(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    keys = set(a) | set(b)
    return sum(a.get(k) != b.get(k) for k in keys) == 1


def _winner(run: Mapping[str, Any]) -> dict[str, Any]:
    strategy = build(run["strategy"], run["symbol"], run["params"])
    return {
        "label": run["label"],
        "strategy": run["strategy"],
        "title": type(strategy).title,
        "params": dict(run["params"]),
        "rules": strategy.describe(),
        "score": run["score"],
        "neighbors_passing": run.get("neighbors_passing"),
        "in_sample": {k: run["in_sample"].get(k) for k in KEY_METRICS},
        "out_of_sample": {k: run["out_of_sample"].get(k) for k in KEY_METRICS},
    }


def _float(value: Any) -> float:
    return math.nan if value is None else float(value)


# --------------------------------------------------------------------------- running


def run_tournament(
    cfg: StrategyConfig,
    symbols: Iterable[str] | None = None,
    bars: Mapping[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """Run every strategy x grid point x symbol and return the results in the
    `results/tournament.json` schema. `bars` overrides the CSV cache (tests)."""
    symbols = list(cfg.assets) if symbols is None else list(symbols)
    if not symbols:
        raise ValueError("the tournament needs at least one symbol")
    t = cfg.tournament
    windows = {
        "in_sample": t.in_sample,
        "out_of_sample": t.out_of_sample,
        "full": (t.in_sample[0], t.out_of_sample[1]),
    }
    history = {s: bars[s] if bars is not None else data.load_daily(s) for s in symbols}
    runs: list[dict[str, Any]] = []
    full_equity: dict[tuple[str, str], pd.Series] = {}
    for symbol in symbols:
        symbol_runs = _run_symbol(symbol, history[symbol], cfg, windows)
        log.info("%s: %d configurations, %d survive", symbol, len(symbol_runs),
                 sum(r["passed"] for r, _ in symbol_runs))
        for run, full in symbol_runs:
            runs.append(run)
            full_equity[(symbol, run["label"])] = full.equity
    add_neighbors(runs)
    winners = pick_winners(runs, symbols)
    strategies = {s: build(w["strategy"], s, w["params"]) for s, w in winners.items() if w is not None}
    proxy = REGIME_PROXY if REGIME_PROXY in history else next(iter(strategies), symbols[0])
    portfolios = {
        name: _portfolio_block(name, windows[name], strategies, history, cfg, proxy)
        for name in ("out_of_sample", "full")
    }
    return {
        "generated_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {s: _data_summary(history[s]) for s in symbols},
        "config": {
            "in_sample": list(t.in_sample),
            "out_of_sample": list(t.out_of_sample),
            "full": list(windows["full"]),
            "filters": t.filters.model_dump(),
            "risk": cfg.risk.model_dump(),
            "slippage_bps": dict(cfg.execution.slippage_bps),
            "fee_bps": dict(cfg.execution.fee_bps),
            "ranking": "in-sample Calmar (CAGR / max drawdown), then in-sample win rate",
        },
        "n_configs": len(runs),
        "runs": runs,
        "winners": winners,
        "portfolio": portfolios["out_of_sample"],
        "portfolio_full": portfolios["full"],
        "winner_equity": {s: weekly(full_equity[(s, w["label"])]) for s, w in winners.items() if w is not None},
    }


def _run_symbol(
    symbol: str, bars: pd.DataFrame, cfg: StrategyConfig, windows: Mapping[str, tuple[str, str]]
) -> list[tuple[dict[str, Any], BacktestResult]]:
    bt_cfg = BacktestConfig.from_strategy(cfg, symbol)
    ppy = periods_per_year(symbol)
    out = []
    for name, cls in REGISTRY.items():
        for grid_point in cls.param_grid:
            strategy = build(name, symbol, grid_point)
            results = {w: run_backtest(bars, strategy, bt_cfg, *span) for w, span in windows.items()}
            metrics = {w: compute_metrics(r, ppy) for w, r in results.items()}
            reasons = fail_reasons(metrics["in_sample"], metrics["out_of_sample"], cfg.tournament.filters)
            full = results["full"]
            run = {
                "symbol": symbol,
                "strategy": name,
                "label": strategy.label(),
                "params": dict(strategy.params),
                "in_sample": metrics["in_sample"],
                "out_of_sample": metrics["out_of_sample"],
                "full": metrics["full"],
                "passed": not reasons,
                "fail_reasons": reasons,
                "score": calmar(metrics["in_sample"]),
                "regimes": regime_breakdown(full, bars),
                "approvals": {"entry_orders": full.entry_orders, "needing_approval": full.orders_needing_approval},
            }
            out.append((run, full))
    return out


def _portfolio_block(
    window: str,
    span: tuple[str, str],
    strategies: Mapping[str, Strategy],
    history: Mapping[str, pd.DataFrame],
    cfg: StrategyConfig,
    proxy: str,
) -> dict[str, Any] | None:
    if not strategies:
        return None
    result: PortfolioResult = run_portfolio({s: history[s] for s in strategies}, strategies, cfg, *span)
    return {
        "window": window,
        "start": span[0],
        "end": span[1],
        "symbols": result.symbols,
        "labels": result.labels,
        "metrics": compute_metrics(result.as_backtest_result(), result.periods_per_year),
        "regimes": regimes_on_proxy(result, history[proxy]),
        "regime_proxy": proxy,
        "kill_switch_would_fire": result.kill_switch_would_fire,
        "daily_loss_limit_hits": result.daily_loss_limit_hits,
        "daily_loss_limit_days": result.daily_loss_limit_days,
        "blocked_entries": result.blocked_entries,
        "shrunk_entries": result.shrunk_entries,
        "approvals": {"entry_orders": result.entry_orders, "needing_approval": result.orders_needing_approval},
        "trades_by_symbol": {s: sum(t.symbol == s for t in result.trades) for s in result.symbols},
        "equity": weekly(result.equity),
    }


def _data_summary(bars: pd.DataFrame) -> dict[str, Any]:
    if bars.empty:
        return {"first": None, "last": None, "bars": 0}
    return {"first": bars.index[0].date().isoformat(), "last": bars.index[-1].date().isoformat(), "bars": len(bars)}


def weekly(series: pd.Series) -> list[list[Any]]:
    """`[[date, value], ...]` at the last observation of each calendar week."""
    values = series.dropna()
    if values.empty:
        return []
    last = values.groupby(values.index.to_period("W")).tail(1)
    return [[ts.date().isoformat(), float(v)] for ts, v in last.items()]


# --------------------------------------------------------------------------- writing results


def to_json_safe(value: Any) -> Any:
    """Plain JSON types: floats rounded to 4 decimals, inf and NaN as None, dates as ISO strings."""
    if isinstance(value, Mapping):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        x = float(value)
        return round(x, 4) + 0.0 if math.isfinite(x) else None  # + 0.0 turns -0.0 into 0.0
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"cannot write a {type(value).__name__} to tournament.json")


def write_results(results: Mapping[str, Any], out_dir: Path | str = RESULTS_DIR) -> tuple[Path, Path]:
    """Write `tournament.json` and `tournament.md` into `out_dir`; return their paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out / JSON_NAME, out / MD_NAME
    safe = to_json_safe(results)
    _atomic_write(json_path, json.dumps(safe, indent=1, allow_nan=False) + "\n")
    _atomic_write(md_path, render_markdown(results))
    log.info("wrote %s and %s", json_path, md_path)
    return json_path, md_path


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- formatting


def _pct(value: Any, digits: int = 1, sign: bool = False) -> str:
    x = _float(value)
    if math.isnan(x):
        return "n/a"
    if math.isinf(x):
        return "inf"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def _num(value: Any, digits: int = 2) -> str:
    x = _float(value)
    if math.isnan(x):
        return "n/a"
    if math.isinf(x):
        return "inf"
    return f"{x:.{digits}f}"


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "n/a"


def _neighbors(run: Mapping[str, Any]) -> str:
    n = run.get("n_neighbors") or 0
    share = run.get("neighbors_passing")
    return "n/a" if not n or share is None else f"{round(share * n)}/{n}"


def _usd(value: Any) -> str:
    return f"${_float(value):,.0f}"


def _window_text(span: Iterable[str]) -> str:
    start, end = span
    return f"{start} to {end}"


def _symbols(results: Mapping[str, Any]) -> list[str]:
    return list(results.get("data") or results.get("winners") or {})


def _runs_for(results: Mapping[str, Any], symbol: str) -> list[Mapping[str, Any]]:
    return sorted((r for r in results["runs"] if r["symbol"] == symbol), key=rank_key)


def _filters_text(filters: Mapping[str, Any]) -> str:
    positive = []
    if filters.get("require_positive_in_sample"):
        positive.append("in-sample")
    if filters.get("require_positive_out_of_sample"):
        positive.append("out-of-sample")
    parts = [
        f"max drawdown at most {filters['max_drawdown_pct']:g}%",
        f"win rate at least {filters['min_win_rate']:.2f}",
        f"profit factor at least {filters['min_profit_factor']:g}",
        f"at least {filters['min_trades']} trades",
    ]
    if positive:
        parts.append("a positive return " + " and ".join(positive))
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _metrics_row(name: str, m: Mapping[str, Any]) -> str:
    return (
        f"| {name} | {_pct(m.get('total_return_pct'), sign=True)} | {_pct(m.get('cagr_pct'), 2, sign=True)} "
        f"| {_pct(m.get('max_drawdown_pct'))} | {_num(m.get('win_rate'))} | {_num(m.get('profit_factor'))} "
        f"| {int(m.get('n_trades') or 0)} | {_pct(m.get('buy_hold_return_pct'), sign=True)} |"
    )


METRICS_HEADER = (
    "| Window | Return | CAGR | Max drawdown | Win rate | Profit factor | Trades | Buy and hold |\n"
    "|---|---|---|---|---|---|---|---|"
)


def _portfolio_rows(results: Mapping[str, Any]) -> list[str]:
    rows = []
    for key, name in (("portfolio", "Out-of-sample"), ("portfolio_full", "Full period")):
        p = results.get(key)
        if not p:
            continue
        m = p["metrics"]
        kills = p.get("kill_switch_would_fire") or []
        rows.append(
            f"| {name}, {p['start']} to {p['end']} | {_pct(m.get('total_return_pct'), sign=True)} "
            f"| {_pct(m.get('cagr_pct'), 2, sign=True)} | {_pct(m.get('max_drawdown_pct'))} | {_num(m.get('sharpe'))} "
            f"| {int(m.get('n_trades') or 0)} | {_num(m.get('win_rate'))} | {_pct(m.get('exposure_pct'), 0)} "
            f"| {', '.join(k['date'] for k in kills) or 'never'} | {p.get('daily_loss_limit_hits', 0)} |"
        )
    return rows


PORTFOLIO_HEADER = (
    "| Window | Return | CAGR | Max drawdown | Sharpe | Trades | Win rate | Time invested "
    "| Kill switch would fire | Daily loss limit days |\n|---|---|---|---|---|---|---|---|---|---|"
)


# --------------------------------------------------------------------------- results/tournament.md


def render_markdown(results: Mapping[str, Any]) -> str:
    cfg = results["config"]
    symbols = _symbols(results)
    lines = [
        "# Tournament results",
        "",
        f"Generated {results['generated_at']} by `python -m bot tournament`. This is a backtest of a "
        "paper-trading research bot, not financial advice. Past results don't predict future returns.",
        "",
        "## Setup",
        "",
        *[
            f"- **{s} data:** {d['first']} to {d['last']}, {d['bars']:,} daily bars."
            for s, d in (results.get("data") or {}).items()
        ],
        f"- **Windows:** in-sample (IS) {_window_text(cfg['in_sample'])}, out-of-sample (OOS) "
        f"{_window_text(cfg['out_of_sample'])}. Indicators warm up on the full history; trades happen "
        "only inside each window, and each window starts flat.",
        f"- **Configurations tested:** {results['n_configs']} ({len(REGISTRY)} strategies, each over its "
        f"parameter grid, on {len(symbols)} assets).",
        f"- **Filters, applied to both windows:** {_filters_text(cfg['filters'])}.",
        "- **Ranking:** the tournament ranks survivors by in-sample Calmar ratio (CAGR / max drawdown), "
        "then in-sample win rate. The out-of-sample window only passes or fails a configuration.",
        f"- **Costs per fill:** slippage {_bps(cfg['slippage_bps'])}; fees {_bps(cfg['fee_bps'])}.",
        f"- **Sizing:** {_usd(cfg['risk']['capital_usd'])} capital, {cfg['risk']['risk_per_trade_pct']:g}% "
        f"risk per trade, at most {cfg['risk']['max_position_pct']:g}% and "
        f"{_usd(cfg['risk']['max_position_usd'])} per position, {cfg['risk']['max_total_exposure_pct']:g}% "
        "total exposure. Each single-asset backtest sizes on its own equity.",
        "",
        "## Winners",
        "",
    ]
    for symbol in symbols:
        lines += _winner_md(results, symbol, heading="###")
    lines += ["## Survivors", "", "| Asset | Strategy | Tested | Survivors |", "|---|---|---|---|"]
    for symbol in symbols:
        for name in REGISTRY:
            runs = [r for r in results["runs"] if r["symbol"] == symbol and r["strategy"] == name]
            if runs:
                lines.append(f"| {symbol} | {name} | {len(runs)} | {sum(r['passed'] for r in runs)} |")
    lines += ["", "## Leaderboards", "",
              f"The top {LEADERBOARD_SIZE} configurations per asset by in-sample score, failures included. "
              "Neighbours counts the grid points one parameter away that also survive.", ""]
    for symbol in symbols:
        lines += _leaderboard_md(results, symbol)
    lines += _portfolio_md(results)
    lines += _regimes_md(results)
    lines += _approvals_md(results)
    lines += _caveats_md(results)
    return "\n".join(lines).rstrip() + "\n"


def _bps(table: Mapping[str, Any]) -> str:
    return ", ".join(f"{klass} {value:g} bps" for klass, value in table.items())


def _winner_md(results: Mapping[str, Any], symbol: str, heading: str) -> list[str]:
    cfg = results["config"]
    winner = (results.get("winners") or {}).get(symbol)
    if winner is None:
        runs = _runs_for(results, symbol)
        lines = [f"{heading} {symbol}: no winner", ""]
        if runs:
            best = runs[0]
            lines += [
                f"No configuration passed every filter in both windows, so {symbol} stays disabled. "
                f"The best in-sample score was `{best['label']}`, which failed on: "
                f"{'; '.join(best['fail_reasons']) or 'nothing'}.",
                "",
            ]
        return lines
    rules = winner["rules"]
    survivors = sum(r["passed"] for r in results["runs"] if r["symbol"] == symbol)
    return [
        f"{heading} {symbol}: {winner.get('title') or winner['strategy']}",
        "",
        f"`{winner['label']}` passed every filter in both windows and ranked first of {survivors} survivors "
        f"on in-sample Calmar ratio ({_num(winner['score'])}). {_neighbor_sentence(results, symbol, winner)}",
        "",
        _approval_sentence(results, symbol, winner),
        "",
        f"- **Entry:** {rules['entry']}",
        f"- **Exit:** {rules['exit']}",
        f"- **Stop loss:** {rules['stop_loss']}",
        f"- **Take profit:** {rules['take_profit']}",
        f"- **Timeframe:** {rules['timeframe']}",
        "",
        METRICS_HEADER,
        _metrics_row(f"In-sample, {_window_text(cfg['in_sample'])}", winner["in_sample"]),
        _metrics_row(f"Out-of-sample, {_window_text(cfg['out_of_sample'])}", winner["out_of_sample"]),
        "",
    ]


def _neighbor_sentence(results: Mapping[str, Any], symbol: str, winner: Mapping[str, Any]) -> str:
    run = _run(results, symbol, winner["label"])
    n, share = run.get("n_neighbors") or 0, run.get("neighbors_passing")
    if not n or share is None:
        return ""
    passing = round(share * n)
    verb = "survives" if passing == 1 else "survive"
    return f"Of its {n} grid neighbours (one parameter different), {passing} also {verb}."


def _approval_sentence(results: Mapping[str, Any], symbol: str, winner: Mapping[str, Any]) -> str:
    approvals = _run(results, symbol, winner["label"])["approvals"]
    start, end = (pd.Timestamp(d) for d in results["config"]["full"])
    years = max((end - start).days / 365.25, 1e-9)
    threshold = _usd(results["config"]["risk"]["approval_threshold_usd"])
    return (
        f"Over the full period it placed about {approvals['entry_orders'] / years:.0f} entries a year, and "
        f"{_share(approvals['needing_approval'], approvals['entry_orders'])} of them were above {threshold}, so "
        "expect to approve most entries in Telegram."
    )


def _run(results: Mapping[str, Any], symbol: str, label: str) -> Mapping[str, Any]:
    return next(r for r in results["runs"] if r["symbol"] == symbol and r["label"] == label)


def _leaderboard_md(results: Mapping[str, Any], symbol: str) -> list[str]:
    lines = [
        f"### {symbol}",
        "",
        "| # | Configuration | IS Calmar | IS win rate | IS return | IS max DD | OOS return | OOS max DD "
        "| OOS win rate | Trades IS/OOS | Neighbours | Result |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rank, run in enumerate(_runs_for(results, symbol)[:LEADERBOARD_SIZE], start=1):
        is_, oos = run["in_sample"], run["out_of_sample"]
        result = "pass" if run["passed"] else "; ".join(run["fail_reasons"])
        lines.append(
            f"| {rank} | `{run['label']}` | {_num(run['score'])} | {_num(is_.get('win_rate'))} "
            f"| {_pct(is_.get('total_return_pct'), sign=True)} | {_pct(is_.get('max_drawdown_pct'))} "
            f"| {_pct(oos.get('total_return_pct'), sign=True)} | {_pct(oos.get('max_drawdown_pct'))} "
            f"| {_num(oos.get('win_rate'))} | {int(is_.get('n_trades') or 0)}/{int(oos.get('n_trades') or 0)} "
            f"| {_neighbors(run)} | {result} |"
        )
    return lines + [""]


def _portfolio_md(results: Mapping[str, Any]) -> list[str]:
    lines = ["## Portfolio of the winners", ""]
    ref = results.get("portfolio") or results.get("portfolio_full")
    if not ref:
        return lines + ["No asset has a winner, so there is no portfolio to test.", ""]
    traded = ", ".join(f"{s} `{ref['labels'][s]}`" for s in ref["symbols"])
    lines += [
        f"The winners ({traded}) trade together on one "
        f"{_usd(results['config']['risk']['capital_usd'])} account. Each entry is sized on the portfolio's "
        "equity and shrunk to fit the total exposure cap. The daily loss limit blocks entries for the rest of "
        "the New York day, and every close where the drawdown reaches the kill-switch limit is listed. The "
        "simulation keeps trading after that point; the live bot would stop until you resume it. "
        "Only the out-of-sample row is a fair estimate: the full period includes the in-sample years the "
        "winners were picked on. `tournament.json` stores the out-of-sample run under `portfolio` and the "
        "full period under `portfolio_full`.",
        "",
        PORTFOLIO_HEADER,
        *_portfolio_rows(results),
        "",
    ]
    for key, name in (("portfolio", "out-of-sample"), ("portfolio_full", "full-period")):
        p = results.get(key)
        if not p:
            continue
        kills = p.get("kill_switch_would_fire") or []
        blocked = p.get("blocked_entries") or []
        by_symbol = ", ".join(f"{s} {n}" for s, n in (p.get("trades_by_symbol") or {}).items())
        lines.append(
            f"- **{name.capitalize()}:** trades by asset: {by_symbol or 'none'}. "
            f"{len(blocked)} entries blocked, {p.get('shrunk_entries', 0)} shrunk by the exposure cap."
        )
        lines += [f"  - Kill switch on {k['date']}: {k['reason']}." for k in kills]
        days = p.get("daily_loss_limit_days") or []
        if days:
            lines.append(f"  - Daily loss limit hit on: {', '.join(days)}.")
    return lines + [""]


def _regime_rows(regimes: Mapping[str, Any]) -> list[str]:
    rows = []
    for group in ("trend", "vol"):
        for name, r in (regimes.get(group) or {}).items():
            rows.append(
                f"| {group} {name} | {_pct(r.get('return_pct'), sign=True)} | {_pct(r.get('max_dd_pct'))} "
                f"| {r.get('n_trades', 0)} | {r.get('n_bars', 0)} |"
            )
    for name, r in (regimes.get("stress") or {}).items():
        span = f" ({r['start']} to {r['end']})" if r.get("start") else ""
        rows.append(
            f"| {name}{span} | {_pct(r.get('return_pct'), sign=True)} | {_pct(r.get('max_dd_pct'))} "
            f"| {r.get('n_trades', 0)} | {r.get('n_bars', 0)} |"
        )
    return rows


REGIME_HEADER = "| Regime or window | Return | Max drawdown | Trades entered | Bars |\n|---|---|---|---|---|"


def _regimes_md(results: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Regimes and stress windows",
        "",
        "Full-period runs. Trend regimes: bull when the close is above a rising 200-day average, bear when "
        "below a falling one, sideways otherwise. Vol regimes compare 20-day realized volatility with its "
        "1-year median. Returns compound only the days spent in each regime. n/a means no bars in the window.",
        "",
    ]
    for symbol in _symbols(results):
        winner = (results.get("winners") or {}).get(symbol)
        if winner is None:
            continue
        run = _run(results, symbol, winner["label"])
        lines += [f"### {symbol} `{winner['label']}`", "", REGIME_HEADER, *_regime_rows(run["regimes"]), ""]
    p = results.get("portfolio_full")
    if p:
        lines += [
            f"### Portfolio, full period (regimes labelled on {p['regime_proxy']})",
            "",
            f"Equity is sampled on {p['regime_proxy']}'s trading days, so weekend crypto P&L lands on the next "
            f"trading day. Trades entered on a day {p['regime_proxy']} didn't trade count only in the stress "
            "windows.",
            "",
            REGIME_HEADER,
            *_regime_rows(p["regimes"]),
            "",
        ]
    return lines


def _approvals_md(results: Mapping[str, Any]) -> list[str]:
    threshold = results["config"]["risk"]["approval_threshold_usd"]
    lines = [
        "## Manual approvals",
        "",
        f"Entries above {_usd(threshold)} wait for your approval in Telegram. The backtest assumes you approve "
        "every one of them in time; in paper trading a rejected, expired or drifted approval skips the trade.",
        "",
        "| Scope | Entry orders | Above the threshold | Share |",
        "|---|---|---|---|",
    ]
    for symbol in _symbols(results):
        winner = (results.get("winners") or {}).get(symbol)
        if winner is None:
            continue
        a = _run(results, symbol, winner["label"])["approvals"]
        lines.append(
            f"| {symbol} winner, full period | {a['entry_orders']} | {a['needing_approval']} "
            f"| {_share(a['needing_approval'], a['entry_orders'])} |"
        )
    for key, name in (("portfolio", "Portfolio, out-of-sample"), ("portfolio_full", "Portfolio, full period")):
        p = results.get(key)
        if p:
            a = p["approvals"]
            lines.append(
                f"| {name} | {a['entry_orders']} | {a['needing_approval']} "
                f"| {_share(a['needing_approval'], a['entry_orders'])} |"
            )
    total = sum(r["approvals"]["entry_orders"] for r in results["runs"])
    needing = sum(r["approvals"]["needing_approval"] for r in results["runs"])
    lines.append(f"| All {results['n_configs']} configurations, full period | {total} | {needing} "
                 f"| {_share(needing, total)} |")
    return lines + [""]


def _caveats_md(results: Mapping[str, Any]) -> list[str]:
    n = results["n_configs"]
    cfg = results["config"]
    runs = results["runs"]
    worst_dd = max((_float(r[w].get("max_drawdown_pct")) for r in runs for w in WINDOW_TAGS), default=math.nan)
    dd_failures = sum(any("max_drawdown" in reason for reason in r["fail_reasons"]) for r in runs)
    return [
        "## Caveats",
        "",
        f"- **Multiple testing.** The tournament tried {n} configurations. Even with no real edge, some "
        "would pass both windows by chance, and the winner is the best of those. Expect live results "
        "below these numbers. A winner whose grid neighbours also survive is less likely to be a fluke.",
        f"- **The drawdown filter rarely binds at this size.** With {cfg['risk']['risk_per_trade_pct']:g}% risk "
        f"per trade and at most {cfg['risk']['max_position_pct']:g}% per position, the worst single-asset "
        f"drawdown in any window was {_pct(worst_dd)} against a {cfg['filters']['max_drawdown_pct']:g}% limit, "
        f"and {dd_failures} of {n} configurations failed on drawdown. Win rate, profit factor and the "
        "positive-return checks did most of the filtering.",
        "- **The out-of-sample window is spent.** It was seen once, here. If you change a rule or a filter "
        "after reading these results and rerun, the out-of-sample window becomes in-sample.",
        "- **Daily bars only.** Stops are checked against each bar's high and low; when a bar touches both "
        "the stop and a target, the backtest assumes the stop hit first. The order of moves inside a day is "
        "unknown, and the daily loss limit and kill switch are checked only at closes, so intraday dips "
        "that recover are invisible here but not to the live bot.",
        "- **No Jev in the backtest.** Jev can only veto entries live, so live trading takes a subset of "
        "these trades. Replaying past headlines through Jev would cost money and leak hindsight.",
        "- **Data differences.** The backtest uses Yahoo's split- and dividend-adjusted bars. The live bot "
        "uses Alpaca bars (SIP feed for stocks, UTC days built from hourly bars for crypto), which are not "
        "dividend-adjusted and can differ in the open, high and low. Signals can differ near thresholds.",
        f"- **Costs are assumptions.** Slippage {_bps(cfg['slippage_bps'])} and fees {_bps(cfg['fee_bps'])} "
        "per fill. Real slippage is larger in fast markets and at gaps, and crypto spreads widen in stress.",
        "- **Approvals are assumed granted.** Entries above the approval threshold wait for Telegram; a "
        "late, rejected or drifted approval skips a trade the backtest took.",
        "- **Survivorship.** SPY, QQQ and BTC/USD were chosen knowing they survived and grew over this "
        "period. QQQ's tech run and Bitcoin's rise flatter any long-only rule tested on them.",
        "- **Small sizes.** Sizing risks 1% of a $10,000 slice per trade with fractional quantities, so "
        "returns on the slice are modest by design; the buy-and-hold column is not a like-for-like "
        "comparison because it is fully invested without stops.",
        "- **Portfolio versus single-asset runs.** Each single-asset backtest sizes on its own equity. The "
        "portfolio shares one account, so its trades can be smaller than the single-asset runs' trades.",
    ]


# --------------------------------------------------------------------------- strategy.md


def render_winners_section(results: Mapping[str, Any]) -> str:
    """The WINNERS section of strategy.md: the rules for every enabled asset, in plain English."""
    symbols = _symbols(results)
    winners = results.get("winners") or {}
    enabled = [s for s in symbols if winners.get(s)]
    lines = [
        f"_Written by `python -m bot tournament --apply` from the run of {results['generated_at']}._",
        "",
        f"The tournament enabled {len(enabled)} of {len(symbols)} assets: "
        f"{', '.join(enabled) if enabled else 'none'}. The bot trades each enabled asset with the one "
        "configuration below, long only. An asset without a winner stays disabled, and the bot doesn't "
        "trade it until a future tournament finds one.",
        "",
    ]
    for symbol in symbols:
        winner = winners.get(symbol)
        if winner is None:
            lines += _disabled_md(results, symbol)
        else:
            lines += _winner_md(results, symbol, heading="###")
    return "\n".join(lines).rstrip()


def _disabled_md(results: Mapping[str, Any], symbol: str) -> list[str]:
    runs = _runs_for(results, symbol)
    lines = [f"### {symbol}: disabled", ""]
    if not runs:
        return lines + [f"The tournament didn't test {symbol}.", ""]
    best = runs[0]
    return lines + [
        f"No configuration passed every filter in both windows, so the bot doesn't trade {symbol}. The "
        f"config keeps `{best['label']}`, the best in-sample score, for reference only, with "
        f"`enabled: false`. It failed on: {'; '.join(best['fail_reasons'])}.",
        "",
    ]


def render_tournament_section(results: Mapping[str, Any]) -> str:
    """The TOURNAMENT section of strategy.md: a compact leaderboard and the portfolio numbers."""
    cfg = results["config"]
    symbols = _symbols(results)
    winners = results.get("winners") or {}
    lines = [
        f"_Last run: {results['generated_at']}. Read `results/tournament.md` for every run, the regime and "
        "stress tables and the caveats, and `results/tournament.json` for the raw numbers._",
        "",
        f"The tournament backtested {results['n_configs']} configurations on daily bars: {len(REGISTRY)} "
        f"strategies, each over its parameter grid, on {', '.join(symbols)}. A configuration survives only "
        f"if it passes every filter in both the in-sample window ({_window_text(cfg['in_sample'])}) and the "
        f"out-of-sample window ({_window_text(cfg['out_of_sample'])}): {_filters_text(cfg['filters'])}. "
        "The tournament ranks survivors by in-sample Calmar ratio (CAGR / max drawdown), then in-sample win "
        "rate. The out-of-sample window only passes or fails a configuration.",
        "",
        "### Leaderboard",
        "",
        "| Asset | Survivors | Winner | IS Calmar | IS return | IS max DD | OOS return | OOS max DD | OOS trades |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for symbol in symbols:
        runs = _runs_for(results, symbol)
        survivors = sum(r["passed"] for r in runs)
        winner = winners.get(symbol)
        if winner is None:
            lines.append(f"| {symbol} | {survivors} of {len(runs)} | none | | | | | | |")
            continue
        is_, oos = winner["in_sample"], winner["out_of_sample"]
        lines.append(
            f"| {symbol} | {survivors} of {len(runs)} | `{winner['label']}` | {_num(winner['score'])} "
            f"| {_pct(is_.get('total_return_pct'), sign=True)} | {_pct(is_.get('max_drawdown_pct'))} "
            f"| {_pct(oos.get('total_return_pct'), sign=True)} | {_pct(oos.get('max_drawdown_pct'))} "
            f"| {int(oos.get('n_trades') or 0)} |"
        )
    lines += ["", "### Winners traded together", ""]
    rows = _portfolio_rows(results)
    if rows:
        lines += [
            "One shared account, with the total exposure cap, the daily loss limit and the kill-switch "
            "drawdown applied.",
            "",
            PORTFOLIO_HEADER,
            *rows,
        ]
    else:
        lines.append("No asset has a winner, so there is no portfolio to test.")
    lines += [
        "",
        "Only the out-of-sample row is a fair estimate: the full period includes the years the winners were "
        f"picked on. With {results['n_configs']} configurations tested, some survivors pass by luck. Treat "
        "these numbers as an optimistic upper bound, and compare them with paper trading before you trust them.",
    ]
    return "\n".join(lines).rstrip()


def _reference_run(results: Mapping[str, Any], symbol: str) -> Mapping[str, Any] | None:
    runs = _runs_for(results, symbol)
    return runs[0] if runs else None


def apply_winners(results: Mapping[str, Any], strategy_path: Path | str = STRATEGY_PATH) -> StrategyConfig:
    """Enable each winner in the CONFIG block's `assets` (a symbol without one is disabled, keeping
    its best in-sample configuration for reference) and rewrite the WINNERS and TOURNAMENT
    sections. Every edit is staged on a copy and validated before it replaces `strategy_path`."""
    path = Path(strategy_path)
    original = path.read_text()
    config = read_config_block(original)
    assets = config.get("assets")
    if not isinstance(assets, dict):
        raise ConfigError(f"{path}: the CONFIG block has no assets mapping")
    winners = results.get("winners") or {}
    for symbol in _symbols(results):
        winner = winners.get(symbol)
        if winner is not None:
            build(winner["strategy"], symbol, winner["params"])  # the runner must be able to build it
            assets[symbol] = {"enabled": True, "strategy": winner["strategy"], "params": dict(winner["params"])}
            continue
        ref = _reference_run(results, symbol) or assets.get(symbol)
        if ref is None:
            continue
        assets[symbol] = {"enabled": False, "strategy": ref["strategy"], "params": dict(ref.get("params") or {})}

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(original)
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        write_config_block(config, tmp)
        replace_section("WINNERS", render_winners_section(results), tmp)
        replace_section("TOURNAMENT", render_tournament_section(results), tmp)
        loaded = load_strategy(tmp)
        _check_round_trip(loaded, config)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info("applied the tournament winners to %s: enabled %s", path, sorted(loaded.enabled_assets) or "none")
    return loaded


def _check_round_trip(loaded: StrategyConfig, written: Mapping[str, Any]) -> None:
    if loaded.model_dump() != StrategyConfig.model_validate(written).model_dump():
        raise ConfigError("strategy.md did not read back as written; left unchanged")
    for symbol, rule in loaded.enabled_assets.items():
        build(rule.strategy, symbol, dict(rule.params))


# --------------------------------------------------------------------------- entry point


def run_and_write(
    apply: bool, cfg_path: Path | str = STRATEGY_PATH, out_dir: Path | str = RESULTS_DIR
) -> dict[str, Any]:
    """`python -m bot tournament [--apply]`: run, write `results/`, optionally update strategy.md."""
    cfg = load_strategy(cfg_path)
    results = run_tournament(cfg)
    write_results(results, out_dir)
    if apply:
        apply_winners(results, cfg_path)
    return results

