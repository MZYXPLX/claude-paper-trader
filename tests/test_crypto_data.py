"""Crypto data parsing and fallback, with canned HTTP responses (no network)."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.data import DataError
from bot.data.crypto import CryptoDataSource, HttpClient, kraken_pair

NOW = datetime(2026, 10, 30, 12, 30, tzinfo=timezone.utc)
DAY = 86400
TODAY = int(datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp())
HOUR_NOW = int(datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp())


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        key = (url, tuple(sorted((params or {}).items())))
        value = self.routes.get(key, self.routes.get(url))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise DataError(f"HTTP 404 from {url}")
        return value


def coinbase_routes(symbol="BTC-USD"):
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
    # Coinbase: [time, low, high, open, close, volume], newest first
    daily = [[TODAY - i * DAY, 90 + i, 110 + i, 100 + i, 105 + i, 10.0] for i in range(5)]
    hourly = [[HOUR_NOW - i * 3600, 99, 101, 100, 100.5 - i, 1.0] for i in range(10)]
    return {
        (url, (("granularity", 86400),)): daily,
        (url, (("granularity", 3600),)): hourly,
        f"https://api.exchange.coinbase.com/products/{symbol}": {"id": symbol, "status": "online", "trading_disabled": False},
    }


def kraken_routes(pair="XBTUSD"):
    url = "https://api.kraken.com/0/public/OHLC"
    # Kraken: [time, open, high, low, close, vwap, volume, count], oldest first, last row still forming
    daily = [[TODAY - (4 - i) * DAY, "100", "110", "90", str(105 + i), "100", "7.5", 10] for i in range(5)]
    hourly = [[HOUR_NOW - (9 - i) * 3600, "100", "101", "99", str(90 + i), "100", "1", 3] for i in range(10)]
    return {
        (url, (("interval", 1440), ("pair", pair))): {"error": [], "result": {"XXBTZUSD": daily, "last": 0}},
        (url, (("interval", 60), ("pair", pair))): {"error": [], "result": {"XXBTZUSD": hourly, "last": 0}},
    }


def test_kraken_pair_names():
    assert kraken_pair("BTC-USD") == "XBTUSD"
    assert kraken_pair("DOGE-USD") == "XDGUSD"
    assert kraken_pair("ETH-USD") == "ETHUSD"


def test_coinbase_parsing():
    src = CryptoDataSource(coinbase_http=FakeHttp(coinbase_routes()), kraken_http=FakeHttp({}), now_fn=lambda: NOW)
    md = src.fetch(["BTC-USD"])
    sd = md.symbols["BTC-USD"]
    assert sd.source == "coinbase"
    # today's candle (i=0) is still forming and is dropped; the rest are sorted oldest first
    assert list(sd.daily["close"]) == [109, 108, 107, 106]
    assert list(sd.daily["open"]) == [104, 103, 102, 101]
    assert sd.daily["time"].iloc[-1] == datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert sd.last_price == 100.5
    assert sd.price_time == NOW  # the current hourly candle is still open
    assert len(sd.hourly) == 10


def test_kraken_fallback():
    cb = FakeHttp({})  # every Coinbase call returns 404
    src = CryptoDataSource(coinbase_http=cb, kraken_http=FakeHttp(kraken_routes()), now_fn=lambda: NOW)
    md = src.fetch(["BTC-USD"])
    sd = md.symbols["BTC-USD"]
    assert sd.source == "kraken"
    assert list(sd.daily["close"]) == [105, 106, 107, 108]
    assert sd.daily["volume"].iloc[0] == 7.5
    assert sd.last_price == 99


def test_both_sources_fail():
    src = CryptoDataSource(coinbase_http=FakeHttp({}), kraken_http=FakeHttp({}), now_fn=lambda: NOW)
    md = src.fetch(["BTC-USD"])
    assert md.symbols == {} and "coinbase" in md.errors["BTC-USD"] and "kraken" in md.errors["BTC-USD"]


def test_validate_products():
    routes = coinbase_routes()
    routes["https://api.exchange.coinbase.com/products/FAKE-USD"] = None
    src = CryptoDataSource(coinbase_http=FakeHttp(routes), now_fn=lambda: NOW)
    assert src.validate_products(["BTC-USD", "FAKE-USD"]) == ["FAKE-USD"]


def test_stale_price_time_from_old_candle():
    routes = coinbase_routes()
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    old = HOUR_NOW - 5 * 3600
    routes[(url, (("granularity", 3600),))] = [[old, 1, 1, 1, 1, 1]]
    src = CryptoDataSource(coinbase_http=FakeHttp(routes), now_fn=lambda: NOW)
    sd = src.fetch(["BTC-USD"]).symbols["BTC-USD"]
    assert sd.price_time == datetime.fromtimestamp(old, timezone.utc) + timedelta(hours=1)
    assert sd.age_minutes(NOW) == pytest.approx(270)


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        return self.responses.pop(0)


def test_http_retries_then_succeeds():
    sleeps = []
    client = HttpClient(session=FakeSession([FakeResponse(429), FakeResponse(503), FakeResponse(200, [1])]), min_interval=0, sleep=sleeps.append)
    assert client.get_json("https://x") == [1]
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_http_does_not_retry_client_errors():
    client = HttpClient(session=FakeSession([FakeResponse(404)]), min_interval=0, sleep=lambda s: None)
    with pytest.raises(DataError, match="HTTP 404"):
        client.get_json("https://x")


def test_http_gives_up():
    client = HttpClient(session=FakeSession([FakeResponse(500)] * 4), min_interval=0, sleep=lambda s: None, retries=3)
    with pytest.raises(DataError, match="after 4 attempts"):
        client.get_json("https://x")
