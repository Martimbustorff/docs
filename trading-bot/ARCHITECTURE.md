# Architecture

This is the contract between modules. Code must match the signatures here. Shared types live in
`bot/models.py`, config in `bot/config.py`, bar timing in `bot/timeutil.py`, and the strategy
contract in `bot/strategies/base.py`. Read those four files first.

## Division of labour

| Layer | Who | When | What |
|---|---|---|---|
| Brain | Opus (you, offline) | Once | Designs strategies, runs the tournament, writes `strategy.md` |
| Decision layer | Jev (`jev-latest`) | Every entry signal | Returns calibrated probabilities for fixed-outcome questions |
| Rules and risk | This code | Every bar or poll | Signals, sizing, limits, kill switch, approvals |
| Execution | Alpaca (paper) | Every order | Executes only |

The live loop never calls an LLM. Jev is the only model in the loop, and it can only veto.

## Timing model (backtest and live must agree)

- Timeframe: daily bars. Stocks' bars close at 16:00 New York time. Crypto bars close at 00:00 UTC.
- The bot computes a signal from bar *i*'s close and fills it at bar *i+1*'s open. In live
  trading, stock orders go out `stock_entry_delay_minutes` after the next open. Crypto orders go
  out right after 00:00 UTC.
- Stops and take-profits are checked intrabar. In the backtest, a bar with low ≤ stop exits at the
  stop, or at the open if the bar gapped through it. If the same bar hits both the stop and the
  take-profit, the backtest assumes the stop hit first. In live trading, the runner checks the
  last trade price every poll and sends a market exit when the price crosses a level.
- A stop update computed on bar *i*'s close applies from bar *i+1*. Stops only ratchet up.
- Costs: `execution.slippage_bps` is applied against you on every fill, and `execution.fee_bps`
  is charged on every fill's notional.

## Modules

### `bot/indicators.py`
Pure causal functions on `pd.Series`/`pd.DataFrame`. Each returns a Series aligned to the input,
with NaN during warmup: `sma(s, n)`, `ema(s, n)`, `atr(df, n)` (Wilder), `rsi(s, n)` (Wilder),
`donchian_high(df, n)` and `donchian_low(df, n)` (the highest high and lowest low of the *prior*
n bars, excluding the current bar), `roc(s, n)`, `realized_vol(s, n)` (annualisation factor as a
parameter).

### `bot/strategies/`
`trend.py`, `breakout.py`, `meanrev.py` and `momentum.py` each define one `Strategy` subclass.
`__init__.py` exposes `REGISTRY: dict[str, type[Strategy]]` and
`build(name: str, symbol: str, params: dict) -> Strategy`.

| name | Entry (long only) | Exit | Stop | Take profit | Grid |
|---|---|---|---|---|---|
| `trend` | close > SMA(slow), SMA(fast) > SMA(slow), and this state just turned on (or re-arms after a stop-out only when close crosses back above SMA(fast)) | close < SMA(slow) | ATR trailing: highest close − `atr_mult`×ATR(14) | none | fast {20,50} × slow {100,200} × atr_mult {3,4} |
| `breakout` | close > Donchian high(`entry_n`) | close < Donchian low(`exit_n`) | entry − `atr_mult`×ATR(14), then trails at max(stop, Donchian low(exit_n)) | none | entry_n {20,55} × exit_n {10,20} × atr_mult {2,3} |
| `meanrev` | close > SMA(200) and RSI(2) < `entry_rsi` | close > SMA(5), or `max_hold` bars elapsed | entry − `atr_mult`×ATR(14), fixed | the SMA(5) exit acts as profit taking; `take_profit` is None | entry_rsi {5,10} × atr_mult {2,3} × max_hold {5,10} |
| `momentum` | ROC(`lookback`) > 0 and close > SMA(50), and 20d realized vol < its 252-bar 90th percentile | ROC(`lookback`) < 0 | ATR trailing: highest close − `atr_mult`×ATR(14) | none | lookback {60,120,252} × atr_mult {3,5} |

