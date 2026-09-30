"""Tournament: filters, in-sample-only ranking, neighbours, JSON output, strategy.md updates."""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot import data
from bot.backtest import tournament as tour
from bot.backtest.tournament import (
    add_neighbors,
    apply_winners,
    calmar,
    fail_reasons,
    pick_winners,
    run_and_write,
    run_tournament,
    to_json_safe,
    weekly,
    write_results,
)
from bot.config import (
    ConfigError,
    TournamentFilters,
    load_strategy,
    read_config_block,
    write_config_block,
)
from bot.strategies import REGISTRY, TrendStrategy

ROOT = Path(__file__).resolve().parent.parent
FILTERS = TournamentFilters(max_drawdown_pct=15.0, min_win_rate=0.40, min_profit_factor=1.2, min_trades=8)
GOOD = {"max_drawdown_pct": 5.0, "win_rate": 0.5, "profit_factor": 1.5, "n_trades": 10, "total_return_pct": 3.0}


def metrics(**overrides: Any) -> dict[str, Any]:
    base = {**GOOD, "cagr_pct": 2.0, "sharpe": 1.0, "exposure_pct": 50.0, "buy_hold_return_pct": 10.0}
    return {**base, **overrides}


def run(symbol: str, params: dict[str, Any], passed: bool = True, is_: dict | None = None,
        oos: dict | None = None, name: str = "trend") -> dict[str, Any]:
    strategy = REGISTRY[name](symbol, **params)
    in_sample = is_ or metrics()
    return {
        "symbol": symbol,
        "strategy": name,
        "label": strategy.label(),
        "params": dict(strategy.params),
        "in_sample": in_sample,
        "out_of_sample": oos or metrics(),
        "full": metrics(),
        "passed": passed,
        "fail_reasons": [] if passed else ["OOS win_rate 0.35 < 0.40"],
        "score": calmar(in_sample),
        "regimes": {},
        "approvals": {"entry_orders": 10, "needing_approval": 9},
    }


# --------------------------------------------------------------------------- filters


def test_passing_metrics_have_no_fail_reasons():
    assert fail_reasons(GOOD, GOOD, FILTERS) == []


def test_fail_reasons_name_the_window_and_the_numbers():
    bad = {"max_drawdown_pct": 18.2, "win_rate": 0.35, "profit_factor": 1.05, "n_trades": 5, "total_return_pct": -3.2}
    assert fail_reasons(GOOD, bad, FILTERS) == [
        "OOS max_drawdown 18.2% > 15.0%",
        "OOS win_rate 0.35 < 0.40",
        "OOS profit_factor 1.05 < 1.20",
        "OOS n_trades 5 < 8",
        "OOS total_return -3.20% is not positive",
    ]
    assert fail_reasons({**GOOD, "win_rate": 0.2}, GOOD, FILTERS) == ["IS win_rate 0.20 < 0.40"]


def test_fail_reasons_never_print_a_value_equal_to_its_threshold():
    reasons = fail_reasons({**GOOD, "win_rate": 0.3996, "max_drawdown_pct": 15.04}, GOOD, FILTERS)
    assert reasons == ["IS max_drawdown 15.04% > 15.00%", "IS win_rate 0.3996 < 0.4000"]


def test_profit_factor_edge_cases():
    assert fail_reasons({**GOOD, "profit_factor": math.inf}, GOOD, FILTERS) == []  # no losing trades
    assert fail_reasons({**GOOD, "profit_factor": None}, GOOD, FILTERS) == ["IS profit_factor nan < 1.20"]
    assert fail_reasons(GOOD, {**GOOD, "profit_factor": math.nan}, FILTERS) == ["OOS profit_factor nan < 1.20"]


def test_a_window_without_trades_fails():
    empty = {"max_drawdown_pct": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "n_trades": 0, "total_return_pct": 0.0}
    assert fail_reasons(GOOD, empty, FILTERS) == [
        "OOS win_rate 0.00 < 0.40",
        "OOS profit_factor 0.00 < 1.20",
        "OOS n_trades 0 < 8",
        "OOS total_return 0.00% is not positive",
    ]


