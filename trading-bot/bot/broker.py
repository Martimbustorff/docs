"""Brokers, the live-trading guard and the order gateway.

`OrderGateway` is the only production caller of `Broker.submit`. It re-checks the kill switch and
the live-trading guard immediately before every order, dedupes by `client_order_id`, records
every order in the store and reports failures to the RiskManager. It never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, Context, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from pydantic import SecretStr

from bot.config import LIVE_ACK_PHRASE, ConfigError, Settings
from bot.models import (
    AccountSnapshot,
    AssetClass,
    BrokerPosition,
    OrderIntent,
    OrderPurpose,
    OrderResult,
    Side,
    asset_class,
)
from bot.timeutil import NY, UTC, bar_close_ts, utcnow

if TYPE_CHECKING:
    from bot.notify import Notifier
    from bot.risk import KillSwitch, RiskManager
    from bot.store import Store

log = logging.getLogger(__name__)
HTTP_TIMEOUT = (5.0, 30.0)  # (connect, read) seconds for every Alpaca call; alpaca-py sets none
SIP_DELAY = timedelta(minutes=16)  # free Alpaca plans may read SIP data older than 15 minutes
MIN_HOURS_PER_DAY = 20
SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)  # SimBroker(market_hours=True)

BAR_COLUMNS = ("open", "high", "low", "close", "volume")
MAX_CLIENT_ORDER_ID = 48
QTY_DECIMALS = 9  # Alpaca accepts up to 9 decimal places for fractional quantities
QTY_EPSILON = 1e-9
LIVE_GATE_MAX_AGE = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)

# Alpaca order statuses folded into the OrderResult vocabulary. Anything not listed is still
# working at the broker and maps to "accepted"; the raw status is kept in OrderResult.message.
_STATUS_MAP = {
    "new": "new",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "rejected": "rejected",
    "canceled": "canceled",
    "expired": "canceled",
    "replaced": "canceled",
}


# --------------------------------------------------------------------------- helpers


def floor_qty(qty: float) -> float:
    """Round a quantity down to Alpaca's precision, so rounding never pushes an order over a cap."""
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    step = Decimal(1).scaleb(-QTY_DECIMALS)
    return float(Decimal(repr(qty)).quantize(step, rounding=ROUND_DOWN, context=Context(prec=60)))


def make_client_order_id(purpose: OrderPurpose, symbol: str, key: str) -> str:
    """Deterministic id per the contract: `{purpose}-{symbol_nodash}-{sha1(key)[:12]}`."""
    nodash = symbol.replace("/", "").replace("-", "")
    return f"{purpose.value}-{nodash}-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def redact(text: str, settings: Settings) -> str:
    """Replace every configured secret value in `text` with ***."""
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr) and value.get_secret_value():
            text = text.replace(value.get_secret_value(), "***")
    return text


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _required_float(value: Any, name: str) -> float:
    number = _to_float(value)
    if number is None or not math.isfinite(number):
        raise ValueError(f"Alpaca returned no usable {name}: {value!r}")
    return number


def _canonical_symbol(symbol: str, alpaca_asset_class: str) -> str:
    """Alpaca reports crypto positions as "BTCUSD"; the bot uses "BTC/USD"."""
    if alpaca_asset_class == "crypto" and "/" not in symbol and symbol.endswith("USD"):
        return f"{symbol[:-3]}/USD"
    return symbol


def _order_result(order: Any) -> OrderResult:
    raw_status = _enum_value(order.status)
    avg_price = _to_float(order.filled_avg_price)
    return OrderResult(
        client_order_id=order.client_order_id,
        broker_order_id=str(order.id),
        status=_STATUS_MAP.get(raw_status, "accepted"),
        filled_qty=_to_float(order.filled_qty) or 0.0,
        filled_avg_price=avg_price or None,  # Alpaca reports 0 before the first fill
        message=f"alpaca status: {raw_status}",
    )


def _is_not_found(exc: APIError) -> bool:
    return exc.status_code == 404


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype=float) for c in BAR_COLUMNS},
        index=pd.DatetimeIndex([], name="date"),
    )


