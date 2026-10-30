"""US stock data from Alpaca market data (IEX feed, free with a paper account), plus the
Alpaca market clock.

Only market data and the clock are used. The trading client is built with paper=True
and no URL override, so it can only ever reach Alpaca's paper environment.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from . import DataError, MarketData, MarketDisabled, SymbolData, candles_frame, utcnow

NY = ZoneInfo("America/New_York")
DAILY_BARS = 120
LOOKBACK_DAYS = 200  # calendar days, roughly 138 trading days


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


class StockDataSource:
    def __init__(self, data_client, trading_client, feed: str = "iex", now_fn: Callable[[], datetime] = utcnow):
        self.data_client = data_client
        self.trading_client = trading_client
        self.feed = feed
        self.now_fn = now_fn

    @classmethod
    def from_env(cls, feed: str = "iex", env: Mapping[str, str] | None = None) -> "StockDataSource":
        env = os.environ if env is None else env
        key = env.get("ALPACA_API_KEY", "").strip()
        secret = env.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            raise MarketDisabled(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY are not set, so the stocks market is disabled. "
                "Add free Alpaca paper keys to run US stocks."
            )
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        data_client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        trading_client = TradingClient(api_key=key, secret_key=secret, paper=True)
        return cls(data_client, trading_client, feed)

    def clock(self):
        """Alpaca's market clock: is_open, next_open, next_close. Handles weekends and holidays."""
        return self.trading_client.get_clock()

    def fetch(self, symbols: list[str]) -> MarketData:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
        from alpaca.data.timeframe import TimeFrame

        now = self.now_fn()
        md = MarketData(market="stocks", fetched_at=now)
        feed = DataFeed(self.feed)
        try:
            bars = self.data_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbols,
                    timeframe=TimeFrame.Day,
                    start=now - timedelta(days=LOOKBACK_DAYS),
                    feed=feed,
                    adjustment=Adjustment.ALL,
                )
            )
            trades = self.data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbols, feed=feed))
        except Exception as e:
            raise DataError(f"Alpaca request failed: {type(e).__name__}: {e}") from e

        today_ny = now.astimezone(NY).date()
        bar_data = getattr(bars, "data", bars) or {}
        for symbol in symbols:
            rows = bar_data.get(symbol) or []
            trade = trades.get(symbol) if trades else None
            if not rows or trade is None:
                md.errors[symbol] = "Alpaca returned no daily bars or no latest trade"
                continue
            # Today's bar is still forming during market hours; keep completed days only.
            completed = [b for b in rows if _utc(b.timestamp).astimezone(NY).date() < today_ny]
            daily = candles_frame(
                [[int(_utc(b.timestamp).timestamp()), b.open, b.high, b.low, b.close, b.volume] for b in completed]
            ).tail(DAILY_BARS).reset_index(drop=True)
            md.symbols[symbol] = SymbolData(
                symbol=symbol,
                daily=daily,
                last_price=float(trade.price),
                price_time=_utc(trade.timestamp),
                source=f"alpaca-{self.feed}",
            )
        return md