def test_positive_return_requirements_can_be_switched_off():
    filters = FILTERS.model_copy(update={"require_positive_out_of_sample": False})
    losing = {**GOOD, "total_return_pct": -1.0}
    assert fail_reasons(GOOD, losing, filters) == []
    assert fail_reasons(losing, GOOD, filters) == ["IS total_return -1.00% is not positive"]


def test_calmar():
    assert calmar({"cagr_pct": 10.0, "max_drawdown_pct": 5.0}) == 2.0
    assert calmar({"cagr_pct": 1.0, "max_drawdown_pct": 0.0}) == math.inf
    assert calmar({"cagr_pct": 0.0, "max_drawdown_pct": 0.0}) == 0.0
    assert math.isnan(calmar({"cagr_pct": None, "max_drawdown_pct": 1.0}))


# --------------------------------------------------------------------------- ranking


def test_winner_is_ranked_on_in_sample_calmar_never_on_out_of_sample():
    grid = TrendStrategy.param_grid
    strong_is = run("SPY", grid[0], is_=metrics(cagr_pct=10.0, max_drawdown_pct=5.0),  # IS Calmar 2.0
                    oos=metrics(cagr_pct=1.0, max_drawdown_pct=10.0, win_rate=0.41))
    strong_oos = run("SPY", grid[1], is_=metrics(cagr_pct=6.0, max_drawdown_pct=4.0),  # IS Calmar 1.5
                     oos=metrics(cagr_pct=30.0, max_drawdown_pct=2.0, win_rate=0.9))
    failed = run("SPY", grid[2], passed=False, is_=metrics(cagr_pct=50.0, max_drawdown_pct=5.0))  # IS Calmar 10

    winners = pick_winners([strong_oos, failed, strong_is], ["SPY"])

    assert winners["SPY"]["label"] == strong_is["label"]
    assert winners["SPY"]["score"] == 2.0
    assert winners["SPY"]["rules"] == REGISTRY["trend"]("SPY", **grid[0]).describe()


def test_ties_on_calmar_break_on_in_sample_win_rate():
    grid = TrendStrategy.param_grid
    high_is_win = run("QQQ", grid[3], is_=metrics(win_rate=0.6), oos=metrics(win_rate=0.41))
    high_oos_win = run("QQQ", grid[4], is_=metrics(win_rate=0.5), oos=metrics(win_rate=0.95, cagr_pct=40.0))

    assert pick_winners([high_oos_win, high_is_win], ["QQQ"])["QQQ"]["label"] == high_is_win["label"]


def test_a_symbol_without_survivors_has_no_winner():
    runs = [run("BTC/USD", p, passed=False) for p in TrendStrategy.param_grid]
    assert pick_winners(runs, ["BTC/USD", "SPY"]) == {"BTC/USD": None, "SPY": None}


def test_neighbors_passing_counts_grid_points_one_parameter_away():
    passing = {(20, 100, 3), (50, 100, 3), (50, 200, 4)}
    runs = [
        run("SPY", p, passed=(p["fast"], p["slow"], p["atr_mult"]) in passing) for p in TrendStrategy.param_grid
    ]
    runs.append(run("QQQ", {"fast": 50, "slow": 100, "atr_mult": 3}))  # another symbol: never a neighbour
    runs.append(run("SPY", {"entry_rsi": 5, "atr_mult": 3, "max_hold": 5}, name="meanrev"))

    add_neighbors(runs)

    by_key = {(r["symbol"], r["label"]): r for r in runs}
    point = by_key[("SPY", "trend(atr_mult=3,fast=20,slow=100)")]
    # Neighbours: fast=50 (passes), slow=200 (fails), atr_mult=4 (fails).
    assert (point["n_neighbors"], point["neighbors_passing"]) == (3, pytest.approx(1 / 3))
    corner = by_key[("SPY", "trend(atr_mult=4,fast=50,slow=100)")]
    assert corner["neighbors_passing"] == pytest.approx(2 / 3)  # (50,100,3) and (50,200,4) pass
    lonely = by_key[("QQQ", "trend(atr_mult=3,fast=50,slow=100)")]
    assert (lonely["n_neighbors"], lonely["neighbors_passing"]) == (0, None)


