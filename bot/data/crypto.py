"""Crypto market data from public endpoints: Coinbase Exchange first, Kraken as fallback.

No account or key is needed. Binance.com is deliberately not used: it rejects requests
from US IP addresses, which is where GitHub's hosted runners usually run.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

import requests

from . import DataError, MarketData, SymbolData, candles_frame, utcnow

log = logging.getLogger(__name__)

COINBASE_BASE = "https://api.exchange.coinbase.com"
KRAKEN_BASE = "https://api.kraken.com/0/public"
USER_AGENT = "claude-paper-trader/1.0 (+https://github.com)"

DAILY_BARS = 120
HOURLY_BARS = 7 * 24
# Coinbase returns at most 300 candles per request; Kraken returns at most 720. Both
# limits are above what we request, so one call per series is enough.

# Kraken uses its own names for a few assets.
KRAKEN_BASE_NAMES = {"BTC": "XBT", "DOGE": "XDG"}


def kraken_pair(symbol: str) -> str:
    base, quote = symbol.split("-")
    return KRAKEN_BASE_NAMES.get(base, base) + quote


class HttpClient:
    """GET JSON with retries, exponential backoff and a minimum gap between requests."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        session: requests.Session | None = None,
        min_interval: float = 0.2,
        retries: int = 3,
        backoff: float = 1.0,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval = min_interval
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.sleep = sleep
        self._last = 0.0

    def get_json(self, url: str, params: dict | None = None):
        last_error = "unknown error"
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as e:
                        raise DataError(f"invalid JSON from {url}") from e
                if resp.status_code not in self.RETRY_STATUS:
                    raise DataError(f"HTTP {resp.status_code} from {url}")
                last_error = f"HTTP {resp.status_code}"
            if attempt < self.retries:
                self.sleep(self.backoff * (2 ** attempt))
        raise DataError(f"{url} failed after {self.retries + 1} attempts: {last_error}")


class CryptoDataSource:
    def __init__(
        self,
        primary: str = "coinbase",
        coinbase_http: HttpClient | None = None,
        kraken_http: HttpClient | None = None,
        now_fn: Callable[[], datetime] = utcnow,
    ):
        if primary not in ("coinbase", "kraken"):
            raise ValueError(f"unknown crypto data source {primary!r}")
        self.primary = primary
        self.coinbase_http = coinbase_http or HttpClient(min_interval=0.2)
        # Kraken's public API allows roughly one request per second.
        self.kraken_http = kraken_http or HttpClient(min_interval=1.0)
        self.now_fn = now_fn

    # --- product validation -------------------------------------------------

    def validate_products(self, symbols: list[str]) -> list[str]:
        """Return the configured symbols that the primary exchange says do not exist.

        Network failures are not treated as unknown products; the data fetch will
        surface those (and fall back to the other exchange).
        """
        unknown = []
        for symbol in symbols:
            try:
                if self.primary == "coinbase":
                    product = self.coinbase_http.get_json(f"{COINBASE_BASE}/products/{symbol}")
                    if product.get("status") not in ("online", None) or product.get("trading_disabled"):
                        unknown.append(symbol)
                else:
                    data = self.kraken_http.get_json(f"{KRAKEN_BASE}/AssetPairs", {"pair": kraken_pair(symbol)})
                    if data.get("error") or not data.get("result"):
                        unknown.append(symbol)
            except DataError as e:
                if "HTTP 404" in str(e) or "HTTP 400" in str(e):
                    unknown.append(symbol)
                else:
                    log.warning("could not validate %s: %s", symbol, e)
        return unknown

    # --- fetching -----------------------------------------------------------

    def fetch(self, symbols: list[str]) -> MarketData:
        now = self.now_fn()
        md = MarketData(market="crypto", fetched_at=now)
        order = [self.primary, "kraken" if self.primary == "coinbase" else "coinbase"]
        for symbol in symbols:
            errors = []
            for source in order:
                try:
                    fetcher = self._coinbase if source == "coinbase" else self._kraken
                    md.symbols[symbol] = fetcher(symbol, now)
                    break
                except DataError as e:
                    errors.append(f"{source}: {e}")
                    log.warning("%s %s failed: %s", source, symbol, e)
            else:
                md.errors[symbol] = "; ".join(errors)
        return md

    def _coinbase(self, symbol: str, now: datetime) -> SymbolData:
        url = f"{COINBASE_BASE}/products/{symbol}/candles"
        # Coinbase rows are [time, low, high, open, close, volume], newest first.
        daily_raw = self.coinbase_http.get_json(url, {"granularity": 86400})
        hourly_raw = self.coinbase_http.get_json(url, {"granularity": 3600})
        if not isinstance(daily_raw, list) or not isinstance(hourly_raw, list):
            raise DataError(f"unexpected candle payload for {symbol}")
        reorder = lambda r: [r[0], r[3], r[2], r[1], r[4], r[5]]  # noqa: E731
        daily = candles_frame([reorder(r) for r in daily_raw])
        hourly = candles_frame([reorder(r) for r in hourly_raw])
        return _build_symbol(symbol, daily, hourly, now, "coinbase")

    def _kraken(self, symbol: str, now: datetime) -> SymbolData:
        pair = kraken_pair(symbol)
        series = {}
        for interval in (1440, 60):
            data = self.kraken_http.get_json(f"{KRAKEN_BASE}/OHLC", {"pair": pair, "interval": interval})
            if data.get("error"):
                raise DataError(f"kraken error for {pair}: {', '.join(data['error'])}")
            result = {k: v for k, v in (data.get("result") or {}).items() if k != "last"}
            if not result:
                raise DataError(f"kraken returned no candles for {pair}")
            # Kraken rows are [time, open, high, low, close, vwap, volume, count], oldest first.
            rows = next(iter(result.values()))
            series[interval] = candles_frame([[r[0], r[1], r[2], r[3], r[4], r[6]] for r in rows])
        return _build_symbol(symbol, series[1440], series[60], now, "kraken")


def _build_symbol(symbol, daily, hourly, now: datetime, source: str) -> SymbolData:
    if hourly.empty or daily.empty:
        raise DataError(f"no candles returned for {symbol}")
    # Keep only completed daily candles: a candle that started today is still forming.
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = daily[daily["time"] < day_start]
    daily = daily.tail(DAILY_BARS).reset_index(drop=True)
    hourly = hourly.tail(HOURLY_BARS).reset_index(drop=True)
    last = hourly.iloc[-1]
    # The latest price is the close of the newest hourly candle. Its observation time is
    # the end of that candle, or now if the candle is still open.
    price_time = min(last["time"].to_pydatetime() + timedelta(hours=1), now)
    return SymbolData(
        symbol=symbol,
        daily=daily,
        hourly=hourly,
        last_price=float(last["close"]),
        price_time=price_time,
        source=source,
    )
