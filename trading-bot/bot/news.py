"""Recent headlines from Alpaca's news API, used as untrusted context for Jev.

Headlines are third-party data. This module only fetches and maps them; `bot.jev` sanitises
them before they reach Jev, and nothing ever executes or interprets them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import News
from alpaca.data.requests import NewsRequest

from bot.config import Settings
from bot.timeutil import UTC, utcnow

log = logging.getLogger(__name__)

ALPACA_MAX_NEWS_LIMIT = 50  # the news endpoint's per-page maximum


@dataclass(frozen=True)
class Headline:
    published_at: datetime  # tz-aware UTC
    source: str
    headline: str
    summary: str
    symbols: tuple[str, ...]


class NewsSource(Protocol):
    """The part of alpaca-py's `NewsClient` this module uses; tests pass a fake."""

    def get_news(self, request_params: NewsRequest) -> Any: ...


def news_symbol(symbol: str) -> str:
    """Alpaca tags crypto news with the pair minus its slash ("BTC/USD" -> "BTCUSD")."""
    return symbol.replace("/", "").upper()


def fetch_headlines(
    settings: Settings,
    symbol: str,
    lookback_hours: int,
    limit: int,
    *,
    client: NewsSource | None = None,
    now: datetime | None = None,
) -> list[Headline]:
    """Newest-first headlines for `symbol` published in the last `lookback_hours`.

    Returns [] (and logs) on any error; headlines are optional context and never block the bot.
    """
    if limit <= 0 or lookback_hours <= 0:
        return []
    end = _as_utc(now or utcnow())
    start = end - timedelta(hours=lookback_hours)
    try:
        if client is None:
            client = _alpaca_client(settings)
        if client is None:
            log.warning("news: Alpaca keys are not configured; continuing without headlines")
            return []
        request = NewsRequest(
            symbols=news_symbol(symbol),
            start=start,
            end=end,
            sort="desc",
            limit=min(limit, ALPACA_MAX_NEWS_LIMIT),
            include_content=False,
        )
        articles = list(client.get_news(request).data.get("news", []))
    except Exception as exc:  # network, auth, rate limit, schema drift
        log.error("news: fetching headlines for %s failed: %s: %s", symbol, type(exc).__name__, str(exc)[:200])
        return []
    headlines = [h for h in map(_to_headline, articles) if h is not None and start <= h.published_at <= end]
    headlines.sort(key=lambda h: h.published_at, reverse=True)
    return headlines[:limit]


def _alpaca_client(settings: Settings) -> NewsClient | None:
    key, secret = settings.alpaca_api_key, settings.alpaca_secret_key
    if key is None or secret is None:
        return None
    return NewsClient(api_key=key.get_secret_value(), secret_key=secret.get_secret_value())


def _to_headline(article: News) -> Headline | None:
    """Map one alpaca `News` article; None if it has no text or is malformed."""
    try:
        headline = (article.headline or "").strip()
        summary = (article.summary or "").strip()
        if not headline and not summary:
            return None
        return Headline(
            published_at=_as_utc(article.created_at),
            source=(article.source or "").strip(),
            headline=headline,
            summary=summary,
            symbols=tuple(article.symbols or ()),
        )
    except Exception as exc:
        log.warning("news: skipping malformed article: %s", type(exc).__name__)
        return None


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