# --------------------------------------------------------------------------- JSON


def test_json_safe_rounds_and_nulls_non_finite_values():
    raw = {
        "inf": math.inf,
        "nan": float("nan"),
        "np": np.float64(1.234567),
        "int": np.int64(7),
        "flag": np.bool_(True),
        "neg_zero": -0.00001,
        "when": pd.Timestamp("2024-01-02"),
        "nested": [(1.00004, -math.inf)],
    }
    assert to_json_safe(raw) == {
        "inf": None,
        "nan": None,
        "np": 1.2346,
        "int": 7,
        "flag": True,
        "neg_zero": 0.0,
        "when": "2024-01-02T00:00:00",
        "nested": [[1.0, None]],
    }
    assert str(to_json_safe(-0.00001)) == "0.0"
    with pytest.raises(TypeError):
        to_json_safe({"bad": object()})


def test_weekly_keeps_the_last_observation_of_each_week():
    idx = pd.date_range("2024-01-03", "2024-01-16", freq="B")  # Wednesday to the Tuesday two weeks on
    series = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    assert weekly(series) == [["2024-01-05", 2.0], ["2024-01-12", 7.0], ["2024-01-16", 9.0]]
    assert weekly(pd.Series(dtype=float)) == []


# --------------------------------------------------------------------------- end to end on synthetic bars


def walk(n: int, seed: int, freq: str, drift: float = 0.0012) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.013, n)))
    open_ = np.concatenate([[100.0], close[:-1]]) * np.exp(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, n)))
    idx = pd.date_range("2017-01-02", periods=n, freq=freq, name="date")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx)


SYNTHETIC = {"SPY": walk(1000, 1, "B"), "BTC/USD": walk(1400, 2, "D")}
LENIENT = {"max_drawdown_pct": 50.0, "min_win_rate": 0.0, "min_profit_factor": 0.0, "min_trades": 1,
           "require_positive_in_sample": False, "require_positive_out_of_sample": False}


def small_config(path: Path) -> Path:
    """Copy strategy.md to `path`, trading SPY and BTC/USD on the synthetic windows with lenient filters."""
    shutil.copy(ROOT / "strategy.md", path)
    config = read_config_block(path.read_text())
    config["assets"].pop("QQQ")
    config["tournament"] = {"in_sample": ["2017-01-02", "2018-12-31"], "out_of_sample": ["2019-01-01", "2020-10-31"],
                            "filters": LENIENT}
    write_config_block(config, path)
    return path


@pytest.fixture
def small_md(tmp_path) -> Path:
    return small_config(tmp_path / "strategy.md")


@pytest.fixture(scope="module")
def results(tmp_path_factory) -> dict[str, Any]:
    path = small_config(tmp_path_factory.mktemp("cfg") / "strategy.md")
    return run_tournament(load_strategy(path), bars=SYNTHETIC)


