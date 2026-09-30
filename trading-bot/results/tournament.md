# Tournament results

Generated 2026-09-30T20:04:29Z by `python -m bot tournament`. This is a backtest of a paper-trading research bot, not financial advice. Past results don't predict future returns.

## Setup

- **SPY data:** 2014-09-17 to 2026-09-29, 3,026 daily bars.
- **QQQ data:** 2014-09-17 to 2026-09-29, 3,026 daily bars.
- **BTC/USD data:** 2014-09-17 to 2026-09-29, 4,396 daily bars.
- **Windows:** in-sample (IS) 2014-09-17 to 2021-12-31, out-of-sample (OOS) 2022-01-01 to 2026-09-29. Indicators warm up on the full history; trades happen only inside each window, and each window starts flat.
- **Configurations tested:** 90 (4 strategies, each over its parameter grid, on 3 assets).
- **Filters, applied to both windows:** max drawdown at most 15%, win rate at least 0.40, profit factor at least 1.2, at least 8 trades, and a positive return in-sample and out-of-sample.
- **Ranking:** the tournament ranks survivors by in-sample Calmar ratio (CAGR / max drawdown), then in-sample win rate. The out-of-sample window only passes or fails a configuration.
- **Costs per fill:** slippage stock 5 bps, crypto 10 bps; fees stock 0 bps, crypto 25 bps.
- **Sizing:** $10,000 capital, 1% risk per trade, at most 33% and $3,500 per position, 100% total exposure. Each single-asset backtest sizes on its own equity.

## Winners

### SPY: Donchian breakout

`breakout(atr_mult=3,entry_n=20,exit_n=10)` passed every filter in both windows and ranked first of 23 survivors on in-sample Calmar ratio (0.83). Of its 3 grid neighbours (one parameter different), 3 also survive.

Over the full period it placed about 5 entries a year, and 98% of them were above $1,000, so expect to approve most entries in Telegram.

- **Entry:** Buy at the next open when the close is above the highest high of the previous 20 days.
- **Exit:** Sell at the next open when the close is below the lowest low of the previous 10 days.
- **Stop loss:** Starts at the signal day's close minus 3 × ATR(14). After every close it moves up to the lowest low of the previous 10 days when that is higher, and it is never lowered.
- **Take profit:** None. The trailing channel stop and the exit rule take the profit.
- **Timeframe:** Daily bars closing at 16:00 New York time. Signals use the completed bar's close; orders fill at the next bar's open.

| Window | Return | CAGR | Max drawdown | Win rate | Profit factor | Trades | Buy and hold |
|---|---|---|---|---|---|---|---|
| In-sample, 2014-09-17 to 2021-12-31 | +12.3% | +1.61% | 1.9% | 0.53 | 3.02 | 40 | +172.3% |
| Out-of-sample, 2022-01-01 to 2026-09-29 | +8.3% | +1.70% | 2.5% | 0.56 | 2.47 | 25 | +70.4% |

### QQQ: Time-series momentum

`momentum(atr_mult=3,lookback=252)` passed every filter in both windows and ranked first of 17 survivors on in-sample Calmar ratio (0.92). Of its 3 grid neighbours (one parameter different), 3 also survive.

Over the full period it placed about 6 entries a year, and 100% of them were above $1,000, so expect to approve most entries in Telegram.

- **Entry:** Buy at the next open when the close is higher than it was 252 days ago, the close is above its 50-day average, and 20-day realized volatility is below the 90th percentile of its last 252 days.
- **Exit:** Sell at the next open when the close is lower than it was 252 days ago.
- **Stop loss:** Trailing stop at the highest close since entry minus 3 × ATR(14), recomputed after every close and never lowered. It starts at the signal day's close minus 3 × ATR(14).
- **Take profit:** None. The trailing stop and the exit rule take the profit.
- **Timeframe:** Daily bars closing at 16:00 New York time. Signals use the completed bar's close; orders fill at the next bar's open.

| Window | Return | CAGR | Max drawdown | Win rate | Profit factor | Trades | Buy and hold |
|---|---|---|---|---|---|---|---|
| In-sample, 2014-09-17 to 2021-12-31 | +19.2% | +2.44% | 2.7% | 0.49 | 2.45 | 49 | +325.9% |
| Out-of-sample, 2022-01-01 to 2026-09-29 | +11.8% | +2.38% | 3.0% | 0.48 | 2.55 | 27 | +89.0% |

