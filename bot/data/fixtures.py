"""Record real market data to tests/fixtures and replay it for offline runs.

Offline runs make no network calls. Recorded timestamps are shifted forward so the
newest candle lines up with the current time (whole days for daily bars, whole hours
for hourly bars). Prices are not changed, so repeated offline runs see the same prices.

Record fresh fixtures with:  python -m bot.data.fixtures --market crypto
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from . import DataError, MarketData, SymbolData, candles_frame, frame_rows, utcnow


def _floor(ts: datetime, unit: str) -> datetime:
    ts = ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0) if unit == "day" else ts


def write_fixtures(md: MarketData, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for symbol, sd in md.symbols.items():
        payload = {
            "symbol": symbol,
            "source": sd.source,
            "as_of": md.fetched_at.isoformat(),
            "last_price": sd.last_price,
            "price_time": sd.price_time.isoformat(),
            "daily": frame_rows(sd.daily),
            "hourly": frame_rows(sd.hourly),
        }
        path = out_dir / f"{symbol}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        written.append(path)
    return written


def load_fixtures(market: str, symbols: list[str], fixtures_dir: Path, now: datetime | None = None) -> MarketData:
    now = now or utcnow()
    md = MarketData(market=market, fetched_at=now)
    market_dir = fixtures_dir / market
    if not market_dir.exists():
        raise DataError(
            f"no offline fixtures for {market} in tests/fixtures/{market}/. "
            f"Record them with: python -m bot.data.fixtures --market {market}"
        )
    for symbol in symbols:
        path = market_dir / f"{symbol}.json"
        if not path.exists():
            md.errors[symbol] = f"no fixture file tests/fixtures/{market}/{symbol}.json"
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        as_of = datetime.fromisoformat(raw["as_of"])
        day_shift = _floor(now, "day") - _floor(as_of, "day")
        hour_shift = _floor(now, "hour") - _floor(as_of, "hour")
        daily = candles_frame(raw["daily"])
        daily["time"] = daily["time"] + pd.Timedelta(day_shift)
        hourly = candles_frame(raw.get("hourly") or [])
        if not hourly.empty:
            hourly["time"] = hourly["time"] + pd.Timedelta(hour_shift)
        age = as_of - datetime.fromisoformat(raw["price_time"])
        md.symbols[symbol] = SymbolData(
            symbol=symbol,
            daily=daily,
            hourly=None if hourly.empty else hourly,
            last_price=float(raw["last_price"]),
            price_time=now - max(age, timedelta(0)),
            source=f"fixture:{raw.get('source', 'unknown')}",
        )
    return md


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from ..config import Paths, load_config

    parser = argparse.ArgumentParser(description="Record live market data as offline fixtures.")
    parser.add_argument("--market", choices=["crypto", "stocks"], required=True)
    args = parser.parse_args(argv)
    load_dotenv()
    config = load_config()
    paths = Paths()
    market_cfg = config.market(args.market)
    if args.market == "crypto":
        from .crypto import CryptoDataSource

        md = CryptoDataSource(primary=market_cfg.data_source).fetch(market_cfg.symbols)
    else:
        from .stocks import StockDataSource

        md = StockDataSource.from_env(feed=market_cfg.data_feed).fetch(market_cfg.symbols)
    if md.errors:
        for symbol, err in md.errors.items():
            print(f"failed {symbol}: {err}")
    if not md.symbols:
        raise DataError("nothing was recorded")
    for path in write_fixtures(md, paths.fixtures / args.market):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
