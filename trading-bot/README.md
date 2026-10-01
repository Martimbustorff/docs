# Trading bot

> **NOT FINANCIAL ADVICE.** This is a paper-trading research project, not an investment
> product. Backtests and paper results don't predict future returns. If you ever point it at a
> live account, you can lose real money, including more than the backtests suggest. You're
> responsible for every order it sends.

A paper-first, long-only trading bot for **SPY**, **QQQ** and **BTC/USD** on daily bars. It puts
risk first and returns second.

| Layer | Who | When | What |
|---|---|---|---|
| Brain | Opus, offline | Once | Designs the strategies, runs the tournament and writes `strategy.md`. |
| Decision layer | Jev (TypeSafe AI's System One, `jev-latest`) | Every entry signal | Returns calibrated probabilities for fixed-outcome questions. It can only veto an entry. |
| Rules and risk | This code | Every poll | Signals, sizing, limits, the kill switch and approvals. |
| Execution | Alpaca, paper account | Every order | Executes orders, nothing else. |

The live loop never calls a language model. Jev is the only model in the loop, and it can only
say no.

## How it works

```text
  OFFLINE, ONCE                     LIVE LOOP: python -m bot run, every poll
  -------------                     ----------------------------------------

  Opus designs strategies           Alpaca daily bars
           |                                |
           v                                v
  tournament: backtests,            strategy: signal on the bar's close
  in-sample + out-of-sample                 |
           |                                |  ENTRY               EXIT / stop / take-profit
           v                                v                               |
  strategy.md  ---- read at start -->  Jev gate  <-- headlines              |
  rules, risk limits,                  veto only,    (untrusted data,       |
  Jev thresholds                       fail-closed    never instructions)   |
                                            |                               |
                                            v                               |
                                       RiskManager                          |
                                       kill switch, loss limits, caps       |
                                            |                               |
                                            v                               |
                                       over $1,000?  <-->  Telegram         |
                                       wait for approval   Approve / Reject |
                                            |                               |
                                            v                               |
                                       OrderGateway  <----------------------+
                                       re-checks the kill switch and the
                                       live guard before every entry
                                            |
                                            v
                                       Alpaca paper: executes only

  SQLite in var/: signals, Jev decisions, orders, trades, equity
      --> dashboard (read-only)    --> Telegram alerts and the daily report
```

- `strategy.md` is the single source of truth. The bot refuses to start if its CONFIG block is
  missing or invalid.
- Entries pass Jev, then the risk manager, then approval if needed.
- Exits never wait for Jev, approval or the daily loss limit.
- `OrderGateway` is the only code that sends orders to the broker.

[ARCHITECTURE.md](ARCHITECTURE.md) has the full contract between modules.

## Quickstart

You need Python 3.11. Every command runs from the repository root.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
chmod 600 .env
python -m bot fetch-data
python -m bot tournament --apply
python -m pytest
python -m bot run --dry-run
```

1. `fetch-data` downloads daily bars into `data/cache/`.
2. `tournament --apply` backtests every strategy and enables at most one winner per asset in
   `strategy.md`. An asset with no surviving strategy stays disabled.
3. `pytest` runs the test suite. It needs no network and no keys.
4. `run --dry-run` runs the live loop against a simulated broker. It uses a fake Jev when
   `TYPESAFE_API_KEY` is empty, and it logs alerts to the console. Press **Ctrl+C** to stop it.

The dry run needs no keys, so `.env` can stay as copied. Know what it does and doesn't do:

- It keeps its own state in `var/dry-run/`, so it never touches the real bot's database or
  kill switch.
- It follows New York market hours. Stock entries only go out between 9:31 and 10:00 New York
  time.
- It never uses Telegram, so nobody can approve entries above $1,000. Most dry-run entries
  expire unapproved.

In a second terminal, start the dashboard on the dry run's state:

```bash
. .venv/bin/activate
BOT_DATA_DIR=var/dry-run python -m bot dashboard
```

Open `http://127.0.0.1:8080`. On `127.0.0.1` the dashboard runs without a password. Anywhere
else it refuses to start until you set `DASHBOARD_PASSWORD`. Without `BOT_DATA_DIR`, the dashboard
shows the real bot's state in `var/`.

## Commands

Run every command as `python -m bot <command>`.

| Command | What it does |
|---|---|
| `fetch-data` | Downloads daily bars for SPY, QQQ and BTC/USD into `data/cache/`. |
| `backtest --symbol SPY --strategy trend [--params k=v ...] [--start YYYY-MM-DD] [--end YYYY-MM-DD]` | Backtests one strategy on one symbol, optionally between two dates. Write crypto as `BTC/USD`. It refuses Yahoo-style symbols such as `BTC-USD`. |
| `tournament [--apply]` | Runs every strategy, parameter set and symbol on the in-sample and out-of-sample windows, and writes `results/`. With `--apply`, it enables the winners in `strategy.md`. |
| `export-report` | Writes `results/tournament.html`, a standalone HTML page of the tournament results. |
| `run [--dry-run] [--once]` | Starts the live loop. With `--dry-run`, it uses a simulated broker, a fake Jev when no key is set, console alerts and its own state in `var/dry-run/`. With `--once`, it runs one poll and exits. |
| `dashboard` | Serves the read-only dashboard on `DASHBOARD_HOST:DASHBOARD_PORT`. |
| `report [--day YYYY-MM-DD]` | Builds the daily report into `var/reports/`. |
| `status` | Shows the trading mode, the live gate, the kill-switch state, the heartbeat, the bot's positions and pending approvals. |
| `kill [--reason ...]` | Trips the kill switch, cancels every open order and closes every position in the Alpaca account. |
| `resume --confirm` | Clears the kill switch. It refuses while `KILL_SWITCH=1` is set. |
| `drill-kill-switch [--paper]` | Runs the kill-switch drill against a simulated broker, or against your Alpaca paper account with `--paper`. The `--paper` drill needs the bot stopped and the paper account flat. |
| `final-check` | Compares paper trading with the backtest, checks the drill and writes the live gate. It never switches the bot to live. |
| `jev-ping` | Checks that your Jev key works. |

Global options go before the command, for example `python -m bot --env-file .env.test status`:

- `--strategy PATH` picks the `strategy.md` file to read. It isn't the strategy name that
  `backtest --strategy` takes.
- `--results-dir PATH` picks the tournament results folder. The default is `results/`.
- `--env-file PATH` picks the `.env` file to load. The default is `.env`.

## Where your keys go

Keys go in `.env` and nowhere else. Start from `.env.example`, which documents every variable.

| Variable | What it's for |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Your Alpaca **paper** keys, for an account that only the bot trades. |
| `TYPESAFE_API_KEY` | Jev. Without it, every live entry is blocked, because Jev fails closed. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts, approvals and `/kill`. See [deploy/TELEGRAM.md](deploy/TELEGRAM.md). |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | HTTP Basic auth for the dashboard. |

Follow these rules:

- Run `chmod 600 .env`. Never commit it. `.gitignore` and `.dockerignore` already exclude it.
- Never paste a key into a chat, including an AI assistant, Telegram, Slack or email.
- Give every exchange key trade permission only, with withdrawals off. Alpaca Trading API keys
  can't withdraw money.
- The bot holds secrets as `SecretStr`. It never logs them, shows them on the dashboard or
  stores them in SQLite.

In Docker, compose passes `.env` to the containers at runtime. The image contains no secrets. The
dashboard container gets no Alpaca, Jev or Telegram keys at all, because it never trades.

## Approvals and the kill switch

### Approvals

Any entry order above $1,000 waits for you on Telegram.

1. The bot sends a message with **Approve** and **Reject** buttons.
2. If you don't answer by the deadline shown in the message, the entry expires. The deadline is
   12 hours for BTC/USD, and 10:00 New York time on the next trading day for SPY and QQQ.
3. If the price has moved more than 2% by the time you approve, the bot skips the trade.
4. The kill switch and the risk limits still run right before the order goes out.

The bot never sends an entry after its execution window has closed, even an approved one. Exits
never wait for approval.

### Kill switch

Any of these trips the kill switch:

- You send `/kill` on Telegram.
- You run `python -m bot kill`.
- `KILL_SWITCH=1` is set in `.env`.
- The file `var/KILL_SWITCH` exists.
- The bot sends 10 orders in one day, or hits 5 errors in a row.
- Bot equity falls 15% from its peak.

When it trips, the bot cancels every open order and closes every position in the Alpaca account,
not only its own. Then it stops opening new ones. Only `python -m bot resume --confirm`, run on
the server, clears it. Telegram can stop the bot but can never restart it.

> **Give the bot its own account.** The kill switch can't tell the bot's positions from yours.
> Use a paper account, and later a live account, that only the bot trades. The bot alerts you
> when the account holds a position it didn't open.

The daily loss limit is separate. At a 2% loss on the day, the bot blocks new entries until the
next New York trading day. Exits keep running.

## Paper first, live later

Live trading is off by default. The bot never switches itself to live.

1. **Backtest.** Run `tournament --apply`, then read `results/tournament.md` and the
   **Winners** section of `strategy.md`.
2. **Dry run.** Run `run --dry-run` and watch a few polls in the console, or on the dashboard
   with `BOT_DATA_DIR=var/dry-run python -m bot dashboard`.
3. **Paper trade.** Deploy on a VPS with paper keys, for at least 30 days and 8 trades.
4. **Drill.** Run `drill-kill-switch`, and repeat it at least every 30 days.
5. **Final check.** Run `final-check`. Read **WHAT COULD BLOW UP THIS ACCOUNT?** in the
   **Final check** section of `strategy.md`.
6. **Live, only if you choose to.** It takes all of these at once: `TRADING_MODE=live`,
   `ALPACA_PAPER=false`, `LIVE_TRADING_ACK` set to the exact phrase in `bot/config.py`, and a
   passed `var/live_gate.json` younger than 7 days. Once the live gate expires, the bot blocks
   new entries, but stops, exits and the kill switch keep working.

[deploy/VPS.md](deploy/VPS.md) walks through each step on a server, including going live.

## Deploy

The bot runs on a small VPS with Docker Compose. Both services use one image:

- `bot` runs `python -m bot run` and restarts on failure.
- `dashboard` runs `python -m bot dashboard`. It's published on the server's `127.0.0.1:8080`
  only, and you reach it through an SSH tunnel.

```bash
docker compose up -d --build
docker compose logs -f bot
ssh -N -L 8080:127.0.0.1:8080 you@your-server
```

Follow these guides in order:

1. [deploy/VPS.md](deploy/VPS.md) covers server hardening, Docker, backups, systemd and going live.
2. [deploy/TELEGRAM.md](deploy/TELEGRAM.md) covers the BotFather bot, your chat id, commands and
   approvals.

## Know the limits

- **Stops live in the bot, not at Alpaca.** The bot checks prices every poll and sends a market
  exit when a stop is crossed. While the bot is down, nothing protects open positions. Every
  Alpaca call times out after 5 seconds to connect and 30 seconds to read. If the loop still
  makes no progress for 5 minutes, a watchdog exits the process, and Docker restarts it.
- **Live bars come from Alpaca, backtest bars from Yahoo.** The bot picks Alpaca data that
  matches the backtest's. Stock bars come from the SIP feed, read at least 16 minutes late, which
  free plans allow. If SIP fails, the bot falls back to IEX, which can differ more. Crypto days
  are built from hourly bars on UTC days.
- **The backtest can't include Jev.** Replaying historical headlines through Jev would cost money
  and leak hindsight. The dashboard's `/jev` page logs every probability next to the trade's
  outcome, so you can judge Jev on real evidence.
- **Paper fills are kinder than live fills.** The final check measures the slippage gap, but live
  markets can still be worse.
- **Few trades mean wide error bars.** Daily bars on three assets produce a handful of trades a
  year per asset. Treat any win rate with suspicion.

## Backtest results

> These are backtests, not paper or live results, and they don't predict future returns. The
> **NOT FINANCIAL ADVICE** note at the top of this file applies.

These numbers come from the tournament run of 2026-09-30 in `results/tournament.json`. They
include slippage and fees. The in-sample (IS) window runs from 2015-10-14 to 2021-12-31. The
out-of-sample (OOS) window runs from 2022-01-01 to 2026-09-29. The tournament picked each winner
on in-sample results only. A new tournament run updates `results/`, not this section.

### Winners

- **SPY:** `breakout(atr_mult=2,entry_n=20,exit_n=20)`. Buy when the close tops the prior 20-day
  high, sell when it closes below the prior 20-day low, with a stop 2 × ATR(14) under the signal
  close that trails up to the 20-day low.
- **QQQ:** `momentum(atr_mult=3,lookback=252)`. Buy when the close is above its level 252 days
  ago and its 50-day average while volatility is calm, sell when the 252-day return turns
  negative, with a trailing stop 3 × ATR(14) under the highest close.
- **BTC/USD:** `momentum(atr_mult=3,lookback=120)`. The same momentum rules with a 120-day
  lookback.

| Asset | Window | Return | Max drawdown | Win rate | Trades |
|---|---|---|---|---|---|
| SPY | IS | +18.1% | 2.4% | 64% | 22 |
| SPY | OOS | +9.9% | 3.0% | 42% | 19 |
| QQQ | IS | +19.2% | 2.7% | 49% | 49 |
| QQQ | OOS | +11.8% | 3.0% | 48% | 27 |
| BTC/USD | IS | +87.5% | 6.1% | 48% | 62 |
| BTC/USD | OOS | +7.6% | 5.6% | 41% | 37 |

### Winners traded together

This backtest runs all three winners on one shared $10,000 account, with the exposure cap, the
daily loss limit and the kill-switch drawdown applied.

| Window | Return | CAGR | Max drawdown | Sharpe | Trades |
|---|---|---|---|---|---|
| Out-of-sample, 2022-01-01 to 2026-09-29 | +31.5% | +5.95% | 6.4% | 0.98 | 83 |
| Full period, 2015-10-14 to 2026-09-29 | +195.9% | +10.41% | 6.4% | 1.66 | 215 |

- Only the out-of-sample row is a fair estimate. The full period includes the in-sample years
  the winners were picked on.
- The 15% drawdown kill switch would never have fired in either window.
- Expect to approve most entries on Telegram. Of the out-of-sample entries, 77 of 83 (93%) were
  above the $1,000 approval threshold. Over the full period, 202 of 215 (94%) were.

### Buy-and-hold did far better

Simply holding each asset returned far more than these rules in both windows:

| Asset | IS buy-and-hold | OOS buy-and-hold |
|---|---|---|
| SPY | +167.6% | +70.4% |
| QQQ | +295.0% | +89.0% |
| BTC/USD | +18,276.4% | +75.4% |

These rules trade that upside for small drawdowns at the current sizing: 1% risk per trade on a
$10,000 slice of the account, with at most $3,500 in any one position. Buy-and-hold drawdowns
reached 24.5% to 83.4% across these windows. The rules' worst was 6.4%.

Read `results/tournament.md` for every run, the regime and stress tables, and the caveats. To
share the results as one standalone HTML page, run `python -m bot export-report`, which writes
`results/tournament.html`.

## Project layout

```text
trading-bot/
├── bot/
│   ├── __main__.py        CLI: python -m bot <command>
│   ├── config.py          Settings from .env, rules from strategy.md
│   ├── models.py          Shared types
│   ├── timeutil.py        Bar-close timing
│   ├── indicators.py      Causal indicators
│   ├── strategies/        trend, breakout, meanrev, momentum
│   ├── data.py            yfinance cache for backtests
│   ├── backtest/          Engine, metrics, regimes, tournament
│   ├── jev.py             Jev gate: veto only, fail-closed
│   ├── news.py            Alpaca headlines, treated as untrusted data
│   ├── risk.py            Kill switch and risk manager
│   ├── broker.py          Alpaca and simulated brokers, live bars, OrderGateway
│   ├── notify.py          Telegram and console notifiers
│   ├── store.py           SQLite store
│   ├── runner.py          Live loop
│   ├── report.py          Daily report
│   ├── live_gate.py       Kill-switch drill and final check
│   └── dashboard/         Read-only FastAPI dashboard
├── tests/                 pytest suite: no network, no keys
├── deploy/
│   ├── VPS.md             Server walkthrough
│   ├── TELEGRAM.md        Telegram setup
│   ├── trading-bot.service  systemd unit that starts compose on boot
│   └── backup.sh          Backs up var/ with sqlite3 .backup
├── results/               Tournament output
├── data/cache/            Daily bars (not committed)
├── var/                   Runtime state: SQLite, kill switch, reports (not committed)
├── strategy.md            Rules, risk limits and Jev thresholds: the source of truth
├── ARCHITECTURE.md        Module contract
├── Dockerfile             One image for both services
├── docker-compose.yml     bot + dashboard
├── .env.example           Every setting, with paper defaults
├── requirements.txt       Runtime dependencies
└── requirements-dev.txt   Adds pytest
```