def _bars_frame(symbol: str, bars: list[Any], start: date, end: date | None, now: datetime) -> pd.DataFrame:
    """Alpaca bars -> the `bot.data` frame shape, completed bars only, dated in the bar's market
    time zone (New York for stocks, UTC for crypto)."""
    crypto = asset_class(symbol) is AssetClass.CRYPTO
    rows: dict[date, tuple[float, ...]] = {}
    for bar in bars:
        ts = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=UTC)
        day = ts.astimezone(UTC if crypto else NY).date()
        closes_at = bar_close_ts(symbol, day)
        if crypto:  # also honour the bar's own 24h span in case Alpaca's day boundary is not 00:00 UTC
            closes_at = max(closes_at, ts + timedelta(days=1))
        if closes_at > now or day < start or (end is not None and day > end):
            continue
        rows[day] = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if not rows:
        return _empty_bars()
    days = sorted(rows)
    frame = pd.DataFrame(
        [rows[d] for d in days],
        columns=list(BAR_COLUMNS),
        index=pd.DatetimeIndex(pd.to_datetime(days), name="date"),
        dtype=float,
    )
    return frame.dropna()


def _stock_bars_request(symbol: str, start: datetime, end: datetime | None, feed: DataFeed) -> StockBarsRequest:
    return StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=feed, adjustment=Adjustment.ALL
    )


def _utc_days_from_hours(hours: list[Any], start: date, end: date | None, now: datetime) -> pd.DataFrame:
    """Hourly crypto bars -> daily bars on UTC days, keeping only days that have fully closed."""
    grouped: dict[date, list[Any]] = {}
    for bar in hours:
        ts = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=UTC)
        grouped.setdefault(ts.astimezone(UTC).date(), []).append(bar)
    rows: dict[date, tuple[float, ...]] = {}
    for day, bars in grouped.items():
        if datetime.combine(day + timedelta(days=1), time(0), tzinfo=UTC) > now:
            continue
        if day < start or (end is not None and day > end):
            continue
        if len(bars) < MIN_HOURS_PER_DAY:
            log.warning("only %d hourly bars on %s; its daily bar may differ from the backtest data", len(bars), day)
        bars.sort(key=lambda b: b.timestamp)
        rows[day] = (
            float(bars[0].open),
            max(float(b.high) for b in bars),
            min(float(b.low) for b in bars),
            float(bars[-1].close),
            sum(float(b.volume) for b in bars),
        )
    if not rows:
        return _empty_bars()
    days = sorted(rows)
    return pd.DataFrame(
        [rows[d] for d in days],
        columns=list(BAR_COLUMNS),
        index=pd.DatetimeIndex(pd.to_datetime(days), name="date"),
        dtype=float,
    ).dropna()