`Signal.features` carries the indicator values used (for example `{"sma_fast":…, "atr":…}`).

### `bot/data.py`
- `load_daily(symbol, start=None, end=None) -> pd.DataFrame`: reads
  `data/cache/{SYMBOL with / and - replaced by _}_1d.csv` (BTC/USD maps to `BTC_USD_1d.csv`).
  The index is a tz-naive `DatetimeIndex` named `date`. Columns are float `open`, `high`, `low`,
  `close` and `volume`. The function drops NaNs, sorts, and raises `FileNotFoundError` with a
  hint to run `python -m bot fetch-data`.
- `fetch_daily(symbols, start="2014-09-17") -> dict[str, int]`: downloads data with yfinance
  (mapping BTC/USD to BTC-USD, `auto_adjust=True`), writes the cache, and returns row counts.
- `alpaca_daily(broker_or_clients, symbol, start, end) -> pd.DataFrame`: the same frame shape,
  from Alpaca's historical data, used live.

### `bot/backtest/engine.py`
```python
@dataclass(frozen=True)
class BacktestConfig:
    capital_usd: float; risk_per_trade_pct: float; max_position_pct: float; max_position_usd: float
    slippage_bps: float; fee_bps: float; approval_threshold_usd: float
    @classmethod
    def from_strategy(cls, cfg: StrategyConfig, symbol: str) -> "BacktestConfig"

@dataclass
class BacktestResult:
    symbol: str; strategy_label: str; params: dict
    trades: list[Trade]; equity: pd.Series  # daily mark-to-market equity, indexed like the bars
    signals: list[Signal]  # every ENTRY/EXIT signal generated (pre-risk)
    orders_needing_approval: int; entry_orders: int

def run_backtest(bars: pd.DataFrame, strategy: Strategy, cfg: BacktestConfig,
                 start: str | None = None, end: str | None = None) -> BacktestResult
```
The engine calls `strategy.prepare` on the **full** history once, then only trades bars in
[start, end]. That way indicators are warm at `start` and never read the future. Sizing:
`qty = min(capital*risk_pct/(entry_est - stop), capital*max_position_pct/entry_est,
max_position_usd/entry_est)`, where `entry_est` is the signal bar's close. Crypto quantities are
fractional. Stock quantities are also fractional, because Alpaca supports that. The engine skips
the entry if `entry_est <= stop`. It closes any open position at the end of the window as
`end_of_data`.

### `bot/backtest/metrics.py`
`compute_metrics(result: BacktestResult, periods_per_year: int) -> dict` returns these keys:
`total_return_pct`, `cagr_pct`, `max_drawdown_pct` (positive number), `win_rate` (0–1),
`profit_factor` (inf when there are no losses; serialise it as null), `n_trades`, `avg_win_pct`,
`avg_loss_pct`, `expectancy_pct`, `sharpe`, `sortino`, `exposure_pct`, `largest_loss_usd`,
`largest_loss_pct`, `max_dd_duration_days`, `buy_hold_return_pct` and `buy_hold_max_dd_pct`.

### `bot/backtest/regimes.py`
`label_regimes(bars) -> pd.DataFrame` has these columns:
- `trend`: `bull` when close > SMA200 and SMA200 is rising over 20 bars, `bear` when it is
  below and falling, `sideways` otherwise.
- `vol`: `high` when 20d realized vol is above its rolling 252-bar median, `low` otherwise.

`STRESS_WINDOWS: dict[str, tuple[str, str]]` covers the 2018 crypto winter
(2018-01-06..2018-12-15), the Q4 2018 equity selloff (2018-09-20..2018-12-24), the COVID crash
(2020-02-19..2020-03-23), the 2022 bear market (2022-01-03..2022-10-12), the crypto crash of
2021-11-10..2022-11-21, and the 2025 tariff shock (2025-02-19..2025-04-08).

`regime_breakdown(result, bars) -> dict` returns the return, max DD and trade count per trend
regime, per vol regime and per stress window.