### BTC/USD: Time-series momentum

`momentum(atr_mult=3,lookback=120)` passed every filter in both windows and ranked first of 8 survivors on in-sample Calmar ratio (1.43). Of its 3 grid neighbours (one parameter different), 1 also survives.

Over the full period it placed about 9 entries a year, and 83% of them were above $1,000, so expect to approve most entries in Telegram.

- **Entry:** Buy at the next open when the close is higher than it was 120 days ago, the close is above its 50-day average, and 20-day realized volatility is below the 90th percentile of its last 252 days.
- **Exit:** Sell at the next open when the close is lower than it was 120 days ago.
- **Stop loss:** Trailing stop at the highest close since entry minus 3 × ATR(14), recomputed after every close and never lowered. It starts at the signal day's close minus 3 × ATR(14).
- **Take profit:** None. The trailing stop and the exit rule take the profit.
- **Timeframe:** Daily bars closing at 00:00 UTC. Signals use the completed bar's close; orders fill at the next bar's open.

| Window | Return | CAGR | Max drawdown | Win rate | Profit factor | Trades | Buy and hold |
|---|---|---|---|---|---|---|---|
| In-sample, 2014-09-17 to 2021-12-31 | +85.1% | +8.81% | 6.1% | 0.48 | 4.55 | 66 | +10025.3% |
| Out-of-sample, 2022-01-01 to 2026-09-29 | +7.6% | +1.56% | 5.6% | 0.41 | 1.75 | 37 | +75.4% |

## Survivors

| Asset | Strategy | Tested | Survivors |
|---|---|---|---|
| SPY | trend | 8 | 5 |
| SPY | breakout | 8 | 7 |
| SPY | meanrev | 8 | 8 |
| SPY | momentum | 6 | 3 |
| QQQ | trend | 8 | 3 |
| QQQ | breakout | 8 | 8 |
| QQQ | meanrev | 8 | 0 |
| QQQ | momentum | 6 | 6 |
| BTC/USD | trend | 8 | 0 |
| BTC/USD | breakout | 8 | 4 |
| BTC/USD | meanrev | 8 | 0 |
| BTC/USD | momentum | 6 | 4 |

## Leaderboards

The top 5 configurations per asset by in-sample score, failures included. Neighbours counts the grid points one parameter away that also survive.

### SPY

| # | Configuration | IS Calmar | IS win rate | IS return | IS max DD | OOS return | OOS max DD | OOS win rate | Trades IS/OOS | Neighbours | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `breakout(atr_mult=3,entry_n=20,exit_n=10)` | 0.83 | 0.53 | +12.3% | 1.9% | +8.3% | 2.5% | 0.56 | 40/25 | 3/3 | pass |
| 2 | `breakout(atr_mult=2,entry_n=20,exit_n=10)` | 0.79 | 0.53 | +14.8% | 2.4% | +9.5% | 2.8% | 0.54 | 40/26 | 3/3 | pass |
| 3 | `breakout(atr_mult=2,entry_n=20,exit_n=20)` | 0.67 | 0.58 | +16.6% | 3.2% | +9.9% | 3.0% | 0.42 | 26/19 | 2/3 | pass |
| 4 | `breakout(atr_mult=3,entry_n=20,exit_n=20)` | 0.64 | 0.60 | +14.2% | 2.9% | +10.4% | 2.2% | 0.50 | 25/16 | 3/3 | pass |
| 5 | `momentum(atr_mult=5,lookback=60)` | 0.62 | 0.61 | +10.2% | 2.2% | +2.5% | 3.4% | 0.45 | 36/22 | 1/3 | pass |

### QQQ