class _TimeoutSession(requests.Session):
    def __init__(self, timeout: tuple[float, float]) -> None:
        super().__init__()
        self._timeout = timeout

    def request(self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return super().request(method, url, *args, **kwargs)


def with_timeout(client: Any, timeout: tuple[float, float] = HTTP_TIMEOUT) -> Any:
    """Give an alpaca-py client its own session with a default timeout. alpaca-py shares one
    class-level `requests.Session` without any timeout, so one stalled connection would freeze
    the loop, and with it every stop, exit and /kill."""
    client._session = _TimeoutSession(timeout)
    return client


# --------------------------------------------------------------------------- live-trading guard


def assert_trading_allowed(
    settings: Settings, live_gate_path: Path, *, now: datetime | None = None, risk_increasing: bool = True
) -> None:
    """Raise ConfigError unless the mode, endpoint, acknowledgement and live gate agree.

    Allowed: paper mode against the paper endpoint, or live mode against the live endpoint with
    the exact acknowledgement phrase and a passed `live_gate.json` younger than 7 days.

    A risk-reducing order (`risk_increasing=False`) skips only the live gate's freshness check:
    an expired final check must stop new entries, never the stops protecting open positions.
    A mismatched mode/endpoint still blocks everything, because the bot can't tell which
    account it would be selling from.
    """
    if settings.trading_mode == "paper":
        if not settings.alpaca_paper:
            raise ConfigError("TRADING_MODE=paper but ALPACA_PAPER=false points at a live account; refusing to trade")
        return
    if settings.trading_mode != "live":
        raise ConfigError(f"unknown TRADING_MODE {settings.trading_mode!r}")
    if settings.alpaca_paper:
        raise ConfigError("TRADING_MODE=live needs ALPACA_PAPER=false; mixed paper/live config refused")
    if settings.live_trading_ack != LIVE_ACK_PHRASE:
        raise ConfigError("TRADING_MODE=live needs LIVE_TRADING_ACK set to the exact phrase in bot.config.LIVE_ACK_PHRASE")
    if risk_increasing:
        _check_live_gate(Path(live_gate_path), now or utcnow())


def _check_live_gate(path: Path, now: datetime) -> None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} not found; run `python -m bot final-check` after paper trading") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("passed") is not True:
        raise ConfigError(f"{path} does not record a passed final check")
    ts = _parse_ts(data.get("ts"))
    if ts is None:
        raise ConfigError(f"{path} has no valid ISO-8601 'ts'")
    age = now - ts
    if age < -CLOCK_SKEW:
        raise ConfigError(f"{path} is dated in the future ({ts.isoformat()})")
    if age >= LIVE_GATE_MAX_AGE:
        raise ConfigError(f"{path} is {age.days} days old; the final check must be younger than 7 days")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- brokers


class Broker(Protocol):
    is_paper: bool

    def account(self) -> AccountSnapshot: ...

    def positions(self) -> dict[str, BrokerPosition]: ...

    def last_price(self, symbol: str) -> float: ...

    def daily_bars(self, symbol: str, start: date, end: date | None = None) -> pd.DataFrame: ...

    def is_market_open(self) -> bool: ...

    def next_open(self) -> datetime: ...

    def submit(self, intent: OrderIntent) -> OrderResult: ...

    def get_order(self, client_order_id: str) -> OrderResult | None: ...

    def cancel_all(self) -> None: ...

    def close_position(self, symbol: str) -> OrderResult | None: ...