### `bot/backtest/tournament.py`
`run_tournament(cfg: StrategyConfig, symbols) -> dict` runs every strategy × every grid point ×
every symbol on the in-sample and out-of-sample windows. It applies `tournament.filters` to both
windows. Survivors pass every filter in both windows, where `min_trades` applies per window. It
ranks survivors by out-of-sample Calmar ratio (CAGR / max DD), then by win rate. It picks one
winner per symbol, or none, in which case that asset stays disabled. It then runs a combined
portfolio backtest of the winners on shared capital, with the total exposure cap applied.

`write_results(results, out_dir="results")` writes `results/tournament.json` (every run's metrics,
filters, pass/fail reasons and regime breakdown) and `results/tournament.md`.
`apply_winners(results, strategy_path)` updates the `assets` in the CONFIG block and rewrites the
WINNERS and TOURNAMENT sections of `strategy.md`, using `Strategy.describe()`.

`bot/backtest/portfolio.py` provides
`run_portfolio(bars_by_symbol, strategies_by_symbol, cfg: StrategyConfig, start, end) -> PortfolioResult`.
It runs one shared-capital backtest across every winner. Each symbol is sized with the same rules
as the live bot, and new entries are shrunk to respect `max_total_exposure_pct`. The result
carries the equity series, the trades, and whether the kill switch or the daily loss limit would
have fired.

`results/tournament.json` schema. The dashboard, the final check and the README read this file.
Floats are rounded to 4 decimals, `inf` becomes `null`, and dates are ISO strings.
```json
{
  "generated_at": "2026-09-30T20:00:00Z",
  "data": {"SPY": {"first": "2014-09-17", "last": "2026-09-29", "bars": 3026}, "...": {}},
  "config": {"in_sample": ["..",".."], "out_of_sample": ["..",".."], "filters": {}, "risk": {},
             "slippage_bps": {}, "fee_bps": {}},
  "n_configs": 90,
  "runs": [{
      "symbol": "SPY", "strategy": "trend", "label": "trend(atr_mult=3,fast=50,slow=200)", "params": {},
      "in_sample": {"<metrics keys>": 0}, "out_of_sample": {}, "full": {},
      "passed": false, "fail_reasons": ["OOS win_rate 0.35 < 0.40"], "score": 0.0,
      "regimes": {"trend": {"bull": {"return_pct": 0, "max_dd_pct": 0, "trades": 0}, "bear": {}, "sideways": {}},
                  "vol": {"high": {}, "low": {}}, "stress": {"covid_crash_2020": {}}},
      "approvals": {"entry_orders": 0, "needing_approval": 0}
  }],
  "winners": {"SPY": {"label": "", "strategy": "", "params": {}, "rules": {"entry": "", "exit": "",
                "stop_loss": "", "take_profit": "", "timeframe": ""}, "score": 0.0}, "BTC/USD": null},
  "portfolio": {"window": "full|out_of_sample", "metrics": {}, "regimes": {},
                "kill_switch_would_fire": [{"date": "", "reason": ""}], "daily_loss_limit_hits": 0,
                "equity": [["2022-01-03", 10000.0]]},
  "winner_equity": {"SPY": [["2014-09-19", 10000.0]]}
}
```
Equity arrays are downsampled to weekly closes to keep the file small.