def test_tournament_results_follow_the_schema(results):
    n_grid = sum(len(cls.param_grid) for cls in REGISTRY.values())
    assert results["n_configs"] == len(results["runs"]) == 2 * n_grid
    assert set(results) >= {"generated_at", "data", "config", "n_configs", "runs", "winners", "portfolio",
                            "winner_equity"}
    assert results["data"]["SPY"] == {"first": "2017-01-02", "last": SYNTHETIC["SPY"].index[-1].date().isoformat(),
                                      "bars": 1000}
    run0 = results["runs"][0]
    assert set(run0) >= {"symbol", "strategy", "label", "params", "in_sample", "out_of_sample", "full", "passed",
                         "fail_reasons", "score", "regimes", "approvals", "neighbors_passing", "n_neighbors"}
    assert set(run0["regimes"]) == {"trend", "vol", "stress"}
    assert set(run0["approvals"]) == {"entry_orders", "needing_approval"}
    for r in results["runs"]:
        assert r["score"] == calmar(r["in_sample"]) or (math.isnan(r["score"]) and math.isnan(calmar(r["in_sample"])))
        assert r["passed"] == (not r["fail_reasons"])


def test_tournament_winner_is_the_best_in_sample_survivor(results):
    for symbol in ("SPY", "BTC/USD"):
        survivors = [r for r in results["runs"] if r["symbol"] == symbol and r["passed"]]
        assert survivors, "the lenient filters should let configurations through"
        best = min(survivors, key=tour.rank_key)
        assert results["winners"][symbol]["label"] == best["label"]
        assert results["winner_equity"][symbol][0][0] >= "2017-01-02"


def test_tournament_portfolio_covers_the_winners(results):
    p = results["portfolio"]
    assert p["window"] == "out_of_sample" and p["start"] == "2019-01-01"
    assert p["symbols"] == ["SPY", "BTC/USD"]
    assert set(p) >= {"metrics", "regimes", "kill_switch_would_fire", "daily_loss_limit_hits", "equity"}
    assert p["equity"][0][0] >= "2019-01-01" and isinstance(p["equity"][0][1], float)
    assert results["portfolio_full"]["window"] == "full"
    assert p["regime_proxy"] == "SPY"


def test_write_results_writes_strict_json_and_markdown(results, tmp_path):
    json_path, md_path = write_results(results, tmp_path / "out")

    def refuse(constant: str) -> None:
        raise AssertionError(f"non-standard JSON constant {constant}")

    loaded = json.loads(json_path.read_text(), parse_constant=refuse)
    assert loaded["n_configs"] == results["n_configs"]
    first = loaded["runs"][0]["in_sample"]["total_return_pct"]
    assert first == round(results["runs"][0]["in_sample"]["total_return_pct"], 4)
    md = md_path.read_text()
    for heading in ("## Setup", "## Winners", "## Leaderboards", "## Portfolio of the winners",
                    "## Regimes and stress windows", "## Manual approvals", "## Caveats"):
        assert heading in md
    assert f"{results['n_configs']} configurations" in md
    assert not list((tmp_path / "out").glob(".*.tmp"))


def test_no_winner_means_no_portfolio(small_md):
    cfg = load_strategy(small_md)
    strict = cfg.tournament.filters.model_copy(update={"min_trades": 10_000})
    cfg = cfg.model_copy(update={"tournament": cfg.tournament.model_copy(update={"filters": strict})})

    results = run_tournament(cfg, symbols=["SPY"], bars=SYNTHETIC)

    assert results["winners"] == {"SPY": None}
    assert results["portfolio"] is None and results["winner_equity"] == {}
    assert all("n_trades" in " ".join(r["fail_reasons"]) for r in results["runs"])
    assert "no winner" in tour.render_markdown(results)


# --------------------------------------------------------------------------- strategy.md


def outside_managed_blocks(text: str) -> str:
    """The text with the WINNERS, TOURNAMENT and CONFIG blocks' contents removed."""
    for name in ("WINNERS", "TOURNAMENT", "CONFIG"):
        begin, end = f"<!-- BEGIN {name} -->", f"<!-- END {name} -->"
        i, j = text.index(begin), text.index(end)
        text = text[: i + len(begin)] + text[j:]
    return text