class AlpacaBroker:
    """alpaca-py TradingClient (paper endpoint iff settings.alpaca_paper) plus the historical data
    clients. Only OrderGateway may call `submit`."""

    def __init__(self, settings: Settings, clock: Callable[[], datetime] = utcnow) -> None:
        api_key, secret_key = settings.alpaca_api_key, settings.alpaca_secret_key
        if not settings.has_alpaca or api_key is None or secret_key is None:
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use Alpaca")
        key, secret = api_key.get_secret_value(), secret_key.get_secret_value()
        self.is_paper = settings.alpaca_paper
        self.trading = with_timeout(TradingClient(api_key=key, secret_key=secret, paper=settings.alpaca_paper))
        self.stock_data = with_timeout(StockHistoricalDataClient(api_key=key, secret_key=secret))
        self.crypto_data = with_timeout(CryptoHistoricalDataClient(api_key=key, secret_key=secret))
        self._clock = clock

    def account(self) -> AccountSnapshot:
        acct = self.trading.get_account()
        return AccountSnapshot(
            equity=_required_float(acct.equity, "equity"),
            cash=_required_float(acct.cash, "cash"),
            buying_power=_to_float(acct.buying_power) or 0.0,
            is_paper=self.is_paper,
            trading_blocked=bool(acct.trading_blocked or acct.account_blocked or acct.trade_suspended_by_user),
            status=_enum_value(acct.status),
        )

    def positions(self) -> dict[str, BrokerPosition]:
        out: dict[str, BrokerPosition] = {}
        for pos in self.trading.get_all_positions():
            symbol = _canonical_symbol(pos.symbol, _enum_value(pos.asset_class))
            qty = float(pos.qty)
            if _enum_value(pos.side) == "short" and qty > 0:
                qty = -qty
            avg = float(pos.avg_entry_price)
            market_value = _to_float(pos.market_value)
            if market_value is None:
                market_value = qty * (_to_float(pos.current_price) or avg)
            out[symbol] = BrokerPosition(
                symbol=symbol,
                qty=qty,
                avg_entry_price=avg,
                market_value=market_value,
                unrealized_pl=_to_float(pos.unrealized_pl) or 0.0,
            )
        return out

    def last_price(self, symbol: str) -> float:
        if asset_class(symbol) is AssetClass.CRYPTO:
            trades = self.crypto_data.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol))
        else:
            trades = self.stock_data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
            )
        price = float(trades[symbol].price)
        if not (math.isfinite(price) and price > 0):
            raise ValueError(f"Alpaca returned an unusable last price for {symbol}: {price}")
        return price

    def daily_bars(self, symbol: str, start: date, end: date | None = None) -> pd.DataFrame:
        """Completed daily bars in [start, end]; the bar still forming is never returned.

        Built to match the backtest's data: stock bars come from the consolidated SIP feed
        (free plans may read it once it is 15 minutes old), and crypto days are aggregated
        from hourly bars so each one spans 00:00-24:00 UTC like the cached daily history.
        """
        now = self._clock()
        start_dt = datetime.combine(start, time(0), tzinfo=UTC)
        end_dt = datetime.combine(end + timedelta(days=1), time(0), tzinfo=UTC) if end else None
        if asset_class(symbol) is AssetClass.CRYPTO:
            request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start_dt, end=end_dt)
            hours = self.crypto_data.get_crypto_bars(request).data.get(symbol, [])
            return _utc_days_from_hours(hours, start, end, now)
        sip_end = now - SIP_DELAY
        try:
            barset = self.stock_data.get_stock_bars(
                _stock_bars_request(symbol, start_dt, min(end_dt, sip_end) if end_dt else sip_end, DataFeed.SIP)
            )
        except APIError as exc:
            log.warning("SIP bars for %s unavailable (%s); falling back to IEX, which can differ from the backtest data", symbol, exc)
            barset = self.stock_data.get_stock_bars(_stock_bars_request(symbol, start_dt, end_dt, DataFeed.IEX))
        return _bars_frame(symbol, barset.data.get(symbol, []), start, end, now)

    def is_market_open(self) -> bool:
        """US equity session; crypto trades around the clock."""
        return bool(self.trading.get_clock().is_open)

    def next_open(self) -> datetime:
        nxt = self.trading.get_clock().next_open
        return (nxt if nxt.tzinfo else nxt.replace(tzinfo=UTC)).astimezone(UTC)

    def submit(self, intent: OrderIntent) -> OrderResult:
        qty = floor_qty(intent.qty)
        if qty <= 0:
            raise ValueError(f"order qty must be positive, got {intent.qty}")
        crypto = asset_class(intent.symbol) is AssetClass.CRYPTO
        request = MarketOrderRequest(
            symbol=intent.symbol,
            qty=qty,
            side=OrderSide.BUY if intent.side is Side.BUY else OrderSide.SELL,
            time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
            client_order_id=intent.client_order_id,
        )
        return _order_result(self.trading.submit_order(request))

    def get_order(self, client_order_id: str) -> OrderResult | None:
        try:
            order = self.trading.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if _is_not_found(exc):
                return None
            raise
        return _order_result(order)

    def cancel_all(self) -> None:
        failed = [str(r.id) for r in self.trading.cancel_orders() or [] if r.status >= 400]
        if failed:
            raise RuntimeError(f"Alpaca could not cancel orders: {', '.join(failed)}")

    def close_position(self, symbol: str) -> OrderResult | None:
        try:
            order = self.trading.close_position(symbol.replace("/", ""))  # "BTC/USD" would split the URL path
        except APIError as exc:
            if _is_not_found(exc):
                return None
            raise
        return _order_result(order)


