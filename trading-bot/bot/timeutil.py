"""Bar timing. Daily bars are indexed by calendar date; these helpers turn a bar date into the
UTC moment the bar closed, so signals are only acted on after the data they use exists."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.models import AssetClass, asset_class

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def bar_close_ts(symbol: str, day: date) -> datetime:
    """Close time of the daily bar dated `day`.

    Stocks: 16:00 America/New_York on that day. Crypto: the UTC day ends at 00:00 UTC next day.
    """
    if asset_class(symbol) is AssetClass.CRYPTO:
        return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=UTC)
    return datetime.combine(day, time(16, 0), tzinfo=NY).astimezone(UTC)


def periods_per_year(symbol: str) -> int:
    return 365 if asset_class(symbol) is AssetClass.CRYPTO else 252


def ny_trading_day(ts: datetime) -> date:
    """The New York calendar date for `ts`; the daily loss limit resets on this boundary."""
    return ts.astimezone(NY).date()