| # | Configuration | IS Calmar | IS win rate | IS return | IS max DD | OOS return | OOS max DD | OOS win rate | Trades IS/OOS | Neighbours | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `momentum(atr_mult=3,lookback=252)` | 0.92 | 0.49 | +19.2% | 2.7% | +11.8% | 3.0% | 0.48 | 49/27 | 3/3 | pass |
| 2 | `momentum(atr_mult=5,lookback=252)` | 0.84 | 0.60 | +13.0% | 2.0% | +2.6% | 2.7% | 0.53 | 25/17 | 3/3 | pass |
| 3 | `momentum(atr_mult=3,lookback=120)` | 0.77 | 0.44 | +17.1% | 2.8% | +11.4% | 2.7% | 0.41 | 48/32 | 3/3 | pass |
| 4 | `momentum(atr_mult=5,lookback=120)` | 0.60 | 0.48 | +11.3% | 2.5% | +3.5% | 2.5% | 0.41 | 25/22 | 3/3 | pass |
| 5 | `momentum(atr_mult=5,lookback=60)` | 0.56 | 0.50 | +8.5% | 2.0% | +3.7% | 2.1% | 0.50 | 34/22 | 3/3 | pass |

### BTC/USD

| # | Configuration | IS Calmar | IS win rate | IS return | IS max DD | OOS return | OOS max DD | OOS win rate | Trades IS/OOS | Neighbours | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `momentum(atr_mult=3,lookback=252)` | 2.01 | 0.47 | +90.4% | 4.6% | +7.9% | 5.2% | 0.34 | 55/29 | 2/3 | OOS win_rate 0.34 < 0.40 |
| 2 | `trend(atr_mult=3,fast=20,slow=100)` | 1.69 | 0.39 | +78.8% | 4.9% | +4.0% | 5.9% | 0.25 | 56/36 | 0/3 | IS win_rate 0.39 < 0.40; OOS win_rate 0.25 < 0.40 |
| 3 | `momentum(atr_mult=3,lookback=60)` | 1.51 | 0.48 | +107.5% | 7.0% | +17.5% | 5.0% | 0.39 | 63/41 | 2/3 | OOS win_rate 0.39 < 0.40 |
| 4 | `breakout(atr_mult=3,entry_n=20,exit_n=20)` | 1.47 | 0.62 | +201.1% | 11.1% | +11.5% | 6.8% | 0.36 | 26/28 | 1/3 | OOS win_rate 0.36 < 0.40 |
| 5 | `momentum(atr_mult=3,lookback=120)` | 1.43 | 0.48 | +85.1% | 6.1% | +7.6% | 5.6% | 0.41 | 66/37 | 1/3 | pass |

## Portfolio of the winners

The winners (SPY `breakout(atr_mult=3,entry_n=20,exit_n=10)`, QQQ `momentum(atr_mult=3,lookback=252)`, BTC/USD `momentum(atr_mult=3,lookback=120)`) trade together on one $10,000 account. Each entry is sized on the portfolio's equity and shrunk to fit the total exposure cap. The daily loss limit blocks entries for the rest of the New York day, and every close where the drawdown reaches the kill-switch limit is listed. The simulation keeps trading after that point; the live bot would stop until you resume it. Only the out-of-sample row is a fair estimate: the full period includes the in-sample years the winners were picked on. `tournament.json` stores the out-of-sample run under `portfolio` and the full period under `portfolio_full`.

| Window | Return | CAGR | Max drawdown | Sharpe | Trades | Win rate | Time invested | Kill switch would fire | Daily loss limit days |
|---|---|---|---|---|---|---|---|---|---|
| Out-of-sample, 2022-01-01 to 2026-09-29 | +29.8% | +5.65% | 5.7% | 1.02 | 89 | 0.47 | 69% | never | 2 |
| Full period, 2014-09-17 to 2026-09-29 | +184.6% | +9.08% | 6.1% | 1.59 | 243 | 0.49 | 74% | never | 3 |

- **Out-of-sample:** trades by asset: SPY 25, QQQ 27, BTC/USD 37. 0 entries blocked, 0 shrunk by the exposure cap.
  - Daily loss limit hit on: 2024-03-05, 2025-10-10.
- **Full-period:** trades by asset: SPY 65, QQQ 75, BTC/USD 103. 0 entries blocked, 0 shrunk by the exposure cap.
  - Daily loss limit hit on: 2016-06-21, 2019-05-17, 2020-11-26.

## Regimes and stress windows