class SimBroker:
    """In-memory broker for tests and --dry-run.

    Market orders fill at once at the price given to `set_price`, moved against you by
    `slippage_bps`. `fill_ratio` < 1 leaves orders partially filled until `cancel_all`.
    Set `fail_next = N` to make the next N `submit`/`close_position` calls raise.
    """

    def __init__(
        self,
        cash: float = 100_000.0,
        slippage_bps: float = 0.0,
        fill_ratio: float = 1.0,
        fail_next: int = 0,
        is_paper: bool = True,
        clock: Callable[[], datetime] = utcnow,
        market_hours: bool = False,
    ) -> None:
        """`market_hours=True` follows the regular New York session (weekdays 09:30-16:00,
        holidays ignored) for dry runs; otherwise `market_open`/`next_open_at` are set by tests."""
        if not 0 < fill_ratio <= 1:
            raise ValueError("fill_ratio must be in (0, 1]")
        self.is_paper = is_paper
        self.cash = cash
        self.slippage_bps = slippage_bps
        self.fill_ratio = fill_ratio
        self.fail_next = fail_next
        self.market_open = True
        self.next_open_at: datetime | None = None
        self.prices: dict[str, float] = {}
        self.bars: dict[str, pd.DataFrame] = {}
        self.orders: dict[str, OrderResult] = {}
        self.submitted: list[OrderIntent] = []
        self.cancel_all_calls = 0
        self._holdings: dict[str, tuple[float, float]] = {}  # symbol -> (qty, avg entry price)
        self._clock = clock
        self._market_hours = market_hours
        self._seq = 0

    def set_price(self, symbol: str, price: float) -> None:
        if not (math.isfinite(price) and price > 0):
            raise ValueError(f"price must be positive, got {price}")
        self.prices[symbol] = price

    def set_bars(self, symbol: str, bars: pd.DataFrame) -> None:
        self.bars[symbol] = bars

    def account(self) -> AccountSnapshot:
        equity = self.cash + sum(p.market_value for p in self.positions().values())
        return AccountSnapshot(equity=equity, cash=self.cash, buying_power=self.cash, is_paper=self.is_paper)

    def positions(self) -> dict[str, BrokerPosition]:
        out = {}
        for symbol, (qty, avg) in self._holdings.items():
            price = self.prices.get(symbol, avg)
            out[symbol] = BrokerPosition(symbol, qty, avg, qty * price, (price - avg) * qty)
        return out

    def last_price(self, symbol: str) -> float:
        if symbol not in self.prices:
            raise ValueError(f"SimBroker has no price for {symbol}; call set_price first")
        return self.prices[symbol]

    def daily_bars(self, symbol: str, start: date, end: date | None = None) -> pd.DataFrame:
        bars = self.bars.get(symbol)
        if bars is None:
            return _empty_bars()
        return bars.loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None].copy()

    def is_market_open(self) -> bool:
        if self._market_hours:
            local = self._clock().astimezone(NY)
            return local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE
        return self.market_open

    def next_open(self) -> datetime:
        if self._market_hours:
            local = self._clock().astimezone(NY)
            day = local.date() if local.time() < SESSION_OPEN else local.date() + timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return datetime.combine(day, SESSION_OPEN, tzinfo=NY).astimezone(UTC)
        return self.next_open_at or self._clock()

    def submit(self, intent: OrderIntent) -> OrderResult:
        self._maybe_fail("submit")
        if intent.client_order_id in self.orders:
            raise ValueError(f"client_order_id {intent.client_order_id!r} must be unique")
        if not (math.isfinite(intent.qty) and intent.qty > 0):
            raise ValueError(f"order qty must be positive, got {intent.qty}")
        qty = intent.qty * self.fill_ratio
        price = self._fill(intent.symbol, intent.side, qty)
        status = "filled" if self.fill_ratio >= 1 else "partially_filled"
        result = OrderResult(intent.client_order_id, self._next_id(), status, qty, price)
        self.orders[intent.client_order_id] = result
        self.submitted.append(intent)
        return result

    def get_order(self, client_order_id: str) -> OrderResult | None:
        return self.orders.get(client_order_id)

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1
        for cid, order in self.orders.items():
            if order.status == "partially_filled":
                self.orders[cid] = replace(order, status="canceled")

    def close_position(self, symbol: str) -> OrderResult | None:
        self._maybe_fail("close_position")
        qty = self._holdings.get(symbol, (0.0, 0.0))[0]
        if qty <= 0:
            return None
        price = self._fill(symbol, Side.SELL, qty)
        broker_id = self._next_id()
        cid = f"sim-close-{symbol.replace('/', '')}-{broker_id}"
        result = OrderResult(cid, broker_id, "filled", qty, price)
        self.orders[cid] = result
        return result

    def _maybe_fail(self, what: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError(f"SimBroker injected failure in {what}")

    def _next_id(self) -> str:
        self._seq += 1
        return f"sim-{self._seq}"

    def _fill(self, symbol: str, side: Side, qty: float) -> float:
        slip = self.slippage_bps / 1e4
        price = self.last_price(symbol) * (1 + slip if side is Side.BUY else 1 - slip)
        held, avg = self._holdings.get(symbol, (0.0, 0.0))
        if side is Side.BUY:
            cost = qty * price
            if cost > self.cash + 1e-9:
                raise ValueError(f"insufficient buying power: need {cost:.2f}, have {self.cash:.2f}")
            self.cash -= cost
            self._holdings[symbol] = (held + qty, (held * avg + cost) / (held + qty))
            return price
        if qty > held + QTY_EPSILON:
            raise ValueError(f"insufficient qty: selling {qty} {symbol}, holding {held}")
        self.cash += qty * price
        remaining = held - qty
        if remaining <= QTY_EPSILON:
            self._holdings.pop(symbol, None)
        else:
            self._holdings[symbol] = (remaining, avg)
        return price


# --------------------------------------------------------------------------- gateway


def _as_result(row: Any) -> OrderResult:
    """The store may hand back an OrderResult, a dict, an sqlite3.Row or a record object."""
    if isinstance(row, OrderResult):
        return row

    def field(name: str) -> Any:
        if isinstance(row, Mapping):
            return row.get(name)
        if hasattr(row, "keys"):
            return row[name] if name in row.keys() else None
        return getattr(row, name, None)

    return OrderResult(
        client_order_id=field("client_order_id"),
        broker_order_id=field("broker_order_id"),
        status=field("status") or "new",
        filled_qty=float(field("filled_qty") or 0.0),
        filled_avg_price=_to_float(field("filled_avg_price")),
        message=field("message") or "",
    )


def _never_reached_broker(result: OrderResult) -> bool:
    return result.status in ("refused", "rejected") and not result.broker_order_id


class OrderGateway:
    """The ONLY path to broker.submit. Re-checks the kill switch and the live-trading guard
    immediately before every risk-increasing order, dedupes by client_order_id via the store,
    records the order, and reports errors to RiskManager."""

    def __init__(
        self,
        broker: Broker,
        risk: RiskManager,
        kill: KillSwitch,
        store: Store,
        settings: Settings,
        notifier: Notifier,
    ) -> None:
        self.broker = broker
        self.risk = risk
        self.kill = kill
        self.store = store
        self.settings = settings
        self.notifier = notifier

    def submit(self, intent: OrderIntent) -> OrderResult:
        """Send one market order. Returns status "refused" when a guard stops it and "rejected"
        when the broker call fails. Resubmitting a known client_order_id returns the known order."""
        known = self._known_order(intent)
        if known is not None:
            log.info("order %s already exists (%s); not resubmitting", intent.client_order_id, known.status)
            return known
        problem = self._shape_problem(intent)
        if problem is None and intent.side is Side.SELL:
            intent, problem = self._fit_to_position(intent)
        if problem is None:
            problem = self._guard_problem(intent)
        if problem is not None:
            return self._refuse(intent, problem)
        return self._send(intent)

    def flatten_all(self, reason: str) -> list[OrderResult]:
        """Cancel every open order, then close every non-zero position in the account."""
        guard = self._live_guard_problem()
        if guard is not None:
            return self._refuse_flatten(reason, guard)
        failures: list[str] = []
        try:
            self.broker.cancel_all()
        except Exception as exc:
            failures.append(f"cancel_all: {self._describe(exc)}")
        try:
            positions = self.broker.positions()
        except Exception as exc:
            failures.append(f"positions: {self._describe(exc)}")
            positions = {}
        results: list[OrderResult] = []
        closed: list[str] = []
        for symbol, pos in sorted(positions.items()):
            result = self._close(symbol, pos, reason, failures)
            if result is None:
                continue
            results.append(result)
            if result.status != "rejected":
                closed.append(f"{symbol} {pos.qty:g} ({result.status})")
        if failures:
            self.risk.after_error()
        summary = f"Flatten ({reason}): {len(closed)} position(s) closed"
        if closed:
            summary += ": " + ", ".join(closed)
        if failures:
            summary += ". FAILURES: " + "; ".join(failures)
        level = "critical" if failures else "warning"
        log.log(logging.CRITICAL if failures else logging.WARNING, summary)
        self._event(level, "flatten", summary, {"reason": reason})
        self._alert(summary)
        return results

    # ------------------------------------------------------------------ submit steps

    def _known_order(self, intent: OrderIntent) -> OrderResult | None:
        """The stored order for this id, or the broker's copy if we crashed before recording it.
        Orders that never reached the broker ("refused", or "rejected" with no broker id) may retry."""
        cid = intent.client_order_id
        try:
            row = self.store.get_order_by_client_id(cid)
        except Exception:
            log.exception("store lookup for %s failed; falling back to the broker", cid)
            row = None
        stored = None if row is None else _as_result(row)
        if stored is not None and not _never_reached_broker(stored):
            return stored
        if stored is not None and stored.status == "refused":
            return None
        try:
            at_broker = self.broker.get_order(cid)
        except Exception:
            log.warning("broker lookup for %s failed; relying on broker-side id uniqueness", cid, exc_info=True)
            return None
        if at_broker is not None:
            log.warning("adopting order %s the broker already has (%s)", cid, at_broker.status)
            self._record(intent, at_broker)
        return at_broker

    @staticmethod
    def _shape_problem(intent: OrderIntent) -> str | None:
        if not (math.isfinite(intent.qty) and intent.qty > 0):
            return f"qty must be a positive number, got {intent.qty}"
        if not 0 < len(intent.client_order_id) <= MAX_CLIENT_ORDER_ID:
            return f"client_order_id must be 1-{MAX_CLIENT_ORDER_ID} characters"
        if intent.purpose.increases_risk and intent.side is not Side.BUY:
            return "long-only: an entry must be a buy"
        if not intent.purpose.increases_risk and intent.side is Side.BUY:
            return f"long-only: a {intent.purpose.value} order cannot buy"
        return None

    def _fit_to_position(self, intent: OrderIntent) -> tuple[OrderIntent, str | None]:
        """Never let a sell open a short: clamp it to the long position the broker reports."""
        try:
            held = self.broker.positions().get(intent.symbol)
        except Exception:
            log.warning("could not read positions before %s; sending it unchanged", intent.client_order_id, exc_info=True)
            return intent, None
        held_qty = held.qty if held is not None else 0.0
        if held_qty <= QTY_EPSILON:
            return intent, f"no long {intent.symbol} position to sell"
        if intent.qty > held_qty:
            log.warning("clamping %s from %s to the %s held", intent.client_order_id, intent.qty, held_qty)
            return replace(intent, qty=held_qty), None
        return intent, None

    def _guard_problem(self, intent: OrderIntent) -> str | None:
        if intent.purpose.increases_risk and self.kill.is_tripped():
            return "kill switch is tripped"
        return self._live_guard_problem(risk_increasing=intent.purpose.increases_risk)

    def _live_guard_problem(self, *, risk_increasing: bool = False) -> str | None:
        try:
            assert_trading_allowed(self.settings, self.settings.live_gate_path, risk_increasing=risk_increasing)
        except ConfigError as exc:
            return f"live-trading guard: {exc}"
        if not self.broker.is_paper and self.settings.trading_mode != "live":
            return "live-trading guard: the broker is live but TRADING_MODE is not live"
        return None

    def _send(self, intent: OrderIntent) -> OrderResult:
        try:
            result = self.broker.submit(intent)
        except Exception as exc:
            result = OrderResult(intent.client_order_id, None, "rejected", message=self._describe(exc))
        recorded = self._record(intent, result)
        if result.status == "rejected":
            log.error("order %s rejected: %s", intent.client_order_id, result.message)
            self.risk.after_error()
            self._event("error", "order_rejected", result.message, {"client_order_id": intent.client_order_id})
            self._alert(f"Order {self._label(intent)} was rejected: {result.message}")
        else:
            log.info("order %s -> %s", self._label(intent), result.status)
            if recorded:  # an unrecorded order already counted as an error
                self.risk.after_success()
        return result

    def _refuse(self, intent: OrderIntent, reason: str) -> OrderResult:
        result = OrderResult(intent.client_order_id, None, "refused", message=reason)
        log.warning("refused %s: %s", self._label(intent), reason)
        self._record(intent, result)
        self._event("warning", "order_refused", reason, {"client_order_id": intent.client_order_id})
        self._alert(f"Order {self._label(intent)} refused: {reason}")
        return result

    # ------------------------------------------------------------------ flatten steps

    def _close(self, symbol: str, pos: BrokerPosition, reason: str, failures: list[str]) -> OrderResult | None:
        if abs(pos.qty) <= QTY_EPSILON:
            return None
        try:
            result = self.broker.close_position(symbol)
        except Exception as exc:
            message = self._describe(exc)
            failures.append(f"{symbol}: {message}")
            cid = make_client_order_id(OrderPurpose.KILL, symbol, f"{reason}|{utcnow().isoformat()}")
            result = OrderResult(cid, None, "rejected", message=message)
        if result is not None:
            self._record(_close_intent(pos, reason, result.client_order_id), result)
        return result

    def _refuse_flatten(self, reason: str, guard: str) -> list[OrderResult]:
        try:
            positions = self.broker.positions()
        except Exception:
            log.warning("could not read positions while refusing flatten", exc_info=True)
            positions = {}
        results = [
            OrderResult(make_client_order_id(OrderPurpose.KILL, s, reason), None, "refused", message=guard)
            for s, p in sorted(positions.items())
            if abs(p.qty) > QTY_EPSILON
        ]
        text = f"Flatten ({reason}) REFUSED: {guard}"
        log.critical(text)
        self._event("critical", "flatten_refused", text, {"reason": reason})
        self._alert(text)
        return results

    # ------------------------------------------------------------------ side effects

    def _record(self, intent: OrderIntent, result: OrderResult) -> bool:
        try:
            self.store.upsert_order(intent, result)
        except Exception as exc:
            text = f"could not record order {intent.client_order_id} ({result.status}): {self._describe(exc)}"
            log.error(text)
            self.risk.after_error()
            self._alert(text)
            return False
        return True

    def _event(self, level: str, kind: str, message: str, data: dict[str, Any]) -> None:
        try:
            self.store.log_event(level, kind, message, data=data)
        except Exception:
            log.exception("could not record %s event", kind)

    def _alert(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except Exception:
            log.exception("notifier failed")

    def _describe(self, exc: Exception) -> str:
        return redact(f"{type(exc).__name__}: {exc}", self.settings)[:500]

    @staticmethod
    def _label(intent: OrderIntent) -> str:
        return f"{intent.client_order_id} ({intent.purpose.value} {intent.side.value} {intent.qty:g} {intent.symbol})"


def _close_intent(pos: BrokerPosition, reason: str, client_order_id: str) -> OrderIntent:
    ref_price = abs(pos.market_value / pos.qty) or pos.avg_entry_price
    return OrderIntent(
        symbol=pos.symbol,
        side=Side.SELL if pos.qty > 0 else Side.BUY,
        qty=abs(pos.qty),
        ref_price=ref_price,
        purpose=OrderPurpose.KILL,
        reason=reason,
        client_order_id=client_order_id,
    )
