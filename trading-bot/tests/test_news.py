import logging
from datetime import datetime, timedelta, timezone

import pytest
from alpaca.data.models.news import NewsSet

import bot.news as news
from bot.config import load_settings
from bot.news import Headline, fetch_headlines, news_symbol

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SECRET = "alpaca-secret-do-not-log"


def article(i, *, hours_ago, headline="Bitcoin climbs", summary="Spot demand rises.", symbols=("BTCUSD",), naive=False):
    created = NOW - timedelta(hours=hours_ago)
    stamp = created.strftime("%Y-%m-%dT%H:%M:%S") + ("" if naive else "Z")
    return {
        "id": 40000000 + i,
        "headline": headline,
        "author": "Benzinga Newsdesk",
        "created_at": stamp,
        "updated_at": stamp,
        "summary": summary,
        "content": "",
        "url": f"https://www.benzinga.com/news/{i}",
        "images": [],
        "symbols": list(symbols),
        "source": "benzinga",
    }


class FakeNews:
    def __init__(self, articles=(), error=None):
        self.articles, self.error = list(articles), error
        self.requests = []

    def get_news(self, request_params):
        self.requests.append(request_params)
        if self.error is not None:
            raise self.error
        return NewsSet({"news": self.articles, "next_page_token": None})


@pytest.fixture
def keyed_settings(tmp_path):
    return load_settings(
        env_file=None,
        environ={"BOT_DATA_DIR": str(tmp_path / "var"), "ALPACA_API_KEY": "PKTEST", "ALPACA_SECRET_KEY": SECRET},
    )


def test_news_symbol_mapping():
    assert news_symbol("BTC/USD") == "BTCUSD"
    assert news_symbol("SPY") == "SPY"


def test_maps_alpaca_articles_and_builds_the_request(tmp_settings):
    client = FakeNews([article(1, hours_ago=1), article(2, hours_ago=3, headline="  ETF inflows  ", summary="")])
    result = fetch_headlines(tmp_settings, "BTC/USD", lookback_hours=24, limit=10, client=client, now=NOW)

    assert result == [
        Headline(NOW - timedelta(hours=1), "benzinga", "Bitcoin climbs", "Spot demand rises.", ("BTCUSD",)),
        Headline(NOW - timedelta(hours=3), "benzinga", "ETF inflows", "", ("BTCUSD",)),
    ]
    [request] = client.requests
    assert request.symbols == "BTCUSD"
    assert request.start == NOW - timedelta(hours=24) and request.end == NOW
    assert request.limit == 10 and request.sort == "desc"


def test_filters_sorts_and_caps(tmp_settings):
    client = FakeNews(
        [
            article(1, hours_ago=5),
            article(2, hours_ago=30),  # older than the lookback window
            article(3, hours_ago=1),
            article(4, hours_ago=2, headline="", summary="   "),  # no text
            article(5, hours_ago=4),
        ]
    )
    result = fetch_headlines(tmp_settings, "SPY", lookback_hours=24, limit=2, client=client, now=NOW)
    assert [h.published_at for h in result] == [NOW - timedelta(hours=1), NOW - timedelta(hours=4)]
    assert client.requests[0].symbols == "SPY"


def test_naive_timestamps_are_utc(tmp_settings):
    client = FakeNews([article(1, hours_ago=2, naive=True)])
    [h] = fetch_headlines(tmp_settings, "SPY", 24, 5, client=client, now=NOW)
    assert h.published_at == NOW - timedelta(hours=2) and h.published_at.tzinfo is not None


def test_limit_is_capped_at_the_api_maximum(tmp_settings):
    client = FakeNews([])
    assert fetch_headlines(tmp_settings, "SPY", 24, 500, client=client, now=NOW) == []
    assert client.requests[0].limit == 50


@pytest.mark.parametrize("lookback, limit", [(24, 0), (0, 10), (-1, 10)])
def test_nothing_requested_means_no_call(tmp_settings, lookback, limit):
    client = FakeNews([article(1, hours_ago=1)])
    assert fetch_headlines(tmp_settings, "SPY", lookback, limit, client=client, now=NOW) == []
    assert client.requests == []


def test_client_error_returns_empty_and_logs(tmp_settings, caplog):
    client = FakeNews(error=ConnectionError("news host unreachable"))
    assert fetch_headlines(tmp_settings, "BTC/USD", 24, 10, client=client, now=NOW) == []
    assert "ConnectionError" in caplog.text and "BTC/USD" in caplog.text


def test_malformed_response_returns_empty(tmp_settings):
    class Broken:
        def get_news(self, request_params):
            return {"unexpected": True}

    assert fetch_headlines(tmp_settings, "SPY", 24, 10, client=Broken(), now=NOW) == []


def test_without_alpaca_keys_returns_empty(tmp_settings, monkeypatch):
    monkeypatch.setattr(news, "NewsClient", lambda **kw: pytest.fail("must not build a client without keys"))
    assert fetch_headlines(tmp_settings, "SPY", 24, 10, now=NOW) == []


def test_uses_settings_keys_and_never_logs_them(keyed_settings, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    built = {}

    class FakeNewsClient(FakeNews):
        def __init__(self, api_key, secret_key):
            built.update(api_key=api_key, secret_key=secret_key)
            super().__init__(error=RuntimeError("401 unauthorized"))

    monkeypatch.setattr(news, "NewsClient", FakeNewsClient)
    assert fetch_headlines(keyed_settings, "SPY", 24, 10, now=NOW) == []
    assert built == {"api_key": "PKTEST", "secret_key": SECRET}
    assert SECRET not in caplog.text