Full-period runs. Trend regimes: bull when the close is above a rising 200-day average, bear when below a falling one, sideways otherwise. Vol regimes compare 20-day realized volatility with its 1-year median. Returns compound only the days spent in each regime. n/a means no bars in the window.

### SPY `breakout(atr_mult=3,entry_n=20,exit_n=10)`

| Regime or window | Return | Max drawdown | Trades entered | Bars |
|---|---|---|---|---|
| trend bull | +16.4% | 3.6% | 52 | 2206 |
| trend bear | +1.7% | 1.1% | 8 | 343 |
| trend sideways | +1.9% | 1.7% | 5 | 476 |
| vol high | +3.5% | 2.9% | 36 | 1277 |
| vol low | +16.6% | 2.7% | 29 | 1748 |
| crypto_winter_2018 (2018-01-06 to 2018-12-15) | +1.9% | 1.4% | 3 | 237 |
| q4_2018_equity_selloff (2018-09-20 to 2018-12-24) | -0.1% | 0.3% | 0 | 66 |
| covid_crash_2020 (2020-02-19 to 2020-03-23) | -1.3% | 1.4% | 0 | 24 |
| bear_market_2022 (2022-01-03 to 2022-10-12) | -0.1% | 1.1% | 2 | 196 |
| crypto_crash_2021_2022 (2021-11-10 to 2022-11-21) | -0.1% | 1.6% | 4 | 260 |
| tariff_shock_2025 (2025-02-19 to 2025-04-08) | -0.6% | 0.7% | 1 | 35 |

### QQQ `momentum(atr_mult=3,lookback=252)`

| Regime or window | Return | Max drawdown | Trades entered | Bars |
|---|---|---|---|---|
| trend bull | +33.0% | 3.4% | 67 | 2219 |
| trend bear | +0.0% | 0.2% | 3 | 282 |
| trend sideways | +0.3% | 1.1% | 5 | 524 |
| vol high | +3.8% | 3.8% | 47 | 1385 |
| vol low | +28.6% | 2.7% | 28 | 1640 |
| crypto_winter_2018 (2018-01-06 to 2018-12-15) | +1.6% | 1.9% | 5 | 237 |
| q4_2018_equity_selloff (2018-09-20 to 2018-12-24) | -0.4% | 1.0% | 0 | 66 |
| covid_crash_2020 (2020-02-19 to 2020-03-23) | -1.8% | 2.1% | 0 | 24 |
| bear_market_2022 (2022-01-03 to 2022-10-12) | -1.5% | 1.7% | 1 | 196 |
| crypto_crash_2021_2022 (2021-11-10 to 2022-11-21) | -3.6% | 4.1% | 4 | 260 |
| tariff_shock_2025 (2025-02-19 to 2025-04-08) | -0.9% | 0.9% | 0 | 35 |

### BTC/USD `momentum(atr_mult=3,lookback=120)`

| Regime or window | Return | Max drawdown | Trades entered | Bars |
|---|---|---|---|---|
| trend bull | +80.4% | 5.2% | 75 | 2332 |
| trend bear | -3.2% | 4.3% | 15 | 1108 |
| trend sideways | +14.0% | 2.6% | 13 | 955 |
| vol high | +49.7% | 4.5% | 50 | 1948 |
| vol low | +33.0% | 4.2% | 53 | 2447 |
| crypto_winter_2018 (2018-01-06 to 2018-12-15) | -3.7% | 3.7% | 5 | 344 |
| q4_2018_equity_selloff (2018-09-20 to 2018-12-24) | -0.7% | 0.7% | 1 | 96 |
| covid_crash_2020 (2020-02-19 to 2020-03-23) | -0.8% | 0.8% | 0 | 34 |
| bear_market_2022 (2022-01-03 to 2022-10-12) | +0.0% | 0.0% | 0 | 283 |
| crypto_crash_2021_2022 (2021-11-10 to 2022-11-21) | -2.4% | 2.4% | 2 | 377 |
| tariff_shock_2025 (2025-02-19 to 2025-04-08) | +0.0% | 0.0% | 0 | 49 |

### Portfolio, full period (regimes labelled on SPY)

Equity is sampled on SPY's trading days, so weekend crypto P&L lands on the next trading day. Trades entered on a day SPY didn't trade count only in the stress windows.