def test_apply_winners_round_trips_through_load_strategy(tmp_path, strategy_cfg):
    path = tmp_path / "strategy.md"
    shutil.copy(ROOT / "strategy.md", path)
    os.chmod(path, 0o640)
    before = path.read_text()
    grid = TrendStrategy.param_grid
    runs = [run("SPY", grid[0]), run("QQQ", grid[5], passed=False)]
    results = {
        "generated_at": "2026-09-30T20:00:00Z",
        "data": {"SPY": {}, "QQQ": {}},
        "config": {"in_sample": ["2014-09-17", "2021-12-31"], "out_of_sample": ["2022-01-01", "2026-09-29"],
                   "full": ["2014-09-17", "2026-09-29"], "filters": FILTERS.model_dump(),
                   "risk": strategy_cfg.risk.model_dump()},
        "n_configs": 2,
        "runs": runs,
        "winners": pick_winners(runs, ["SPY", "QQQ"]),
        "portfolio": None,
        "portfolio_full": None,
    }
    add_neighbors(runs)

    loaded = apply_winners(to_json_safe(results), path)  # works from the JSON form as well

    assert loaded == load_strategy(path)
    assert loaded.assets["SPY"].model_dump() == {"enabled": True, "strategy": "trend", "params": runs[0]["params"]}
    assert loaded.assets["QQQ"].model_dump() == {"enabled": False, "strategy": "trend", "params": runs[1]["params"]}
    assert loaded.assets["BTC/USD"] == strategy_cfg.assets["BTC/USD"]  # not in this tournament: untouched
    for key in ("risk", "jev", "execution", "tournament", "live_gate", "version", "timeframe"):
        assert getattr(loaded, key) == getattr(strategy_cfg, key)
    after = path.read_text()
    assert outside_managed_blocks(after) == outside_managed_blocks(before)
    winners_md = after[after.index("<!-- BEGIN WINNERS -->"): after.index("<!-- END WINNERS -->")]
    assert "### SPY: Trend following" in winners_md
    assert REGISTRY["trend"]("SPY", **grid[0]).describe()["entry"] in winners_md
    assert "### QQQ: disabled" in winners_md and "OOS win_rate 0.35 < 0.40" in winners_md
    assert "results/tournament.md" in after[after.index("<!-- BEGIN TOURNAMENT -->"):]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["strategy.md"]


def test_apply_winners_leaves_the_file_alone_when_a_winner_is_invalid(tmp_path):
    path = tmp_path / "strategy.md"
    shutil.copy(ROOT / "strategy.md", path)
    before = path.read_text()
    bad = {"strategy": "trend", "params": {"fast": 200, "slow": 100, "atr_mult": 3}, "label": "trend(bad)",
           "rules": {}, "score": 1.0}
    results = {"generated_at": "x", "data": {"SPY": {}}, "runs": [], "winners": {"SPY": bad}}

    with pytest.raises(ValueError, match="fast"):
        apply_winners(results, path)

    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["strategy.md"]


def test_apply_winners_refuses_a_file_without_a_config_block(tmp_path):
    path = tmp_path / "strategy.md"
    path.write_text("# nothing here\n")
    with pytest.raises(ConfigError):
        apply_winners({"data": {}, "winners": {}, "runs": []}, path)


def test_run_and_write_with_apply(small_md, tmp_path, monkeypatch):
    monkeypatch.setattr(data, "load_daily", lambda symbol, start=None, end=None: SYNTHETIC[symbol])

    results = run_and_write(apply=True, cfg_path=small_md, out_dir=tmp_path / "results")

    assert (tmp_path / "results" / "tournament.json").exists()
    assert (tmp_path / "results" / "tournament.md").exists()
    cfg = load_strategy(small_md)
    for symbol, winner in results["winners"].items():
        assert cfg.assets[symbol].enabled == (winner is not None)
        if winner is not None:
            assert cfg.assets[symbol].strategy == winner["strategy"]
            assert dict(cfg.assets[symbol].params) == winner["params"]
    text = small_md.read_text()
    assert "The tournament hasn't run yet" not in text and "Not run yet.\n\n<!-- END TOURNAMENT" not in text