### `bot/jev.py`
```python
class JevClient(Protocol):
    def ask(self, state: dict, questions: dict[str, dict], model: str, timeout_s: float) -> "RawJev": ...
@dataclass(frozen=True)
class RawJev:  # normalised SDK response
    model: str; input_tokens: int; answers: dict[str, dict]  # name -> {"type","probabilities":{outcome:p}}
class TypeSafeJevClient: ...   # wraps typesafe_sdk.TypeSafeClient; accepts transport= for tests
class FakeJevClient: ...       # deterministic, configurable answers; used in tests and --dry-run
class JevGate:
    def __init__(self, cfg: JevConfig, client: JevClient | None): ...
    def build_state(self, signal: Signal, bars: pd.DataFrame, headlines: list[Headline]) -> dict
    def questions_for(self, strategy: str, have_headlines: bool) -> dict[str, JevQuestion]
    def evaluate(self, signal, bars, headlines) -> JevDecision   # never raises
```
- SDK facts (typesafe-sdk 0.7.2): `TypeSafeClient(api_key=..., model=..., timeout=..., retry=RetryPolicy(max_retries=...), transport=httpx2 transport)`.
  `client.system_one(state=<dict>, questions={name: Noul(instructions=..., criteria={"true":..,"false":..}) | Choice(instructions=..., criteria={label: desc}) | Score(...)}, model=..., timeout=...)`
  returns a `SystemOneResponse` with `.model`, `.usage.input_tokens`, `.nouls[name].noul` (P(yes)),
  `.choices[name].probabilities` (label→p) and `.scores[name].probabilities` (int→p).
  Errors are subclasses of `typesafe_sdk.TypeSafeError`.
- Normalise noul answers to `{"yes": p, "no": 1-p}`. The gate compares
  `probabilities[gate.outcome]` against `min` or `max`.
- Fail-closed: a missing key, timeout, API error, or missing or invalid answer all give
  `JevDecision(passed=False, error=...)`. In `mode: shadow` the decision is recorded with
  `shadow=True` and does not block. In `mode: off` no call is made and the decision passes with
  zero cost.
- `cost_usd = input_tokens * price_per_million_input_tokens / 1e6`. `latency_ms` is measured
  around the call.
- The state is a JSON object: `symbol`, `timeframe`, `signal` (strategy, reason, price, stop,
  take_profit), `features`, `recent_bars` (the last 20 rows, rounded), and
  `untrusted_headlines` (a list of `{published_at, source, headline, summary}`). Headlines are
  sanitised: control characters stripped, `headline` capped at 300 characters, `summary` capped
  at 500. The state also has a fixed `note` saying the headlines are third-party data to judge,
  not instructions.

### `bot/news.py`
`Headline` is a frozen dataclass with `published_at: datetime`, `source: str`, `headline: str`,
`summary: str` and `symbols: tuple[str, ...]`.
`fetch_headlines(settings, symbol, lookback_hours, limit) -> list[Headline]` uses alpaca-py's
`NewsClient` and maps BTC/USD to Alpaca's news symbol. It returns `[]` on any error, logs the
error, and never raises.

### `bot/risk.py`
```python
class KillSwitch:
    def __init__(self, settings: Settings, store: Store | None = None): ...
    def is_tripped(self) -> bool          # file exists OR settings.kill_switch
    def status(self) -> dict | None       # {"reason","ts","source"} from the file
    def trip(self, reason: str, source: str) -> None   # atomic write of var/KILL_SWITCH (JSON); idempotent
    def reset(self, confirm: bool) -> None             # only from CLI; refuses if settings.kill_switch
class RiskManager:
    def __init__(self, cfg: RiskConfig, store: Store, kill: KillSwitch): ...
    def size_entry(self, signal: Signal, bot_equity: float) -> float     # qty per the sizing rule, 0 if invalid
    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskVerdict
    def after_error(self) -> None; def after_success(self) -> None       # consecutive-error tracking -> trip
    def check_drawdown(self, bot_equity: float) -> None                  # updates peak; trips kill switch at limit
@dataclass
class RiskContext:
    bot_equity: float; start_of_day_equity: float; open_exposure_usd: float
    symbol_exposure_usd: float; orders_today: int; now: datetime
```
`check` runs these rules in order. Risk-reducing purposes (`OrderPurpose.increases_risk` is
False) return ALLOW unless the broker is in live mode without the live gate. For ENTRY:
1. Kill switch tripped → BLOCK.
2. `orders_today >= max_orders_per_day` → trip the kill switch and BLOCK.
3. The day's loss ≥ `daily_loss_limit_pct` of `start_of_day_equity` → BLOCK. The block lasts for
   the rest of the New York day.
4. Position caps (the per-symbol pct, the USD cap, total exposure) → shrink the order through
   `adjusted_qty`, or BLOCK if the order would be less than $1.