| Regime or window | Return | Max drawdown | Trades entered | Bars |
|---|---|---|---|---|
| trend bull | +147.2% | 6.3% | 182 | 2206 |
| trend bear | +4.8% | 1.5% | 20 | 343 |
| trend sideways | +9.8% | 1.7% | 11 | 476 |
| vol high | +30.1% | 5.0% | 106 | 1277 |
| vol low | +118.8% | 4.3% | 107 | 1748 |
| crypto_winter_2018 (2018-01-06 to 2018-12-15) | -1.5% | 2.9% | 13 | 237 |
| q4_2018_equity_selloff (2018-09-20 to 2018-12-24) | -1.0% | 1.5% | 1 | 66 |
| covid_crash_2020 (2020-02-19 to 2020-03-23) | -2.6% | 2.6% | 0 | 24 |
| bear_market_2022 (2022-01-03 to 2022-10-12) | -1.2% | 2.2% | 3 | 196 |
| crypto_crash_2021_2022 (2021-11-10 to 2022-11-21) | -4.8% | 5.6% | 10 | 260 |
| tariff_shock_2025 (2025-02-19 to 2025-04-08) | -0.8% | 0.8% | 1 | 35 |

## Manual approvals

Entries above $1,000 wait for your approval in Telegram. The backtest assumes you approve every one of them in time; in paper trading a rejected, expired or drifted approval skips the trade.

| Scope | Entry orders | Above the threshold | Share |
|---|---|---|---|
| SPY winner, full period | 65 | 64 | 98% |
| QQQ winner, full period | 75 | 75 | 100% |
| BTC/USD winner, full period | 103 | 86 | 83% |
| Portfolio, out-of-sample | 89 | 83 | 93% |
| Portfolio, full period | 243 | 228 | 94% |
| All 90 configurations, full period | 5979 | 5195 | 87% |

## Caveats

- **Multiple testing.** The tournament tried 90 configurations. Even with no real edge, some would pass both windows by chance, and the winner is the best of those. Expect live results below these numbers. A winner whose grid neighbours also survive is less likely to be a fluke.
- **The drawdown filter rarely binds at this size.** With 1% risk per trade and at most 33% per position, the worst single-asset drawdown in any window was 16.7% against a 15% limit, and 1 of 90 configurations failed on drawdown. Win rate, profit factor and the positive-return checks did most of the filtering.
- **The out-of-sample window is spent.** It was seen once, here. If you change a rule or a filter after reading these results and rerun, the out-of-sample window becomes in-sample.
- **Daily bars only.** Stops are checked against each bar's high and low; when a bar touches both the stop and a target, the backtest assumes the stop hit first. The order of moves inside a day is unknown, and the daily loss limit and kill switch are checked only at closes, so intraday dips that recover are invisible here but not to the live bot.
- **No Jev in the backtest.** Jev can only veto entries live, so live trading takes a subset of these trades. Replaying past headlines through Jev would cost money and leak hindsight.
- **Data differences.** The backtest uses Yahoo's split- and dividend-adjusted bars. The live bot uses Alpaca bars (SIP feed for stocks, UTC days built from hourly bars for crypto), which are not dividend-adjusted and can differ in the open, high and low. Signals can differ near thresholds.
- **Costs are assumptions.** Slippage stock 5 bps, crypto 10 bps and fees stock 0 bps, crypto 25 bps per fill. Real slippage is larger in fast markets and at gaps, and crypto spreads widen in stress.
- **Approvals are assumed granted.** Entries above the approval threshold wait for Telegram; a late, rejected or drifted approval skips a trade the backtest took.
- **Survivorship.** SPY, QQQ and BTC/USD were chosen knowing they survived and grew over this period. QQQ's tech run and Bitcoin's rise flatter any long-only rule tested on them.
- **Small sizes.** Sizing risks 1% of a $10,000 slice per trade with fractional quantities, so returns on the slice are modest by design; the buy-and-hold column is not a like-for-like comparison because it is fully invested without stops.
- **Portfolio versus single-asset runs.** Each single-asset backtest sizes on its own equity. The portfolio shares one account, so its trades can be smaller than the single-asset runs' trades.