5. Notional above `approval_threshold_usd` → NEEDS_APPROVAL.
6. Otherwise ALLOW.

### `bot/broker.py`
```python
class Broker(Protocol):
    is_paper: bool
    def account(self) -> AccountSnapshot
    def positions(self) -> dict[str, BrokerPosition]
    def last_price(self, symbol: str) -> float
    def daily_bars(self, symbol: str, start: date, end: date | None = None) -> pd.DataFrame
    def is_market_open(self) -> bool
    def next_open(self) -> datetime
    def submit(self, intent: OrderIntent) -> OrderResult      # market order
    def get_order(self, client_order_id: str) -> OrderResult | None
    def cancel_all(self) -> None
    def close_position(self, symbol: str) -> OrderResult | None
class AlpacaBroker: ...  # alpaca-py TradingClient(paper=settings.alpaca_paper) + data clients
class SimBroker: ...     # in-memory; fills market orders at a settable price; used by tests/dry-run
class OrderGateway:
    """The ONLY path to broker.submit. Re-checks the kill switch and the live-trading guard
    immediately before every risk-increasing order, dedupes by client_order_id via the store,
    records the order, and reports errors to RiskManager."""
    def __init__(self, broker: Broker, risk: RiskManager, kill: KillSwitch, store: Store,
                 settings: Settings, notifier: Notifier): ...
    def submit(self, intent: OrderIntent) -> OrderResult
    def flatten_all(self, reason: str) -> list[OrderResult]   # cancel_all + close every bot position
def assert_trading_allowed(settings: Settings, live_gate_path: Path) -> None
```
The live-trading guard is `assert_trading_allowed`. It raises `ConfigError` unless one of these
holds:
- `trading_mode == "paper"` and `alpaca_paper` is True.
- `trading_mode == "live"`, `alpaca_paper` is False, `live_trading_ack == LIVE_ACK_PHRASE`, and
  `var/live_gate.json` has `passed: true` and is younger than 7 days.

Alpaca rules:
- Crypto orders use `time_in_force=GTC` and fractional qty.
- Stock orders use `time_in_force=DAY`, a market order with fractional qty.
- `client_order_id` must be at most 48 characters. Build it as
  `f"{purpose}-{symbol_nodash}-{sha1(key)[:12]}"`.

### `bot/notify.py`
```python
class Notifier(Protocol):
    def send(self, text: str) -> None                       # never raises
    def request_approval(self, approval_id: int, text: str) -> int | None   # returns message_id
    def poll(self) -> list[Command]                          # commands + approval callbacks since last poll
@dataclass(frozen=True)
class Command:
    kind: Literal["approve","reject","kill","status","pnl","help","unknown"]; approval_id: int | None
    chat_id: str; text: str
class TelegramNotifier: ...   # raw Bot API over httpx; long-poll getUpdates with persisted offset
class ConsoleNotifier: ...    # logs; used when Telegram isn't configured
```
- The notifier ignores and logs every update whose chat id isn't `TELEGRAM_CHAT_ID`.
- Inline keyboard `callback_data` values are `approve:<id>` and `reject:<id>`. The notifier
  answers each callback query and edits the message to show the decision.
- It escapes Markdown or sends plain text, retries 429s and 5xx responses with backoff, and never
  logs the token.

### `bot/store.py`
SQLite (WAL) at `settings.db_path`. `Store(path)` creates the schema idempotently. Tables:

```sql
signals(id INTEGER PK, ts TEXT, symbol TEXT, strategy TEXT, kind TEXT, reason TEXT, price REAL,
        stop_price REAL, take_profit REAL, features TEXT, bar_date TEXT,
        jev_decision_id INTEGER, gate TEXT,      -- 'passed' | 'vetoed' | 'error' | 'shadow' | 'off' | 'n/a'
        risk_action TEXT, risk_reason TEXT, approval_id INTEGER, order_client_id TEXT,
        status TEXT,                              -- 'new'|'blocked'|'awaiting_approval'|'rejected'|'expired'|'queued'|'submitted'|'filled'|'skipped'
        outcome_pnl REAL, outcome_pnl_pct REAL, counterfactual_pnl_pct REAL,
        UNIQUE(symbol, strategy, kind, bar_date))
jev_decisions(id INTEGER PK, ts TEXT, signal_id INTEGER, symbol TEXT, model TEXT, latency_ms REAL,
        input_tokens INTEGER, cost_usd REAL, passed INTEGER, shadow INTEGER, error TEXT, answers TEXT)
orders(id INTEGER PK, client_order_id TEXT UNIQUE, broker_order_id TEXT, ts TEXT, symbol TEXT,
        side TEXT, qty REAL, ref_price REAL, notional REAL, purpose TEXT, reason TEXT, status TEXT,
        filled_qty REAL, filled_avg_price REAL, signal_id INTEGER, message TEXT)
trades(id INTEGER PK, symbol TEXT, strategy TEXT, entry_ts TEXT, entry_price REAL, exit_ts TEXT,
        exit_price REAL, qty REAL, pnl REAL, pnl_pct REAL, exit_reason TEXT, fees REAL, signal_id INTEGER)
positions(symbol TEXT PK, state TEXT)            -- JSON of PositionState
approvals(id INTEGER PK, signal_id INTEGER, ts_requested TEXT, notional REAL, status TEXT,
        decided_ts TEXT, message_id INTEGER, expires_ts TEXT)   -- 'pending'|'approved'|'rejected'|'expired'
events(id INTEGER PK, ts TEXT, level TEXT, kind TEXT, message TEXT, data TEXT)
equity(ts TEXT PK, account_equity REAL, bot_equity REAL, cash REAL, exposure REAL)
kv(key TEXT PK, value TEXT)
```

Typed helpers include:
- `insert_signal(signal, bar_date) -> int | None`. It returns None if the row is a duplicate.
- `update_signal(id, **fields)`, `insert_jev(decision, signal_id, symbol) -> int`,
  `upsert_order(intent, result)`, `get_order_by_client_id`, `insert_trade(trade, signal_id)`.
- `get_positions() -> dict[str, PositionState]`, `put_position(state)`, `delete_position(symbol)`.
- `create_approval(signal_id, notional, expires_ts) -> int`, `set_approval(id, status)`,
  `pending_approvals()`.
- `log_event(level, kind, message, data=None)`, `record_equity(...)`, `kv_get` and `kv_set`.
- `orders_today(now) -> int` counts risk-increasing orders on the New York day.
- `realized_pnl_since(ts)`, `trades(since=None)`, `signals(limit, offset)` and
  `jev_stats(since=None) -> {"n","avg_latency_ms","avg_cost_usd","total_cost_usd"}`.

Timestamps are stored as ISO-8601 UTC strings. The store never stores secrets.

### `bot/runner.py`
`Runner(settings, cfg, broker, gateway, risk, kill, jev_gate, notifier, store, clock=utcnow)`
provides `tick()`, which does one poll iteration, and `run_forever()`. On every tick it:
1. Runs `assert_trading_allowed`. If the kill switch is tripped, it makes sure the bot is flat
   (once), then sleeps.
2. Reconciles positions and orders with the broker. It detects fills and records trades and
   outcomes, and alerts on every fill.
3. Handles Telegram commands and approval callbacks. Expired approvals become `expired`. Approved
   entries are re-priced and skipped if the price has drifted more than
   `approval_max_price_drift_pct`.
4. Checks stops and take-profits against `broker.last_price` and submits exits.
5. For each enabled symbol whose new daily bar has closed and hasn't been processed (tracked with
   the kv key `last_bar:<symbol>`), it fetches bars, builds the strategy from the config, and
   updates the trailing stop. It runs the exit logic if the bot is in a position, or the entry
   logic if it is flat. An entry goes through `jev_gate.evaluate` (with headlines from `news`),
   then `risk.size_entry`, then `risk.check`. Depending on the verdict, the order is queued, sent
   for approval, or blocked. Queued orders execute in the next execution window: at once for
   crypto, and at the open plus `stock_entry_delay_minutes` for stocks.
6. Checks the daily loss limit, the drawdown kill and the error counters, records equity every 15
   minutes, and sends the daily report at `execution.daily_report_time`.

Any exception inside a tick is logged and alerted, counted by `risk.after_error()`, and never
kills the process. The process restarts safely because the kv markers, the unique constraints
and the deterministic `client_order_id` make it idempotent.

### `bot/report.py`
`daily_report(store, settings, cfg, day) -> str` returns Markdown with:
- The day's trades and the open positions.
- P&L for the day, cumulative realized and unrealized.
- Win rate, the largest loss, and Jev's average latency and average cost per decision (day and
  cumulative).
- Counts of vetoes, approvals and errors, and the kill-switch state.

It saves to `var/reports/YYYY-MM-DD.md`. The runner sends it to Telegram.

### `bot/live_gate.py`
- `kill_switch_drill(settings, cfg) -> dict` runs against SimBroker, or against the Alpaca paper
  account with `--paper`. It opens a small position, trips the switch, and asserts:
  - Orders are cancelled.
  - Positions are flat.
  - A new ENTRY is refused.
  - An alert was sent.

  It writes `var/kill_switch_drill.json` as `{passed, ts, steps}`.
- `final_check(settings, cfg) -> dict` answers:
  - Does paper match the backtest? It replays the backtest over the paper period on the same
    bars. It computes the signal match rate (before Jev and risk), the fill slippage versus the
    assumed slippage, and the return gap between paper and the replay restricted to the trades
    that actually executed.
  - Did the kill switch fire in testing? A drill no older than `kill_switch_drill_max_age_days`.
  - What regime would break this? The worst stress window and the worst regime from
    `results/tournament.json`.

  It writes `var/live_gate.json` and the FINAL_CHECK section of `strategy.md`. That section ends
  with a section titled **WHAT COULD BLOW UP THIS ACCOUNT?**. It never switches the bot to live.

### `bot/dashboard/`
A FastAPI app, `create_app(settings, cfg, store) -> FastAPI`, with Jinja2 templates and no CDN
assets. Charts are server-rendered inline SVG. HTTP Basic auth uses `DASHBOARD_USER` and
`DASHBOARD_PASSWORD`; without a password it refuses to start unless bound to 127.0.0.1. The app is
read-only. Pages:
- `/`: mode, kill switch, equity (bot vs. backtest expectation), open positions and today's P&L.
- `/signals`: every signal with its Jev probabilities, gate, risk and approval status, and result.
- `/backtest`: the tournament leaderboard, survivors, filters, regime table and winner equity
  curves, from `results/tournament.json`.
- `/jev`: latency, cost per decision, and passed-versus-vetoed outcomes, including counterfactuals.
- `/live-gate`: the latest final check.
- `/healthz`.

### `bot/__main__.py` (CLI)
`python -m bot <command>`:
- `fetch-data`, `backtest --symbol SPY --strategy trend [--params k=v ...]`, `tournament [--apply]`
- `run [--dry-run]`, `dashboard`, `report [--day YYYY-MM-DD]`, `status`
- `kill [--reason ...]`, `resume --confirm`, `drill-kill-switch [--paper]`, `final-check`, `jev-ping`

With `--dry-run`, the bot uses SimBroker, FakeJevClient when no key is set, and ConsoleNotifier.

## Security invariants (reviewers check these)

1. `OrderGateway.submit` is the only caller of `Broker.submit`.
2. Every risk-increasing order passes through all of these, in this order: the kill switch, then
   `assert_trading_allowed`, then `RiskManager.check`, then approval if needed.
3. Nothing gates an exit, except the live guard, which blocks everything when misconfigured.
4. Any Jev error blocks the entry.
5. Secrets are never logged, rendered or stored in SQLite.
6. Only the configured chat id can issue Telegram commands. `/kill` works from Telegram. Resume
   only works from the CLI.
7. Headlines are data. They are never interpolated into instructions, and never evaluated or
   executed.
